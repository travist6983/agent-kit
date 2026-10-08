---
name: human-explore
description: >-
  The human-in-the-loop learning producer AND the only approval path in the brain: promote proposed
  expectations to canonical. Trigger for "bless these expectations", "review the proposed
  expectations", "promote this to canonical", "let's walk this feature together and I'll correct
  you". A tester drives; live corrections become canonical ground truth — every bless carries a
  human signature (--by), never autonomous. Also reviews the propose queue (bless/reject). Does NOT
  fix or ship.
allowed-tools: Read, Write, Glob, Grep, Bash, AskUserQuestion, ToolSearch, mcp__claude-in-chrome__tabs_context_mcp, mcp__claude-in-chrome__tabs_create_mcp, mcp__claude-in-chrome__navigate, mcp__claude-in-chrome__find, mcp__claude-in-chrome__form_input, mcp__claude-in-chrome__read_page, mcp__claude-in-chrome__get_page_text, mcp__claude-in-chrome__javascript_tool, mcp__claude-in-chrome__computer
---

# human-explore — the bless gate

`human-explore` is the human side of the brain. It does the one thing the autonomous skills are
forbidden to do: **promote an expectation to canonical.** That gate is deliberate — a wrong canonical
expectation makes the evaluator optimize toward a wrong target, so a human must sign off. Read
`references/bless-gate.md` — the dual job (capture + promote) lives there.

## What this block does NOT do (atomic boundary)
It does **not** fix or ship, and it does **not** compile expectations into test files or specs — a
blessed expectation lives in the brain, where the loops look it up (`kb search expectations`). It
captures human-grade signal and blesses.

## Inputs / Outputs
**Inputs:** a human tester (driving + correcting), and/or the queue of `proposed` expectations.
**Output:** `human-explore.json` `{ blessed: [...], rejected: [...], canonical_added: [...] }`
— written beside the run's other seam files under the evidence root (`evidence.root` in
`stack.config.json`, default `runs/`) — but the canonical expectations are the real product.

## Procedure
### A. Drive + correct (capture ground truth)
**Invoking `kb`.** Every `kb <verb>` below is the `qa-kb` CLI wrapper
(`.claude/skills/qa-kb/scripts/kb`), which must be **on your PATH** (one symlink per machine —
`docs/brain-setup.md`). It is **not** there in a fresh checkout: an unresolved `kb` exits **127**
(`kb: command not found`) and the brain write is simply *lost*, so fall back to the script instead of
skipping the call — **absolutely**, never cwd-relative, because a loop's cwd is normally a sandbox
checkout provisioned by `infra-worktree`, not this repo:

```bash
# <agents-repo> = the checkout of this framework THIS file was loaded from; set both in ONE shell.
KB="${KB_BIN:-<agents-repo>/.claude/skills/qa-kb/scripts/kb.py}"
python3 "$KB" <verb> ...        # ≡ the bare `kb <verb> ...` calls below
```

Either route reads the tracked brain pointer (`brain.config_file` in `stack.config.json`, default
`.claude/loops/kb.config.json`) and lands on the **shared brain** — one Postgres + pgvector database
every machine uses. The DSN is explicit (`database_url` or `KB_DATABASE_URL`), never defaulted: a
missing DSN is an error, and a loopback host is refused unless `KB_ALLOW_UNSHARED=1` is set for
genuinely offline work. A bless is never silently written somewhere only one machine can see.

**Driving the browser.** The live walk runs on the agent-harness browser driver named by
`browser.live_driver` in `stack.config.json` (default `claude-in-chrome`) — that is why the
`allowed-tools` list carries those tools; swap the driver and the list follows. The app address is
`app.url`, or `app.environments.<name>.url` when the tester wants a specific environment. Nothing
here is hard-coded: no host, no port, no feature names.

A tester walks the feature with you — optionally **in a persona's shoes** (`qa-personas`: pick a
persona by `id` or `role` from the configured pack, `personas_file` in `stack.config.json`, e.g. the
pack's `member` / `core` persona on a small screen; roles come from the pack, never from this skill).
When they correct behavior ("no — the Task filter should persist after a reload"), capture that as an
expectation and **bless it with their signature**:
```bash
kb propose expectation --area "<area>" --statement "<the corrected behavior>" --source human-explore
kb bless expectation --id <new id> --by <human-id>
```

### B. Review the proposed queue (the bless gate)
```bash
kb search expectations --status proposed --area "<area>"
```
For each, decide with the human: `kb bless expectation --id <id> --by <human-id> [--expect-version <v>]`
or `kb reject expectation --id <id> --by <human-id> --reason "<why>"`. **Never bless without a real
`--by`.** Use `--expect-version` so a concurrent change can't be clobbered (R4).

## Success criteria & failure categories (declared up front)
**Success:** human corrections captured as canonical expectations; the proposed queue triaged
(blessed/rejected) with a human signature.
**Failure categories:**
- `autonomous-bless` — blessed without a human `--by` (forbidden — defeats the whole gate).
- `stale-bless` — blessed against an old version without `--expect-version` (R4).

## Knowledge brain (qa-kb)
**Writes canonical** expectations (`bless`, human-signed) — the only producer allowed to. Reads the
`proposed` queue.

## Key principles
1. **Only a human blesses.** Canonical = human-signed, always (`--by`).
2. **Corrections are gold.** A tester's live correction is the highest-quality signal the brain gets.
3. **Atomic.** Capture + bless. Never fix or ship.
