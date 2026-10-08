#!/usr/bin/env bash
# Convert a run-recording GIF into a broadly-compatible MP4.
# Same tool as qa-repro/scripts/gif_to_mp4.sh; kept here so a case run has no runtime dependency on a
# sibling skill for the conversion step. Upload is NOT duplicated — see qa-repro/scripts/upload_evidence_media.sh.
# Usage: gif_to_mp4.sh <input.gif> <output.mp4>
set -euo pipefail
SRC="${1:?usage: gif_to_mp4.sh <in.gif> <out.mp4>}"
OUT="${2:?usage: gif_to_mp4.sh <in.gif> <out.mp4>}"
[ -f "$SRC" ] || { echo "gif not found: $SRC" >&2; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg not installed" >&2; exit 1; }
# full colour, broadly compatible; even dimensions required for yuv420p
ffmpeg -y -i "$SRC" \
  -movflags +faststart -pix_fmt yuv420p \
  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" \
  "$OUT"
ls -lh "$SRC" "$OUT"
