# Collateral proof: what else the fix could break, proven by running code

Adapted from pstack's `blast-radius` skill (MIT, Lauren Tan; github.com/cursor/plugins, `pstack`).
Renamed here because this framework already uses "blast radius" for dev-fix's R6 denylist and its
`blocked-blast-radius` status; two meanings for one word would mislead the next agent.

This is how fix-verify proves MUST-HAVE 2 (no silent collateral change) instead of eyeballing a few
neighboring fields. It runs as step 2b, in a **fresh sub-agent** that is handed the diff, the
sandbox path and the bug brief, and **not** dev-fix's design_decisions or its own claims that the
change is safe. A judge that starts from the author's reasoning tends to repeat it.

Listing the callers is not the job; grep does that in a second. The job is the breakage grep will
not show.

## Do not trust your own writeup

A writeup that sounds right reads the same whether or not it is true. So the artifact does not
rest on prose. Find the one or two facts the change's safety depends on and push each as far down
this ladder as is cheap. Record where it stopped as `safety_fact.rung`.

| rung | proof_kind | what it means | what the artifact must carry |
|---|---|---|---|
| 1 | stated | you said so; worthless alone | nothing more |
| 2 | cited | you pointed at the line: a real `path:line`, or the library's own source | `ref` |
| 3 | walked | you walked the bad case step by step and it cannot reach | `ref` |
| 4 | ran | a script or test calls the real code and fails loud if you are wrong | `command` and its `output` |
| 5 | reproduced | you reproduced it in the running app | `output` (the observed quote) |

Rung 4 is usually one small script that imports the same library the app ships and calls the exact
function you are worried about. validate.py's **QA-COLLATERAL-P1** checks the rung against the
evidence, so a rung-4 claim without the command and its output does not validate.

## Steps

1. **Read the change.** The diff, the symbols it adds, changes and deletes, and what it now does
   differently, including the part the diff does not spell out.
2. **Find the one fact it is safe because of.** Most risky-looking changes are safe because of a
   single fact, such as "this call only drops cache entries that are already dead". If it holds,
   most risky cases clear at once. Spend the time here, not on a long list of maybes.
3. **Look where grep stops.** Read the source of the library being called, at the version the
   target pins (and any local patch). Work out when things run: microtasks, unmount and teardown,
   framework render order. Follow what a symbol search misses: JSON an API returns, a DB column, a
   wire format, another language reading the same bytes, a feature flag, code three hops away.
4. **Be honest about each risk.** Give it a real likelihood and a real cost. Keep the risks you
   confirmed in `risks`; list what you checked and cleared in `cleared`. Cite real `path:line`. A
   search that finds nothing is an answer; never invent a caller or an API.
5. **Prove the safety fact.** Write the script under `<REPRO_DIR>/collateral/`, never in the target
   tree, run it against the sandbox's own installed dependencies, and paste the command and its
   output. Delete nothing the target owns; commit nothing.

## The artifact

`<REPRO_DIR>/collateral.json`, written through
`contracts/emit.py --contract contracts/qa-verify.collateral.json`:

- `status`: `safe` (rung 3 or deeper, and no confirmed risk rated high), `risky` (a confirmed
  risk), `unproven` (the safety fact stopped at rung 1 or 2), `blocked` (could not look).
- `change`, `safety_fact`, `risks`, `cleared`, `before_merge` (the cheapest test or repro that
  would catch the real bug, including the script), `summary`.

## How fix-verify uses it

The `no_collateral` must-have takes its result from `status`: `safe` is `pass`, `risky` is `fail`,
`unproven` and `blocked` are `unknown`. Only a `pass` there can feed a `pass` verdict; anything else
is at best `partial`, which escalates to a human with the artifact attached. `before_merge` goes
into `judge_rationale` so a human taking over has the test to run.
