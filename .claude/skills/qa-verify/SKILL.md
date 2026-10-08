---
name: qa-verify
description: >-
  The evaluator's verdict gate, with TWO modes whose "pass" points OPPOSITE ways — say which you
  mean. Trigger by mode. Default fix-verify: "verify the fix", "did the fix actually work", "confirm PROJ-123 is
  fixed" — pass = the bug is GONE (replay RED→GREEN, independent DOM read, after-video, masking
  judge; refuses without qa-repro's oracle). Mode repro-judge: "judge the repro", "independently
  confirm PROJ-123 reproduces", "repro-judge" — pass = the bug IS present, ruled by a fresh judge
  that never trusts qa-repro's conclusion; writes judge.json (pass | fail | inconclusive).
  Loop/oracle contexts only — for a casual "check this change works", use the built-in verify skill.
  Does NOT fix, decide retries, ship, or write to the issue tracker.
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Agent, ToolSearch, mcp__claude-in-chrome__list_connected_browsers, mcp__claude-in-chrome__select_browser, mcp__claude-in-chrome__tabs_context_mcp, mcp__claude-in-chrome__tabs_create_mcp, mcp__claude-in-chrome__navigate, mcp__claude-in-chrome__find, mcp__claude-in-chrome__form_input, mcp__claude-in-chrome__read_page, mcp__claude-in-chrome__get_page_text, mcp__claude-in-chrome__javascript_tool, mcp__claude-in-chrome__read_console_messages, mcp__claude-in-chrome__read_network_requests, mcp__claude-in-chrome__computer, mcp__claude-in-chrome__browser_batch, mcp__claude-in-chrome__gif_creator
---

# qa-verify — prove the fix, refuse the false green

qa-verify is **the second half of the evaluator**. It consumes the oracle `qa-repro` emitted and
decides whether a candidate fix actually works — by three independent gates that must *all* agree.
Its discipline is the "no trust-me" rule set in `references/verify-rules.md` — **read it; it is the
heart of this skill**.

**Why it must be excellent:** a verifier that can be fooled by a success toast greens unfixed bugs,
and the autonomous loop ships them. The single job here is to be **impossible to fool** — the user's
real, visible state is the only truth.

## What this block does NOT do (atomic boundary)

It does **not** fix code (that's `dev-fix`), decide whether to retry or escalate (the recipe), ship
(`infra-ship`), or write to the issue tracker. It takes a fix that's already applied and returns a
verdict. Keeping it to "verify + judge + return verdict" is what makes it composable.

## Modes

qa-verify runs in exactly one `MODE` per invocation:

- **`fix-verify`** (default) — *did a candidate fix resolve the bug?* Consumes qa-repro's oracle + an
  applied fix, re-runs the replay RED→GREEN, and judges the fix is real (not a mask). This is the
  original evaluator gate.
- **`repro-judge`** — *is this bug real — does the symptom genuinely reproduce?* An INDEPENDENT
  confirmation of a reproduction, ruled by a fresh judge that does NOT trust qa-repro's conclusion. No
  fix is involved. See **"Mode: repro-judge"** below. This is the second step of the intake→repro→judge
  proving slice; its output is the ratified contract in `contracts/qa-verify.repro-judge.json`.

If no `MODE` is passed, default to `fix-verify` (back-compat — existing callers are unaffected).

## Where configuration comes from (no literals in this skill)

Everything environment-specific is read from `.claude/loops/stack.config.json` (`cfg("<dotted key>")`
via `lib/adapters`, or `python3 lib/adapters/cli.py …`) or arrives in the environment the recipe sets.
This skill never hard-codes a URL, port, role name, credential file or storage endpoint:

| Need | Source |
|---|---|
| the app under test | `APP_URL` input (the recipe derives it from `app.url` / the sandbox's `urls.app`) |
| sign-in roles the oracle may run as | `adapters.auth_provider.config.roles` (lower-case canonical; compared case-insensitively) |
| credential files the oracle signs in from | `adapters.auth_provider.config.env_file` / `local_env_file` — rendered into the oracle by qa-repro; `E2E_ENV_FILE` / `E2E_LOCAL_ENV_FILE` override at run time |
| the state channel to read for a backend/state bug | `conventions.json` → `app_handles` (the app's own service/state handle, when the target repo declares one) |
| after-video upload | the `evidence_store` adapter, through qa-repro's shared helper (below) |
| the persona lens | the pack `personas_file` points at (`.claude/loops/personas.json`), referenced by id/role only |
| the judge model | `models.verifier` — the runner passes it as `--model` |
| the browser drivers | `browser.oracle_driver` (`playwright-cli`, headless replay) and `browser.live_driver` (`claude-in-chrome`, live re-observation + recording) |

A role that is absent from the stack (`"provider": "none"`) is a structured `blocked` with reason
`adapter-unavailable:<role>` — never a crash, never a silent skip, never a guess.

## Inputs / Outputs

**Inputs (both modes):**
- `MODE` — `fix-verify` (default) | `repro-judge`.
- `REPRO_DIR` — the `qa-repro` artifact dir (`brief.json` with `expected_verbatim`, `symptom_quote`,
  `media/<KEY>-repro.*`, `repro.json`, and — fix-verify only — the replay oracle `repro.json.replay_spec`
  names: `replay-oracle.sh`, with its `replay-grader.js` beside it unless the surface is pure-backend).
- `APP_URL` — reachable app (qa-verify checks this; it does not own/bounce servers). In `fix-verify`
  it must run the **fixed** code; in `repro-judge` it runs the **buggy/current** build being confirmed.
- `CHROME_DEVICE_ID` — **optional** deviceId of the exact Chrome extension instance to drive, for
  deterministic, headless-safe browser selection (see **Browser bootstrap** below). The runner forwards
  it (env or `CHROME_DEVICE_ID=` prompt token). Unneeded when exactly one Chrome instance is connected.
- **fix-verify only:** `E2E_ENV_FILE` / `E2E_LOCAL_ENV_FILE` — **optional** credential files the step-1
  oracle signs in from. Inside an `infra-worktree` sandbox the recipe passes them (`recipe-engine.md`,
  *Environment*); unset, the oracle uses the paths qa-repro rendered into it from
  `adapters.auth_provider.config.env_file` / `local_env_file`. `REPLAY_STATE_DIR` — **optional**, the
  oracle's saved sessions (default `~/.loop-replay`).
- **fix-verify only:** the **candidate fix already applied** to the working tree / sandbox, plus a
  `DIFF` (or a way to get `git diff`) so the masking detector can inspect it.

**Output — `fix-verify` mode:** `verify.json` in `REPRO_DIR`, written through `contracts/emit.py`
against **`contracts/qa-verify.json`** (it validates before writing — a contract violation is a loud
failure, never a half-written seam file). The shape, in the loop's shorthand:
```
{ verdict: pass|partial|fail|blocked, confidence: 0-100, masking_suspected: bool, key: "<KEY>",
  replay: "red→green"|"still-red"|"blocked", replay_note?, observed_quote, expected_verbatim,
  judge_rationale, must_haves: [{check: parity|no_collateral|visual|no_trust_me, result, ...}],
  video2: "<path or URL of <KEY>-fixed.mp4>", judged_blind: bool, model: "<the judge's model id>",
  attempt: n, retry_reason?, timing?,
  media?: [{kind, url, label?}] }   // OPTIONAL — evidence-store URLs for the after-video pair
```
> `video2` keeps the local path; `media[]` adds the URLs the `evidence_store` adapter returned (same
> shared helper + `after-*` kinds as `qa-repro/references/recording.md` §4) so the control plane and
> `infra-ship` get streamable after-video URLs. Emit `media` **only for a real `pass`** (never embed a
> "fixed" video for a non-pass), and **omit it** when the evidence store is `none` or the upload fails
> (best-effort — the artifact still validates without it). `judged_blind` is `true` only when this
> skill ran as an `Agent` spawn with a fresh context; a producer grading itself must say so.

**Output — `repro-judge` mode:** **`judge.json`** in `REPRO_DIR` — **not** `verify.json`. The two
modes write separate files on purpose: a shared file let a later fix-verify overwrite the judge's
evidence, and a consumer could not tell which mode produced it. The **four required keys** below, plus
**one OPTIONAL additive key, `media`**. The runner branches only on `verdict`; any key other than these
five is a contract violation (`contracts/qa-verify.repro-judge.json`):
```
{ verdict: "pass" | "fail" | "inconclusive", confidence: <int 0-100>, observed_quote: <string>,
  masking_suspected: <bool>,
  media?: [{kind, url, label?}] }   // OPTIONAL; kind from the shared enum — repro-judge emits before-* (see below)
```
- `verdict: "pass"` = "yes, this bug is real and the symptom reproduces."
- `verdict: "fail"` = **a real negative**: the judge ran the confirmation with adequate evidence and the
  symptom did **not** occur / the evidence contradicts it — triage may treat the repro claim as refuted.
- `verdict: "inconclusive"` = the judge **ran but cannot rule** — ambiguous or too-thin evidence, symptom
  surface unreachable while the rest worked, partial observation. This is the same reject-vs-retry
  distinction qa-repro's `status` tri-state carries: **never** emit `fail` when you couldn't actually
  tell, and never treat `inconclusive` as a refuted repro — retry or escalate.
- The skill does **not** emit `reproduced_confirmed` — the runner derives that from `verdict`.
- `media` is **optional and usually absent**: most runs read qa-repro's existing recording (evidence handed
  in via `REPRO_DIR`) and add no media. Emit it **only** when the judge records a fresh live re-observation
  and uploads it through the evidence store — and because the build under judgement is still buggy, those
  are `before-*` kinds. Omit the key entirely when nothing fresh was uploaded (best-effort — the four-key
  artifact still validates).

## Browser bootstrap & instance selection (both modes — read before any Chrome step)

Whenever this skill drives the live browser (repro-judge R1's live re-observe, fix-verify step 2's video
#2), the **first browser action is the deterministic instance-selection in
`.claude/skills/qa-repro/references/browser-cookbook.md` §A1.1** — run it before `tabs_context_mcp`.
Load `list_connected_browsers` + `select_browser` in the same bootstrap `ToolSearch` (§A1).
(fix-verify step 1's replay oracle does not use Chrome at all — it is a headless `playwright-cli`
script with its own session and sign-in, so instance selection doesn't apply to it; its only
preflight is `command -v playwright-cli`, and a missing install shows up as its exit 2.)

Why it matters here: this skill is often invoked **headless** by a runner (`claude -p`), where there is
**no human to answer**. If more than one Chrome extension instance is connected (e.g. two Chrome profiles
on the same host), `list_connected_browsers`' "ask the user which one" guidance and `switch_browser`'s
broadcast **both block on a human and deadlock** — the run hangs until the runner's timeout and exits
without writing its artifact. So, per §A1.1: pick by `CHROME_DEVICE_ID` when set, else use the sole
connected instance, else **FAIL FAST** — **never** `AskUserQuestion`, **never** `switch_browser`.
**Recommended posture: connect exactly one instance on the runner host** so selection is unambiguous and
`CHROME_DEVICE_ID` is optional.

**Fail-fast = a tooling-failure, not a verdict (headless-safe).** An **environment/tooling failure** is any
case where you could not actually look — the browser can't be selected deterministically, **or** `APP_URL` is
unreachable *and* the recorded evidence is too thin to rule. In those cases do **not** invent a `pass`/`fail` —
both are substantive judgments the runner acts on (`fail` can reject a real repro). The tri-state is:
1. **Tooling failure that prevents the judge from running at all** (browser unselectable, no evidence to
   read) → **emit a loud one-line diagnostic and write NO artifact**, so the runner records
   *"skill did not write its artifact"* → `ok:false` → retry/escalate. Fail **immediately**; the runner's
   timeout is only a backstop.
2. **The judge ran but cannot rule** (ambiguous/thin evidence, partial observation) → write
   `verdict: "inconclusive"` — a first-class outcome the runner must treat as retry-or-escalate,
   **never** a repro rejection.
3. **A substantive `fail`** is reserved for when you *did* look with adequate evidence and the symptom
   was absent or contradicted — a real negative, not a tooling gap.

## Mode: repro-judge — independently confirm the bug reproduces

The job: given the bug and qa-repro's evidence, rule **pass** (the symptom genuinely reproduces) or
**fail** (could not confirm) — **without trusting qa-repro's own verdict**. This is the gate that stops
the loop from grading its own homework.

**Model:** repro-judge is a verifier — **run it on the `models.verifier` model from stack.config; never
trade quality for tokens on a verifier.** The runner passes `--model` for this stage; the skill assumes a
strong model and records the one it ran on.

### R0. Independence (the whole point)
Spawn a **fresh sub-agent** (the `Agent` tool) as the judge. Hand it **only**:
- the bug brief (`brief.json`: surface, `symptom_quote`, `expected_verbatim`, `actual_verbatim`) and
  the observable evidence (`media/<KEY>-repro.*`, and `APP_URL` for a live re-observation), and
- the `KEY`.

Do **not** hand it `repro.json.status` or any "reproduced" conclusion — the judge must reach its own.
A judge that reads qa-repro's verdict first is not independent, and this gate is void.

**The judge's prompt is neutral.** It names the surface, the quoted symptom and the quoted expectation,
and asks what the judge observed. It never says "confirm the bug", "this reproduced", or "prove the
reporter right" — a leading prompt is a non-independent judge by another route.

### R1. Re-observe the symptom independently
**Health-check `APP_URL` first, then choose the channel.** If the app is reachable, the judge drives the
described repro steps live — but its **first** browser action is the deterministic instance-selection
(**Browser bootstrap** above / cookbook §A1.1): never ask, never broadcast, or fail fast. If `APP_URL` is
unreachable (or the surface is fully captured), the judge instead **reads the recorded evidence** — the
before-video plus qa-repro's independent DOM/state/geometry capture in `brief.json` — which for a
presentation/DOM defect is sufficient to rule on **without** opening a browser at all (so no instance
ambiguity arises). Either way it reads the **real state on the channel the bug lives on** (DOM/CSSOM, the
app's own service/state handle from `conventions.json` → `app_handles`, or network/console — the qa-repro
§A4 rule) and captures one **verbatim `observed_quote`** of what it actually saw — not a paraphrase of the
ticket. It forms this judgment from the observable evidence, **not** from qa-repro's `status` (R0).

### R2. Rule the verdict
- **pass** — the judge independently observed the symptom described in `symptom_quote`/`actual_verbatim`.
  `confidence` = how strongly the observation matches (0-100).
- **fail** — the judge could NOT confirm the symptom (behaviour matched expected, symptom absent, or the
  evidence was insufficient to be sure). Uncertainty never reproduces: if you looked with adequate
  evidence and the symptom was absent → **fail** (real negative); if you ran but could not actually
  tell → **inconclusive** — never a `pass`, and never a `fail` dressed as certainty.

### R3. Masking check
Set `masking_suspected: true` if the symptom may be **hidden/suppressed rather than truly absent** — e.g.
a swallowed error, a suppressed console/log, a loading state that hides the broken value, or a success
toast painted over unchanged underlying state. Masking means "the bug may still be there, just not
visible," so it **caps confidence < 80** and must not silently become a `fail`.

### R4. Emit `judge.json` (four required keys + optional `media`) and stop
Write `{ verdict, confidence, observed_quote, masking_suspected }` to `<REPRO_DIR>/judge.json` —
**never `verify.json`** (that's fix-verify's file; the modes must not overwrite each other).
If — and only if — the judge recorded a fresh live re-observation and uploaded it (`recording.md` §4,
`upload_evidence_media.sh`, `before-*` kinds), add the optional `media: [{kind,url,label?}]` array; otherwise
omit it. **No other key** (validate against `contracts/qa-verify.repro-judge.json`). Do not write to the
issue tracker, do not decide the next step; the recipe/runner derives `reproduced_confirmed` from `verdict`.

---

## Mode: fix-verify — Procedure (default)

**Invoking `kb`.** Every `kb <verb>` below is the `qa-kb` CLI wrapper
(`.claude/skills/qa-kb/scripts/kb`), which must be **on your PATH** (one symlink per machine —
`docs/brain-setup.md`). It is **not** there in a fresh checkout: an unresolved `kb` exits **127**
(`kb: command not found`) and the brain write is simply *lost*, so fall back to the script instead of
skipping the call — **absolutely**, never cwd-relative, because a loop's cwd is normally an
`infra-worktree` sandbox, not this repo:

```bash
# <framework-repo> = the checkout of THIS framework that this file was loaded from; set both in ONE shell.
KB="${KB_BIN:-<framework-repo>/.claude/skills/qa-kb/scripts/kb.py}"
python3 "$KB" <verb> ...        # ≡ the bare `kb <verb> ...` calls below
```


### 0. Preconditions (refuse a meaningless verify)
- Read `repro.json`. If `status != reproduced` → write `verdict: blocked, reason: no-oracle` and stop.
  You cannot verify a bug nobody proved. (false-green guard.) The same `blocked, reason: no-oracle` if
  `replay_spec` is anything other than `replay-oracle.sh`, or that file isn't in `REPRO_DIR` — an older
  artifact's oracle isn't runnable by this gate. `blocked` escalates; it is never a re-fix. A new oracle
  comes from `qa-repro` re-run by a human, or against the pre-fix ref — never the fixed sandbox, where the
  bug is gone and a re-repro would come back `not_reproduced`.
- Health-check `APP_URL`. If down → `verdict: blocked, reason: app-unreachable` (the recipe owns servers).
- Confirm the app is running the **fixed** code (for a backend change, the API process must have been
  restarted — if you can't confirm, say so and return `blocked` rather than verify stale code).

### 1. Replay gate (the machine oracle)
Re-run qa-repro's oracle against the fixed build, with the `ROLE` its header's `RUN:` line names — one of
the lower-case roles in `adapters.auth_provider.config.roles`, or `none` (UI and pure-backend oracles run
the same way):
```bash
APP_URL="$APP_URL" ROLE=<ROLE from the header> [E2E_ENV_FILE=<creds>] [E2E_LOCAL_ENV_FILE=<secrets>] bash "$REPRO_DIR/replay-oracle.sh"; echo "exit=$?"
```
It prints one JSON line `{"pass", "observed"}` and asserts the **correct** behavior, so with the bug it
exited 1 (RED). Now:
- **exit 0 (GREEN)** — the graded behavior is fixed. Record `replay: red→green` and keep `observed`.
- **exit 1 (RED)** — `verdict: fail`, `replay: still-red` (the fix didn't change the graded behavior).
- **exit 2 (BLOCKED)** — the oracle could not run (app down, `playwright-cli` missing, sign-in rejected, a
  locator it needs is gone, the grader threw). That is **not** a fail and not a pass: `verdict: blocked`
  with the `observed` reason. Retry once only if the reason is transient.

**Never edit `replay-oracle.sh` or `replay-grader.js` here** — a verifier that rewrites its oracle until it
goes green is grading its own homework. If the fix legitimately moved a control the oracle locates, say
so in `judge_rationale` and return `blocked`. The recipe escalates it; a new oracle is `qa-repro`'s job (step 0).

### 2. Mechanical gate (read the real state) + record video #2
Drive the **same** repro steps from `brief.json` in the browser — **first select the Chrome instance
deterministically (Browser bootstrap above / cookbook §A1.1: never ask, never broadcast, else fail fast)** — (per
`.claude/skills/qa-repro/references/recording.md`, filename `<KEY>-fixed.gif`, watermark off; convert
with `.claude/skills/qa-repro/scripts/gif_to_mp4.sh` — **producing the MP4 is mandatory, not optional**).
Then, at capture time, **upload both after-artifacts to the evidence store** with
`.claude/skills/qa-repro/scripts/upload_evidence_media.sh` (`recording.md` §4 — the single home of the
upload helper; it calls the `evidence_store` adapter configured in stack.config) as `after-gif` /
`after-mp4`, collecting the returned URLs into `media[]`. Storage config lives in
`adapters.evidence_store`, never in this skill and never as a hard-coded secret; best-effort (omit `media`
if the store is `none` or the upload fails). Apply the **four MUST-HAVEs** from `references/verify-rules.md`:
1. **Parity** — quote the live DOM/API state and show it matches `expected_verbatim` (not a paraphrase).
2. **No collateral** — confirm neighboring state the fix shouldn't touch is unchanged.
3. **Visual** — the corrected state is visible on screen (screenshot/DOM after refresh).
4. **No "trust me"** — a toast/log/JSON saying success is **not** a pass; the read state is.

Then judge from the **reporting persona's lens** (`qa-personas`, the pack `personas_file` names): would
*this* user — e.g. the pack's onboarding-tier `member` on a phone — consider it actually fixed, on their
device and to their standard, not just green in the abstract?

### 3. Judge gate (real fix, not a mask)
Inspect the `DIFF` with the **masking detector** (R3, in the reference): swallowed exceptions,
suppressed logs, narrow special-cases, disabled tests, symptom-surface-only changes. Then spawn an
**LLM-judge** (Agent) with `expected_verbatim` + video #2 + the live-state quotes + the diff summary →
returns confidence 0-100 and whether it's a real fix. The prompt is neutral: it asks whether the observed
state matches the quoted expectation and whether the diff addresses a cause — never "confirm this fix
works". If green depends on masking → `masking_suspected: true`, cap confidence < 80.

### 4. Verdict
- **pass** — replay RED→GREEN **and** all four MUST-HAVEs hold **and** judge ≥ 80 **and** not masking.
- **partial** — symptom gone but a guard tripped (collateral change / confidence < 80 / thin visual).
- **fail** — replay still red, state ≠ expected, or a regression appeared.
- **blocked** — no oracle / app down (step 0), or the replay oracle could not run (exit 2, step 1).

A green replay with judge confidence < 80 is a **partial**, never a pass — uncertainty is a human's
call (escalate even if green). Never embed/emit a "fixed" video for anything but a real `pass`.

### 5. Feed the brain (qa-kb)
- **`kb confirm app_map --id <id> --source qa-verify` for EVERY `app_map` row you actually used** — it
  refreshes `last_confirmed_at` without touching `value` or bumping `version`, so it can't clobber a
  concurrent loop and needs no `--expect-version`. This is what makes selector freshness *falsifiable*:
  a row nothing has confirmed in weeks shows up in `kb decay`'s `stale_app_map` report. Verify is the
  right place for it — you just proved the selector works against the fixed build.
- If a selector had actually **changed**, that's a write, not a confirm: `kb write app_map
  --expect-version …` (pass the version you read so a concurrent loop can't clobber — R4).
- If the replay flaked (exit 0/1 across reruns), `kb write flakiness --spec "<KEY> replay-oracle.sh" --area <area>
  --source qa-verify [--failed]` — `<area>` is the issue's area (dev-intake's `area`, the one the repro's
  `app_map` rows use); without it kb derives a junk area from the id. Consider `kb quarantine` if it crosses the
  suite-health threshold (R7).
- Do **not** bless expectations (human-only) — at most `propose` a refinement if the expected behavior
  needs sharpening.

### 6. Emit `verify.json` and stop
Write it through `contracts/emit.py --contract contracts/qa-verify.json` and return the verdict. The
recipe decides ship / retry / escalate.

## Success criteria & failure categories (declared up front)

**Success:** a correct verdict with evidence — for `pass`, replay RED→GREEN, four MUST-HAVEs shown with
quoted live state, video #2, judge ≥ 80, masking ruled out.

**Failure categories (classify before adapting):**
- `false-green-via-toast` — accepted a success signal instead of reading state. The cardinal sin.
- `masking-missed` — greened a symptom patch. Re-inspect the diff with the detector.
- `stale-code` — verified against an app not running the fix (backend not restarted). Return `blocked`.
- `flaky-replay` — intermittent → record flakiness, don't average it into a pass.
- `oracle-blocked` — the replay exited 2 → `blocked` with its `observed` reason, never `fail`; never
  patch the oracle to get past it.
- `judge-overconfident` — judge said pass but live state disagrees; the read state wins, not the judge.

## Key principles
1. **The read state is the only truth.** Toasts, logs, JSON, and the judge are inputs — the live DOM/DB wins.
2. **Three gates, all must agree.** Replay green, four MUST-HAVEs, judge ≥ 80. Any one short → not a pass.
3. **No oracle, no verify.** Never verify a bug that wasn't reproduced.
4. **A symptom patch is not a fix.** Masking caps confidence and flags a human (R3).
5. **Atomic.** Verify and return a verdict — never fix, ship, or decide the retry.
