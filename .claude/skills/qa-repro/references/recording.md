# Recording recipe — the before-video (video #1)

qa-repro records **one** video — the bug reproducing (video #1). qa-verify records the after-video
(video #2) with the same recipe. Each browser session yields a **GIF** (animates inline in a PR) + an
**MP4** (full-quality download), both from a single capture — deterministic, no screen-recording
permission.

## 1. Wrap the repro steps in a GIF recording

`gif_creator` overlays (click indicators, action labels) are genuinely useful on a repro video —
keep them; turn the **watermark off**.

**Duration + completeness are non-negotiable (a 3–4s clip that doesn't show the bug or the input is a
fail):**
- The **full MP4 must be ≥ 15 seconds.** (The inline GIF may be shorter, but the linked MP4 must not.)
- The **before/repro** video must **show the bug actually happening** — drive the full repro and capture
  the *visible symptom* (the error toast, the wrong value, the 409), not just the end state. Trigger it
  more than once if it's intermittent, so it's unmistakable on screen.
- The **after/verify** video must **show the user input action** (typing the value, clicking the control)
  AND the correct result — the fix working end to end, not a static "after" frame.
- Capture a **complete, deliberate walkthrough**: navigate → locate the field → perform the action
  step-by-step (screenshot after *each* segment/click/state-change) → show the result. `gif_creator`
  needs **≥ 12 frames** for a usable length, so take many screenshots; a 2–4 frame clip is far too short.

```
gif_creator start_recording (tabId)
computer screenshot            # first frame — immediately after start
… repro actions, screenshot after each meaningful step …
computer screenshot            # final frame — immediately before stop
gif_creator stop_recording
gif_creator export download:true filename:"{KEY}-repro.gif" options:{ showWatermark:false }
```

**Foreground the Chrome window** — a backgrounded tab freezes `requestAnimationFrame` and yields
stale, identical frames. If screenshots look frozen, check `document.visibilityState` via
`javascript_tool`.

## 2. Land both files in the evidence dir, convert GIF → MP4

`gif_creator export download:true` writes to `~/Downloads`. Copy into the evidence dir (never `mv`
across the project boundary), then convert with the bundled script:

```bash
KEY=PROJ-123
MEDIA="$EVID_ROOT/$KEY/media"     # EVID_ROOT: a skill input, default evidence.root from stack.config (runs/)
mkdir -p "$MEDIA"
SRC="$HOME/Downloads/$KEY-repro.gif"
[ -f "$SRC" ] || SRC=$(find "$HOME/Downloads" -maxdepth 1 -name "$KEY-repro*.gif" -print -quit)
[ -f "$SRC" ] || { echo "GIF export not found for $KEY-repro"; exit 1; }
cp "$SRC" "$MEDIA/$KEY-repro.gif"
bash <agents-repo>/.claude/skills/qa-repro/scripts/gif_to_mp4.sh "$MEDIA/$KEY-repro.gif" "$MEDIA/$KEY-repro.mp4"
rm -f "$SRC"
```

## 3. Sanity-check

- Both files exist and are non-zero; the GIF animates (the loop can `Read` it to confirm a frame).
- **The MP4 is ≥ 15s — verify it:** `ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 out.mp4`.
  `gif_creator`'s frame timing often yields a shorter MP4; if it's under 15s, capture more frames OR
  stretch it. Note `setpts` alone drops the final frame's hold (duration ≈ (N−1)/N × target), so add a
  `tpad` clone-hold on the last frame:
  `ffmpeg -y -i raw.mp4 -filter:v "setpts=${F}*PTS,tpad=stop_mode=clone:stop_duration=2.5" -r 24 -pix_fmt yuv420p out.mp4`
  (pick `F = 16 / raw_duration`). Then confirm the before-video visibly shows the symptom and the
  after-video shows the input action — re-record if either is missing.
- A multi-MB GIF is fine; a 50 MB GIF means the capture was too long — tighten the steps, don't pad filler.

## 4. Upload BOTH artifacts at capture time → `media[]` (control-plane streaming)

The recording is only useful to the control plane if it can **stream it the moment it's captured** —
not only after a PR exists. So immediately after the GIF **and** MP4 land in `$MEDIA`, upload **both**
through the `evidence_store` adapter and collect the returned URLs into `media[]`. **The MP4 conversion
in step 2 is not optional** — always run `gif_to_mp4.sh` so there is an MP4 to upload.

Use the shared helper — it is the single home of the upload step (qa-verify and qa-run-case call this
exact script, as they already reuse `gif_to_mp4.sh`):

```bash
KEY=PROJ-123
MEDIA="$EVID_ROOT/$KEY/media"
UP=<agents-repo>/.claude/skills/qa-repro/scripts/upload_evidence_media.sh
# qa-repro emits before-* ; qa-verify fix-verify emits after-* (after-gif/after-mp4)
MEDIA_JSON="$(
  { "$UP" "$KEY" "$MEDIA/$KEY-repro.gif" before-gif "Reproduced (before) — GIF"
    "$UP" "$KEY" "$MEDIA/$KEY-repro.mp4" before-mp4 "Reproduced (before) — MP4"
  } | jq -cs .    # → a JSON array of {kind,url,label}, ready to drop into repro.json.media
)"
```

- The helper calls `evidence_store upload(path, key)` and prints one `{kind, url, label}` object per
  file. With the default `local` provider the `url` is a `file://` path under `<evidence.root>/<KEY>/`;
  a remote provider returns a signed read URL that a browser can fetch with no auth header and the
  right Content-Type (`video/mp4` / `image/gif`).
- **Config comes from `adapters.evidence_store` in `stack.config.json`** — the provider and its options.
  Secrets are env vars the config *names*. **Never hardcode a secret.** Any cross-origin read rule a
  remote bucket needs is set once, out of band, and documented with that provider; a skill never sets it.
- **Best-effort:** if the adapter is `none` or an upload fails, the helper exits non-zero (3 when
  unavailable) and prints nothing to stdout — **omit `media` entirely** (it is optional in
  `contracts/qa-repro.json` and `contracts/qa-verify.repro-judge.json`; the artifact still validates).
  Never fail a reproduction over an upload.

> **The recording is evidence, not the test.** Verification is reading the live DOM/console/network
> against the verbatim expected result (that's qa-verify's job); the GIF/MP4 documents it for a human.
> Never let "I have a video" substitute for "I read the real state."
