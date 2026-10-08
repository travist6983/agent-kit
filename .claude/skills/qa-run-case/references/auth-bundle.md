# Auth bundle — how the run reaches a real, authenticated session in the target app

The run authenticates from **files**, never from the prompt (SKILL.md principle 5): a Playwright storage
state plus a small `run.env` under `AUTH_DIR` (`<EVID_ROOT>/<KEY>/auth/`, mode 0600). **This file is the
canonical definition of that bundle** — there is no other producer contract. Two things can produce it:

1. **This skill itself (the default when you run it by hand).** With no `AUTH_DIR`, or an empty one, the
   cli driver mints the bundle in step 0 from the `auth_provider` adapter and the configured `login` block
   (§Self-mint below), writes both files, then proceeds exactly as if a caller had supplied them.
2. **A caller that mints ahead of dispatch** (a recipe, a scheduled job, a control plane, or you with
   `playwright-cli state-save`) — anything that writes the two files below in this shape. `qa-run-chatbot`
   consumes the same bundle.

Which account signs in, and how, is the `auth_provider` adapter's business
(`adapters.auth_provider` in `.claude/loops/stack.config.json`: `roles`, `credential_env_pattern`,
`login`, `pre_auth`). The skill never reads a password from the prompt and never logs one.

**Never** echo `storage-state.json`, `run.env`, a password, or the pre-auth token to the transcript, a
screenshot, a `step-<n>.json`, or stdout. Reference them only by path.

## The bundle

```
<AUTH_DIR>/
  storage-state.json   # a Playwright context.storage_state() dict: {cookies:[…], origins:[{origin, localStorage:[…]}]}.
                       #   The authenticated session for ROLE. Load it; recreate it only on the self-mint path.
  run.env              # environment + optional pre-auth, as KEY=value lines (see contract below). No passwords.
```

### `run.env` contract (the keys, verbatim)

| Key | Meaning | Source | Empty means |
|---|---|---|---|
| `E2E_BASE_URL` | the app origin the run targets, e.g. `https://staging.example.com` | `APP_URL` (which defaults to `app.url`; a named environment's URL is `app.environments.<name>.url`) | — (always set) |
| `E2E_API_URL` | the API origin, e.g. `https://api.staging.example.com` | `app.api_url` in stack.config (top-level; environment entries carry only `url`) | same-origin API — API reads use `location.origin` |
| `E2E_ENVIRONMENT` | the environment name this run targets | the key of `app.environments` whose `url` matches `E2E_BASE_URL`, else empty | unnamed environment (an ad-hoc `APP_URL`) |
| `E2E_ROLE` | the configured role the session belongs to | `ROLE` token, default the first of `adapters.auth_provider.config.roles` | — (always set) |
| `E2E_PERIMETER_ORIGIN` | the origin an HTTP perimeter guards (`scheme://host`) — usually the `E2E_BASE_URL` origin, but the producer states it explicitly rather than the run assuming it | the producer (see Pre-auth below) | **no perimeter** — skip header injection |
| `E2E_PERIMETER_HEADER` | the request-header name the perimeter expects | the producer | **no perimeter** — skip injection |
| `E2E_PERIMETER_TOKEN` | a fresh value for that header, minted per run | the producer | **no perimeter** — skip injection (the honest signal: if the env is in fact guarded, navigation bounces → BLOCKED) |

Source it in bash: `set -a; . "$AUTH_DIR/run.env"; set +a` (so the vars export to child `playwright-cli`
processes). Then never `echo` any of them.

**Bundle validation (step 0):** both files present and non-empty, or the self-mint path runs (cli) /
the run is BLOCKED (chrome — it cannot mint). A bundle whose `storage-state.json` is present but whose
session bounces is **BLOCKED** ("auth bundle did not authenticate — session bounced to `<where>`"), never a
FAIL. The `E2E_PERIMETER_*` triple being empty is **not** an error — it means "this env has no perimeter"
(`pre_auth: "none"`); proceed without injection.

## Self-mint (cli driver, when `AUTH_DIR` is absent or empty)

This is how you run the skill once with nothing but stack.config and the two credential env files
(`docs/test-credentials.md`). Every value below comes from config or the adapter — nothing is typed into
the prompt.

```bash
REPO_ROOT="${LOOPS_REPO_ROOT:-$(git rev-parse --show-toplevel)}"
AUTH_DIR="$EVID_ROOT/$KEY/auth"; mkdir -p "$AUTH_DIR"; chmod 700 "$AUTH_DIR"
ROLE="${ROLE:-$(cfg adapters.auth_provider.config.roles | python3 -c 'import sys,ast; v=sys.stdin.read().strip(); print((ast.literal_eval(v) if v.startswith("[") else [v])[0])')}"

# 1. credentials — exit 3 → BLOCKED adapter-unavailable:auth_provider · exit 1 → BLOCKED credentials-missing:<ROLE>
CREDS="$(python3 "$REPO_ROOT/lib/adapters/cli.py" auth_provider credentials "$ROLE")" || { echo "BLOCKED: $(printf '%s' "$CREDS" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error","credentials-missing"))')"; exit 0; }
export E2E_USERNAME="$(printf '%s' "$CREDS" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["username"])')"
export E2E_PASSWORD="$(printf '%s' "$CREDS" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["password"])')"

# 2. sign in through the configured login block, then save the state — one chained command (env does not persist)
LOGIN_PATH="$(cfg adapters.auth_provider.config.login.path)"
U_SEL="$(cfg adapters.auth_provider.config.login.username_selector)"
P_SEL="$(cfg adapters.auth_provider.config.login.password_selector)"
S_SEL="$(cfg adapters.auth_provider.config.login.submit_selector)"
LANDMARK="$(cfg adapters.auth_provider.config.login.signed_in_landmark)"
FAIL_RX="$(cfg adapters.auth_provider.config.login.failure_regex)"
playwright-cli -s=tester open --persistent ; \
  playwright-cli -s=tester run-code --filename="$EVID_ROOT/$KEY/cli-scripts/inject-perimeter.js" ; \
  playwright-cli -s=tester --raw goto "${APP_URL%/}${LOGIN_PATH}" ; \
  playwright-cli -s=tester --raw fill "$U_SEL" "$E2E_USERNAME" ; \
  playwright-cli -s=tester --raw fill "$P_SEL" "$E2E_PASSWORD" ; \
  { [ -n "$S_SEL" ] && playwright-cli -s=tester --raw click "$S_SEL" || playwright-cli -s=tester --raw press Enter ; } ; \
  playwright-cli -s=tester run-code --filename="$EVID_ROOT/$KEY/cli-scripts/wait-signed-in.js" ; \
  playwright-cli -s=tester --raw state-save "$AUTH_DIR/storage-state.json" ; chmod 600 "$AUTH_DIR/storage-state.json"
```

`wait-signed-in.js` polls for `LANDMARK` (`await page.waitForSelector(process.env.LANDMARK, { timeout: 15000 })`)
and, if the page text instead matches `FAIL_RX`, returns `{ signed_in: false, reason: 'login-failed' }` →
**BLOCKED** (`credentials-unusable:<ROLE>`). Landmark never appearing and no failure text → **BLOCKED**
("login did not reach the signed-in landmark `<selector>` within 15s"). Both scripts read their inputs from
`process.env` — the password never lands in a `cli-scripts/` artifact.

Then write `run.env` (no secrets in it — the password stays in the process env and dies with it):

```bash
API_URL="$(cfg app.api_url)"
ENV_NAME="$(python3 -c 'import sys,json; sys.path.insert(0,sys.argv[1]); from lib.adapters import cfg
envs=cfg("app.environments") or {}; want=sys.argv[2].rstrip("/")
print(next((k for k,v in envs.items() if (v or {}).get("url") and v["url"].rstrip("/")==want), ""))' "$REPO_ROOT" "$APP_URL")"
umask 077; cat > "$AUTH_DIR/run.env" <<EOF
E2E_BASE_URL=${APP_URL%/}
E2E_API_URL=$API_URL
E2E_ENVIRONMENT=$ENV_NAME
E2E_ROLE=$ROLE
E2E_PERIMETER_ORIGIN=${E2E_PERIMETER_ORIGIN:-}
E2E_PERIMETER_HEADER=${E2E_PERIMETER_HEADER:-}
E2E_PERIMETER_TOKEN=${E2E_PERIMETER_TOKEN:-}
EOF
```

Self-mint is **once per run**; the rest of the run `state-load`s the file it just saved, exactly like a
caller-supplied bundle. The session stays open from the sign-in, so the first `goto "$APP_URL"` in
SKILL.md §0.5 confirms the landmark on the real landing page.

## Pre-auth perimeter (`pre_auth: "header_token"`) — the CLI driver must inject the header itself

Some hosted environments sit behind an HTTP perimeter (a gateway, an identity proxy) **in front of** the
app's own login. The perimeter is **not** carried in the storage-state cookies — it is passed by injecting
`E2E_PERIMETER_HEADER: E2E_PERIMETER_TOKEN` on **every** request to the perimeter origin.

**Where the triple comes from.** `adapters.auth_provider.config.pre_auth` names only the *mode*; the
built schema has no key for the header name, the perimeter origin, or a token source. So with
`pre_auth: "header_token"` the **producer** supplies all three: a caller writes them into `run.env`; on the
self-mint path the skill copies them from its own process environment (`E2E_PERIMETER_ORIGIN`,
`E2E_PERIMETER_HEADER`, `E2E_PERIMETER_TOKEN` exported by whoever launched it — a CI secret, a shell
export). If the mode is `header_token` and any of the three is empty, grade **BLOCKED**
("pre_auth is header_token but `<VAR>` is empty — the perimeter cannot be passed headlessly") *before*
navigating; do not try the perimeter bare and then misread the challenge as an app defect.

Two rules are load-bearing — follow **both** exactly:

1. **Never global `setExtraHTTPHeaders`.** A custom header on a **cross-origin** identity-provider request
   trips a CORS preflight the provider rejects → session-refresh breaks → the SPA bounces to the login
   page. Inject the header **only** for requests to `E2E_PERIMETER_ORIGIN`.
2. **Exactly ONE route interception per origin.** `route.continue()` is terminal; two routes on the same
   origin clobber each other. Scope one `context.route(\`${E2E_PERIMETER_ORIGIN}/**\`, …)` that merges
   the header onto the existing request headers, installed **before** the first `navigate`.

The **cli driver** installs this via a `run-code --filename=` script (see
`references/playwright-cli-driving.md` §3). The **chrome driver** does *not* inject — it relies on the
paired Chrome's existing session (`references/chrome-driving.md` §0); if that Chrome isn't authenticated
to the env, grade **BLOCKED**.

`pre_auth: "sso_human_once"` means a human completed an SSO handshake once with the oracle driver and
saved the browser state (`docs/test-credentials.md`); a caller hands that saved state over as
`storage-state.json`, and the run treats it exactly like `none` (no injection). The self-mint path cannot
complete an SSO handshake: with `sso_human_once` and no bundle supplied, grade **BLOCKED** ("pre_auth is
sso_human_once and no saved session was supplied at `<AUTH_DIR>`").

**If, after loading the bundle and (cli) installing injection, navigation still bounces to the login page
or a perimeter challenge → BLOCKED** ("auth bundle did not authenticate — session bounced to `<where>`").
"Login page" is detected from config: the URL is under `adapters.auth_provider.config.login.path`, or the
`login.signed_in_landmark` selector is absent after the page settles. Recording a false FAIL off an
unauthenticated session is exactly the trap the BLOCKED escape hatch exists to prevent — a BLOCKED can be
re-minted and retried, but a FAIL reads as a real defect.

## Freshness

A caller-minted bundle honors the auth session's TTL and mints a fresh pre-auth token per run; a long
queue wait can still age it out, which is why the authenticated-session confirmation (SKILL.md step 0.5)
is mandatory rather than assumed. The skill never refreshes a caller-supplied bundle mid-run — a stale
one is a BLOCKED the caller re-mints on retry. A self-minted bundle is fresh by construction.
