# com.loops.qa-dream.job.sh — the per-job sidecar install.sh sources for this label.
#
# Sourced by install.sh AFTER it has rendered and read the plist, so $SRC (the rendered
# plist), $TEMPLATE, $SCRIPT, $JOB_PATH, $JOB_HOME, $LOOPS_DIR, $REPO, read_plist() and
# stack_cfg() are all available. Everything here is a fact about THIS JOB that cannot be read
# out of the plist. Anything the plist already states (paths, schedule, command line, log
# transcripts) belongs there, not here. See JOB-CONTRACT.md for the hook contract.
#
# Optional hooks: job_preflight() — extra checks, return non-zero to fail the preflight.
#                 job_epilogue()  — extra post-install notes.

# Where the dream drops its dated per-pass reports. Not derivable from the plist.
JOB_ARTIFACT_DIR="$LOOPS_DIR/dreams"

# What `launchctl kickstart` actually costs, said plainly at the moment it is offered.
JOB_KICKSTART_WARNING="this WRITES TO THE BRAIN"

:
