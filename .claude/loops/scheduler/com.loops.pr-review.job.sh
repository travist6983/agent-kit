# com.loops.pr-review.job.sh — the per-job sidecar install.sh sources for this label.
#
# Sourced by install.sh AFTER it has rendered and read the plist, so $SRC (the rendered
# plist), $TEMPLATE, $SCRIPT, $JOB_PATH, $JOB_HOME, $LOOPS_DIR, $REPO, read_plist(),
# job_command() and stack_cfg() are all available. Everything here is a fact about THIS JOB
# that cannot be read out of the plist; anything the plist already states (paths, schedule,
# command line, log transcripts) is read back from it. See JOB-CONTRACT.md.

JOB_ARTIFACT_DIR="$LOOPS_DIR/pr-reviews"

# `kickstart` on this label runs a real sweep and a real model call. What it does NOT do
# is post, unless --live is in ProgramArguments — which job_preflight states out loud.
JOB_KICKSTART_WARNING="a real sweep and a real model call; it posts only if --live is in ProgramArguments"

JOB_GATE_NOTE="This job reviews PULL REQUESTS. With --live in ProgramArguments its comments
carry the identity the vcs_host adapter resolves for the runner, so on a colleague's PR
they appear to come from that person. The template ships WITHOUT --live and WITHOUT a
--scope pair (dry-run, author scope 'own'); loop-limits.json records the same posture. The
plist's flags are the gate, not the JSON — going live is a deliberate edit to the tracked
template, after the hand-run gate in FIRST-RUN-pr-review.md."

# The pr-feed CLI this job's sweep runs. Named once, here, so every command this sidecar
# prints points at the same script the runner uses.
PR_FEED_CLI="$REPO/.claude/skills/dev-pr-feed/scripts/pr_feed.py"
ADAPTER_CLI="$REPO/lib/adapters/cli.py"

# ── extra preflight, all of it specific to sweeping the PR board ─────────────
job_preflight() {
  local rc=0 app_root wt_root wt_parent provider probe probe_rc limits live file_scope dry_n cmd eff_scope eff_limit ledger dry

  # pr_review.sh exits 6 when LOOP_APP_ROOT is not a git checkout, because every `checkout`
  # that tick would fail and the model call would be wasted. The job inherits nothing from a
  # shell, so resolve it exactly as the runner will: the plist if it sets it, otherwise the
  # same derivation pr_review.sh and pr_feed.py use — the vcs_host provider's repo_dir, else
  # stack.config app.repo_dir, relative paths resolved against this repo.
  app_root="$(read_plist 'EnvironmentVariables:LOOP_APP_ROOT')"
  [ -n "$app_root" ] || app_root="$(stack_cfg adapters.vcs_host.config.repo_dir "")"
  [ -n "$app_root" ] || app_root="$(stack_cfg app.repo_dir "")"
  case "$app_root" in
    '')  ;;
    /*)  ;;
    *)   app_root="$(cd "$REPO" && cd "$app_root" 2>/dev/null && pwd || printf '%s/%s' "$REPO" "$app_root")" ;;
  esac
  if [ -n "$app_root" ] && PATH="$JOB_PATH" git -C "$app_root" rev-parse --git-dir >/dev/null 2>&1; then
    ok "LOOP_APP_ROOT is a git checkout: $app_root"
  else
    bad "LOOP_APP_ROOT is NOT a git checkout: ${app_root:-<unset: set app.repo_dir in stack.config or LOOP_APP_ROOT in the plist>} — every checkout would fail (pr_review.sh exit 6)"
    rc=1
  fi

  # Review worktrees are created beside the target checkout, never inside it. pr_feed
  # mkdir -p's the leaf, so the parent is what has to exist and be writable. Derived the
  # same way the runner derives it: the plist, else integrations.pr_review.worktree_root,
  # else a sibling of the checkout named <project.name>-review-worktrees.
  wt_root="$(read_plist 'EnvironmentVariables:LOOP_PR_WORKTREE_ROOT')"
  [ -n "$wt_root" ] || wt_root="$(stack_cfg integrations.pr_review.worktree_root "")"
  if [ -z "$wt_root" ] && [ -n "$app_root" ]; then
    local n; n="$(stack_cfg project.name "")"; [ -n "$n" ] || n="$(basename "$app_root")"
    wt_root="$(dirname "$app_root")/$n-review-worktrees"
  fi
  if [ -n "$wt_root" ]; then
    wt_parent="$wt_root"
    while [ ! -d "$wt_parent" ] && [ "$wt_parent" != "/" ]; do wt_parent="$(dirname "$wt_parent")"; done
    if [ -w "$wt_parent" ]; then
      ok "worktree root is writable: $wt_root"
    else
      bad "cannot write review worktrees at $wt_root (nearest existing ancestor $wt_parent is not writable)"
      rc=1
    fi
  fi

  # Finding the binaries on the PATH still does not prove the VCS host will answer. Ask the
  # vcs_host adapter for the cheapest thing it knows — under the job's PATH and HOME, which is
  # where the credential cache a provider reads has to be. Exit 3 is the adapter saying the
  # role is absent (provider none); anything else non-zero is the provider's own failure,
  # which on a tick is pr_feed's `auth-expired` / `api-unavailable` class.
  provider="$(stack_cfg adapters.vcs_host.provider "none")"
  probe="$(PATH="$JOB_PATH" HOME="$JOB_HOME" python3 "$ADAPTER_CLI" vcs_host default_branch 2>/dev/null)"; probe_rc=$?
  case "$probe_rc" in
    0) ok "vcs_host adapter answers (provider $provider): default branch $(printf '%s' "$probe" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("result"))' 2>/dev/null)" ;;
    3) bad "vcs_host adapter is UNAVAILABLE (provider $provider) — every tick would be blocked adapter-unavailable:vcs_host; configure adapters.vcs_host in stack.config"
       rc=1 ;;
    *) bad "vcs_host adapter (provider $provider) FAILED under the job PATH+HOME (rc $probe_rc) — a tick would fail auth-expired / api-unavailable: $(printf '%s' "$probe" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error"))' 2>/dev/null)"
       say "        Re-authenticate the provider's credential the way its docs say (adapters.vcs_host.config.reauth_hint, if set: $(stack_cfg adapters.vcs_host.config.reauth_hint "not set"))."
       rc=1 ;;
  esac

  # prs_per_tick and author_scope are decisions that live in the tracked JSON. pr_review.sh
  # still runs without it, on its own literals, and says so on stderr — on an unattended
  # tick in the middle of the night nobody reads that. Prove the file answers now instead.
  limits="$(PATH="$JOB_PATH" python3 - "$LOOPS_DIR/loop-limits.json" 2>/dev/null <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))["pr_review_loop"]
n, s = int(o["prs_per_tick"]), str(o["author_scope"])
if n < 1 or s not in ("own", "all", "others"):
    raise SystemExit(1)
print("prs_per_tick=%d author_scope=%s posting_default=%s" % (n, s, o.get("posting_default", "?")))
PY
)"
  if [ -n "$limits" ]; then
    ok "loop-limits.json answers: $limits"
  else
    bad "loop-limits.json has no readable pr_review_loop — the tick would fall back to pr_review.sh's literals"
    rc=1
  fi

  # ...BUT the file is not the last word. pr_review.sh lets --scope / --limit in
  # ProgramArguments OVERRIDE it. Derive the EFFECTIVE values from the plist and say so when
  # they differ from the tracked decision.
  cmd="$(job_command "$SRC")"
  eff_scope="$(printf '%s\n' "$cmd" | tr ' ' '\n' | grep -A1 -x -- '--scope' | tail -1)"
  case "$eff_scope" in own|all|others) : ;; *) eff_scope="" ;; esac
  eff_limit="$(printf '%s\n' "$cmd" | tr ' ' '\n' | grep -A1 -x -- '--limit' | tail -1)"
  case "$eff_limit" in ''|*[!0-9]*) eff_limit="" ;; esac
  if [ -n "$eff_scope" ] || [ -n "$eff_limit" ]; then
    warn "ProgramArguments OVERRIDE loop-limits.json for this job: effective author_scope=${eff_scope:-<file>} prs_per_tick=${eff_limit:-<file>}"
    say "        The line above is the tracked DECISION; these are what the armed job will use."
    [ "${eff_scope:-own}" = "own" ] || say "        author_scope '${eff_scope}' means this job can comment on OTHER PEOPLE'S pull requests."
    # The flag is REQUIRED for a live wide scope (pr_review.sh exit 25 otherwise), so under
    # that posture this line always fires. Say whether it agrees with the tracked decision —
    # agreement is the intended shape, disagreement is the thing to stop and read.
    file_scope="$(printf '%s\n' "$limits" | sed -n 's/.*author_scope=\([a-z]*\).*/\1/p')"
    if [ -n "$eff_scope" ] && [ -n "$file_scope" ]; then
      if [ "$eff_scope" = "$file_scope" ]; then
        say "        The --scope flag MATCHES the file (both '$eff_scope') — the re-affirmation a live wide scope requires."
      else
        warn "the --scope flag ('$eff_scope') DISAGREES with loop-limits.json ('$file_scope') — the flag wins; fix one of them"
      fi
    fi
  else
    ok "ProgramArguments add no --scope/--limit override — the file's numbers are the effective ones"
  fi

  # The posting posture is an ABSENCE in ProgramArguments, and an absence is invisible.
  # Say which way it is armed, in the preflight, where the operator is looking.
  live="$(printf '%s\n' "$cmd" | grep -c -- '--live' 2>/dev/null || true)"
  case "$live" in ''|*[!0-9]*) live=0 ;; esac
  if [ "$live" -eq 0 ]; then
    ok "posting posture: DRY-RUN (no --live in ProgramArguments) — reviews are written, nothing is posted"
  else
    warn "posting posture: LIVE (--live IS in ProgramArguments) — this job WILL post comment threads onto pull requests under the runner's vcs_host identity"
    say "        (LOOP_PR_IDENTITY if set, else what the provider reports; pr_review.sh prints it in the live banner)"
    if [ "${eff_scope:-own}" != "own" ]; then
      warn "LIVE + author_scope '$eff_scope' — every hour, on COLLEAGUES' pull requests, under that identity."
      say "        pr_review.sh allows it only because --scope is on the command line: an inherited"
      say "        widening from loop-limits.json is refused (exit 25). This one is explicit, so it"
      say "        will run. Be certain that is what you want before typing YES."
    fi
  fi

  # The seen-ledger, and specifically how much of it is DRY-RUN. An entry written by a
  # dry-run tick is indistinguishable to `unreviewed` from a delivered review, so the first
  # LIVE tick after a dry-run soak reports a healthy empty queue and delivers nothing. The
  # count is the whole warning.
  ledger="$(read_plist 'EnvironmentVariables:LOOP_PR_LEDGER_FILE')"
  [ -n "$ledger" ] || ledger="$LOOPS_DIR/pr-review-ledger.json"
  if [ -f "$ledger" ]; then
    dry="$(PATH="$JOB_PATH" python3 - "$ledger" 2>/dev/null <<'PY'
import json, sys
try:
    o = json.load(open(sys.argv[1])) or {}
except Exception:
    print("?"); raise SystemExit
print("%d of %d" % (len([v for v in o.values() if isinstance(v, dict) and v.get("mode") == "dry-run"]), len(o)))
PY
)"
    dry_n="${dry%% *}"
    case "$dry_n" in ''|*[!0-9]*) dry_n="?" ;; esac
    if [ "$live" -ne 0 ] && [ "$dry_n" = "0" ]; then
      ok "seen-ledger: $dry entries are mode=dry-run — no dry-run review is shadowing a live delivery"
    elif [ "$live" -ne 0 ]; then
      warn "seen-ledger: $dry entries are mode=dry-run — a LIVE tick SKIPS every one of them"
      say "        Their findings only ever reached a preview file. Re-queue the ones you want"
      say "        delivered, one at a time, BEFORE the first live tick:"
      say "            python3 $PR_FEED_CLI ledger --forget <pr-id>"
    else
      ok "seen-ledger present: $dry entries are mode=dry-run (re-queue them before going live)"
    fi
  else
    if [ "$live" -ne 0 ]; then
      bad "NO seen-ledger at $ledger, and this job is armed LIVE — pr_review.sh will REFUSE the tick (exit 25)"
      say "        An absent ledger reads as 'nothing has ever been reviewed', so the first live"
      say "        tick would re-comment on pull requests that already carry loop threads."
      say "        Seed it (per PR) or pass --first-run deliberately:"
      say "            python3 $PR_FEED_CLI mark --pr <id> --commit <sha> --mode live"
      rc=1
    else
      warn "no seen-ledger yet at $ledger — normal before the first tick; it is created on the first \`mark\`"
    fi
  fi

  return "$rc"
}

# ── extra post-install notes ─────────────────────────────────────────────────
job_epilogue() {
  cat <<EOF

See what the sweep has reviewed, and make it review something again:
    python3 $PR_FEED_CLI unreviewed --limit 5
    python3 $PR_FEED_CLI ledger

Read one tick without running one (no lock, no model call, no posting):
    bash $SCRIPT --dry-run

Promote dry-run to LIVE posting (a deliberate human edit, in this order):
    1. RE-QUEUE the PRs whose findings never reached anyone. Every dry-run tick wrote a
       ledger entry that \`unreviewed\` cannot tell from a delivered review, so without this
       step the first live tick reports a healthy empty queue and delivers nothing:
           python3 $PR_FEED_CLI ledger                     # see the modes
           python3 $PR_FEED_CLI ledger --forget <pr-id>
       One at a time. Do NOT delete the ledger — it is the only thing stopping the loop
       commenting on the same revision every hour.
    2. add <string>--live</string> to ProgramArguments in the TRACKED template:
           $TEMPLATE
       never in ~/Library/LaunchAgents — that copy is re-rendered on every install, and
       until it is, --check will tell you the two disagree. Set loop-limits.json
       pr_review_loop.posting_default to "live" in the same edit so the record matches.
    3. prove it by hand once, watching it, before any timer does it unattended:
           bash $SCRIPT --live --limit 1
    4. bash $HERE/install.sh --label $LABEL --check     # posture line flipped; ledger counted
    5. bash $HERE/install.sh --label $LABEL --yes       # bootout + re-bootstrap

What a live tick REFUSES to do (exit 25), so you find out here rather than on an
unattended tick with nobody watching:
    * run at all with no seen-ledger file, unless you pass --first-run on purpose;
    * inherit an author_scope other than 'own' from loop-limits.json — widening it must be
      re-affirmed with --scope on the command line (i.e. in ProgramArguments).
EOF
}

:
