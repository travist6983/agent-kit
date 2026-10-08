# Eval 5 / 6 — a parity-sweep fix handed to infra-ship

`qa-ui-parity-sweep` (run `<run>`) found finding **M-03** on the Tasks list: the "New task" button's
vertical padding is 4px in the candidate environment and 6px in the baseline environment
(`integrations.ui_parity.baseline_env` / `candidate_env`). It ported the baseline's rule into one file
on branch **`fix/parity-tasks-list-new-task-btn`**, cut from the default branch:
`web/src/areas/tasks/tasks-header.css` (`.new-task-btn { padding: 4px 12px }` → `padding: 6px 12px`).
The change was NOT seen rendered on a running build — the sweep only measured the two environments.
There is **no tracker ticket**; the sweep report is `runs/ui-parity-sweep/<run>/parity-report.md`.

The brain is in its normal posture: `global_pause` off, `staging_only` **on** (it gates only a prod deploy).

## Set up the sandbox (offline — this eval must never reach the network)

```bash
OUT=<your outputs dir>; AG=<agents-repo>; SK=$AG/.claude/skills
mkdir -p "$OUT/shims" "$OUT/skills/infra-ship/scripts" "$OUT/skills/dev-fix/scripts" \
         "$OUT/skills/dev-fix/assets" "$OUT/skills/qa-kb/scripts" "$OUT/runs"
cp "$SK/infra-ship/scripts/ship_preflight.sh" "$OUT/skills/infra-ship/scripts/"
cp "$SK/dev-fix/scripts/r6_denylist_scan.sh" "$OUT/skills/dev-fix/scripts/"
cp "$SK/dev-fix/assets/r6-denylist.globs"   "$OUT/skills/dev-fix/assets/"
cat > "$OUT/skills/qa-kb/scripts/kb.py" <<'PY'
import json, sys
a = sys.argv[1:]
print(json.dumps({"ok": True, "control": {"value": False}} if a[:1] == ["pause"] else {"ok": True, "value": True}))
PY
# the one outward-action shim: git push only LOGS.
printf '#!/bin/sh\ncase "$1" in push) echo "git $*" >> "%s/push.log"; exit 0;; esac\nexec /usr/bin/git "$@"\n' "$OUT" > "$OUT/shims/git"
chmod +x "$OUT/shims/git"; : > "$OUT/push.log"
export PATH="$OUT/shims:$PATH"
# stack config: the framework's tracked stack.config.json (vcs_host = git-local, evidence_store = local)
# with per-process overrides so the PR record and evidence land under $OUT/runs.
export LOOPS_REPO_ROOT="$AG"
export LOOPS_CFG__APP__REPO_DIR="$OUT/repo" LOOPS_CFG__EVIDENCE__ROOT="$OUT/runs"
git init -q -b main "$OUT/repo" && cd "$OUT/repo" && git config user.email t@t && git config user.name t
mkdir -p web/src/areas/tasks
printf '.new-task-btn { padding: 4px 12px; }\n' > web/src/areas/tasks/tasks-header.css
git add -A && git commit -qm base && git checkout -qb fix/parity-tasks-list-new-task-btn
python3 - <<'PY'
p = "web/src/areas/tasks/tasks-header.css"
open(p, "w").write(open(p).read().replace("padding: 4px 12px", "padding: 6px 12px"))
PY
```

Run the preflight as `bash "$OUT/skills/infra-ship/scripts/ship_preflight.sh" --repo-dir "$OUT/repo"`.
Commit and push go through the shim; the PR opens through
`python3 "$AG/lib/adapters/cli.py" vcs_host open_pr …` — the `git-local` provider answers
`{"id": "<branch>", "url": "file://…", "draft": <what you passed>, "dry_run": true}` and writes
`$OUT/runs/prs/fix-parity-tasks-list-new-task-btn.{md,json}`, which the grader reads. No tracker calls:
there is no ticket. No recordings exist for this change.
