# qa-verify rules — the four MUST-HAVEs, the false-green guards, the masking detector

The verification discipline every loop verdict rests on: the MUST-HAVE validation rules, the
wrong-fix guard, the confidence-≥80 validator, and the R3 root-cause rule.

## The four MUST-HAVEs (apply to every verification)

1. **Transcript ↔ state parity.** Every "it's fixed / it saved / it was removed" claim must be
   independently verifiable in the **real state at that moment** — the live DOM, the API response, the
   DB — not the app's own success message.
2. **No silent collateral change.** Confirm the state the fix should *not* have touched is unchanged —
   neighboring fields, adjacent rows, other areas. A fix that corrects X but quietly mutates Y is a
   regression, not a pass ("no silent field clobbering").
3. **Visual confirmation.** Confirm the corrected behavior is actually **visible to a user** — a
   screenshot / DOM read after a refresh — not merely present in an API payload. The user's experience
   is the truth.
4. **No "trust me" passes.** If you cannot *see/read* the corrected state, the fix has **not** passed —
   even if a toast, a log line, or the JSON says success. This is the rule the whole evaluator exists
   to enforce: the classic trap is a save that shows "Saved" while the row on reload is unchanged.
   Worked example on the neutral domain: a Task's due date is edited, the toast says "Saved", and
   after a reload the Task still shows the old date — the toast is not evidence; the reloaded row is.

## False-green guards (a pass you must refuse to give)

- **Never verify a non-repro.** If the `qa-repro` artifact's `status` isn't `reproduced`, there is no
  oracle to verify against — return `verdict: blocked, reason: no-oracle`. Running verification against
  a could-not-reproduce or a known wrong fix just manufactures a false "verified".
- **Never verify a reviewer-rejected fix.** If `dev-code-review`/the recipe flagged the fix WRONG_FIX,
  don't green it.
- **Never verify stale code.** If you cannot confirm the running app includes the fix (a backend change
  whose process was not restarted), return `verdict: blocked, reason: stale-code` rather than grade the
  old build.

## Masking detector (R3 — root cause over symptom)

A replay can go green for the *wrong reason*. Inspect the candidate diff; if the green depends on any
of these, set `masking_suspected: true`, **cap confidence below 80**, and flag for human — a symptom
patch is not a pass:

- A `try/catch` (or `.catch()`) that **swallows** the error instead of fixing the cause (empty catch,
  catch that returns success, broad catch around the failing call).
- **Suppressed or deleted** logging / error surfacing that was exposing the bug.
- A **narrow special-case** keyed to the repro itself (a hardcoded id/string/flag, an `if (key === …)`)
  rather than the general cause.
- A **disabled / skipped test or assertion**, or a **loosened type** that dodges the compile/runtime
  error rather than addressing it.
- The fix changes the **symptom surface** (e.g. hides/changes the toast) while the underlying state is
  still wrong — the success-toast trap in reverse: the message changes, the row does not.

The detector is language-agnostic — the patterns above describe shapes (a swallowed error, a removed
log line, a repro-keyed branch, a skipped assertion, a loosened type), not any one language's syntax.
Apply them to whatever `stack.languages` names.

## The LLM-judge (the subjective gate)

Spawn a judge (Agent) given: the verbatim `expected_result`, the after-video (video #2), the live-state
quotes, and the diff summary. It answers two things and returns a confidence 0–100:
1. Does the **observed behavior match `expected_result`** (not a paraphrase — the real thing)?
2. Is the fix a **real fix, not a mask** (per the detector above)?

**The prompt is neutral.** Give the judge the quoted expectation, the quoted observation and the diff
summary, and ask the two questions above. Do not tell it the replay went green, do not say "confirm the
fix", and do not hand it the producer's own rationale — a judge primed with the answer is not a gate.

**Confidence ≥ 80 → may auto-pass. Below 80 → escalate even if the replay is green** (the review
rubric: report/act only at ≥80; uncertainty is a human's call). A green replay with low judge
confidence is a `partial`, never a `pass`.

## Verdicts

- `pass` — replay flipped RED→GREEN **and** all four MUST-HAVEs hold **and** judge ≥ 80 **and** not masking.
- `partial` — primary symptom gone but a guard tripped (collateral change, low confidence, thin visual).
- `fail` — replay still red, or state doesn't match expected, or a regression appeared.
- `blocked` — no oracle (repro wasn't `reproduced`) / reviewer-rejected fix / stale code / the replay
  oracle (`replay-oracle.sh`) exited 2 — it could not run, which says nothing about the fix.
