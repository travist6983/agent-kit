# dev-code-review — the six stack-agnostic specialists

The review fans out to six specialists, each a fresh `Agent` spawn over the same diff chunk (chunk
at 50 files), each returning strict JSON findings. None of them knows a framework. **What "this
repo's rules" are comes from two config-named files, read first and quoted by id in every
convention finding:**

- `stack.repo_guidelines_file` (default `CLAUDE.md`, relative to `app.repo_dir`) — the prose
  guidelines the target repo keeps for its own contributors;
- `stack.conventions_file` (default `.claude/loops/conventions.json`) — `rules[]` (each with an
  `id`), `shared_core_globs`, `blast_radius_denylist_globs`, `domain_glossary`.

A specialist that would need a framework fact not present in those files does not invent it. It
either finds the fact in the repo (an existing pattern, three sibling files doing it the same way)
or it does not flag.

## Shared rules for every specialist

- **High signal only.** Flag what is clearly real: won't compile, wrong result, an unambiguous
  violation of a cited rule, exploitable security, scale-failing performance. Not style, not taste,
  not "I would have done it differently". When unsure, return nothing.
- **Only the diff.** A problem the diff did not introduce is *pre-existing*; it is not a finding
  (the validator sends serious ones to `## Noticed, not changed`). A hazard the diff newly exposes
  — a new caller of an unsafe function, a new path around an existing guard — **is** a finding.
- **Confidence ≥ 80**, integer, honest. 80 means "I re-read the surrounding code and I would bet on
  it"; a hunch is 40 and is not emitted.
- **Fingerprint** = `sha1(dimension + file + normalized issue text)`. The validator dedups across
  specialists and across chunks on it; two specialists reporting the same line keep the
  higher-severity one.
- **Empty is a valid answer.** A specialist whose surface the diff doesn't touch returns `[]`.
- **Finding shape** (the `findings[]` item of `contracts/dev-code-review.json`):
  `{ dimension, severity: critical|high|medium|low, confidence, file, line, issue, rule_id?,
  suggestion?, fingerprint }`. `rule_id` is required when the finding is a convention violation.

## The six

| dimension | Surface | Flags (examples, not an exhaustive list) |
|---|---|---|
| **frontend** | UI code: components, templates, styles, client state, routing, accessibility | render that cannot work (unbound reference, wrong prop type that will throw); state mutated outside the pattern the repo uses everywhere else; an interactive element with no accessible name / keyboard path when the repo's guidelines require one; a hand-rolled control where the guidelines name a shared one |
| **backend** | Server code: handlers, services, validation, error handling, background jobs | a request path that returns success on an unhandled branch; validation the guidelines require and the handler skips; a new endpoint with no authorization check where siblings have one; a contract change (response shape) with no caller update |
| **data** | Persistence: queries, schema, migrations, caching, serialization | a query that can return the wrong rows (missing tenant / workspace scope, wrong join); a migration that loses data or is not reversible; N+1 introduced in a loop; a cache key that omits a dimension the value depends on. A diff touching a `blast_radius_denylist_globs` path is CRITICAL on sight (R6) |
| **security** | Trust boundaries, auth, secrets, injection, data exposure | user input reaching a query / shell / template unescaped; an authorization check removed or weakened; a secret-looking literal; a new field in a response that leaks another Member's or Workspace's data; a log line that writes credentials or tokens |
| **tests** | Test coverage and test honesty | the fixed path has no test that would have caught the original defect; a test skipped, focused (`.only`-style) or deleted; an assertion loosened so the test cannot fail; a test that asserts the mock, not the behavior |
| **perf** | Scale and latency | work moved into a per-item loop that was batched; an unbounded fetch where a page existed; blocking I/O on a hot path; a new synchronous call to an external system in a request handler. Flag only what fails at scale, not micro-optimizations |

The **tests** specialist has one loop-specific extra: when `fix.json` names a failure class, it
checks that a test now exists for *that* class, not just "some test changed".

## The validator (second pass, same context as the verdict)

For every specialist finding:
1. Re-read the code at `file:line` in the sandbox. Confirm the line is in the diff's added or
   changed hunks (`git diff <base> -U0`). Not in the diff → pre-existing → reject (serious ones go to
   `## Noticed, not changed`).
2. Confirm the claim is true as written. A convention finding must cite a `rule_id` that exists in
   `conventions.rules[]` or a quotable line of the guidelines file.
3. Drop anything under 80 after re-reading. Drop known false positives recorded in the brain
   (`qa-kb search` on the fingerprint), and record every new false positive you reject *as* a false
   positive, with provenance, so the next run skips it.
4. Dedup by fingerprint.

What survives is `findings[]`; the counts fill `critical` / `high`; the verdict rule in `SKILL.md`
§4 derives from them plus the masking judge.
