#!/usr/bin/env bash
# Regression: the loop skills' descriptions stay on the diet (X-TRIG-M9) and the known trigger
# traps stay fixed (X-TRIG-M1/M2/M3). Parses each SKILL.md frontmatter description.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
SK="$SK" python3 - <<'PY'
import os, re, sys

SKILLS = ["dev-code-review","dev-escalate","dev-fix","dev-intake","dev-loop-defect",
          "dev-loop-feature-complete","dev-loop-feature-prerelease","dev-loop-hard-defect",
          "human-explore","infra-prod-verify","infra-ship","infra-worktree","qa-explore",
          "qa-dream","qa-kb","qa-personas","qa-regression","qa-repro",
          "qa-verify"]
BUDGET = 1100  # chars ≈ 160 words; audit budget is ~100 words — hard ceiling with headroom
FORBID = ["carved out of", "copied from create-pr", "decomposed from", "persona packs from",
          "/loop my assigned defects", "whenever a change needs a high-signal review"]

fails = []
for name in SKILLS:
    p = os.path.join(os.environ["SK"], name, "SKILL.md")
    s = open(p).read()
    m = re.search(r"description: (?:>-|>|\|-?)\n((?:  .*\n)+?)(?:allowed-tools:|---)", s)
    if not m:
        fails.append(f"{name}: no description block"); continue
    d = " ".join(l.strip() for l in m.group(1).splitlines())
    if len(d) > BUDGET:
        fails.append(f"{name}: description {len(d)} chars > {BUDGET} budget (X-TRIG-M9)")
    if "Trigger" not in d:
        fails.append(f"{name}: no explicit Trigger phrases")
    for f in FORBID:
        if f in d:
            fails.append(f"{name}: forbidden phrase {f!r} (provenance noise / dead trigger)")

# X-TRIG-M1: the cold whole-loop phrasing belongs to intake, not the atom.
devfix = open(os.path.join(os.environ["SK"], "dev-fix", "SKILL.md")).read()
m = re.search(r"description: (?:>-|>|\|-?)\n((?:  .*\n)+?)allowed-tools:", devfix)
d = " ".join(l.strip() for l in m.group(1).splitlines())
# Key-agnostic: the example key follows project.ticket_key_pattern (PROJ-123 by default).
if re.search(r'"fix [A-Z][A-Z0-9]*-[0-9]+"', d) and "belongs to" not in d:
    fails.append('dev-fix: claims a cold "fix PROJ-123" without deferring it (X-TRIG-M1)')

for f in fails: print(f"  [FAIL] {f}")
print(f"  descriptions: {len(SKILLS)*1 - len(fails)} ok, {len(fails)} failed")
sys.exit(1 if fails else 0)
PY
