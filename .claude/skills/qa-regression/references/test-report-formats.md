# qa-regression — test report formats (`test_report.format`)

Counts come from the report the runner **wrote**, never from a terminal summary. Every project in
`conventions.projects[]` (and the repo-wide `stack.test_report` fallback) declares where that report
lands and what shape it has:

```jsonc
"test_report": { "format": "junit", "glob": "web/test-results/*.xml" }
```

`glob` is relative to the checkout the commands run in (`app.repo_dir`, or the sandbox dir the recipe
passed as `--cwd`); `**` recurses. `scripts/parse_test_report.py` reads every file the glob matches and
prints one JSON object with `results {passed, failed, skipped, total}`, a per-test list, `files_run`,
the optional area slice and the `kb_flakiness_args` the brain write consumes. `total = passed + failed`
— skipped tests are reported but never counted as executed, so a run that only skipped is still a
zero-test run (exit 1, blocking).

Two formats ship. A runner that emits neither usually has a reporter plugin for one of them; configure
that rather than teaching this skill a third shape.

## `junit` — JUnit XML (the default)

The shape almost every runner can emit with one flag (`--reporter junit`, `--junitxml`,
`--logger trx` converted, `-f JUnit`, `--format junit`, …):

```xml
<testsuites>
  <testsuite name="tasks/service.test.js" file="src/tasks/service.test.js" tests="3" failures="1">
    <testcase classname="tasks/service" name="creates a task in the current project" />
    <testcase classname="tasks/service" name="rejects a comment on an archived task">
      <failure message="expected 403, got 200">AssertionError: expected 403, got 200
        at src/tasks/service.test.js:41</failure>
    </testcase>
    <testcase classname="tasks/service" name="renders invoice totals"><skipped/></testcase>
  </testsuite>
</testsuites>
```

How it is read:

| element | becomes |
|---|---|
| each `<testcase>` | one test row |
| `<failure>` or `<error>` child | `status: failed`; `message` = the attribute, else the first text line |
| `<skipped>` child | `status: skipped` (reported, not counted) |
| no child | `status: passed` |
| `file` on the case → `file` on its `<testsuite>` → the suite `name` → `classname` → the report filename | `file` (the test's file/suite), used for `files_run`, `area_slice_run` and the `--area-regex` match |
| `spec` | `"<file> > <name>"` — the id the brain's `flakiness` store keys on |

Multiple files (one per project, or one per shard) are concatenated; order does not matter.

## `json` — a generic JSON report

There is no single JSON test-report standard, so the parser walks the document and accepts the common
shapes without being told which runner produced it. A node is **one test** when it carries a
recognised status key, a recognised name key, and no nested list of tests; a node that holds a list
under a recognised list key is a **container** whose own `file`/`path` is inherited by the tests inside.

| role | keys recognised (first present wins) |
|---|---|
| test name | `fullName`, `full_name`, `fullTitle`, `full_title`, `full_description`, `title`, `name`, `description`, `test`, `spec`, `id` |
| test file / suite | `file`, `path`, `filePath`, `file_path`, `testFilePath`, `location`, `suite`, `classname`, `class` |
| status | `status`, `state`, `outcome`, `result`, `verdict` |
| failure message | `message`, `failureMessage`, `failure_message`, `error`, `reason`, `failureMessages` (first item), `exception` (its `message`) |
| nested tests | `tests`, `testResults`, `test_results`, `results`, `cases`, `examples`, `assertionResults`, `items` |

Status values are normalised case-insensitively, ignoring `-`/`_`/spaces:

| normalised | accepted words |
|---|---|
| `passed` | passed, pass, ok, success, succeeded, passing |
| `failed` | failed, fail, error, errored, broken, failure, failing, timedout, timeout |
| `skipped` | skipped, skip, pending, todo, disabled, ignored, notrun, not_run, excluded |

Two documents the walker reads identically:

```json
{ "testResults": [
    { "testFilePath": "src/invoices/totals.test.js",
      "assertionResults": [
        { "fullName": "totals include tax", "status": "passed" },
        { "fullName": "totals round half up", "status": "failed",
          "failureMessages": ["expected 10.01 to equal 10.00"] } ] } ] }
```

```json
{ "examples": [
    { "id": "./spec/models/invoice_spec.rb[1:1]", "file_path": "./spec/models/invoice_spec.rb",
      "full_description": "Invoice totals include tax", "description": "include tax", "status": "passed" },
    { "id": "./spec/models/invoice_spec.rb[1:2]", "file_path": "./spec/models/invoice_spec.rb",
      "description": "round half up", "status": "failed", "exception": { "message": "expected 10.01" } } ] }
```

(The second shape's `spec` reads `"./spec/models/invoice_spec.rb > Invoice totals include tax"` — the
fullest description present wins, and `id` is the last resort; either way the key is stable across
runs, which is what the flakiness store needs.)

**Summary-only reports.** A JSON report that carries totals but no per-test rows (`summary: {passed,
failed, total}` or top-level `numPassedTests`/`numFailedTests`/`numTotalTests`) still yields
`results`, with `summary_only: true` and an empty `tests` list — the counts are the runner's, but there
are no per-test flakiness data points to write and `area_slice_run` cannot be derived from it. Prefer a
per-test reporter when the runner offers one.

## What the parser does NOT do

It runs no tests, calls no brain, and never guesses: no matching file → exit 2 ("do not trust any
count"); an unreadable file → exit 3; zero executed tests → exit 1 (blocking under QA-REGRESSION-C1).
A runner whose summary line can lie across differently-filtered invocations is exactly why the count
comes from the report file and never from the terminal: configure a per-test reporter (`test_report`)
and such a quirk never reaches the gate.
