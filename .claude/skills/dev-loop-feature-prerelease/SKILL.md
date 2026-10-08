---
name: dev-loop-feature-prerelease
description: >-
  Recipe ③: the no-auto-ship loop for SENSITIVE or pre-release DEFECTS — the areas the target repo
  declares sensitive (on the neutral example domain: Invoices, Members, Settings/auth), feature-flagged
  areas, anything flagged for human QA. NOTE: a defect pipeline (repro → fix), NOT a feature builder —
  feature-type tickets (`Ticket.type`) go to dev-loop-feature-complete. Trigger when dev-intake routes
  here, or "this touches a sensitive area — fix it but don't ship", "stage this for QA, no auto-ship".
  Runs intake → provision → repro → fix → verify → regression, then STAGES the fully-evidenced change
  for the human-QA queue and STOPS — it never deploys, regardless of confidence (that's the whole
  point). THIN orchestrator on the shared engine.
allowed-tools: Read, Write, Bash, Agent, Skill, ToolSearch
---

# dev-loop-feature-prerelease — fix it, prove it, stage it for humans (Recipe ③)

For tickets where the cost of a wrong autonomous ship is high: the areas the target repo declares
sensitive, pre-release flags, anything `dev-intake` marked `sensitive`. Same atomic skills, same
engine (`../dev-loop-defect/references/recipe-engine.md`, `../dev-loop-defect/scripts/recipe_router.py`)
— but it **stops before shipping** and hands a human a staged, fully-evidenced change to QA and release.

**Where the sensitive-area list lives.** It is app-domain knowledge and never lives in this skill. On
the neutral example domain it might be `["invoices", "members", "settings/auth"]`; a ticket such as
`PROJ-123` ("Invoice total wrong after a Member is removed from a Workspace") lands here because
`dev-intake` matched its area to that list. Today the list is `dev-intake`'s: `scripts/assign_tier.py`
ships a small default and takes the adopter's own class name through `--risk`, and `dev-intake`'s
routing rule 1 sends anything that matches here. A stack.config key for the list
(`loops.sensitive_areas`) is **requested, not built** — do not look for it in
`.claude/loops/stack.config.json` yet; until it lands, the adopter's list is expressed through
`dev-intake`'s risk class.

## The sequence (note the ending)
```
dev-intake → infra-worktree PROVISION → qa-repro → dev-fix ─┐
   ┌────────────────────────────────────────────────────────┘
   ├── Agent: qa-verify       (BLIND)
   ├── Agent: qa-regression
   └── Agent: dev-code-review (BLIND)
                ↓  CHECKER (check_convergence.py)
        dev-pre-pr → STAGE (human-QA queue) → STOP
              FINALLY: infra-worktree TEARDOWN
```
Same parallel layer, blind judges and checker as Recipe ① — see *The parallel layer* and
*Blind judging* in `../dev-loop-defect/references/recipe-engine.md`.

**Sensitive areas are always pinned to the highest verification tier.** Tiers (T0 trivial · T1
standard · T2 complex) and the rules that assign them are defined in `docs/graph-engineering.md` §6;
one of those rules is *risk class is sensitive → T2*, and T0 excludes sensitive areas by rule
regardless of how small the diff is. So this recipe never runs a T0-shortened verify. (A config knob
for the floor, `loops.tiering.sensitive_min_tier`, is requested, not built; the rule itself is what
applies today.) Blindness matters most here — this is the lane whose output a human is asked to trust.

Same environment rule as Recipe ①: `infra-worktree` provisions through the `sandbox` adapter and
supplies `app_url`/`worktree` (`started:false` — run its `next_steps`, which start the app with
`app.start_command`, then prove reachability against `app.health_path`); teardown runs
unconditionally in a finally. The staged change must outlive the sandbox: the sandbox branch belongs to
the repository, not the worktree, so teardown leaves it in place unless the adopter set the sandbox
adapter's `delete_branch_on_teardown` — in which case the STAGE push below is mandatory, never skipped.

**No `infra-ship`, no `infra-prod-verify`.** The output is a staged, review-ready change + the full
evidence bundle (before/after recordings, the `replay-oracle.sh` replay, verify verdict, regression
result), queued for a human to QA and release.

## What's different
- **Never auto-ship.** Even at high confidence, the loop stops at STAGE. (`dev-intake` routes sensitive
  areas here precisely so confidence can't override the human gate.)
- **Stage = a warm handoff, not an escalation.** Assemble the change + evidence (reuse `dev-escalate`'s
  renderer for the bundle) and queue it for human QA. The human side of this lane is `human-explore`:
  a tester walks the staged change with the loop, and their live corrections are what get *blessed*
  into the brain's canonical expectations — the propose→bless path, never autonomous. **The bundle
  carries `dev-fix`'s recorded design decisions** (`fix.json.design_decisions`, plus the plan-synthesis
  runner-up): because this path skips `infra-ship`, the staged handoff is the human's full view of the
  calls the loop made on its own. The renderer leads with the `must_read` items — the ones that need
  the human's decision — and lists the rest as one-line context, so the handoff gets the same "only
  interrupt for what needs action" discipline as the auto-ship recipes' pull-request descriptions. It
  also carries **Noticed, not changed** in full: on this path — the sensitive areas — a pre-existing
  gap spotted along the way has no other home.
- **STAGE is an outward step: one plain git push, then the adapters.**
  1. *Guard, then push.* Pushing is not a `vcs_host` op — `vcs_host` opens and reads pull requests;
     the push is plain `git`. Run `../infra-ship/scripts/ship_preflight.sh --repo-dir <worktree>
     --base <project.default_branch>` first: it checks the R9 kill-switch, that the branch is not the
     base or a `project.protected_branch_patterns` match, the prod-lane refusal (a push to a branch
     matching `deploy.prod_lane_branch_patterns` **is** a production deploy on a deploy-on-push setup —
     this recipe never makes one), and the R6 blast-radius denylist — all **without pushing**, and it
     reports the `remote`. Any abort is a stop (prod-lane → escalate). Then `git push -u <remote>
     <branch>` — only the sandbox branch the preflight cleared (it carries `project.branch_prefixes.fix`,
     e.g. `fix/PROJ-123`, which deploys nothing on push). A checkout with no remote at all stays local:
     the branch already lives in the repository's refs and that is the dry-run equivalent of a push.
  2. *Record on the ticket* through the `issue_tracker` adapter: `add_comment` with the rendered
     bundle, `attach` for the recordings.
  3. *Announce* through the `notifications` adapter: `post(channel, summary)`, where `channel` is the
     `notification_channel` in `dev-escalate`'s `assets/escalate.config.json` (default `escalations`;
     `post_escalation.py --channel` overrides it per call). With the default `file` provider that is
     `runs/notifications/<channel>.md`; a real chat or tracker destination is a provider the adopter
     plugs into the `notifications` role.
  No pull request is opened — that is `infra-ship`'s job and this recipe never calls it. With the
  built defaults (`issue_tracker: file`, `notifications: file`, `vcs_host: git-local`) the whole
  handoff lands under `runs/`, which is a complete dry run. A `none` provider for `issue_tracker` or
  `notifications` is a structured `blocked` with reason `adapter-unavailable:<role>` — the bundle is
  still written to `<evidence.root>/<KEY>/` so nothing is lost, but the loop reports that the handoff
  did not reach a human.
- The retry/escalate engine up to STAGE is the shared one (same-area→re-fix, unrelated-break→escalate, etc.). A
  `qa-verify` low-confidence result here is *expected* to need a human — that's the design.

## The pre-PR gate
- **`dev-pre-pr` before STAGE.** The readiness gate: it reconciles this loop's `regression.json` (and
  `review.json`, where the recipe ran one) **by freshness**, runs the deterministic conventions scan
  (`stack.conventions_file`, default `.claude/loops/conventions.json`) on the added lines, rolls up the
  blockers, and emits one verdict. Router: `ready`/`warnings` → advance; `blocked` → back to `dev-fix`
  with the blockers as context; `scan-unavailable` → escalate (fail closed). It never re-runs a build
  a fresh artifact already answered — and never trusts one older than the diff.

## Key principles
1. **Sensitive never auto-ships.** The human gate is the point; confidence does not override it.
2. **Stage warm.** The human gets a fully-evidenced, QA-ready change — not a cold "please fix."
3. **Same rigor to STAGE.** Repro/fix/verify/regression run with full discipline; only the deploy is withheld.
4. **Thin.** Shared engine + atomic skills; the only delta is "stage, don't ship."
