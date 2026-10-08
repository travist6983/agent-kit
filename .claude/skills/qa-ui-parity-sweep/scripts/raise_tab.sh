#!/usr/bin/env bash
# raise_tab.sh <title> — bring the Chrome tab whose title is exactly <title> to the front.
#
# A tab in a window behind other windows reports document.visibilityState 'hidden', and Chrome then
# freezes requestAnimationFrame and clamps timers: the app's transitions never finish and every
# measurement is fiction. Screenshots do NOT raise it, and `tell application "Google Chrome" to
# activate` raises only window 1, so find the tab by a unique title (set it first:
# document.title = '<title>') and raise ITS window.
#
# macOS-only helper (AppleScript + the IOKit console-lock flag). On any other platform it exits 1
# with reason "unsupported-platform": raise the window by hand and re-check P.status().
# Exit 0 raised · 1 no such tab / unsupported platform · 2 the screen is locked (nothing can be raised).
# The app resets document.title on navigation: stamp the title again before each raise. If another
# session launched a HEADLESS Chrome, `tell application "Google Chrome"` can bind to it and find no
# windows (exit 1) — then raise the operator's Chrome by PID via System Events instead.
set -uo pipefail
TITLE="${1:?usage: raise_tab.sh <exact tab title>}"
if [ "$(uname -s)" != "Darwin" ]; then
  echo '{"raised": false, "reason": "unsupported-platform"}'; exit 1
fi
if ioreg -n Root -d1 -a 2>/dev/null | plutil -extract IOConsoleUsers xml1 -o - - 2>/dev/null | grep -A1 CGSSessionScreenIsLocked | grep -q '<true/>'; then
  echo '{"raised": false, "reason": "screen-locked"}'; exit 2
fi
R="$(osascript - "$TITLE" <<'OSA' 2>&1
on run argv
  set wanted to item 1 of argv
  tell application "Google Chrome"
    repeat with w from 1 to count of windows
      repeat with t from 1 to count of tabs of window w
        if (title of tab t of window w) is wanted then
          set active tab index of window w to t
          set index of window w to 1
          activate
          return "raised"
        end if
      end repeat
    end repeat
  end tell
  return "not-found"
end run
OSA
)"
if [ "$R" = "raised" ]; then echo '{"raised": true}'; exit 0; fi
printf '{"raised": false, "reason": "%s"}\n' "$(printf '%s' "$R" | tr '"\n' "' " | cut -c1-200)"; exit 1
