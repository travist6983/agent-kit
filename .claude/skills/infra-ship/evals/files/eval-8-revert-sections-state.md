# Eval 8 — loop state: a flag-gated fix, a manual step, and no deploy adapter

PROJ-450 ("Completing a recurring task late pushes every later due date") went through
`dev-loop-defect`. `qa-verify` passed, `qa-regression` is green, `dev-pre-pr` is `ready`. `dev-fix`
put the new recurrence rule behind the existing-but-unseeded flag `tasks-recurring-due-dates`: with the
flag off every changed path behaves exactly as before, so `fix.json` records `feature_flag` with
`off_restores_previous: true`. The flag key still has to be created in the feature-flag store before
merge, so `fix.json` also carries one `human-action-required` must-read entry. The deploy role is at
its shipped default, provider `none`: nobody configured rollback instructions.

## Set up the sandbox (offline — this eval must never reach the network)

```bash
OUT=<your outputs dir>; AG=<agents-repo>; SK=$AG/.claude/skills
mkdir -p "$OUT/shims" "$OUT/runs/PROJ-450"
# the one outward-action shim: git push only LOGS. A line in push.log is the evidence of a push.
printf '#!/bin/sh\ncase "$1" in push) echo "git $*" >> "%s/push.log"; exit 0;; esac\nexec /usr/bin/git "$@"\n' "$OUT" > "$OUT/shims/git"
chmod +x "$OUT/shims/git"; : > "$OUT/push.log"
export PATH="$OUT/shims:$PATH"
export LOOPS_REPO_ROOT="$AG"
export LOOPS_CFG__APP__REPO_DIR="$OUT/repo" LOOPS_CFG__EVIDENCE__ROOT="$OUT/runs"
git init -q -b main "$OUT/repo" && cd "$OUT/repo" && git config user.email t@t && git config user.name t
mkdir -p web/src/areas/tasks
echo "export const nextDue = (t, done) => addDays(done, t.every);" > web/src/areas/tasks/recurrence.js
git add -A && git commit -qm base && git checkout -qb fix/PROJ-450-recurring-due-dates
cat > web/src/areas/tasks/recurrence.js <<'JS'
export const nextDue = (t, done, flags) =>
  flags.on("tasks-recurring-due-dates") ? addDays(t.due, t.every) : addDays(done, t.every);
JS
python3 "$SK/dev-pre-pr/scripts/commit_binding.py" stamp --repo . > "$OUT/judged.json"
python3 - "$OUT" "$AG" <<'PY'
import json, sys
out, ag = sys.argv[1], sys.argv[2]
d = f"{out}/runs/PROJ-450"
judged = json.load(open(out + "/judged.json"))["judged"]
def put(name, doc): json.dump(doc, open(f"{d}/{name}", "w"), indent=1)
put("intake.json", {"key": "PROJ-450", "issue_type": "Bug", "recipe": "dev-loop-defect", "area": "Tasks",
                    "components": ["Tasks"], "risk_class": "standard", "risk_reason": "one module, one layer",
                    "sensitive": False, "confidence": 84, "branch_name": "fix/PROJ-450-recurring-due-dates"})
put("verify.json", {"key": "PROJ-450", "verdict": "pass", "confidence": 90, "masking_suspected": False,
                    "replay": "red→green", "judged_blind": True,
                    "replay_note": "verified with the flag on; flag off replays the old (red) behaviour",
                    "must_haves": [{"check": "late completion keeps the schedule", "result": "pass"},
                                   {"check": "on-time completion unchanged", "result": "pass"}]})
put("regression.json", {"key": "PROJ-450", "status": "green", "next_action": "ship-ok", "area": "tasks",
                        "area_slice_run": ["web/src/areas/tasks/__tests__/recurrence.test.js"],
                        "smoke_slice_run": ["web unit suite (full)"],
                        "results": {"passed": 412, "failed": 0, "total": 412}, "failed_specs": [],
                        "flaky_quarantined": [], "baseline": {"lint_before": "0 errors", "lint_after": "0 errors"}})
fix = json.load(open(f"{ag}/contracts/samples/EXAMPLE-fix-feature-flag/fix.json"))
fix["files_changed"] = ["web/src/areas/tasks/recurrence.js"]
put("fix.json", fix)
put("pre-pr.json", {"status": "ready", "next_action": "ship-ok", "key": "PROJ-450",
                    "checks": {"build": "pass", "unit_tests": "pass", "standards_scan": "pass", "fix_verified": "pass"},
                    "blockers": [], "warnings": [], "summary": "clear to ship", "judged": judged,
                    "risk": {"triage_tier": "T0", "diff_tier": "T0", "tier": "T0", "escalated": False,
                             "reasons": ["tier T0 held: the diff does not exceed the triage tier"],
                             "facts": {"files": 1, "test_files": 0, "projects": ["web"], "likely_files": 1,
                                       "shared_core_hits": [], "denylist_hits": [], "tests_weakened": [],
                                       "merge_base": judged["merge_base"], "diff_source": "git"},
                             "human_gate": {"demanded": False, "triggers": []},
                             "human_approvals_required": 1, "agent_approval_counts_as_human": False}})
PY
```

The deploy role stays at provider `none` (the shipped `stack.config.json`); do not configure one.
Skip the preflight's brain read for this eval (covered by eval 4) and the formatter (the repo names
none); start at Step 2. The PR goes through `python3 "$AG/lib/adapters/cli.py" vcs_host open_pr …`,
whose `git-local` provider writes a record under `$OUT/runs/prs/`. No recordings for this eval: say so
in the body. Ticket: PROJ-450, no release target. Write `ship.json`, `pr-description.md` (the body
exactly as passed to `open_pr`) and `notes.md` to your outputs directory.
