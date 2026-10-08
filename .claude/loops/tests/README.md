# Regression suite

The deterministic tests behind every skill's tested core, persisted as a re-runnable suite. These are the
same checks run while building each skill, captured so they can be re-run any time (CI, after an edit, before a ship).

## Run it
```bash
bash .claude/loops/tests/run_all.sh
```
Expected: **all suites GREEN** — including on a machine with no brain configured and no `playwright-cli`
installed. Brain-backed suites (and the qa-kb harness) print `SKIPPED (brain unreachable)`; oracle suites
print a skip when `playwright-cli` is absent. A skip is not a pass of the guarantee — it only means that
guarantee was not exercised on this machine.

## What's covered
| Test | Skill core | Checks |
|---|---|---|
| `test_recipe_router.sh` | `dev-loop-*` retry/escalate engine | every failure class + precedence (paused beats budget) |
| `test_ship_preflight.sh` | `infra-ship` `ship_preflight.sh` branch gates | every prod-lane branch (from `deploy.prod_lane_branch_patterns`) refused with exit 7, incl. case games, the both-keys override (exact branch only), lane pushes hidden behind a push refspec / pushRemote / upstream (never overridable), a `"` in the branch still yields parseable JSON, R9/base-branch codes kept; offline stub brain, no network |
| `test_prod_signal.sh` | `infra-prod-verify` decision logic (R2) | confirm/inconclusive/regress + boundary + rollback flag |
| `test_escalation_render.sh` | `dev-escalate` handoff renderer | full loop memory assembled, incl. failing spec; `must_read` decisions render as actions, the rest as context; *Noticed, not changed* rendered |
| `test_pr_thread_bar.sh` | `infra-ship` `select_pr_threads.py` + `audit_pr_threads.py` (the PR thread bar) | an 11-entry fix receipt → 0 threads; must-read-only, ranking, per-PR cap + overflow, anchors, re-ship dedupe, noticed block, exit 4 on unclassified must-reads, and the read-back audit catching an improvised caveat thread |
| `test_sandbox.sh` | `infra-worktree` lifecycle (R4/R5) | isolation, cap, idempotent crash-safe teardown |
| `test_replay_oracle.sh` | `qa-repro` replay-oracle template (the RED→GREEN gate) | green 0 / red 1 / throwing grader 2; a missing locator, a failed goto, an unfilled template or grader, a dead host, a prod host, a bad or injected ROLE, and an unsourced credential file all BLOCK (2); one JSON line every time. Skips without `playwright-cli` |
| `test_contracts.sh` | `contracts/validate.py` | every ratified contract validates its `contracts/samples/EXAMPLE-*` set; the zero-tests-but-green regression artifact keeps FAILING |
| `test_dream_harvest.sh` | `qa-dream` `harvest.py` | reproducible, bounded, honest brief over the synthetic `fixtures/dream*` transcripts — never the user's live ones |
| `test_fix_lessons.sh` | `qa-kb` `fix_lessons` (4th store) | insert / dedup-bump / semantic recall |
| `test_kb_durability.sh` | `qa-kb` durability + receipts | a write is never silently dropped (outbox), and `validate.py --receipts` makes an artifact's write claims falsifiable |
| qa-kb `harness.py` | all four stores + ops + durability | every verb, in **both** embedder modes (live semantic proof / simulated-down degraded contract) |

`run_all.sh` picks up **every** `test_*.sh` in this directory — that IS the registration. The table above
is the guided tour, not the roster; the script's own header lists the current members.

## Safety
- **No changes to the target app, no outward actions.** Router/prod-signal/escalation/sandbox are pure or
  dry-run — nothing posts to the issue tracker or the VCS host, nothing deploys, nothing touches the target
  app's working tree. Adapter calls go to fakes. (The brain-backed suites *do* go over the network — the
  brain is a shared remote database; see the next bullet.)
- **Brain-backed tests hit the REAL, SHARED brain.** `test_fix_lessons`, `test_kb_durability` and
  `harness.py` connect through `kb.py`'s own DSN resolution (`kb.resolve_dsn()` + `kb.connect()`, so every
  `auth.mode` applies) — i.e. the **shared brain** named in `.claude/loops/kb.config.json` that every
  developer and machine uses. There is no local test database, and pointing them at one would only recreate
  the split-brain that let residue pile up unnoticed (QA-KB-C3 / X-REAL-C1). So:
  - every row they write carries a dedicated test `source` (`kb-harness`, `test-fixlessons`, `test-kbdur`),
    and each suite cleans up by **that source only** — never by ticket, because a `fix_lessons` bump
    COALESCEs the caller's ticket onto a row it did not create;
  - the runner sweeps the residue afterward — knowledge rows **and** the `kb_writes` / `kb_state` ledger
    rows every mutating verb now writes — then **reports what's left** (`⚠ brain tidy: …`) instead of
    swallowing a failed cleanup;
  - `harness.py` fingerprints the rows it did *not* write and asserts they come back byte-identical.
  **Running the suite touches shared knowledge**, so residue discipline is not optional: if a run prints a
  tidy warning, clear it (`kb undo --source <marker> --yes`, the ledger-driven reversal) before walking away.
- **One lock for every brain-touching entry point.** `lib/brain_lock.sh` (shell suites) and `harness.py`
  share one bolt at `KB_BRAIN_LOCK`, else `<KB_HOME or .claude/loops>/brain-tests.lock`. `run_all.sh`
  takes it once and exports `KB_LOCK_HELD` so the suites nest instead of deadlocking; a second concurrent
  run REFUSES (exit 3) rather than emitting reds that are really two runs deleting each other's fixtures.
- **Skips are per-suite, and only for an absent dependency.** `test_fix_lessons`, `test_kb_durability`
  and the harness step in `run_all.sh` probe with `kb pause --status` and **SKIP** cleanly when the brain
  can't be reached — no `database_url` configured, credentials missing, network or firewall in the way — so
  the rest of the suite still runs. Once the brain answers, `harness.py` never skips: an embedder outage
  (key missing or a 429) switches it to the degraded **EMBEDDER DOWN** contract rather than passing checks
  that didn't run.

## Prerequisites
- `python3` + `psycopg2` + `pgvector` (already used by `qa-kb`).
- For the brain-backed tests: a reachable shared brain (`database_url` in `kb.config.json` or
  `KB_DATABASE_URL`, plus whatever `auth.mode` needs — see `docs/brain-setup.md`) and the embedding key
  (the variable `embedding.api_key_env` names) in `kb.secret.env` for the live-embedder half.
- For the oracle tests: `playwright-cli` on `PATH` (`browser.oracle_driver` in `stack.config.json`).
