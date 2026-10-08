# `fixtures/dream-catchup` — the catch-up drain fixture for `test_dream_harvest.sh`

The sibling of `fixtures/dream/`, and it exists for exactly one property: **a capped harvest
is a partial catch-up, never a skip.**

`harvest.py` used to cap with `files[-N:]` — keep the NEWEST sessions, drop the oldest —
while deriving `watermark_to` from what it kept. So a capped run advanced the watermark past
sessions it had never opened, and Claude Code transcripts rotate in about three to four
weeks: that material was gone for good. A measured 30-day window scanned 38 sessions and
buried 68. The cap now keeps the **oldest** and the watermark stops at the newest file the
pass actually read.

**Nothing here is real.** The repo root is the synthetic `/tmp/loops-catchup-repo`, the
ticket is `PROJ-9002`, and no path, session id or ticket key corresponds to anything.

## Layout

| Path | Pinned mtime | Records inside |
|---|---|---|
| `projects/-tmp-loops-catchup-repo/sess-catchup-01.jsonl` | `2026-03-15T09:00:00Z` | 08:50 – 08:59 |
| `…/sess-catchup-02.jsonl` | `10:00:00Z` | 09:50 – 09:59 |
| `…/sess-catchup-03.jsonl` | `11:00:00Z` | 10:50 – 10:59 |
| `…/sess-catchup-04.jsonl` | `12:00:00Z` | 11:50 – 11:59 |
| `…/sess-catchup-05.jsonl` | `13:00:00Z` | 12:50 – 12:59 |
| `repo/runs/PROJ-9002/task-metadata.json` | `13:30:00Z` | — |

Each session carries two human turns and a closing recap, so it is unambiguously non-empty
and identifiable by id in the brief.

## Why the timestamps are shaped this way

**Distinct mtimes, one hour apart.** `watermark_to` is emitted at whole-second resolution, so
two files stamped in the same second cannot be separated by a watermark — the next pass
re-reads both. `fixtures/dream/` deliberately pins every file to one instant (it is testing
caps and determinism, not paging); this fixture must not, or the drain would stall. That
stall is itself asserted, once, via `--max-sessions 1` — where the single slot is always
consumed by the boundary file the next pass re-reads, and the brief has to *say* so rather
than loop quietly.

**Records end before their own file's mtime.** A pass that starts exactly at a file's mtime
finds nothing in it. That is the point: a scanned session that yields nothing must still
count as processed, or it refills the cap on every pass and the backlog never drains. The
`sessions_empty_in_window == 1` assertion on drain pass 2 is that rule, executable.

**The run artifacts are pinned LATER than every transcript** (`13:30`). Task dirs are scanned
uncapped, so their timestamps would happily drag `watermark_to` past the deferred sessions.
The capped passes assert the watermark stops short of `13:30`; the final, uncapped pass
asserts it finally reaches it.

**Sessions do not overlap in time.** A single scalar watermark cannot express "file X is
done" for a session that began before the batch it was deferred behind, so an overlapping
deferred session can still lose its pre-watermark records — the residual limitation of a cap
plus a scalar watermark, documented in `harvest.py`'s header. This fixture pins the property
that IS guaranteed: with a cap of 2 over 5 sessions, four passes harvest all five sessions,
each exactly once, and `catch_up_needed` clears.

## If you edit this fixture

The mtimes live in `test_dream_harvest.sh` (the `PINS` map in the catch-up block), not on
disk — git does not carry mtimes, and `touch -t` is local time, which would shift the window
with the machine's timezone. Change a file here and you must change that map, the expected
watermarks (`10:00`, `13:30`), and the expected pass count.
