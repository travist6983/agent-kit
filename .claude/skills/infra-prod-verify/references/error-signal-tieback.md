# infra-prod-verify — the error-signal tie-back + R9 rollback

This skill answers the R2 question: **did the specific thing the ticket was about actually stop
happening in production?** A green deploy is not a confirmed fix. The proof is two-part: a **post-deploy
smoke** passes, **and** the **original error signal drops** for the deployed build.

## The signal source: whatever the `analytics` adapter names
The signal source is a role, not a vendor: `adapters.analytics` in `.claude/loops/stack.config.json`.
An error-tracking tool, a product-analytics tool, a log query or a metrics backend all fit, as long as
the provider implements the two operations the role declares:

| op | what this skill uses it for |
|---|---|
| `query_error_signal(signature, since, version)` → `ErrorSignal{signature, count, first_seen, last_seen, version, samples, raw}` | the **before** window, the **after** window, and (optionally) a *new-signature* probe |
| `deployed_version(env)` → `str \| None` | the release the after window is scoped to (the `deploy` role is asked first) |

The ticket (or `dev-intake`) carries an **error reference** — the provider's identifier for the bug: an
event name, an error fingerprint, a saved query id, a log pattern. It is data the loop carries, not
something this skill discovers; `infra-prod-verify` passes it to the adapter verbatim as `signature`.

`scripts/prod_signal.py` queries two windows and prints one JSON object:
- **before:** a window ending at the deploy time (the baseline), `--before-window` (default `24h`),
  scoped to `--previous-version` when the previous release is known.
- **after:** a window starting at the deploy time, `--after-window` (default `2h`), scoped to the
  **deployed release** when releases are versioned — so you measure the new code, not the old.

It normalizes each `ErrorSignal` to a **rate** (events per hour over its window; a provider-supplied
`rate` field wins). A raw count misleads when traffic changed, so the decision always compares rates.
When the provider can answer "which error signatures first appeared after `since`", pass that query as
`--new-signature-query`; a non-zero count is a *new signature*.

## `none` is the safe default
With `"analytics": { "provider": "none" }` the query exits **3** with `adapter-unavailable:analytics`.
That is not an error to work around: the verdict becomes **`inconclusive`** and the loop escalates with
a *prod-confirm handoff* — a human confirms in production and re-runs the decision with
`--human-confirmed-by <name>`, which records `prod-confirmed` with human provenance. An autonomous
`prod-confirmed` **always** requires an analytics adapter. Nothing in this skill guesses a rate.

## The decision (logic is in `scripts/evaluate_prod_signal.py`, tested)
- **Smoke fails** → `prod-regressed` (first gate; the signal is not consulted). This holds even when a
  human confirmation flag is passed — a red oracle on the deployed build is hard evidence.
- **Smoke blocked / skipped-mutating** → `inconclusive`. Never roll back on a blocked oracle.
- **Human confirmation** (`--human-confirmed-by`) with a non-red smoke → `prod-confirmed`, provenance
  `{"mode": "human", "confirmed_by": …}`.
- **No analytics evidence** (`adapter-unavailable`, query failed, a window missing) → `inconclusive`.
- **A new error signature appears** post-deploy → `prod-regressed` (the fix introduced something).
- **after_rate ≤ before_rate × (1 − min_drop)** (default min_drop 0.5, boundary inclusive) **and** smoke
  passed **and** no new signature → `prod-confirmed`.
- **after_rate ≥ before_rate** (unchanged or worse) → `prod-regressed`.
- **before = after = 0** → `inconclusive` (the reference never fired; there is no baseline to drop from).
- Otherwise (dropped, but not convincingly) → `inconclusive` → **escalate, do not auto-confirm.**

`rollback_recommended` is true only on `prod-regressed`; `escalate` is true on `prod-regressed` and
`inconclusive`. Inconclusive is never a pass.

## Rollback (R9) — the whole reason this is a pre-launch gate
On `prod-regressed`: **roll back immediately** — prefer a **feature-flag flip** (instant, safe) over a
deploy revert — and `dev-escalate` with the prod evidence (before/after rates, the new signature if
any, the smoke output). A staging-green fix that regresses production is a **high-severity** signal.

Rollback is a human-executed action in this framework: `deploy.rollback_hint(env)` returns the
instructions for the configured stack (which flag to flip, which release to redeploy); the escalation
carries them verbatim. The skill never issues a deploy command itself, and it checks the brain's
`global_pause` flag before any production action.

**Never retry a fix against production.** The only outcomes are *confirm* or *roll back + escalate*.
Retrying risks a loop of bad deploys.

## Pre-launch status
`infra-prod-verify` is a **pre-launch gate (R2/R9)**: no loop may ship to production autonomously
until this skill has confirmed a real deploy end to end. Its decision logic is tested with synthetic
signals (`.claude/loops/tests/test_prod_signal.sh`); a live run requires an `analytics` provider with
credentials, a `deploy` provider (or `--version`) for the release scope, a production URL in
`app.environments`, and the ticket's error reference. Until then every run ends `inconclusive` and
hands off to a human — by design.
