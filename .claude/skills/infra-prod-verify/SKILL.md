---
name: infra-prod-verify
description: >-
  Confirm a shipped fix actually worked IN PROD — not just that the deploy succeeded — and roll back
  if it didn't. Trigger for "verify in prod", "did the fix hold in prod", "confirm PROJ-123 deployed
  and the error dropped", or as the prod-verify step after infra-ship (post-safety-gates only). Runs
  the post-deploy smoke AND checks the ticket's ORIGINAL error signal through the analytics adapter
  on the deployed build (the R2 tie-back). Outcomes are exactly two: prod-confirmed, or
  prod-regressed → ROLLBACK (prefer a feature-flag flip, R9) + escalate with prod evidence. With no
  analytics adapter the verdict is inconclusive and a human confirms (--human-confirmed-by). NEVER
  retries a fix against prod. Pre-launch gate: required before the first autonomous prod ship.
allowed-tools: Read, Write, Bash, WebFetch, ToolSearch
---

# infra-prod-verify — confirm in prod, or roll back

A staging-green fix that regresses production is the scariest failure mode this whole system has — it's
the 2am incident. `infra-prod-verify` is the R2/R9 gate that prevents it: after a deploy, it proves the
fix **held in prod** by tying back to the **original error signal** (through the `analytics` adapter),
and on any regression it **rolls back immediately**. Read `references/error-signal-tieback.md` — the
signal query, the decision, and the rollback all live there.

## What this block does NOT do (atomic boundary)
It does **not** fix (`dev-fix`), ship (`infra-ship`), or retry a fix against prod (forbidden). It
confirms or rolls back + escalates. It never issues a deploy command: rollback instructions come from
`deploy.rollback_hint(env)` and a human executes them.

## Inputs / Outputs
**Inputs:** the deployed release ref + deploy time (from `infra-ship` / the `deploy` adapter), the
ticket's **error reference** (the analytics provider's identifier for this bug — event name, error
fingerprint, saved query id — carried from `dev-intake`), and the roles in
`.claude/loops/stack.config.json`: `adapters.analytics` (the signal), `adapters.deploy` (release +
rollback hint), `app.environments.<env>.url` (where the smoke runs), `adapters.auth_provider` (the
smoke's sign-in role + credentials).
**Output:** `prod-verify.json`, written to **`<EVID_ROOT>/<KEY>/prod-verify.json`** (`EVID_ROOT`
defaults to `evidence.root`, `runs/`):
`{ verdict: prod-confirmed | prod-regressed | inconclusive, reason, rollback_recommended, escalate,
provenance{mode: autonomous|human, confirmed_by}, evidence{smoke, before_rate, after_rate, drop_pct,
new_signature, min_drop, windows, env, deployed_version, analytics_unavailable} }`.

## Procedure
### 0. Preflight (R9 + where the smoke may run)
```bash
bash scripts/prod_verify_preflight.sh --ticket PROJ-123 [--env prod] [--mirror-env staging]
```
Fails closed on the brain's `global_pause` (R9: an explicit `False` or nothing happens), on a missing
`app.environments.<env>.url`, and on a missing replay oracle. It never executes anything against the
app. Its `smoke_mode` decides step 1: `prod` (the oracle is marked read-only), `mirror` (a non-prod
environment with a URL is named by `--mirror-env`), or `skipped-mutating`.

### 1. Post-deploy smoke
Re-run the ticket's replay oracle against the deployed build — `repro.json.replay_spec` names it
(`replay-oracle.sh`, the same headless `browser.oracle_driver` oracle `qa-verify` ran red→green). **Only
an oracle that changes no data may run against production.** It replays the repro steps, which usually
create, edit or delete records, and production data is never a test fixture (R6).
- **`smoke_mode: prod`** — the oracle has the exact line `# PROD-SAFE: read-only` (qa-repro adds it only
  when the replay and the grader just navigate and read) → run it against production:
  ```bash
  APP_URL=<app_url from preflight> ALLOW_PROD=1 [ROLE=<an auth_provider role>] \
    [E2E_ENV_FILE=<credentials valid on that host>] bash <EVID_ROOT>/<KEY>/replay-oracle.sh
  ```
  The oracle template refuses a production host unless `ALLOW_PROD=1` is set **and** that line is
  present, so a mutating oracle still BLOCKs if the flag is set by mistake. This step is the only one
  that sets it. Production needs credentials valid on that host for the role — the `auth_provider`
  adapter's `credential_env_pattern` names the variables; without them the run exits 2.
- **`smoke_mode: mirror`** → never set `ALLOW_PROD`. Run the oracle against the mirror environment's URL
  only after `deploy.deployed_version(<mirror-env>)` matches the deployed release; otherwise it is not a
  smoke of this build — fall through to skipped.
- **`smoke_mode: skipped-mutating`** → record `--smoke skipped-mutating` and handle it as exit 2 below.

Read the **exit code**: **0** (GREEN) → `--smoke pass`; **1** (RED, the bug is back) → `--smoke fail`
→ `prod-regressed` immediately (the signal is not consulted); **2** (BLOCKED: no credentials for the
host, app unreachable, not marked read-only, grader threw) is neither pass nor fail → `--smoke blocked`
→ `inconclusive`, escalate. Never roll back on a blocked oracle.

### 2. Tie back to the original error signal (analytics adapter)
```bash
python3 scripts/prod_signal.py --signature "<error reference>" --deployed-at <ISO deploy time> \
  [--env prod] [--before-window 24h] [--after-window 2h] [--version <deployed release>] \
  [--previous-version <ref>] [--new-signature-query "<provider query for errors first seen after deploy>"] \
  > <EVID_ROOT>/<KEY>/prod-signal.json
```
Queries the error reference over a **before** window (ending at deploy) and an **after** window (scoped
to the deployed release when the `deploy` adapter — or `--version` — knows it), normalizes both to a
**rate** (a rate, not a raw count — traffic shifts), and probes for a **new error signature** when the
provider can answer that. Exit **3** = `adapter-unavailable:analytics` (`"provider": "none"`): keep the
JSON, it feeds step 3 as `inconclusive`. Never substitute a guess for a missing adapter.

### 3. Decide (deterministic — tested)
```bash
python3 scripts/evaluate_prod_signal.py --smoke pass|fail|blocked|skipped-mutating \
  --signal <EVID_ROOT>/<KEY>/prod-signal.json [--min-drop 0.5] [--ticket PROJ-123] \
  > <EVID_ROOT>/<KEY>/prod-verify.json
# rates already in hand:   --before-rate <r> --after-rate <r> --new-signature true|false
# a human confirmed it:    --human-confirmed-by "<name>"      (recorded as provenance)
```
→ `prod-confirmed` (smoke pass + rate dropped ≥ min_drop + no new signature) / `prod-regressed`
(smoke fail, or new signature, or rate not dropped) / `inconclusive` (dropped but not convincingly, or
no analytics evidence, or a blocked smoke → escalate, don't auto-confirm). `--human-confirmed-by`
yields `prod-confirmed` with human provenance unless the smoke was RED. An autonomous
`prod-confirmed` **requires** an analytics adapter — `none` can only ever produce `inconclusive`.

### 4. On regression → ROLL BACK (R9) + escalate; on confirm → close
- `prod-regressed` → **roll back now**: prefer a **feature-flag flip** (instant) over a deploy revert;
  `python3 lib/adapters/cli.py deploy rollback_hint env=prod` returns the configured instructions, which
  the escalation carries verbatim. Then `dev-escalate` with the prod evidence (before/after rate, new
  signature, smoke output, the rollback hint). High severity.
- `inconclusive` → escalate (a human decides); do **not** auto-confirm. When the reason is a missing
  analytics adapter, the handoff asks the human to confirm in production and re-run step 3 with
  `--human-confirmed-by <name>`.
- `prod-confirmed` → write `prod-verify.json`, record the metric, close the loop.

**Never retry a fix against prod.** Confirm or roll-back-and-escalate — those are the only exits.

## Success criteria & failure categories (declared up front)
**Success:** a correct verdict backed by smoke + the analytics before/after evidence (or an explicit
human confirmation with provenance); on regression, a rollback was triggered and escalated.
**Failure categories (spec):** smoke fails; error signal unchanged/worse; new error signature appears;
no signal source (`adapter-unavailable:analytics`) — inconclusive, never confirmed.

## Knowledge brain (qa-kb)
Checks `global_pause` first (R9, fail closed). Reads the ticket's error reference (via the artifacts).
Writes prod-outcome **metrics** (R5/R8): `kb metric --name prod_outcome --value 1 --ticket <KEY>
--metadata '{"outcome": "<verdict>", "rollback": <bool>, "provenance": "<mode>"}'`. The tracker ticket
(through the `issue_tracker` adapter, via `dev-escalate`) + the rollback are the human-facing signal.

## Live-run prerequisites
Autonomous prod shipping stays off while the brain's `staging_only` posture flag is set and until the
R2/R5/R6/R9 gates are verified. A live run needs an `analytics` provider with credentials, a `deploy`
provider (or `--version`) for the release scope, a production URL in `app.environments`, and the ticket's
error reference. Without them every run ends `inconclusive` with a prod-confirm handoff — by design.
The **decision logic is tested with synthetic signals** (`.claude/loops/tests/test_prod_signal.sh`).

## Key principles
1. **Deploy success ≠ fix confirmed.** Prove the *specific error* dropped, or it didn't work.
2. **Confirm or roll back — never retry against prod.** Two exits only.
3. **Roll back with a flag flip** where possible — instant and safe (R9).
4. **Inconclusive is not a pass.** A weak signal — or no signal source — escalates to a human.
5. **Atomic.** Confirm or roll-back-and-escalate; never fix or re-ship here.
