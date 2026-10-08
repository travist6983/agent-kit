# Embedding the before/after recordings in the PR — the side-by-side body block

This is the **authoritative** recipe for how `infra-ship` puts the loop's recordings into the PR. The
before-video comes from `qa-repro` and exists on a **defect** only; the after-video comes from
`qa-verify` and exists on every lane. This step **assembles** whichever the run produced.

## The two rules this exists to enforce

1. **Media goes in the PR DESCRIPTION (body), never in a comment/thread.** A GIF dropped in a comment is
   the wrong surface (several hosts flatten GIFs in comment surfaces to a static first frame). The
   before/after block lives in the body — and because the adapter contract has no body-update op, it is
   assembled **before** `vcs_host open_pr` and goes in with the create.
2. **On a DEFECT it is a SIDE-BY-SIDE before → after, with MP4 download links** — a 2-column table
   (before | after) of the two GIFs, and below it `Full-quality video: [before MP4](…) · [after MP4](…)`.
   One combined GIF, or an after with no before, is not the format *for a defect*: the before is the
   evidence the bug was real.
   **On a FEATURE it is the after alone.** A before-capture on a feature reverts the working tree to
   manufacture a same-view A/B, costs minutes, and changes no verdict. A feature PR with one recording is
   **correct**, not a missing-recording fallback.

## What PR hosts support (plan for the common case)

- **GIF, PNG, JPG render inline** in a PR **description** on every mainstream host via `![alt](url)`, and
  animated GIFs animate there.
- **MP4 rarely has an inline player** in a PR description; many hosts serve attachments as a download.
  → MP4 is intentionally a `[download]` link, not an embed. That's why the body carries **GIF (inline) +
  MP4 (link)** for each side: the GIF is the at-a-glance proof, the MP4 is the full-quality walkthrough.
- Whether the host can **host** the file itself is a provider detail. The URL the body uses is the
  `evidence_store` URL; a provider that also attaches files to the PR returns a `host_url` you record
  alongside it. With the default `local` evidence store the URLs are `file://` paths — correct for a dry
  run, and the run report must say so.

## Inputs — where the four files come from

The loop must have produced both recordings (each a GIF that animates + an MP4 download), per
`qa-repro/references/recording.md` (recipe) and `scripts/gif_to_mp4.sh` (GIF→MP4). All four land in one
**media dir** (`$MEDIA`, e.g. `<evidence.root>/<KEY>/media`). Naming, by loop type:

| | before (left panel) | after (right panel) |
|---|---|---|
| **defect loop** | `$KEY-repro.gif` / `$KEY-repro.mp4` (the reproduced bug, from `qa-repro`) | `$KEY-fixed.gif` / `$KEY-fixed.mp4` (the verified fix, from `qa-verify`) |
| **feature loop** | *(none)* | `$KEY-fixed.gif` / `$KEY-fixed.mp4` (the feature working, from `qa-verify`) |

Auto-detect the before file: `$KEY-repro.*`. A `$KEY-before.*` left by an older feature run still
renders if present, but nothing produces one now.
**If a recording is genuinely absent** (e.g. a non-UI change): skip the block and say so in the body
(`_No UI recording for this change._`) — never fake it, and never silently drop it.

**All four files must exist — GIF *and* MP4 for both sides.** `qa-repro`/`qa-verify` make the MP4
conversion mandatory. If an MP4 is missing here, that's an upstream gap: note it in the report and fall
back to GIF-only for that side rather than silently pretending it shipped.

**The evidence-store URLs are usually already in hand** — `qa-repro` wrote `repro.json.media[]`
(`before-*`) and `qa-verify` wrote `verify.json.media[]` (`after-*`) at capture time, through the
`evidence_store` adapter. Read those; they become the **primary** `url` in `ship.json.media[]`. Only an
entry that is missing upstream is uploaded here.

## Step 1 — gather identifiers

`KEY` (the ticket key), `$MEDIA`, and `CLI=<agents-repo>/lib/adapters/cli.py`. The PR does not exist
yet — the body goes in with the create (Step 4).

## Step 2 — URLs: read `media[]`, upload what is missing

```bash
# prefer the capture-time entries
BEFORE_GIF_URL=$(jq -r '.media[]? | select(.kind=="before-gif") | .url' "$TASK_DIR/repro.json")
BEFORE_MP4_URL=$(jq -r '.media[]? | select(.kind=="before-mp4") | .url' "$TASK_DIR/repro.json")
AFTER_GIF_URL=$(jq -r  '.media[]? | select(.kind=="after-gif")  | .url' "$TASK_DIR/verify.json")
AFTER_MP4_URL=$(jq -r  '.media[]? | select(.kind=="after-mp4")  | .url' "$TASK_DIR/verify.json")

# anything missing: upload through the evidence_store adapter (exit 3 = adapter-unavailable → say so in
# the body and the report; never invent a URL)
upload() {  # $1=local file → prints the url
  python3 "$CLI" evidence_store upload path="$1" key="$KEY" | jq -r '.result.url'
}
BEFORE_BASE=$([ -f "$MEDIA/$KEY-repro.gif" ] && echo "$KEY-repro" || echo "$KEY-before")
[ -n "$BEFORE_GIF_URL" ] || BEFORE_GIF_URL=$(upload "$MEDIA/$BEFORE_BASE.gif")
[ -n "$AFTER_GIF_URL" ]  || AFTER_GIF_URL=$(upload  "$MEDIA/$KEY-fixed.gif")
[ -n "$BEFORE_MP4_URL" ] || BEFORE_MP4_URL=$(upload "$MEDIA/$BEFORE_BASE.mp4")
[ -n "$AFTER_MP4_URL" ]  || AFTER_MP4_URL=$(upload  "$MEDIA/$KEY-fixed.mp4")
```
Use the **full URL** in the markdown — repo-relative paths won't resolve. If an upload fails, don't fail
the whole PR over a missing video — fall back to a body note pointing at the local evidence path and flag
it in the report.

## Step 3 — the block that goes into the create body

Pick the shape by loop type:
- **defect:** the 2-column table — `Reproduced (before)` | `Verified fixed (after)`
- **feature:** a single `## After` block — `<feature> working` (e.g. `Add task working`), with
  `Full-quality video: [after MP4](…)` beneath. One line of the Summary says what the surface looked
  like before.

```bash
read -r -d '' MEDIA_BLOCK <<EOF
## Before → After
| $LEFT_LABEL | $RIGHT_LABEL |
|---|---|
| ![before]($BEFORE_GIF_URL) | ![after]($AFTER_GIF_URL) |

Full-quality video: [before MP4]($BEFORE_MP4_URL) · [after MP4]($AFTER_MP4_URL)
EOF

# Compose the full body (select_pr_threads.py's description_top — empty on most PRs — then Summary,
# Tickets, Changes, the MEDIA_BLOCK, Testing, and "Noticed, not changed" if there is anything to list),
# then open the PR with it — SKILL.md Step 4. It must carry the same release target the title does.
```

Some hosts accept a sizing suffix if a GIF is huge in the diff view (`![before]($URL =600x)`); it is a
host extension, not markdown — use it only when your provider's host renders it.

## Sanity-check (do this before declaring the PR done)

- The body (not a comment) contains the `## Before → After` 2-column table **and** the `Full-quality video:`
  line with two MP4 links.
- On a real host, open the PR URL and confirm **both GIFs animate** and are legible (the button/state is
  clearly visible — a too-long or too-compressed capture reads as a single static frame; if so, re-record
  a tighter, higher-quality sequence per `recording.md` and re-upload). The whole point is a human can
  verify from the GIF. With `git-local` + `local` the PR is a file: check the markdown, state the dry run.
- Record media into `ship.json` as a **structured array `media[]`**, one entry per artifact — NOT a prose
  string (a prose `media_embedded` string is GIF-only and unparseable). Each entry:

  ```jsonc
  // ship.json
  "media": [
    { "kind": "before-gif", "url": "<evidence-store URL>", "host_url": "<PR host attachment URL, if any>", "label": "Reproduced (before) — GIF", "in_body": true  },
    { "kind": "before-mp4", "url": "<evidence-store URL>", "host_url": "<…>", "label": "Reproduced (before) — MP4", "in_body": false },
    { "kind": "after-gif",  "url": "<evidence-store URL>", "host_url": "<…>", "label": "Verified fixed (after) — GIF", "in_body": true  },
    { "kind": "after-mp4",  "url": "<evidence-store URL>", "host_url": "<…>", "label": "Verified fixed (after) — MP4", "in_body": false }
  ],
  "media_in_body": true
  ```
  - `kind` is the shared four-value enum (`before-gif|before-mp4|after-gif|after-mp4`) — same vocabulary as the
    `repro.json`/`verify.json` contracts, so the whole loop speaks one media language.
  - **`url` = the evidence-store URL (PRIMARY)**, taken from `repro.json.media[]` / `verify.json.media[]`
    or the upload above. **`host_url`** is optional: only a provider that attaches files to the PR returns
    one. (If the upstream `media[]` is absent because no evidence store was configured, say so in the report.)
  - `in_body` marks which entries the PR **description** embeds (the two GIFs) vs. links (the two MP4s);
    `media_in_body:true` is the dict-level flag. **All four upload; the GIFs embed and the MP4s are
    `Full-quality video` links.**
  - **`ship.json` is NOT contract-validated.** `ship.json.media[]` is a deliberate *superset* of the
    `repro.json`/`verify.json` contract media item — it adds `host_url` and `in_body` on top of `{kind,url,label}`.
    Do **not** run `contracts/validate.py` on `ship.json` (there is no ship contract; the extra keys would trip
    the contracts' `additionalProperties:false`). The shared piece across all three is only the `kind` enum
    vocabulary and the `{kind,url,label}` core.

## Gotchas

- **Size limits** for PR attachments vary by host and are often undocumented. Keep recordings tight; if an
  upload is rejected for size, shrink the GIF (fewer frames, smaller viewport) and retry.
- A GIF showing a **static first frame** is the signal you're on a comment surface (wrong) or the capture
  was too long/low-quality — fix the surface (body) or re-record, don't ship it.
