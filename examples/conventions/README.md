# `examples/conventions/` — how a target repo expresses its house rules

The framework does not know your stack. It knows one file — `.claude/loops/conventions.json` — and
three skills read it:

| skill | what it reads |
|---|---|
| `dev-pre-pr` | `rules` (the deterministic added-lines scan), `suppress_marker`, `test_path_regex`, `projects` (which build/lint/test commands a changed path implies), `guidelines_file` (the reader-judged rules) |
| `dev-code-review` | `guidelines_file` + `rules` — the constraints the specialists review against; the optional `review_brief` (see "Reviewer brief") |
| `dev-fix` | `guidelines_file`, `blast_radius_denylist_globs` (the R6 forced-human-gate paths), `shared_core_globs` (what counts as a shared/core edit), `app_handles`, `domain_glossary` |

The file the framework ships is deliberately small and language-agnostic: no debug output in added
non-test lines, no focused tests, no secret-looking literals, no `TODO` without a ticket key. It is a
floor, not a house style. `example-pack.json` in this directory shows what a real pack looks like for a
**hypothetical** stack — a Ruby API with a TypeScript single-page UI, serving the usual SaaS nouns
(Workspaces, Projects, Tasks, Comments, Invoices, Members, Settings). Nothing in it is required; every
rule is there to show one feature of the format.

## The shape

```jsonc
{
  "version": "1.0.0",
  "guidelines_file": "CLAUDE.md",          // the reader-judged rules live here (prose)
  "suppress_marker": "standards:allow",    // a line containing this is skipped by the scanner
  "test_path_regex": "(_spec\\.rb$|\\.test\\.tsx?$|/spec/|/__tests__/)",
  "rules": [ /* see "Rules" */ ],
  "projects": [ /* see "Projects" */ ],
  "blast_radius_denylist_globs": [ "db/migrate/**", "config/credentials*", "infra/**", ".github/workflows/**" ],
  "shared_core_globs": [ "app/models/concerns/**", "web/src/shared/**" ],
  "app_handles": { "primary_nav": "[data-testid=primary-nav]", "toast": "[role=status]" },
  "domain_glossary": { "Workspace": "the tenant; every row belongs to exactly one", "Member": "a user inside a workspace" },
  "review_brief": { /* optional; see "Reviewer brief" */ }
}
```

Keys beginning with `_comment` are ignored everywhere the file is read, so you can annotate your pack
the way `example-pack.json` does.

### Rules

Each entry in `rules` is one mechanical check. The full field list and the three rule types (`line`,
`file_absent`, `sibling_file`) are documented in
`.claude/skills/dev-pre-pr/references/violation-rules.md`. The two decisions that matter most:

1. **Does a regex decide this with near-zero false positives?** If not, it is not a rule — it is a
   paragraph in `guidelines_file`, and `dev-code-review` / `dev-pre-pr`'s reader half will judge it.
2. **Can it block on its own?** Only a rule you would bet a loop cycle on is `heuristic: false`. Anything
   that needs a human to look (`"is this literal a secret?"`, `"is this query scoped by its base class?"`)
   is `heuristic: true` with a `confirm_question`; it is reported, and it blocks only after the gate opens
   the file and confirms it.

Write `{{ticket_key_pattern}}` inside a regex to mean "a ticket key of this project's shape"; the
scanner substitutes `project.ticket_key_pattern` from stack.config.

### Projects

`projects` tells the gate which commands a changed path implies, so a stylesheet-only branch never pays
for a backend test run and a backend-only branch never pays for a UI build:

```jsonc
"projects": [
  { "name": "api", "path_globs": ["app/**", "lib/**", "config/**", "Gemfile*"],
    "build": null, "lint": "bundle exec rubocop", "unit_test": "bundle exec rspec",
    "test_report": { "format": "junit", "glob": "tmp/rspec/*.xml" } },
  { "name": "web", "path_globs": ["web/**"],
    "build": "npm --prefix web run build", "lint": "npm --prefix web run lint", "unit_test": "npm --prefix web test -- --run",
    "test_report": { "format": "junit", "glob": "web/test-results/*.xml" } }
]
```

A changed path that matches no project falls into `other` and uses the repo-wide
`stack.build_command` / `stack.lint_command` / `stack.unit_test_command` from stack.config. **The
commands here must be the exact ones CI runs** — same flags, same configuration — or a local green means
nothing; `dev-pre-pr` and `qa-regression` both run them, which is what keeps the two from disagreeing.

### Blast radius and shared core

`blast_radius_denylist_globs` is the R6 list: paths `dev-fix` may never edit without a forced human
gate (migrations, secrets and environment config, auth configuration, infrastructure, CI definitions).
`shared_core_globs` marks the paths whose edits make a defect "hard" (routing to the loop with a code
review gate). Both are globs on repo-relative paths.

### Handles and glossary

`app_handles` names stable UI selectors the browser-driving skills can rely on. `domain_glossary` maps
your nouns to one-line meanings, so an agent reading a ticket about "a Member" knows what it is
without exploring. Neither affects the scanner.

### Reviewer brief

`review_brief` is optional, and the code owners write it. It tells `dev-code-review` what failure
looks like in their area, how many nits they are willing to read, and which paths are not worth a
reviewer's time. Leave it out and the review behaves exactly as it did before the key existed.

```jsonc
"review_brief": {
  "max_nits": 3,                                   // optional, integer >= 0
  "skip_paths": ["web/src/generated/**", "**/*.snap"],
  "areas": [
    { "name": "invoices", "owner": "billing owners",
      "path_globs": ["app/services/invoices/**", "web/src/invoices/**"],
      "failure_looks_like": ["an Invoice total that disagrees with the sum of its line items"],
      "not_worth_flagging": ["naming inside generated serializers"] }
  ]
}
```

| field | meaning |
|---|---|
| `max_nits` | the most open `low` findings a review reports. The rest are stamped `withheld: true` with a reason, kept in `review.json` and not posted. Omitted: `loop-limits.json review_brief.default_max_nits` (5, argued, not measured). Critical, high and medium findings and the verdict are never touched |
| `skip_paths` | globs (the same rule as `projects[].path_globs`) for changed files the specialists do not read. A match-all glob (`*`, `**`, `**/*`) is refused |
| `areas[].name` | unique, non-empty |
| `areas[].path_globs` | which changed files belong to the area (non-empty) |
| `areas[].failure_looks_like` | the owner's statement of what a real bug looks like there (non-empty). A specialist reading the area's files gets these lines |
| `areas[].owner`, `areas[].not_worth_flagging` | optional |

Keys beginning with `_comment` are ignored; any other unknown key, a wrong type or a duplicate area
name makes the brief malformed.

**Never skipped.** A skip glob never covers a file that is hit by the R6 denylist (your
`blast_radius_denylist_globs`, and also the framework floor `dev-fix/assets/r6-denylist.globs`,
because your list replaces the floor and may leave a category out) or matched by
`shared_core_globs`. Such a file is listed in `brief.skip_refused` and reviewed as normal, and a
denylisted one is still a CRITICAL finding. The example pack lists `db/migrate/**` on purpose to show
the refusal. A consequence: you cannot skip a generated `*.sql` or schema file.

**Unavailable.** A malformed brief, an R6 scan that cannot run, no decided nit cap (the brief omits
`max_nits` and loop-limits has no valid number), or a diff that changes this conventions file itself
(a change may not grant itself skip paths) all make the brief `unavailable`: everything is reviewed,
nothing is withheld, and `review.json` says `brief: {status: unavailable, error}`.

**The count comes first.** With or without a brief, `review.json` `summary` and the first line of
`review.md` start with the finding count, e.g. `3 findings (1 critical, 2 low).`; a clean review
reads exactly `0 findings: nothing needs fixing.`

Two commands:

```bash
python3 .claude/skills/dev-code-review/scripts/review_brief.py plan  --repo <app checkout> [--base main]
python3 .claude/skills/dev-code-review/scripts/review_brief.py check --review runs/PROJ-123/review.json --repo <app checkout> [--report runs/PROJ-123/review.md]
```

`plan` shows what a review of the current branch would skip, refuse and cap (exit 2 = unavailable,
with the reason). `check` re-derives the plan and names any inconsistency in a finished review (exit
1 with a violation class). Honest limit: the reviewer's own `review_brief.py finalize` step is what
applies the brief. `check` is tested, but no recipe, runner, `dev-pre-pr` step or ship gate calls it
yet.

## Adopting it

1. Copy `example-pack.json` to `.claude/loops/conventions.json` (or start from the shipped default and
   add rules).
2. Delete every rule that is about the hypothetical stack. Keep the ones that happen to fit.
3. For each rule you add: one fixture that must trip it, one compliant fixture that must not. Run
   `bash .claude/skills/dev-pre-pr/scripts/standards_check.sh --repo <fixture-repo>` and read the JSON.
4. Fill `projects` with the commands your CI actually runs.
5. Point `guidelines_file` at the document that holds the rules a reader must judge.
