# infra-ship — ship policy, the R9 kill-switch, and the vcs_host adapter wiring

## The non-negotiable gate: R9 kill-switch (check it FIRST, every time)
Before **any** push / PR / merge / deploy, call `qa-kb` (invoke `kb.py` with `python3` directly, as
`ship_preflight.sh` does — don't rely on the bash wrapper being on PATH, and resolve it absolutely,
because a loop's cwd is a sandbox worktree):
```bash
KB="${KB_BIN:-<agents-repo>/.claude/skills/qa-kb/scripts/kb.py}"
python3 "$KB" pause --status
# ship ONLY on an explicit control.value == false → anything else (true, unreadable, exit 127) = HALT,
# do not ship, dev-escalate. The brain is any Postgres + pgvector the kb.config points at.
```
`scripts/ship_preflight.sh` does this as its first action and aborts with `global-pause` if set. A
global pause means a human (or the meta-monitor) hit the kill switch — shipping anyway is the exact
middle-of-the-night incident this exists to prevent.

## Prefer a feature flag over a redeploy
Where possible, ship the fix **behind a feature flag** so a bad fix is reverted by a **flag flip**, not
a redeploy/rollback. This makes `infra-prod-verify`'s rollback (R9) a fast, safe operation.

## Staging-only until the safety gates are verified
Until **R2 (prod-verify), R5 (budget breaker), R6 (blast-radius + input sanitize), R9 (pause + rollback)**
are all built and verified, **infra-ship runs staging-only** — it may format, commit, push a loop branch,
and open a PR (live by default; `SHIP_DRY_RUN=1` is the only thing that makes it a no-PR rehearsal —
`staging_only` never sets `dry_run`), but it must **not autonomously deploy to prod**. `staging_only`
feeds `prod_deploy_allowed`, and the preflight's prod-lane guard is where that refuses something: a push
to a branch matching stack.config `deploy.prod_lane_branch_patterns` is a production deploy in the
target repo (a CD trigger keyed on the branch name, no PR in between), so it aborts `prod-lane-branch`
(exit 7) unless `staging_only` is off **and** a human set `SHIP_PROD_LANE_OK=<that exact branch>` for the
run (a push refspec or upstream in git config that maps a loop branch onto a lane ref is refused too, and
no override unlocks it). The default pattern list is empty — no push is a deploy until the adopter says
which ones are. Prod enablement is a staged, human decision, one low-risk area first.

## Runner-agnostic ship target
`infra-ship` doesn't assume who invoked it. It opens a PR from a loop branch (`project.branch_prefixes`:
`fix/…`, `feature/…`) against `project.default_branch` (or `--base`) and stops; putting a change on a
deploy-lane branch is a human's call, because pushing a lane branch is itself the deploy. Auth failure
(the vcs_host provider not authenticated, the remote rejecting the push) → **`dev-escalate`**, never an
interactive prompt (this is autonomous).

## PR creation — git mechanics
1. base/protected-branch guard + prod-lane guard (`project.default_branch`,
   `project.protected_branch_patterns`, `deploy.prod_lane_branch_patterns`).
2. format: whatever formatter the target repo's guidelines file (`stack.repo_guidelines_file`) names;
   no framework default.
3. stage **source only**: `git add -u` + each `new_files` path (never `-A`); unstage any
   `plan.md`/`TASK.md`/`*-report.md`.
4. commit via HEREDOC; type from the branch prefix (`project.branch_prefixes`: `fix/`→`fix`,
   `feature/`→`feat`; conventional prefixes otherwise).
5. push to the preflight's `remote` (with `git pull --rebase` retry on non-fast-forward).
6. **release target, when the tracker provides one** — the normalized `Ticket` has no release field, so
   this is provider-specific (`raw`): bracket the **version** in the title, full name in the body:
   `python3 lib/adapters/cli.py vcs_host open_pr --json '{"branch": "<branch>", "base": "<default_branch>", "title": "<type>: PROJ-123 [<version>] — <summary>", "body": "…(Tickets section lists PROJ-123 + its full release target, e.g. Release 1.2.3)…", "draft": false, "labels": ["agent-loop"]}'`
   (read `result.id` / `result.url` / `result.draft` / `result.dry_run`). **Pass `draft: true` only when
   the invoker passed `--draft`** (the opt-in exception for `qa-ui-parity-sweep`) — the PR opens
   published. Multiple release targets → `[1.2.3, 1.3.0]`. This call is the only PR route — never a host
   CLI or an MCP PR tool (Steps 5–6 parse this JSON) — and nothing enables auto-merge, votes, or
   bypasses branch protection; pipeline runs, stage approvals and deploy reverts are human-only.
   **No release target on the ticket → omit the bracket and flag it in the body**
   (`⚠️ No release target on PROJ-123 — confirm before merge`); never guess a version.
7. embed before/after media in the PR **body** as a side-by-side `## Before → After` block (before | after
   GIFs that animate + `[before MP4]` · `[after MP4]` download links) — per `references/pr-media-embedding.md`.
   **In the body, never a comment.** Required whenever the loop produced recordings (SKILL.md Step 5).
   Assembled before the create: the adapter contract has no body-update op.
8. **post a thread only for a must-read item — usually none.** `scripts/select_pr_threads.py` is the one
   funnel: it keeps only `fix.json` `design_decisions` entries marked `must_read: true` with a recognised
   reason and a reviewer ask, caps them at `loop-limits.json` `defect_pr_threads.max_per_pr` (2), and
   writes neutral thread specs (`{path, line, body}`) that `vcs_host post_review_comment` takes as its
   kwargs. Everything else lives in the description or stays in `fix.json`. The must-read test, the
   recipe and the fallback: `references/pr-thread-bar.md`.

## What "wired" means here
PR creation is wired through the `vcs_host` role. The default provider, `git-local`, writes the PR as a
file under `<evidence.root>/prs/` and answers `dry_run: true` — the whole path **preflight → commit →
push → PR → threads → audit** runs offline against it, which is how the tests and evals work. A real host
is one provider class away (`lib/adapters/README.md`, "Adding a provider"): the skill does not change.
Open the PR **published** by default (a draft notifies nobody, so the run looks finished while the
review has not started; the only drafts are the invoker's explicit `--draft`, recorded `draft: true` and
listed in the invoker's report), and leave the merge to a human (branch protection on the default branch
enforces this). Still **NOT** enabled: **prod deploy** — that stays gated until the R2/R5/R6/R9 safety
gates are verified. `infra-ship` ships the PR for human review and stops there.
