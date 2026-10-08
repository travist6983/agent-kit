---
name: dev-escalate
description: >-
  Hand a stuck defect back to a human WARM — the full loop memory, never a cold ticket. Trigger for
  "escalate PROJ-123", "hand this to a human", "the loop is stuck — escalate", or as the escalate
  step when a recipe hits any stop condition. The deterministic renderer fills the configurable
  template (escalate.config.json) with the attempt log, failure class, repro + expected/actual,
  verify/regression verdicts, the loop's decisions (must-read ones as actions, the rest as one-line
  context), and every before/after recording (scans media/) — and fails loudly rather than emit an
  empty handoff. Assigns per area, transitions the ticket. Posting to the issue tracker is an
  outward action: dry-run by default, posts only when the recipe authorizes, and only through the
  issue_tracker + notifications adapters. Does NOT fix, reproduce, or ship.
allowed-tools: Read, Write, Bash, AskUserQuestion
---

# dev-escalate — warm handoff, never a cold ticket

When a loop stops, the worst outcome is a human opening a ticket that just says "the bot couldn't fix
it." `dev-escalate` is the opposite: it packages **everything the loop learned** so the human picks up
mid-stream. The rule is that **the tracker issue IS the stored signal at handoff** — so the comment
must carry the real memory, and the template must be **configurable**, not baked into the skill.

## What this block does NOT do (atomic boundary)
It does **not** reproduce, fix, regress, or ship. It composes the handoff and (when authorized) posts it.
The decision *to* escalate belongs to the recipe; `dev-escalate` executes a clean handoff.

## Inputs / Outputs
**Inputs:** the loop's `TASK_DIR` (whatever artifacts exist: `intake/brief/repro/fix/verify/regression.json`
+ `media/`), the stop `REASON` (failure class), and `assets/escalate.config.json` (template + per-area
assignee + transition). The tracker itself, the project's key pattern and the notification provider come
from `.claude/loops/stack.config.json` (`adapters.issue_tracker`, `project.ticket_key_pattern`,
`adapters.notifications`) — never from this skill.
**Output:** `TASK_DIR/escalate.json`
`{ key, area, assignee, assignee_configured, transition, notification_channel, dry_run, posted,
delivered{issue_tracker, notifications}, warnings[], comment }` plus `TASK_DIR/handoff.md` (the rendered
comment, always written, so the handoff lands on disk even when no adapter is reachable).

## Procedure

**Invoking `kb`.** Every `kb <verb>` below is the `qa-kb` CLI wrapper
(`.claude/skills/qa-kb/scripts/kb`), which must be **on your PATH** (one symlink per machine —
`docs/brain-setup.md`). It is **not** there in a fresh checkout: an unresolved `kb` exits **127**
(`kb: command not found`) and the brain write is simply *lost*, so fall back to the script instead of
skipping the call — **absolutely**, never cwd-relative, because a loop's cwd is normally a sandbox
worktree, not this repo:

```bash
# <framework-repo> = the checkout of this framework THIS file was loaded from; set both in ONE shell.
KB="${KB_BIN:-<framework-repo>/.claude/skills/qa-kb/scripts/kb.py}"
python3 "$KB" <verb> ...        # ≡ the bare `kb <verb> ...` calls below
```

### 1. Assemble the handoff (deterministic, testable)
```bash
python3 scripts/render_escalation.py --task-dir "$TASK_DIR" --reason "$REASON" > "$TASK_DIR/escalate.json"
```
This pulls every artifact present and fills the configurable template with: the attempt log (what each
stage returned), the **failure class**, the **best-guess root cause**, the reproduction + **expected vs
actual**, the verify/regression verdicts, the **decisions the loop made** (`fix.json.design_decisions`
plus the `plan_synthesis` runner-up), **Noticed, not changed** (`fix.json.noticed_not_changed` plus the
*Noticed, not changed* bullets of `review.md` — pre-existing issues that never become PR threads, all
of them, uncapped), and the **before/after media** paths. Read the rendered `comment`
— if anything load-bearing is missing, that's a signal the upstream skills under-recorded.

The renderer exits **2** on a bad config, a corrupt artifact or a template error and **3** when the task
dir holds no artifacts at all — a missing artifact is normal (loops stop early), an empty handoff is not.

> **Why decisions belong in the bundle, and why most of them are one line:** a handoff is the moment a
> human takes the ticket over, so it carries the loop's whole record. But it keeps the same discipline
> as the PR thread bar (`infra-ship/references/pr-thread-bar.md`): the renderer puts the `must_read`
> entries under **Needs your decision** with a "please confirm", and lists everything else — plain
> strings, older entries, the runner-up — as one-line context. A "please confirm" on every call
> teaches the reader to skip the one that matters. `dev-loop-feature-prerelease` stages through this
> renderer instead of `infra-ship`, so on that path this comment is the human's only view of them.
> `(none flagged)` when there is nothing at all.

### 2. Pick the assignee (configurable, per area)
From `escalate.config.json` `assignees.by_area[area]` (fall back to `assignees.default`). The `area`
is whatever `dev-intake` wrote into `intake.json` — area names are the target repo's, not this
framework's. Values are opaque here: a user id, handle or email that the configured `issue_tracker`
adapter resolves. If the result is null (not yet configured), leave unassigned and **say so in the
report** (`assignee_configured: false` + a `no-assignee-configured` warning) — never silently drop the
routing.

### 3. Post to the issue tracker — GATED (outward action)
Posting a comment / assigning / transitioning a ticket is **sending on the user's behalf**. Default to
**dry-run** (render + return, no post):

```bash
python3 scripts/post_escalation.py --task-dir "$TASK_DIR" --reason "$REASON"               # dry-run: handoff.md + what WOULD be sent
python3 scripts/post_escalation.py --task-dir "$TASK_DIR" --reason "$REASON" --authorize   # only when the recipe authorizes
```

With `--authorize` the poster goes through `lib/adapters` and nothing else:
`issue_tracker.add_comment(key, comment)`, then `issue_tracker.assign(key, assignee)` when one is
configured, then `issue_tracker.transition(key, transition_on_escalate)` when that is set, then
`notifications.post(notification_channel, summary)` — always, so a tracker-less stack still receives a
warm handoff (with the default `file` providers that is `runs/tickets/<KEY>.comments.md` and
`runs/notifications/escalations.md`). Post for real **only when the recipe explicitly authorizes** it
(post-safety-gates) — and even then only to a key that matches `project.ticket_key_pattern`; anything
else is `wrong-tracker-target` and the poster refuses (exit 2). A role whose provider is `none` is a
structured `blocked` with reason `adapter-unavailable:<role>` (exit 3 when neither destination exists)
— never a crash, never a silent skip, and the handoff is still in `handoff.md`.

### 4. Record the signal
The tracker comment is the stored signal. Optionally `kb metric --name escalation --value 1
--ticket <KEY> --area <area>` so the meta-monitor (R8) can trend escalation rate.

## Success criteria & failure categories (declared up front)
**Success:** the human receives (or, dry-run, would receive) a comment carrying the *entire* loop memory
— attempt log, failure class, repro + expected/actual, verify/regression, both videos, root-cause guess —
assigned to the right person, ticket transitioned.
**Failure categories:**
- `thin-handoff` — the comment is missing load-bearing context (a cold ticket in disguise). Fix the upstream record.
- `no-assignee-configured` — `assignees.by_area` lacks this area and `assignees.default` is null → leave unassigned + flag (never silent).
- `wrong-tracker-target` — a key that does not match `project.ticket_key_pattern`, or a tracker other than the one `stack.config.json` declares, is a bug; the poster refuses rather than guesses.
- `adapter-unavailable:<role>` — the stack has no tracker *and* no notifications provider → `blocked`; the handoff stays in `handoff.md` for a human to carry.

## Knowledge brain (qa-kb)
The tracker issue is the handoff signal. Optionally writes an `escalation` **metric** (R5/R8). Writes
nothing canonical.

## Key principles
1. **Warm, never cold.** The whole point is the human doesn't restart from zero.
2. **Template is config, not code.** Edit `escalate.config.json`; the skill renders it.
3. **Posting is outward → gated.** Dry-run by default; real post only when the recipe authorizes, and only through the adapters.
4. **Never silently unassigned.** A null area-assignee is surfaced, not swallowed.
5. **Atomic.** Compose + (authorized) post the handoff. No fix, no repro, no ship.
