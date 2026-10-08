# `fixtures/dream-worktree` — the sandbox + prose fixture for `test_dream_harvest.sh`

The third sibling of `fixtures/dream/` and `fixtures/dream-catchup/`, and it exists for one
defect: **the harvest never looked inside an `infra-worktree` sandbox** (SF-4).

`harvest.py` used to default its run-artifact scan to `<repo>/runs` for the two repo
roots only. But the loops do not run in a repo root — `qa-dream/SKILL.md` says so outright
("a loop's cwd is normally an `infra-worktree` sandbox, not this repo") — so a window in
which a real loop ran reported `tickets_seen: 0`, and every `fix_lessons` / `expectations` /
`flakiness` / `app_map` candidate that loop's artifacts would have fed was invisible. Three
dreams in a row wrote **zero** `fix_lessons` for exactly this reason. The scan now covers
every live `git worktree`, and says in `artifact_sources` what it looked at.

**Nothing here is real.** The tickets are `PROJ-9201` / `PROJ-9202` / `PROJ-9203`, the repo is
built from scratch inside the test's temp dir, and no path, key or session id corresponds to
anything that exists.

## Layout

| Path | What it stands in for |
|---|---|
| `repo/seed.txt` | something to commit, so `git worktree add` has a checkout to make |
| `repo/runs/PROJ-9201/intake.json` | a run that happened in the repo root itself — the dedupe target |
| `sandbox-tasks/PROJ-9202/*` | the loop that ran **inside the sandbox**: intake + four prose files |
| `sandbox-tasks/PROJ-9203/plan.md` | a run dir whose ONLY artifact is prose |

The `sandbox-tasks/` tree is not a worktree on disk. The test copies it into the worktree it
creates at run time (`live-worktree/runs`), because git worktrees cannot be committed
to git and a `.git` directory cannot be tracked inside another repository.

## What the test builds around it, and why it cannot be a static fixture

`test_dream_harvest.sh` copies this directory to a temp dir and then, with real git:

1. `git init` + one commit in `repo/`;
2. `git worktree add live-worktree` → copies `sandbox-tasks/` in as its `runs/`;
3. `git worktree add gone-worktree` → then **deletes the directory**. `git worktree list`
   keeps reporting a pruned worktree, so this is the "listed but gone" path that must be
   skipped rather than crashed on;
4. symlinks `repo-link → repo` and passes **the symlink** as `--repo`.

Step 4 is the load-bearing one. `git worktree list --porcelain` reports the MAIN worktree as
well — the repo root, a second time — and it prints **resolved** paths. Passing a symlinked
spelling guarantees git's spelling differs from ours on every platform (on macOS a checkout
under `/tmp` or `/var` already comes back as `/private/…`), so a dedupe that compares strings
instead of `os.path.realpath` scans the same directory twice. That is the assertion
`len(artifact_sources) == 2` is there to catch.

Real git, not a stub: the point is that `--porcelain` is parsed as git actually emits it,
including the `prunable` annotation on the deleted worktree and the unsorted order it returns
worktrees in (main, then oldest-added first — never the sorted order the brief promises).

## The prose files, and the cap that has to hold

`PROJ-9202` carries **four** prose files. The harvest takes **two**: the *final* pr-body
variant (initial / plain / final are drafts of ONE document, not three sources) and
`plan.md`. The other two are counted in `filtered.prose_files_skipped_total`.

`pr-body.final.md` is deliberately ~3.6 KB, so the 2000-char per-file cap fires and the drop
is counted. Its markers make each rule falsifiable:

| Marker | Must the brief contain it? |
|---|---|
| `FINALBODYMARKER` | **yes** — the final variant is the one that was taken |
| `PLANMARKER` / `PLANONLYMARKER` | **yes** — `plan.md` is recognised, and recognised *alone* |
| `TAILCUTMARKER` | **no** — it sits past the 2000-char cap |
| `PLAINBODYMARKER` / `INITIALBODYMARKER` | **no** — the other two drafts are not taken |
| `IMAGELINEMARKER` / `BASE64BODYMARKER` | **no** — image lines and data URIs are stripped |
| `GENERATEDWITHMARKER` / `COAUTHORMARKER` / `SESSIONLINKMARKER` / `TRAILERSESSIONMARKER` | **no** — the PR trailers are stripped before the cap, so the cap spends its budget on the argument |

This is the fix's real risk: prose is where a feature loop writes its root cause and fix
shape, which is exactly `fix_lessons` material — and also exactly how a compact brief turns
into the transcript pile the whole skill exists to avoid. Bounded per file, bounded per
ticket, and every dropped character counted.

## Timestamps

Every file under a `runs/` tree is pinned to `2026-03-15T12:00:00Z` from python (an
absolute epoch — `touch -t` is local time and would shift the window with the machine's
timezone), and the harvest runs with `--now 2026-03-16T00:00:00Z --since 7d`. Git's own
files are left alone. Edit a file here and the counts in `test_dream_harvest.sh` (three
tickets, two prose files, two skipped) move with it.
