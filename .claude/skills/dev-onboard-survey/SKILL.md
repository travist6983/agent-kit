---
name: dev-onboard-survey
description: >-
  The onboarding loop's first look at a codebase, with no model in it. Trigger for "survey this
  repo", "what is this codebase made of", "where do I even start in this repo", "map the areas
  before we read", or when dev-loop-onboard starts a run or refreshes a stale map. survey.py
  counts and reads, never guesses: languages by lines, build and package manifests, the
  commands the repo itself declares (each with file and line), entry points, tests, docs and
  decision records, churn hotspots from git history, and a first cut of areas sized for one
  reader each, with the import edges between them. Writes survey.json. Does NOT explain the
  code, read the record or judge quality; how, why and map do that from its areas.
allowed-tools: Read, Glob, Grep, Bash
---

# dev-onboard-survey: count before you read

A newcomer's first hour in a codebase is mostly orientation: what language, how do I build it,
where does it start, what are the big pieces, where are the docs, which files keep changing. None
of that needs judgment, and every minute a model spends on it is a minute of context spent on
`ls`. So this step is a script. It produces the same answer every time on the same commit, which
is what lets the loop fan out over its areas, compare runs and refresh only what moved.

## What this block does NOT do (atomic boundary)

It does not explain any code, mine commits or pull requests for intent, rate code quality, or
decide what the areas mean. It writes one `survey.json` and stops. `dev-onboard-how` and
`dev-onboard-why` fan out over its areas; `dev-onboard-map` seeds the map from it and may rename,
merge or split areas; the staleness check re-runs it.

## Success criteria and failure categories (declared first)

- **Written:** `survey.json` validates against `contracts/dev-onboard-survey.json` (the script
  writes through `contracts/emit.py`, so an invalid survey never lands on disk), and every area,
  command and entry point traces to a file in the repo.
- **Thin but honest:** a tree with no git history, a shallow clone, or a language the survey does
  not count still writes a survey. What it could not do goes in `notes`, in plain words, so the
  why stage knows to expect less. A thin survey is not a failure; a guessed one would be.
- **Bad usage (exit 2):** the repo path does not exist. Nothing is written.

## Inputs / Outputs

- `REPO`: the checkout to learn, read only. Under git the file list is `git ls-files`, so ignored
  files are already out; otherwise a walk that skips dependency, build and cache directories.
- `PROJECT`: the name the brain stamps on rows; defaults to the repo directory's name.
- Output: `<evidence.root>/onboard/<project>/survey.json` (`evidence.root` from stack.config,
  default `runs/`).

## Procedure

```bash
python3 .claude/skills/dev-onboard-survey/scripts/survey.py \
  --repo "$REPO" --project "$PROJECT" \
  --out "$EVID_ROOT/onboard/$PROJECT/survey.json" [--max-area-lines 3000] [--history 300]
```

It prints one JSON line: `{ok, areas, code_files, code_lines, out}`. Then read `survey.json`
yourself before handing it on. Three things are worth a sentence in the run's decision log:

1. **The areas.** An area is one reader's worth of code: one how agent and one why agent will each
   take one. The script descends into a directory that holds nearly all the code, into wrapper
   directories (`src`, `packages`, `services`, `internal`, ...), and into any directory over
   `--max-area-lines`; loose files beside subdirectories become an area of their own, listed by
   path. If an area is still over the cap with nothing to split on, the notes say so and the how
   stage splits it by file.
2. **The commands.** `declared` means a Makefile, justfile, package.json or pyproject named it, with
   the line. `convention` means the toolchain's own default for a manifest that declares nothing
   (`go test ./...` for a `go.mod`). Neither has been run; the how stage runs them when it needs
   to raise a claim from walked to ran.
3. **The notes.** Each one is something the next stages will feel: no git means no why stage worth
   running and no incremental refresh; a shallow clone means `git fetch --unshallow` before the
   why stage if the history matters.

## How the areas are cut

- Source files only: tests, docs and generated files (`*.min.js`, `*_pb2.py`, `*.pb.go`, ...) are
  counted separately or skipped, never cut into areas. Binary files and files over 1 MB are
  skipped and counted in `totals.skipped`.
- Ids are the directory's last name, widened with its parents only when two would clash.
- `depends_on` comes from import statements the survey can resolve to a file (Python, and relative
  imports in JavaScript and TypeScript). A missing edge means not detected, never absent.
- `tests` lists test files that import into an area or are named for it.
- `hotspot_commits` sums how many of the last `--history` commits touched each file in the area.

## Key principles

- **Count, don't guess.** Everything here is read from a file or counted. Interpretation belongs to
  the stages that cite their sources.
- **Same commit, same survey.** Sorted output and no clock in the body, so two surveys diff cleanly
  and a refresh can compare them.
- **Say what you could not see.** A note costs one line and saves a later stage from trusting an
  empty field.
