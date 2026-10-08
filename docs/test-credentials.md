# Test credentials — what the replay oracle signs in with

The replay oracle (`qa-repro` / `qa-verify`) signs in as a real user before it can grade anything.
If it cannot sign in it exits **2 = BLOCKED**, `qa-verify` returns `blocked`, and `dev-pre-pr` blocks
the ship. That is correct behaviour and it is also a dead loop, so the credentials are load-bearing.

Everything the oracle knows about signing in comes from **one place**: the `auth_provider` adapter's
block in `.claude/loops/stack.config.json`. Nothing is hard-coded — not the role names, not the
variable names, not the login form.

```jsonc
"auth_provider": {
  "provider": "env-file",
  "config": {
    "env_file":        ".env.e2e",          // usernames (tracked if your gitignore allows)
    "local_env_file":  ".env.e2e.local",    // passwords (gitignored, per machine)
    "process_env":     true,                // CI injects secrets through the environment
    "roles":           ["owner", "admin", "member", "observer"],
    "credential_env_pattern": "E2E_{ROLE}_{FIELD}",
    "login": {
      "path":               "/login",
      "username_selector":  "input[name=email]",
      "password_selector":  "input[name=password]",
      "submit_selector":    "button[type=submit]",
      "failure_regex":      "invalid|incorrect|try again",
      "signed_in_landmark": "[data-testid=user-menu]"
    },
    "pre_auth": "none"
  }
}
```

## The roles

`config.roles` is the canonical role vocabulary — **lower-case**, compared case-insensitively
everywhere. The default pack mirrors the neutral example domain: `owner`, `admin`, `member`,
`observer`. Rename them to whatever your app calls its roles; the oracle, the preflight and the
personas pack all read the list rather than assuming one.

A run asks for a role (`ROLE=member bash replay-oracle.sh`); with no `ROLE` the oracle uses the
**first** configured role. A role that is not in the list BLOCKs with `invalid ROLE`, which is the
right failure — a typo must not silently sign in as someone else.

**The first role alone unblocks the loop.** Everything else is about removing a workaround. A loop
that only has `owner` and needs to check what a `member` cannot see ends up flipping roles in the
database mid-run — that works, but the repro is then not exercising the real permission path, and
it is invisible to the oracle. Add roles in order of value:

1. the first configured role (`owner`) — required; without it nothing signs in
2. the most common non-privileged path (`member`) — permissions, visibility, "a member can't see X"
3. read-only surfaces (`observer`) — cheap to add while you're there
4. admin-only surfaces (`admin`) — only if the loop will touch them

The provisioning preflight blocks with `missing <VARIABLE>` for whichever role a run asks for, so a
missing role fails loudly at provisioning rather than mysteriously at verify.

## The two-file split

You need **two files**, not one, plus the process environment:

| Source | Holds | Tracked? |
|---|---|---|
| `config.env_file` (default `.env.e2e`) | usernames and anything non-secret (hints, extras) | may be committed if your gitignore permits — this repo's `.gitignore` ignores `.env.*`, so add `!.env.e2e` if you want it shared |
| `config.local_env_file` (default `.env.e2e.local`) | **passwords** | **never** — gitignored, per machine |
| the process environment | whatever CI injects | n/a — disable with `"process_env": false` |

Later wins: the local file overrides the shared one, and the environment overrides both. Paths are
resolved relative to **this** repo's root (the directory holding `.claude/loops/`), or absolute. The
sandbox adapter hands the oracle both paths (`E2E_ENV_FILE` / `E2E_LOCAL_ENV_FILE`), so a sandbox never
needs its own copy.

### Variable names — `credential_env_pattern`

Variables resolve from `credential_env_pattern` with `{ROLE}` upper-cased (`-` → `_`) and `{FIELD}`
one of `USERNAME` or `PASSWORD`. With the default pattern `E2E_{ROLE}_{FIELD}`:

```bash
# .env.e2e — usernames, no secrets
E2E_OWNER_USERNAME=e2e-owner@example.com
E2E_MEMBER_USERNAME=e2e-member@example.com
E2E_OBSERVER_USERNAME=e2e-observer@example.com
E2E_ADMIN_USERNAME=e2e-admin@example.com
```

```bash
# .env.e2e.local — passwords, gitignored, per machine
E2E_OWNER_PASSWORD=…
E2E_MEMBER_PASSWORD=…
E2E_OBSERVER_PASSWORD=…
E2E_ADMIN_PASSWORD=…
# optional extras: any E2E_<ROLE>_<NAME> reaches the adapter as credentials.extra["name"]
E2E_ADMIN_TOTP_SECRET=…
```

The pattern must contain both `{ROLE}` and `{FIELD}`; the oracle renderer refuses to render without
them. The `env-file` provider also honours `config.prefix` (default `E2E`) when no pattern is set.
Errors always name the missing **variable**, never a value, and the adapter redacts passwords in
every log line.

## Which users, and where they must exist

The oracle signs into the app **where the sandbox runs**, which is usually `localhost` against the
sandbox's own database. Two independent things have to line up, and a user minted for a shared
environment (staging, a QA tenant) often satisfies neither:

1. **The identity store.** A local or sandbox build frequently authenticates against a *development*
   identity tenant, not the one staging uses. A user that exists in staging's tenant does not exist in
   the development tenant — the password is irrelevant; it is an account-existence problem.
2. **The application database.** Even after a successful sign-in, the app needs a matching user and
   membership row *in the sandbox's database*. If the sandbox adapter clones a local database
   (`adapters.sandbox.config.db_clone_command`), the user must exist in the database being cloned.

**Both halves must hold.** The reliable recipe is to create the test users **by signing up through
the local app**, once per role, which satisfies both in one step:

1. Start the app locally (`app.url`).
2. Sign up as the first role's user (`e2e-owner@example.com`), set a password you will put in the
   local file.
3. Complete onboarding so the user lands in a Workspace — the oracle's signed-in check expects
   `signed_in_landmark`, not an onboarding wizard.
4. Invite the other role users into that same Workspace at the role you want, and accept each invite.
5. Record the usernames in `.env.e2e` and the passwords in `.env.e2e.local`.

If sandboxes clone a database, every sandbox created **afterwards** inherits these users; a sandbox
provisioned before you added them will not have them — re-provision.

## The login recipe — `config.login`

The oracle template's sign-in block is rendered from `config.login` by `qa-repro`'s
`render_oracle.py`; the emitted `replay-oracle.sh` is self-contained, so `qa-verify` and
`infra-prod-verify` can run it from the evidence directory without this repo's config.

| key | required | what the oracle does with it |
|---|---|---|
| `path` | yes | opens `APP_URL` + `path` |
| `username_selector` | yes | fills the username there; "login form not found" BLOCKs if absent |
| `password_selector` | yes | fills the password there |
| `submit_selector` | no | clicks it; when empty, presses Enter in the password field |
| `failure_regex` | no | page text matching it after submit ⇒ `credentials-unusable` (BLOCKED, never RED) |
| `signed_in_landmark` | yes | the selector that proves the session — the oracle waits for it, never for a toast |

A missing required key is a **render error**, never a guess: the oracle will not invent a selector.
Any rendered value can be overridden for one run through the environment (`LOGIN_PATH`,
`LOGIN_SIGNED_IN_LANDMARK`, `CREDENTIAL_ENV_PATTERN`, `AUTH_PRE_AUTH`, `E2E_ENV_FILE`,
`E2E_LOCAL_ENV_FILE`, `ORACLE_ROLES`, `PROD_HOSTS`), so a recipe can point one rendered copy at
another environment.

The oracle saves the signed-in browser state per `(APP_URL, role)` under `REPLAY_STATE_DIR`
(default `~/.loop-replay`) and reuses it on later runs; a stale state falls back to a fresh sign-in. A target whose host matches `app.environments.prod.url`
is **refused** unless the oracle is explicitly marked read-only and `ALLOW_PROD=1` is set.

`config.login_hint` (a string, or `{role: hint, default: hint}`) is for the **live** driver — the
human-readable "how to sign in" the exploration and recording skills read. It is prose, not a
selector list, and it is never parsed.

## `pre_auth` — a perimeter in front of the app

Some apps sit behind something that is not the app's own login: an SSO wall, a VPN portal, a header
token. `config.pre_auth` names it:

| `pre_auth` | meaning | replay oracle |
|---|---|---|
| `none` | the login form is the first thing a browser meets | supported |
| `sso_human_once` | a human signs in once with the oracle driver and saves the browser state; the oracle reuses that saved session and BLOCKs (never guesses) when it is missing or expired | supported |
| `header_token` | a token header opens the perimeter | recognised in config; **not** driven by the replay oracle today — a signed-in `ROLE` is BLOCKED with a clear reason |

With `sso_human_once`, the saved state lives under `REPLAY_STATE_DIR`; the BLOCKED message names the
exact file a human must save it to.

## Prove it before trusting it

```bash
# static — the configured files/environment have what the run needs (no app required)
python3 .claude/skills/infra-worktree/scripts/oracle_preflight.py \
  --app-url http://localhost:3010 --roles owner,member

# one specific file instead of the configured pair
python3 .claude/skills/infra-worktree/scripts/oracle_preflight.py \
  --app-url http://localhost:3010 --roles owner --env-file /path/to/.env.e2e.local

# conclusive — actually signs in through the rendered oracle (needs the app up)
python3 .claude/skills/infra-worktree/scripts/oracle_preflight.py \
  --app-url http://localhost:3010 --roles owner --live \
  --oracle runs/PROJ-123/replay-oracle.sh
```

Exit **0** = credentials present for every requested role and the target is not production;
**1** = the oracle would exit 2 (`credentials-missing` / `credentials-unusable` /
`adapter-unavailable:auth_provider`) — stop here, not at verify; **2** = unreadable or bad usage.

The static pass is instant and catches missing files, missing roles and empty values. **Only `--live`
proves the account exists in the identity store the sandbox actually authenticates against** — that
is the half no amount of file-reading can settle. `infra-worktree` runs this preflight at
provisioning so a dead credential surfaces in minute one, not after the fix is written.

## See also

- [`stack-config.md`](stack-config.md) — every `auth_provider` key alongside the rest of the file.
- [`adapters.md`](adapters.md) — the `auth_provider` role contract (`credentials(role)`,
  `login_hint(role)`) and how to supply a provider that is not an env file.
- [`getting-started.md`](getting-started.md) — where credentials sit in the first dry run.
