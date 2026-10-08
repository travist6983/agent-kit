# dev-fix — plan-synthesis mode (competing strategies before coding)

## When to use it (and when NOT to)
Use plan-synthesis when the **solution space is wide**: a hard defect (multi-file / shared-core), a
feature (`dev-loop-feature-complete`), or any fix where the *approach* isn't obvious. **Skip it** for a
narrow, obvious defect — generating three strategies for a one-line null-check is waste — even when
intake routed it "hard" only because the fix touches a shared file. The whole point
is to avoid the trap of committing to the first approach and then patching it repeatedly when it turns
out wrong; for an obvious fix there's no trap.

## Why generate-and-pick beats first-attempt-iterated
When several genuinely different approaches exist, picking the first that comes to mind and iterating on
it is worse than laying out 2–3 and choosing deliberately — you can't iterate your way out of the wrong
*locus*. This is the judge-panel insight applied to a single fix: a little divergence up front beats a
lot of correction later.

## The method (keep it lean — a paragraph per strategy, then a pick)
1. **Generate 2–3 DISTINCT strategies** — distinct in *locus* or *philosophy*, not variations of one:
   - *Defect example:* (a) fix at the shared helper; (b) guard at the boundary/caller; (c) correct the
     data/state at the source.
   - *Feature example:* (a) reuse an existing shared-module component as-is (the modules under
     `shared_core_globs` in the conventions file); (b) a new minimal component; (c) extend the shared
     primitive for everyone.
2. **For each, state the trade-offs** briefly: approach (1–2 sentences), **blast radius** (files /
   shared-component consumers touched), **reversibility** (flag-flip? backward-compat default?), **risk**
   (what could break), **test cost**, and **fit** with the target repo's conventions (its guidelines
   file, `stack.repo_guidelines_file`, plus the conventions ruleset).
3. **Score and pick ONE.** Favor: actually fixes the root cause · smallest blast radius that *fully*
   fixes · most reversible · no scope creep · most consistent with the target repo's existing patterns.
4. **Record the pick AND the runner-up (with why-not)** in `fix.json.plan_synthesis` (Output, below).
   This is the part that pays off: if the pick fails `qa-verify`/`qa-regression`, the recipe's re-fix
   tries the **runner-up strategy** — a genuinely different approach (classify-before-adapt, principle
   3), not a blind re-patch of the same one. The runner-up is re-fix memory, **not** a PR comment: a
   strategy you rejected for a clear reason gives the reviewer nothing to do.

## Where the gate goes
A human-driven feature workflow would put a **human** plan-review gate here. In the autonomous loop
the **evaluator/recipe picks**, and the human reviews the result: the PR description says in a
sentence which approach was taken and why, `fix.json.plan_synthesis` holds the full comparison for
anyone who takes the ticket over, and for sensitive areas `dev-loop-feature-prerelease` stages the
whole thing for human QA.

The one case that reaches the reviewer as a thread is a genuine **product fork**: the strategies give
users materially different behavior (hide the control vs disable it with an explanation) and the
ticket doesn't decide it. Then — and only then — also record it as a `design_decisions` entry with
`must_read: true, must_read_reason: "product-call"` and the question in `reviewer_must_confirm`. Two
engineering routes to the same user-visible behavior are never a product fork.

## Output
Add to `fix.json`:
```json
"plan_synthesis": {
  "chosen": {"strategy": "...", "why": "..."},
  "alternatives": [{"strategy": "...", "why_not": "..."}],
  "considered": 3
}
```
Then proceed to Step 5 with the chosen strategy. Don't synthesize for narrow fixes — `plan_synthesis`
is simply absent then.
