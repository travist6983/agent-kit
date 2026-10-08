# dev-pre-pr — the conventions rules: what a grep decides, and what needs a reader

The readiness gate has two halves. `scripts/standards_check.sh` decides the mechanical half from the
`rules` array of the target repo's conventions file (`.claude/loops/conventions.json`, pointed at by
`stack.conventions_file` in stack.config); everything a regex cannot decide honestly needs a reader.
Keeping the halves separate is the whole point — a rule that a regex can decide should never cost a
model call, and a rule that needs judgment should never be faked with a regex that gets it wrong 30%
of the time.

The framework ships a small language-agnostic default ruleset. The target repo replaces or extends it
with its own house rules; `examples/conventions/` shows how a framework-style pack looks.

## Half 1 — mechanical (the script decides; you just read the JSON)

The scanner reads **only the lines this branch added** (`git diff -U0` against the merge-base with
`project.default_branch`, plus untracked files). It cannot blame you for pre-existing code, which is
the failure mode that makes developers stop reading standards reports.

The shipped default rules:

| id | Sev | What it catches |
|---|---|---|
| `no-debug-console` | MED* | `console.log/debug/trace(` in added non-test JS/TS |
| `no-debug-print` | MED* | `print(` in added non-test Python library code (CLI entry points excluded) |
| `no-focused-tests` | HIGH | `.only(` / `fit(` / `fdescribe(` in a test file — a focused test silently skips the suite |
| `no-secret-literals` | CRIT* | an assignment that looks like a credential, or a known key prefix / PEM header |
| `todo-without-ticket` | LOW | `TODO` / `FIXME` / `HACK` / `XXX` with no key matching `project.ticket_key_pattern` on the line |

`*` = **heuristic**. A heuristic rule is a *lead*, not a verdict, and the scanner marks it
`heuristic: true` so it can never block on its own. See "Confirming a heuristic finding" below.

**Escape hatch:** a line containing the `suppress_marker` (default `standards:allow`) is skipped. Use
it with a trailing reason (`// standards:allow — this CLI's contract is stdout`) so the next reader
knows why, and use it sparingly — a branch with five suppressions is telling you the fix is wrong, not
the rule.

**Output.** One JSON object: `{ok, base, rules_file, scanned, added_lines, counts, blocking,
heuristic, violations[], notes[]}`. Each violation carries `{file, line, severity, rule_id, rule, fix,
snippet, heuristic}` plus `confirm_question` when the rule declares one. Exit `0` = nothing blocking ·
`3` = at least one non-heuristic critical/high · `2` = could not scan (missing/invalid rules file, not
a git repo, diff failed) — **fail closed**, the caller treats `2` as `blocked`, never as clean.

## Half 2 — needs a reader (the model decides; the scanner deliberately doesn't try)

The target repo's guidelines file (`conventions.guidelines_file`, default `CLAUDE.md`) holds the rules
no regex decides honestly. Read the changed hunks and judge them against it. Report them exactly like
the scanner's findings — same `{file, line, severity, rule, fix}` shape, `source: reader` — so the
report has one voice. Typical reader rules, which an adopter phrases in their own nouns:

- **Layer boundaries** — a UI component reaching past its service/client module into another area's
  internals, or a request handler owning logic that belongs in a service. Structural, not textual.
- **Tenant / ownership scoping** — a query that can read across workspaces because it skipped the scope
  filter the rest of the codebase applies. Whether it is a bug depends on what the call site is for.
- **Soft-delete bypass** — a query that reaches deleted rows by skipping the default filter. Intent
  decides.
- **Raw SQL in a migration** — the R6 denylist already blocks migration files outright, so this only
  appears when a human gate already opened. Judge the SQL itself.
- **Missing test for new behavior** — new branching logic with no unit test. `dev-code-review`'s
  test-coverage specialist is the primary owner; flag here only when the gap is obvious and unclaimed.
- **Design-token substitution** — a literal where the design system supplies a token, or a
  "close enough" token swap. Only decidable by reading the surrounding stylesheet.

## Confirming a heuristic finding

A heuristic finding names a file and a line but has not earned the right to block. Open the file and
answer the rule's own question (`confirm_question` in the rule, repeated in the scanner's output)
before it counts:

- **`no-secret-literals`** — is this a real credential, or a placeholder / docs value that could never
  authenticate anything? Confirm it, and it becomes a **CRITICAL blocker** — a committed secret is the
  worst thing on this list, and it needs rotating even after the line is removed. Clear it, and say in
  the report that you checked, so the next run doesn't re-litigate it.
- **`no-debug-console` / `no-debug-print`** — is this module a CLI whose contract *is* stdout, or
  library code where the statement is a leftover?
- **Any adopter heuristic** — the rule's `confirm_question` is the question. A rule without one is a
  rule whose author did not say how to confirm it; ask for the question before you trust the rule.

Unconfirmed heuristic findings still appear in the report, under **Warnings**, labeled unconfirmed. They
are information, not an accusation.

## The rule format

Edit `.claude/loops/conventions.json` — never inline a regex in the skill body, because two sources of
truth means one of them is wrong within a month. The file's top level:

| key | meaning |
|---|---|
| `version` | the ruleset's own version |
| `guidelines_file` | the reader-rules document (Half 2); default `CLAUDE.md` |
| `suppress_marker` | the per-line escape hatch; default `standards:allow` |
| `test_path_regex` | what `skip_tests: true` means for this repo |
| `rules` | the array below |
| `projects`, `blast_radius_denylist_globs`, `shared_core_globs`, `app_handles`, `domain_glossary` | read by other skills (project layout and commands, the R6 denylist, shared-core paths, UI handles, the domain vocabulary) — see `examples/conventions/README.md` |

Fields of a rule:

`id` · `severity` (critical|high|medium|low) · `include_path` (regex on the repo-relative path) ·
`exclude_path` (optional) · `skip_tests` (bool, uses `test_path_regex`) · `pattern` (regex on the
added line) · `rule` (the label) · `fix` (what to do instead — this is what the developer actually
reads) · `heuristic` (bool) · `confirm_question` (required when `heuristic` is true — how a reader
confirms or clears it) · `layer` (optional free label, e.g. a `projects[].name`) · `type`:

- `line` (default) — flag every added line matching `pattern`.
- `file_absent` — flag a matching added line **only if** the whole file lacks `absent_pattern`. This is
  how a "you did X but never did Y" rule works without an AST (a subscription with no unsubscribe, a
  listener with no removal, a query with no scope filter anywhere in the file).
- `sibling_file` — the cross-file check: for a changed file matching `include_path`, require the file
  named by `sibling_template` to exist in the repo or in the change set. Template variables: `{stem}`
  (basename without extension, minus `strip_suffix` if it ends with it), `{ext}` (with the dot),
  `{dir}`, `{name}`. A template containing `/` matches a relative path; a bare name matches anywhere.
  Example: `{"type": "sibling_file", "include_path": "^app/services/.*_service\\.rb$",
  "strip_suffix": "_service", "sibling_template": "spec/services/{stem}_service_spec.rb"}`.

Any regex may contain `{{ticket_key_pattern}}`; the scanner expands it to
`project.ticket_key_pattern` from stack.config (anchors stripped), so a rule can mention "a ticket key"
without hard-coding a tracker's key shape. Keys starting with `_comment` are ignored, so a pack can
carry its own notes.

Then prove it both ways before you commit it: a fixture that must trip it, and a compliant fixture that
must not. A rule that has only been tested positive is a false-positive generator waiting for its first
real branch.
