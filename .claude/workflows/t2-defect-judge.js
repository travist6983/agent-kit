export const meta = {
  name: 't2-defect-judge',
  description: 'T2 parallel judging layer: blind verify + regression + review fan-out, checked before synthesis',
  whenToUse:
    'After dev-fix has written fix.json on a T2 (complex or sensitive) defect or feature run. ' +
    'Replaces the serial qa-verify -> qa-regression -> dev-code-review chain with the diamond: ' +
    'three independent judges concurrently, a deterministic checker at the convergence point, ' +
    'then dev-pre-pr. Ends BEFORE infra-ship - shipping is a human gate between workflows.',
  phases: [
    { title: 'Judge', detail: 'blind verify + regression + review dimensions, all concurrent' },
    { title: 'Check', detail: 'deterministic convergence checker - nothing flagged merges silently' },
    { title: 'Synthesize', detail: 'dev-pre-pr readiness verdict from checked evidence only' },
  ],
}

// ---------------------------------------------------------------------------------------
// args: { key, evidRoot, worktree, frameworkRepo, tier, producerModel, checkerModel, base,
//         appUrl, e2eEnvFile, now }
// `now` is passed IN because Date.now()/new Date() are unavailable in workflow scripts.
// A workflow script cannot read files, so the values that live in .claude/loops/stack.config.json
// are passed in by the caller: `producerModel` / `checkerModel` from `models.*`, `base` from
// `project.default_branch` (omit it and verify_gate.py reads the config itself), `appUrl` from
// `app.url`, `e2eEnvFile` from the auth_provider's env file.
// ---------------------------------------------------------------------------------------
const a = args || {}
const KEY = a.key
const EVID = a.evidRoot || `./runs/${KEY}`
const REPO = a.frameworkRepo || a.agentsRepo || '.'
const TIER = a.tier || 'T2'
const PRODUCER = a.producerModel || 'opus'
const CHECKER = a.checkerModel || 'sonnet'
const BASE = a.base || ''
const NOW = a.now || 'unknown'

if (!KEY) throw new Error('t2-defect-judge: args.key (the ticket key) is required')

// Every judge returns a small receipt. The AUTHORITY is the seam file it wrote to disk -
// the schema exists so the script can route without re-reading files it cannot access.
const RECEIPT = {
  type: 'object',
  properties: {
    wrote: { type: 'string', description: 'absolute path of the seam file this agent wrote' },
    verdict: { type: 'string', description: 'the headline verdict recorded in that file' },
    confidence: { type: 'integer', description: '0-100, or 0 when not applicable' },
    blind: { type: 'boolean', description: 'true only if this agent never saw the producer self-assessment' },
    note: { type: 'string', description: 'one line a human would want to read first' },
  },
  required: ['wrote', 'verdict', 'note'],
}

const CHECK = {
  type: 'object',
  properties: {
    status: { type: 'string', description: 'clean | flagged | uncheckable' },
    next_action: { type: 'string', description: 'continue | re-fix | escalate' },
    flags: { type: 'array', items: { type: 'string' }, description: 'one line per flag: kind - artifact - detail' },
    raw: { type: 'string', description: 'the checker script stdout, verbatim' },
  },
  required: ['status', 'next_action', 'flags'],
}

const READY = {
  type: 'object',
  properties: {
    status: { type: 'string', description: 'ready | warnings | blocked' },
    next_action: { type: 'string', description: 'ship-ok | re-fix | escalate' },
    fix_verified: { type: 'string', description: 'the checks.fix_verified value from verify_gate.py' },
    blockers: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
  required: ['status', 'next_action', 'fix_verified', 'summary'],
}

// What a blind judge may never read. fix.json's design_decisions / notes / confidence /
// pending_fix_lesson are the PRODUCER'S SELF-ASSESSMENT: a judge that reads "I'm confident this
// is the root cause" is anchored before it looks. The diff and files_changed are facts about
// the code and are fair game.
const BLIND_RULE = `
YOU ARE A BLIND JUDGE. You did not write this fix and you must not assume it works.
You may read: ${EVID}/context.json, ${EVID}/repro.json, the diff, and the code in ${a.worktree || '<worktree>'}.
You may NOT read or ask for: fix.json's "design_decisions", "notes", "confidence", or
"pending_fix_lesson" fields. Those are the producer's opinion of its own work.
Reach your own verdict. Agreeing with the producer is not your job; being independent is.
`.trim()

const EMIT_RULE = `
Write your seam file with the deterministic emitter, never a hand-written heredoc:
  python3 ${REPO}/contracts/emit.py --contract <contract> --out <path> --set k=v ...
It validates BEFORE writing, so a contract violation never lands on disk. Pass
--started-at "${NOW}" so the artifact carries its own timing.
Record model + judged_blind honestly. WRITE THE FILE BEFORE YOU RETURN - your return value is
only a receipt; the file on disk is the real hand-off and the only thing that survives a restart.
`.trim()

// =======================================================================================
phase('Judge')
log(`${KEY} (${TIER}): spawning 3 independent judges - none reads another's output`)

// These three depend ONLY on dev-fix. Verified against the contracts: qa-regression's inputs
// are the diff + lint-baseline + the oracle (it drives no browser, never reads verify.json);
// dev-code-review's inputs are the diff + fix.json. The edges between them were ordering, not
// dependency. A barrier IS correct here: the checker needs all three together.
const judges = await parallel([
  () => agent(
    `${BLIND_RULE}

Invoke the qa-verify skill in its DEFAULT fix-verify mode for ${KEY}.
Replay the oracle at ${EVID}/replay-oracle.sh against the FIXED build
(APP_URL=${a.appUrl || '<app.url from stack.config>'}, E2E_ENV_FILE=${a.e2eEnvFile || '<the auth_provider env file>'}).
Exit 0 GREEN, 1 RED, 2 BLOCKED - exit 2 is NO EVIDENCE about the fix, not a failed fix,
so report verdict "blocked" rather than "fail".
Read the real DOM/console/network state yourself; never trust a success toast.
Judge R3 masking: did the symptom get suppressed rather than the root cause fixed?
A pass carrying masking_suspected=true is a FAIL.

${EMIT_RULE}
Contract: ${REPO}/contracts/qa-verify.json -> ${EVID}/verify.json`,
    { label: `verify:blind:${KEY}`, phase: 'Judge', model: PRODUCER, schema: RECEIPT }),

  () => agent(
    `Invoke the qa-regression skill for ${KEY} in ${a.worktree || '<worktree>'}.
Build + lint against the pre-fix baseline (the commands in stack.config / conventions.json),
then run the unit tests covering the fixed AREA plus the full unit suite of every changed
package or module. Classify every red:
environment -> flaky (R7 quarantine at flake_rate >= 0.30 over >= 5 runs) -> same-area
regression -> unrelated-break. A broken build or a run that executed ZERO tests is BLOCKING,
never ship-ok.
You are a separate agent from the one that wrote the fix, so RUN the tests - do not transcribe
someone else's results.

${EMIT_RULE}
Contract: ${REPO}/contracts/qa-regression.json -> ${EVID}/regression.json`,
    { label: `regression:${KEY}`, phase: 'Judge', schema: RECEIPT }),

  () => agent(
    `${BLIND_RULE}

Invoke the dev-code-review skill on the ${KEY} diff. Run the 6-specialist split plus the R3
masking judge, fanning out concurrently. Judge against the target repo's guidelines file and
conventions ruleset (stack.repo_guidelines_file / conventions.json). Validate findings at
confidence >= 80.
Judge the DIFF, not the build and not the runtime behaviour - other judges own those.
An approve that carries unresolved CRITICAL/HIGH findings is a contradiction, not an approval:
either they justify a re-fix cycle or they belong in "Noticed, not changed". Say which.

${EMIT_RULE}
Contract: ${REPO}/contracts/dev-code-review.json -> ${EVID}/review.json`,
    { label: `review:blind:${KEY}`, phase: 'Judge', model: PRODUCER, schema: RECEIPT }),
])

// A null is a DEAD AGENT, not an absent opinion. Filtering it away is exactly the failure the
// checker exists to prevent - the synthesis would proceed on two inputs believing it had three.
const NAMES = ['qa-verify', 'qa-regression', 'dev-code-review']
const dead = NAMES.filter((_, i) => !judges[i])
if (dead.length) {
  log(`FAILURE: ${dead.join(', ')} returned null (skipped or died). Not proceeding on partial evidence.`)
  return {
    key: KEY, status: 'blocked', next_action: 'escalate',
    reason: `judge agent(s) produced no result: ${dead.join(', ')}. A missing judge is a ` +
            `reported failure, never a silently reduced quorum - dev-pre-pr would otherwise ` +
            `see two green artifacts and no third opinion.`,
    judges: NAMES.map((n, i) => ({ judge: n, receipt: judges[i] || null })),
  }
}
judges.forEach((j, i) => log(`  ${NAMES[i]}: ${j.verdict} - ${j.note}`))

// =======================================================================================
phase('Check')

// The checker is a DETERMINISTIC script, not a judgment call. The agent only runs it and
// reports what it said - which is why this stage gets the cheapest model.
const check = await agent(
  `Run the convergence checker for ${KEY} and report its output verbatim. Run exactly:

  python3 ${REPO}/.claude/skills/dev-loop-defect/scripts/check_convergence.py \\
    --evid "${EVID}" --key "${KEY}" --tier "${TIER}" --producer-model "${PRODUCER}"

Exit 0 = clean, 1 = FLAGGED, 2 = uncheckable (fail closed).
Do NOT interpret, soften, or re-judge its findings, and do not fix anything. Copy its JSON into
"raw" and flatten each flag to "kind - artifact - detail". This script is the only thing standing
between a parallel judging layer and a confident synthesis built on bad material.`,
  { label: `checker:${KEY}`, phase: 'Check', model: CHECKER, effort: 'low', schema: CHECK })

if (!check) {
  return { key: KEY, status: 'blocked', next_action: 'escalate',
           reason: 'the checker agent itself returned null - convergence could not be verified, so nothing may merge' }
}
if (check.status !== 'clean') {
  check.flags.forEach(f => log(`  FLAG: ${f}`))
  return {
    key: KEY, status: 'blocked', next_action: check.next_action || 'escalate',
    reason: `convergence checker: ${check.status}. A flagged input never merges silently.`,
    flags: check.flags, checker_raw: check.raw,
    judges: NAMES.map((n, i) => ({ judge: n, receipt: judges[i] })),
  }
}
log('checker: clean - all three artifacts present, sane and mutually consistent')

// =======================================================================================
phase('Synthesize')

const ready = await agent(
  `Invoke the dev-pre-pr skill for ${KEY} (evidence dir ${EVID}, repo ${a.worktree || '<worktree>'}).

Run the fix-verification gate FIRST and adopt its answer verbatim:
  python3 ${REPO}/.claude/skills/dev-pre-pr/scripts/verify_gate.py \\
    --verify "${EVID}/verify.json" --changed-from-git --repo "${a.worktree || '.'}"${BASE ? ` --base ${BASE}` : ''}
(--base defaults to project.default_branch from stack.config.json when omitted.)
Exit 1 means BLOCKED - use its next_action. Missing, stale, malformed, not-pass, or pass with
masking_suspected all block, at every tier, with no override.

Then the conventions scan (the target repo's ruleset in .claude/loops/conventions.json),
reconcile regression.json and review.json BY FRESHNESS, and emit one verdict. Reconcile - do
not re-run what a fresh artifact already answered, and do not trust one older than the diff.

${EMIT_RULE}
Contract: ${REPO}/contracts/dev-pre-pr.json -> ${EVID}/pre-pr.json`,
  { label: `pre-pr:${KEY}`, phase: 'Synthesize', schema: READY })

if (!ready) {
  return { key: KEY, status: 'blocked', next_action: 'escalate',
           reason: 'dev-pre-pr returned null - no readiness verdict exists, so there is nothing to ship' }
}

log(`${KEY}: ${ready.status} (fix_verified=${ready.fix_verified}) -> ${ready.next_action}`)

// STOPS HERE. Shipping is a human gate, and a human gate sits BETWEEN workflows, never inside
// one - a workflow cannot prompt mid-run.
return {
  key: KEY,
  tier: TIER,
  status: ready.status,
  next_action: ready.next_action,
  fix_verified: ready.fix_verified,
  blockers: ready.blockers || [],
  summary: ready.summary,
  judges: NAMES.map((n, i) => ({ judge: n, verdict: judges[i].verdict, blind: judges[i].blind === true })),
  checker: 'clean',
  human_gate: 'Review the readiness verdict, then run infra-ship separately. This workflow ' +
              'deliberately does not ship: R9 kill-switch and the R6 denylist are checked in ' +
              "ship's own preflight, and a workflow cannot ask you anything mid-run.",
}
