---
name: infra-ship
description: >-
  Ship a verified fix: format, commit (including NEW files), push, and open the pull request through
  the vcs_host adapter, per the target repo's branch policy — after the gates. Trigger for "ship
  PROJ-123", "open the PR", "push and create the pull request", or as the ship step of a defect loop.
  ship_preflight.sh runs first: R9 kill-switch (fail closed), not-on-the-base-or-a-protected-branch,
  a prod-lane refusal (a push to a branch matching stack.config `deploy.prod_lane_branch_patterns` IS
  a production deploy), and the mechanical R6 denylist scan of the full change set; staging_only
  gates only the prod deploy — the PR opens live. Embeds before/after media in the PR body. PR
  comment threads are rare: only a must-read item (reviewer must act before merge) gets one, via the
  capped select_pr_threads.py funnel — the expected count is zero; everything else lives in the
  description. Prefers a feature flag over a redeploy; NEVER auto-deploys to prod. Does NOT fix,
  verify, or confirm prod.
allowed-tools: Read, Write, Bash, AskUserQuestion, ToolSearch
---

# infra-ship — ship behind the kill-switch

`infra-ship` turns a verified fix into a PR (and, post-gates, a deploy). The git mechanics are ordinary;
what makes it the defect loop's ship step is the **R9 kill-switch check before anything leaves the
machine**, and the discipline that it **never autonomously ships to prod** until the safety gates exist.
Read `references/ship-policy.md` — the policy, the kill-switch, and how the PR reaches the VCS host all
live there.

Every external system is reached through the adapter layer (`lib/adapters/`, configured in
`.claude/loops/stack.config.json`): the PR opens through **`vcs_host`**, review threads post and list
through **`vcs_host`**, recordings upload through **`evidence_store`**. No skill step names a vendor.
A role whose provider is `none` fails closed: the CLI exits **3** with `adapter-unavailable:<role>`, and
this skill records `abort: "blocked"` with that reason — never a crash, never a guess.

## What this block does NOT do (atomic boundary)
It does **not** fix (`dev-fix`), verify (`qa-verify`), run regression (`qa-regression`), or confirm the
fix in prod (`infra-prod-verify`). It ships the PR and hands the prod-confirmation to the next step.

## Inputs / Outputs
**Inputs:** a verified change on a loop branch (`project.branch_prefixes`) + the loop's artifacts (for
the PR body + media).
**Output:** `ship.json`, written to **`<EVID_ROOT>/<KEY>/ship.json`** (EVID_ROOT = stack.config
`evidence.root`, default `runs/`)
`{ ok, abort, branch, base, commit_type, commit, pr_id?, pr_url?, pr_dry_run?, draft, media?, media_in_body?, decision_threads?, thread_selection?, thread_audit?, dry_run, paused }`
(`draft` = `true` only when the invoker passed `--draft` and the PR was opened as a draft; otherwise `false`.)
(`pr_dry_run` = what the provider reported: the default `git-local` provider writes the PR as a file under
`<evidence.root>/prs/` and returns `dry_run: true` — record it, so nobody mistakes a file for a review.)
(`decision_threads` = the ids of the threads Step 6 posted — usually none — or a `body-fallback` note;
`thread_selection` = `select_pr_threads.py`'s `counts`, so a reader can see what was kept off the PR;
`thread_audit` = `audit_pr_threads.py`'s read-back — `noise_thread` / `over_cap_on_pr` are the failures;
`media` = a **structured array** `[{kind, url, host_url?, label?, in_body}]` — NOT a prose string, and a
deliberate *superset* of the `repro.json`/`verify.json` contract media item; `ship.json` is not
contract-validated. See Step 5.)

## Procedure

**Invoking `kb`.** Every `kb <verb>` below is the `qa-kb` CLI wrapper
(`.claude/skills/qa-kb/scripts/kb`), which must be **on your PATH** (one symlink per machine —
`docs/brain-setup.md`). It is **not** there in a fresh checkout: an unresolved `kb` exits **127**
(`kb: command not found`) and the brain write is simply *lost*, so fall back to the script instead of
skipping the call — **absolutely**, never cwd-relative, because a loop's cwd is normally a sandbox
worktree, not this repo:

```bash
# <agents-repo> = this framework's checkout, the one THIS file was loaded from; set both in ONE shell.
KB="${KB_BIN:-<agents-repo>/.claude/skills/qa-kb/scripts/kb.py}"
python3 "$KB" <verb> ...        # ≡ the bare `kb <verb> ...` calls below
CLI="<agents-repo>/lib/adapters/cli.py"   # every adapter call below is `python3 "$CLI" <role> <op> ...`
```

### 1. Preflight (run this first — it's the safety gate)
```bash
bash scripts/ship_preflight.sh [--repo-dir DIR] [--base BRANCH]   # --base defaults to project.default_branch
```
It checks the **R9 kill-switch** (`kb pause --status` → abort `global-pause` if set), the
**base/protected-branch** guard (`project.default_branch`, `project.protected_branch_patterns`), reads
the **`staging_only`** posture, runs the **prod-lane guard** (below), the **R6 blast-radius denylist**
(scans unstaged+staged+untracked changes against `dev-fix/assets/r6-denylist.globs` — a hit aborts
`r6-denylist`: forced human gate, never ship), derives the **commit type**, and previews everything
that would be committed (`changed_source` + `new_files`) — all **without** pushing. Every abort exits
**non-zero** (1 bad-repo-dir · 2 pause · 3 base/protected branch · 4 R6 hit · 5 R6-scan-failed · 6
nothing-to-ship · 7 prod-lane-branch), so `&&`-chains stop. If `abort` is non-null → stop and
`dev-escalate` (paused / R6 / prod lane) or fix the branch state. This script is the tested core
(`tests/test_ship_preflight.sh`, `tests/test_r6_denylist.sh`). It reads stack.config through
`lib/adapters/config.py`; when it runs from a copy (a test tree), set `LOOPS_REPO_ROOT` to this checkout
or it falls back to schema defaults and says `"config": "defaults"`.

**`staging_only` gates the prod deploy, not the PR.** It sets `prod_deploy_allowed` and nothing else.
`dry_run` comes only from `SHIP_DRY_RUN=1` (an explicit per-run no-PR rehearsal); by default the
PR opens live even in staging posture, because a PR is the human review gate, not a deploy.

**Prod-lane guard (abort `prod-lane-branch`, exit 7).** Some repos deploy on a branch *name* alone: a CD
trigger on `release/*` (or whatever the adopter configured) with no PR in between, so pushing such a
branch **is** a prod deploy. Those names are stack.config **`deploy.prod_lane_branch_patterns`**
(globs; the default `[]` means no push is a deploy and the guard is inert — adopters whose pushes deploy
set it, see `docs/getting-started.md`). The preflight refuses them (case-insensitively) unless **both**
`prod_deploy_allowed` is true (a human turned `staging_only` off) **and** a human set
`SHIP_PROD_LANE_OK=<this exact branch>` for this run; the override is recorded as `prod_lane_override`
in the preflight JSON. Never set that variable yourself, and never rename a branch to get past the guard
— a refused lane branch escalates. Humans may push lane branches by hand; this only stops infra-ship
from being the one that pushes. Loop branches carry `project.branch_prefixes` (`fix/…`, `feature/…`),
which deploy nothing on push. The guard also reads git config, because the local name is not the whole
story: a push refspec (`remote.<r>.push`, or the branch's `pushRemote` / `remote.pushDefault` remote)
whose destination is a lane ref, or an upstream (`branch.<b>.merge`) on one, aborts `prod-lane-branch`
too, with `via`/`target` naming the mapping. The override never unlocks that case — remove the mapping.
Every branch / file-list string in the preflight JSON is escaped, so a `"` in a branch name cannot break
the line.

### 2. Format + stage source — including NEW files
Run the target repo's formatter if its guidelines file (`stack.repo_guidelines_file`) names one — there
is no framework default, and this skill never guesses a toolchain. Stage **both** tracked modifications
and the new files the preflight listed: `git add -u`, then `git add <each path in new_files>` (a fix
that creates a component/unit test must ship it — `git add -u` alone silently drops new files). Still
**never `-A`** — stage `new_files` explicitly so pipeline artifacts can't slip in; unstage any
`plan.md`/`TASK.md`/`*-report.md`. Confirm with `git diff --cached --name-only`.

### 3. Commit (HEREDOC) + push (rebase-retry)
Commit with the type from the preflight (`fix`/`feat`/…), the ticket key, and a tight file list. Push
`git push -u <remote> <branch>` (`remote` from the preflight JSON) — only the branch the preflight
cleared, never a different or renamed one; on non-fast-forward, `git pull --rebase` then re-push.

### 4. Open the PR — live and PUBLISHED; prod deploy still gated
**Release target (optional).** If the ticket your `issue_tracker` provider returned carries a release
target (a fix version / milestone in its `raw` payload — the normalized `Ticket` has no dedicated field,
so this depends on the provider), bracket its **version** in the title and put the full name in the body;
teams that tag releases in PR titles triage by it. Never invent one: with no release target, **omit the
bracket and flag it** in the body (`> ⚠️ No release target on PROJ-123 — confirm before merge.`). Then
assemble:
- **Title** (≤70 chars *including* the bracket): `<type>: PROJ-123 [<version>] — <summary>` — e.g.
  `fix: PROJ-123 [1.2.3] — invoice total ignores the member discount`. `<type>` is the branch-prefix type
  from the preflight; multiple release targets → join them `[1.2.3, 1.3.0]`.
- **Body**: Summary / Tickets (list the ticket **and its full release target**, e.g.
  `PROJ-123 — Release 1.2.3`) / Changes / a **`## Before → After`** media block (the side-by-side
  before/after GIFs + MP4 download links — assembled in **Step 5**, embedded in the BODY, never a comment)
  / Testing (a caveat about what could not be verified is **one line** here; so is each `dev-pre-pr`
  warning worth keeping) / then the funnel's `noticed_block` verbatim — the `## Noticed, not changed`
  section, empty on most PRs.
- **Run the thread funnel now, so the top and bottom of the body are known up front** (command and rules
  in Step 6). Prepend its `description_top` verbatim — the banner when a thread will be posted, plus a
  `## ⚠️ Before merging` section for any must-read item past the cap — and append its `noticed_block`.
  Both are empty on most PRs, and then there is no banner and no section.
- **Assemble the whole body before the create.** The adapter contract has no "update PR body" op, so the
  media block (Step 5 uploads first, the PR is opened second) and the funnel's blocks must be in the body
  that goes into `open_pr`.

**Open the PR published by default.** Pass `draft=false`. A draft is not a state the loop uses, and a
draft-then-publish two-step bought nothing while giving a run a way to stop halfway and leave a PR parked
where nobody is notified. Publishing is not auto-merging: the PR is still for human review — required
reviewers and branch protection gate completion, not comment threads (Step 6 posts one only for what the
reviewer must act on, usually none).

**The one exception is opt-in: `--draft`, passed by the invoker.** It exists for `qa-ui-parity-sweep`,
whose fix PRs port a style rule it could not see rendered on a running build — a draft there says
"unverified, look before you publish" instead of asking reviewers to review it. When (and only when) the
invoking skill passes `--draft`: pass `draft=true` to `open_pr`, confirm the response's `draft` is `true`
(if it is not, say so — never record a draft that isn't one), set `"draft": true` in `ship.json`, and start
the PR description with `> 📝 Draft from <invoker> — not yet verified on a running build; publish when
reviewed.` Every gate still runs unchanged — preflight (R9, base branch, prod lane, R6 scan), the loop
label, the thread funnel. The invoker owns the follow-up: it must list every draft it opened in its own
report, so nothing sits unnoticed. `infra-ship` never chooses a draft on its own judgment, and never
publishes a draft later. A parity-sweep draft has no tracker ticket: the invoker passes its own key
(`PARITY-<run>-<finding>`), the release-target lookup is skipped, the title is `fix: [parity] <summary>`
with no ticket key or version bracket, and the body names the sweep report instead of a ticket. That is
the only ticketless ship. It also has no `dev-fix` `fix.json`, so it has nothing for Step 6's funnel to
select from: post no threads, record `thread_selection: {"skipped": "no fix.json (parity sweep)"}`, and
let the report and the description carry everything.

Create it with the label **`agent-loop`** — the machine-readable "a loop opened this" mark. `dev-pr-feed`
keeps labelled PRs out of the scheduled PR-review queue: the loop already reviewed its own diff, and this
PR is held to the thread bar (Step 6), so a review tick stacking more threads on it would be the
calibration-PR noise all over again. (A label survives every host's list API; a marker in the body does
not — descriptions are truncated or omitted in list views.)

**Create it only this way** — `vcs_host open_pr` through the adapter CLI, never through a host-specific
CLI, an MCP pull-request tool or any other route: Steps 5–6 read `id` / `url` / `draft` / `dry_run` from
exactly this normalized response, and another tool's response shape silently breaks them. After the
create, the PR belongs to its reviewers: **never enable auto-merge, never approve or vote, never bypass
branch protection** — by any tool. Queueing pipeline runs, approving stages, and reverting deploys are
**human-only**.

**Capture the create result as JSON** so Steps 5–6 can address the PR:
```bash
# BODY already holds description_top + Summary/Tickets/Changes + the media block + Testing + noticed_block
PR_JSON=$(python3 "$CLI" vcs_host open_pr --json "$(jq -n --arg b "$BRANCH" --arg base "$BASE" \
            --arg t "$TITLE" --arg body "$BODY" --argjson draft "${DRAFT:-false}" \
            '{branch:$b, base:$base, title:$t, body:$body, draft:$draft, labels:["agent-loop"]}')"); RC=$?
# RC 3 → ship.json abort "blocked", reason $(jq -r .error <<<"$PR_JSON") (adapter-unavailable:vcs_host) → dev-escalate
# RC 1 → auth-failure / op error: $(jq -r .error <<<"$PR_JSON") → dev-escalate, never an interactive prompt
PRID=$(jq -r '.result.id'       <<<"$PR_JSON")
PRURL=$(jq -r '.result.url'     <<<"$PR_JSON")
ISDRAFT=$(jq -r '.result.draft // false'   <<<"$PR_JSON")
PRDRY=$(jq -r '.result.dry_run // false'   <<<"$PR_JSON")   # git-local: true — the PR is a file, say so
```
Auth failure → `dev-escalate`, never an interactive prompt.
**Prod deploy stays GATED** — never auto-deploy to prod until the R2/R5/R6/R9 gates are verified;
shipping the PR is as far as this skill goes.

### 5. Embed the recordings — in the BODY, never a comment

**How many recordings you have depends on the lane, and the rule differs:**

| Lane | Before | After | PR body |
|---|---|---|---|
| **Defect** (Recipes ①②③) | `qa-repro`'s repro video — the bug happening | `qa-verify`'s after | **`## Before → After` side-by-side table. An after with no before is WRONG** — the before *is* the evidence the bug was real. |
| **Feature** (Recipe ④) | **none** | `qa-verify`'s after | **`## After` — the after alone.** Not a defect, so there is no "before" to contrast, and one line of description covers what the surface looked like before. |

A feature PR with only an after is **correct**, not a missing-recording fallback. A before-capture on a
feature would revert the working tree to manufacture a same-view A/B: it costs minutes, changes no
verdict, and serialises the run behind a checkout of live files. Do not introduce it, and do not treat
its absence as an error.

Put the recording(s) in the PR **description**: the media URLs come from `repro.json.media[]` /
`verify.json.media[]` (already uploaded through the `evidence_store` adapter at capture time), or from
`python3 "$CLI" evidence_store upload path=<file> key=$KEY` now when an upstream entry is missing. On a
defect the block is the side-by-side table with a `Full-quality video: [before MP4] · [after MP4]` line
beneath; on a feature it is the after GIF with its own `Full-quality video: [after MP4]` line. Because
there is no body-update op, this block is assembled **before** Step 4's create.

**A single combined GIF, or media dropped in a comment, is wrong** (comment surfaces on several hosts
flatten a GIF to a static first frame). Confirm every GIF **animates and is legible** on the PR before
declaring done — when the provider is a real host; with the `local` evidence store and the `git-local`
PR file the links are `file://` paths, which is fine for a dry run and must be stated in the report. If a
capture reads as one static frame, re-record tighter/higher-quality per `qa-repro/references/recording.md`
and re-upload. Full recipe — upload, the exact table markdown, defect-vs-feature column labels, and the
missing-recording fallback — is in `references/pr-media-embedding.md`. **Upload every artifact you have —
GIFs AND MP4s** (four on a defect, two on a feature); the GIFs embed and the MP4s are `Full-quality video`
links. Record media into `ship.json` as a **structured array** `media: [{kind, url, host_url?, label?,
in_body}]` — one entry per artifact, `kind` from the shared `before-*`/`after-*` enum, **`url` = the
evidence-store URL (primary, from `repro.json.media[]` / `verify.json.media[]`)** and `host_url` = the
PR host's own attachment URL when your provider hosts attachments (secondary, optional). Never a prose
`media_embedded` string: it was GIF-only and unparseable. If a change has genuinely no UI recording,
say so in the body; never fake or silently drop it.

### 6. Post a thread only for what the reviewer must act on — usually nothing
A PR thread is an interrupt, and the reviewer pays for every one. The calibration case was a PR with two
simple bugs that got 14 active threads under an old "flag every decision and alternative" rule, every one
accurate and every one noise: none asked the reviewer to *do* anything. Read `references/pr-thread-bar.md`
before posting anything; it has the must-read test, the reasons that qualify, and where everything else goes.

Every thread goes through one funnel, and what reached the PR is read back afterwards. Resolve the
scripts absolutely (a loop's cwd is a worktree sandbox), the same way as `kb`:
```bash
S="<agents-repo>/.claude/skills/infra-ship/scripts"   # <agents-repo> = the checkout this file came from
# 1. what is on the PR now (nothing on a fresh PR; everything, on a re-ship)
python3 "$CLI" vcs_host list_review_threads id="$PRID" > "$TASK_DIR/pr-threads-before.json"
# 2. the funnel, now with --existing. On a first ship the selection matches Step 4's (Step 4 ran before
#    the PR existed); on a re-ship it drops what is already there — if description_top changed, the
#    body cannot be patched (no update op): record the new description_top in the run report
python3 "$S/select_pr_threads.py" --fix "$TASK_DIR/fix.json" --existing "$TASK_DIR/pr-threads-before.json" \
  [--candidates "$TASK_DIR/pr-candidates.json"] [--review-report "$TASK_DIR/review.md"] \
  --out-dir "$TASK_DIR/pr-threads" > "$TASK_DIR/pr-threads.json"
# 3. post exactly the selection — by rank from the JSON, never a glob — and keep the returned ids
THREAD_IDS=""
for r in $(jq -r '.threads[].rank' "$TASK_DIR/pr-threads.json"); do
  id=$(python3 "$CLI" vcs_host post_review_comment id="$PRID" --json "$(cat "$TASK_DIR/pr-threads/thread-$r.json")" \
         | jq -r '.result.thread_id // .result.id')
  THREAD_IDS="${THREAD_IDS:+$THREAD_IDS,}$id"
done
# 4. read back, and audit (--runner = the identity your vcs_host provider posts as, e.g. `git config user.name`
#    for git-local; optional, it only stops a colleague's mid-ship comment counting as loop noise)
python3 "$CLI" vcs_host list_review_threads id="$PRID" > "$TASK_DIR/pr-threads-after.json"
python3 "$S/audit_pr_threads.py" --before "$TASK_DIR/pr-threads-before.json" \
  --after "$TASK_DIR/pr-threads-after.json" --posted-ids "$THREAD_IDS" [--runner "<identity>"]
```
The funnel keeps only `design_decisions` entries marked `must_read: true` with a recognised
`must_read_reason` and a reviewer ask, ranks them, and caps them **per PR** at `loop-limits.json`
`defect_pr_threads.max_per_pr` (2) — a thread already on the PR uses its slot and is never posted twice.
Zero threads is the common result: then you post nothing, and that is the step done right.

**Nothing bypasses the funnel.** A caveat that occurs to you at ship time, or an unresolved review
finding about *this diff*, goes in `pr-candidates.json` only if it passes the must-read test — with a
reason, a `files` anchor, and an ask. A pre-existing issue never goes there, whatever its severity: it
belongs in *Noticed, not changed* and the run report. Everything else belongs in the description or
nowhere, never in a thread.

Record the funnel's `counts` in `ship.json` `thread_selection` and the audit in `thread_audit`. The
audit exits **3** when a thread reached the PR outside the funnel (`noise-thread`) or the PR now holds
more loop threads than the cap (`over-cap`) — say so at the top of the run report; don't hide it and
don't delete threads to cover it. The funnel exits **4** when an entry claims must-read without a
recognised reason or an ask: post nothing, set `ship.json` `abort: "unclassified-must-read"`, and let
the recipe route it — `fix.json` is `dev-fix`'s artifact, not yours to edit. Never post around either
script. The posting loop and the body-fallback are in `references/pr-thread-bar.md`.

### 7. Hand off to infra-prod-verify (post-gates only)
After a real deploy (once the gates are verified), chain into `infra-prod-verify` to confirm the fix
landed in prod and the error signal dropped — with auto-rollback on regression. **Not** enabled until the
gates are verified.

## Success criteria & failure categories (declared up front)
**Success:** kill-switch clear, source-only commit on a non-base, non-protected, non-prod-lane branch,
pushed, PR assembled and opened **published** (a draft only when the invoker passed `--draft`, recorded
as `draft: true`), and the PR's threads are exactly `select_pr_threads.py`'s selection — usually none.
No prod deploy unless the gates enabled it.
**Failure categories:**
- `global-pause` — R9 flag set → halt + escalate (never override).
- `pause-check-failed` — the brain could not be read → fail closed, escalate.
- `on-base-branch` — refused to ship from `project.default_branch` or a `project.protected_branch_patterns`
  match (`protected_by` names which).
- `prod-lane-branch` — refused to push a branch matching `deploy.prod_lane_branch_patterns`, whose push
  is a production deploy in the target repo (by name, or via a push refspec / upstream in git config —
  `via` says which); needs `staging_only` off **and** a human's per-run `SHIP_PROD_LANE_OK=<branch>` (a
  config-mapped lane push is never overridable) → escalate, never rename around it.
- `r6-denylist` / `r6-scan-failed` — the change set touches the blast-radius denylist, or the scan could
  not run → forced human gate.
- `nothing-to-ship` — no source change and no unpushed commit.
- `blocked` — `adapter-unavailable:vcs_host` (provider `none`, CLI exit 3) → escalate; never fall back to
  a host CLI.
- `auth-failure` — the vcs_host adapter / remote not ready (CLI exit 1) → escalate, don't prompt.
- `policy-block` / `pipeline-red` / `merge-conflict` — surface; the recipe decides retry/escalate.
- `left-as-draft` — a PR was opened as a draft that the invoker did NOT ask for (no `--draft`), or a
  `--draft` PR was not recorded as `draft: true` in `ship.json`. Nobody is notified of a draft, so the run
  looks finished while the review has not started. Open published unless the invoker passed `--draft`.
- `noise-thread` — a thread reached the PR outside the funnel (`audit_pr_threads.py` exit 3,
  `thread_audit.noise_thread`), or for an item that fails the must-read test. Accurate content does not
  excuse it; the reviewer still paid for it.
- `over-cap` — the PR holds more loop threads than `defect_pr_threads.max_per_pr` (`thread_audit.over_cap_on_pr`).
- `unclassified-must-read` — the funnel exited 4: `dev-fix` marked something must-read without a reason
  or an ask. Nothing is posted; the recipe routes it.

## Knowledge brain (qa-kb)
**Reads** the `global_pause` flag (R9) — the gate — and the `staging_only` posture. May write a `ship`
**metric** (R5/R8). Writes nothing canonical. The PR-thread numbers (`thread_selection`, `thread_audit`)
go into `ship.json`; the recipe turns them into the `pr_threads_*` metrics, so "are loop PRs getting
quieter?" is answerable from the brain.

## Key principles
1. **Kill-switch first.** Check `qa-kb` `global_pause` before anything leaves the machine.
2. **Flag over redeploy.** Prefer shipping behind a feature flag so revert is a flag flip.
3. **Never auto-prod until the gates exist.** No prod deploy until R2/R5/R6/R9 are verified — and a push
   to a `deploy.prod_lane_branch_patterns` branch *is* a prod deploy, so the preflight refuses it.
4. **PRs by one route, and the merge is human.** Only `vcs_host open_pr` with the `agent-loop` label
   (never a host CLI or an MCP PR tool); never auto-merge, approve, or bypass branch protection; pipeline
   runs, stage approvals, and deploy reverts are human-only.
5. **Source only.** Never commit pipeline artifacts; never `git add -A`.
6. **Threads are earned.** The description explains the change; a thread is only for what the reviewer
   must act on before merge and would miss otherwise. Zero is the usual count, two is the cap, and every
   thread goes through `select_pr_threads.py`.
7. **Atomic.** Ship the PR; hand prod-confirmation to `infra-prod-verify`.
