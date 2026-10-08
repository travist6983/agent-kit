#!/usr/bin/env python3
"""
pr_feed.py — the pull-request surface for the loops, on the `vcs_host` adapter.

This is the deterministic half of `dev-pr-feed`. Every verb here is mechanical: it talks to the
VCS host (through the adapter layer in `lib/adapters`), to git, and to the seen-ledger, and it
returns JSON. It never judges code — that is `dev-code-review`'s job — and it never decides
whether to retry, which belongs to the recipe.

Design notes worth knowing before you change anything:

* **No vendor code.** Every host call is one of the `vcs_host` role's operations (`list_prs`,
  `get_pr`, `get_pr_diff`, `post_review_comment`, `list_review_threads`) and the optional
  `ci_cd.build_status` for the `health` verb. Which provider answers is `stack.config.json`'s
  business. An absent provider is a structured `blocked`, never a crash and never a guess.
* **Stdlib + git + the adapter package.** No third-party imports, so the script runs even when
  the brain's Postgres driver isn't installed.
* **Every failure carries a CLASS**, not just a message. The recipe routes on the class, and a
  retry that repeats the same action is not improvement. See FAILURE_CLASSES.
* **Fail closed on the ledger.** If we cannot read the ledger we do NOT fall back to "review
  everything" — that would spam real colleagues' pull requests with duplicate comments.

Verbs: list · unreviewed · health · checkout · post · mark · teardown · ledger · lock
Every candidate-filtering verb takes `--base` (default: LOOP_PR_TARGET, else project.default_branch).
Exit codes: 0 ok · 1 usage · 2 recoverable failure (class in JSON) · 3 fail-closed refusal
            4 tick lock busy
"""

import argparse
import datetime
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys

# The framework checkout: <root>/.claude/skills/dev-pr-feed/scripts/pr_feed.py
FRAMEWORK_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
if FRAMEWORK_ROOT not in sys.path:
    sys.path.insert(0, FRAMEWORK_ROOT)

_ADAPTER_IMPORT_ERROR = None
try:
    from lib.adapters import AdapterError, AdapterUnavailable, get as adapter_get, load_config
except Exception as _e:  # surfaced as config-invalid JSON from main(), never a bare traceback
    _ADAPTER_IMPORT_ERROR = _e
    AdapterError = AdapterUnavailable = None  # type: ignore[assignment]

# --- Configuration -------------------------------------------------------------------------
# Nothing here names a host, an organisation or a machine. Values come from stack.config.json
# (through the adapter package) or from the LOOP_* environment the runner exports; the skill
# stays runner-agnostic — a pipeline, the CLI, or any other runner passes its own.

# Whose PRs the feed returns. "own" is the default because a scheduled loop posts under the
# runner's OWN VCS-host identity — comments on a colleague's PR appear to come from that
# person, which is a decision a human makes deliberately, not a default a cron job inherits.
#   own        — only PRs authored by the configured identity
#   all        — every candidate (colleagues included)
#   others     — everyone EXCEPT the configured identity
AUTHOR_SCOPE = os.environ.get("LOOP_PR_AUTHOR_SCOPE", "own")

# PRs the defect/feature loops opened carry the loop label (infra-ship adds it at create time).
# The review QUEUE skips them: the loop already reviewed its own diff (dev-code-review on the hard
# recipes), and its PR is held to infra-ship's thread bar — at most defect_pr_threads.max_per_pr
# threads, usually none. A review tick stacking up to max_threads_per_pr more on it is noise.
# Under the default author_scope "own" every published loop PR would otherwise be in this queue
# (same host identity). Explicit verbs (checkout/post/mark --pr N) still work on them — a human
# asking for a review of one gets it. LOOP_PR_INCLUDE_LOOP_PRS=1 puts them back in the queue.
LOOP_PR_LABEL_DEFAULT = "agent-loop"
INCLUDE_LOOP_PRS = os.environ.get("LOOP_PR_INCLUDE_LOOP_PRS") == "1"

# The git remote the target checkout fetches PR refs from. LOOP_PR_REMOTE, else
# adapters.vcs_host.config.remote, else git's conventional name. Never assumed when a checkout
# names its host remote differently (`upstream`, `github`, …).
REMOTE_DEFAULT = "origin"

# `--base` on a verb, when given. Set once in main(); target_branch() prefers it.
_BASE_OVERRIDE = None

# The ledger. See references/seen-ledger.md for why the default backend is a file.
LEDGER_BACKEND = os.environ.get("LOOP_PR_LEDGER_BACKEND", "file")  # file | kb
LEDGER_FILE = os.environ.get(
    "LOOP_PR_LEDGER_FILE",
    os.path.join(FRAMEWORK_ROOT, ".claude", "loops", "pr-review-ledger.json"),
)
KB_CLI = os.environ.get(
    "KB_CLI",
    os.path.join(FRAMEWORK_ROOT, ".claude", "skills", "qa-kb", "scripts", "kb.py"),
)

# Stamped into every comment we post, and the way we recognise our own comments on a later tick.
BOT_MARKER = "Posted by the PR-review loop (`dev-pr-feed`)"

# The local ref namespace a review checkout fetches into (never a branch the developer uses).
REF_NAMESPACE = "refs/loops/pr-"

# How long we are willing to wait to read the R9 kill switch before failing closed.
PAUSE_TIMEOUT = int(os.environ.get("LOOP_PAUSE_TIMEOUT", "20"))

FAILURE_CLASSES = (
    "auth-expired",       # the host credential is dead — a human re-auths; retrying won't help
    "api-unavailable",    # host 5xx or network — a later tick may succeed
    "ledger-unreadable",  # brain/file unreadable — FAIL CLOSED, never re-review everything
    "checkout-failed",    # fetch or worktree add failed
    "stale-iteration",    # the PR moved under us mid-run; abandon rather than mis-anchor
    "post-rejected",      # 403 / comment API refusal
    "paused",             # R9 global pause is on
    "pr-closed",          # the PR merged/closed mid-run — drop it, never retry
    "config-invalid",     # LOOP_PR_AUTHOR_SCOPE / LOOP_PR_LEDGER_BACKEND is not a value this
                          # script knows, or a required adapter/config value is absent — refuse
                          # rather than silently widen scope, switch store or guess a repo
    "already-posted",     # the ledger says this PR was already posted LIVE at its current head —
                          # a second live post would duplicate comments on a real person's PR
)

# Severity order for the per-PR volume cap: the most severe findings are the ones that post.
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def env_int(name):
    """A positive int from the environment, or 0 (= not set / not valid). Read at CALL time, never
    at import: an import-time int() on a bad value kills every verb with a traceback and no JSON."""
    try:
        v = int((os.environ.get(name) or "0").strip())
    except ValueError:
        return 0
    return v if v > 0 else 0


# --- Plumbing ------------------------------------------------------------------------------

def out(payload, code=0):
    """Single exit point so every caller gets the same JSON envelope."""
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    sys.exit(code)


def fail(failure_class, message, code=2, **extra):
    assert failure_class in FAILURE_CLASSES, f"unknown failure class {failure_class}"
    out({"status": "failed", "failure_class": failure_class, "message": message, **extra}, code)


def fail_blocked(role, op, reason):
    """An adapter role is absent (`provider: none`) or cannot serve this op. The spec's shape:
    a structured `blocked` whose reason is exactly `adapter-unavailable:<role>`. The class is
    config-invalid because the fix is in stack.config.json — never a retry."""
    out({"status": "blocked", "failure_class": "config-invalid",
         "reason": f"adapter-unavailable:{role}", "role": role, "op": op,
         "message": f"the {role} adapter cannot serve `{op}`: {reason}. Configure a provider for "
                    f"adapters.{role} in .claude/loops/stack.config.json."}, 3)


def run(cmd, cwd=None, check=True, timeout=180):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"`{shlex.join(cmd)}` exited {p.returncode}: {(p.stderr or p.stdout).strip()[:600]}")
    return p


def same_commit(a, b):
    """True when two SHAs name the same commit, tolerating abbreviated forms (git's own rule)."""
    if not a or not b:
        return a == b
    a, b = str(a).strip().lower(), str(b).strip().lower()
    n = min(len(a), len(b))
    if n < 7:          # shorter than git's own minimum abbreviation — refuse to guess
        return a == b
    return a[:n] == b[:n]


def run_bounded(cmd, timeout):
    """
    Run a command under a HARD wall-clock bound; return None if it overran.

    subprocess.run(timeout=...) is not enough here. The brain CLI may shell out to an external
    auth helper (a cloud CLI minting a token, say), and that grandchild inherits the captured
    pipes — so after the timeout kills the direct child, the pipe stays open and cleanup blocks
    anyway. A nominal bound can take several times longer than it promises. Starting a new
    session and killing the whole process group is what actually bounds it.

    This matters because the thing we use it for is the R9 kill switch. A kill switch you have to
    wait ten minutes to read is not a kill switch.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out_, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, out_, err)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        return None


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def slug(pr_id):
    """A PR id as a path/ref-safe token. Ids are opaque strings: a number on one host, a branch
    name on the git-local provider."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(pr_id)).strip("-") or "pr"


# --- Stack config + adapters ---------------------------------------------------------------

_STACK = None


def stack():
    """The loaded stack.config.json, or a config-invalid failure. Cached per process."""
    global _STACK
    if _STACK is None:
        if _ADAPTER_IMPORT_ERROR is not None:
            fail("config-invalid", f"could not import lib.adapters from {FRAMEWORK_ROOT}: "
                                   f"{type(_ADAPTER_IMPORT_ERROR).__name__}: {_ADAPTER_IMPORT_ERROR}", code=3)
        try:
            _STACK = load_config()
        except (FileNotFoundError, ValueError) as e:
            fail("config-invalid", str(e), code=3)
    return _STACK


def cfg(dotted, default=None):
    return stack().get(dotted, default)


def adapter(role):
    """The adapter for a role. A `none` provider surfaces on the first op, not here."""
    try:
        return adapter_get(role)
    except AdapterUnavailable as e:
        fail_blocked(role, "(construct)", e.reason)
    except AdapterError as e:
        fail("config-invalid", f"adapters.{role} could not be built: {e.message}", code=3)


class HostError(Exception):
    """A VCS-host call that failed, WITHOUT exiting.

    `host()` turns this straight into fail() for every ordinary caller. The one caller that must
    not exit on it is the live POST loop: a failure on comment k>1 used to exit status=failed
    with no thread_ids at all, so the comments that HAD landed were recorded nowhere. `not_sent`
    says whether the request provably never took effect — only then may a post be treated as
    "nothing reached the PR"."""

    def __init__(self, failure_class, message, http_status=None, not_sent=False):
        super().__init__(message)
        self.failure_class = failure_class
        self.message = message
        self.http_status = http_status
        # A 4xx is the server REFUSING the request (400/401/403/404/409/429 …) — nothing was
        # created. A 5xx or a timeout is ambiguous: a gateway can time out after the comment was
        # written. DNS failure / connection refused never sent a byte.
        self.not_sent = bool(not_sent or (http_status is not None and 400 <= http_status < 500))


_AUTH_MARKERS = ("unauthorized", "unauthenticated", "forbidden", "not authorized", "expired",
                 "credential", "token", "login", "permission denied", "401", "403")
_NEVER_SENT_MARKERS = ("connection refused", "name or service not known", "nodename nor servname",
                       "temporary failure in name resolution", "no route to host", "not set")


def _http_status(details):
    for k in ("http_status", "status_code", "status", "code"):
        v = details.get(k)
        if isinstance(v, int) and 100 <= v < 600:
            return v
    return None


def classify_host_error(e, op):
    """Map an adapter failure onto a failure class. The provider's own message is the evidence;
    the classification is generic — any host's 401/403 means the same thing."""
    details = getattr(e, "details", {}) or {}
    status = _http_status(details)
    low = (getattr(e, "message", None) or str(e) or "").lower()
    not_sent = bool(details.get("not_sent")) or any(m in low for m in _NEVER_SENT_MARKERS)
    if status in (401, 403) or any(m in low for m in _AUTH_MARKERS):
        cls = "post-rejected" if op == "post_review_comment" else "auth-expired"
        return HostError(cls, f"the VCS host refused `{op}`: {str(e)[:400]}", http_status=status, not_sent=True)
    return HostError("api-unavailable", f"`{op}` failed on the VCS host: {str(e)[:400]}",
                     http_status=status, not_sent=not_sent)


def _host(op, *args, **kwargs):
    """Call one vcs_host operation. Raises HostError; exits `blocked` on an absent provider."""
    fn = getattr(adapter("vcs_host"), op, None)
    if fn is None:
        fail_blocked("vcs_host", op, "the configured provider does not implement it")
    try:
        return fn(*args, **kwargs)
    except AdapterUnavailable as e:
        fail_blocked("vcs_host", op, e.reason)
    except AdapterError as e:
        raise classify_host_error(e, op)
    except subprocess.TimeoutExpired:
        raise HostError("api-unavailable", f"`{op}` timed out on the VCS host")
    except Exception as e:  # a provider bug is the host being unavailable, not our traceback
        raise HostError("api-unavailable", f"`{op}` failed mid-flight: {type(e).__name__}: {e}")


def host(op, *args, **kwargs):
    """_host(), exiting with the failure's class on any error — what every ordinary verb wants."""
    try:
        return _host(op, *args, **kwargs)
    except HostError as e:
        fail(e.failure_class, e.message, http_status=e.http_status)


# --- The target repo (git) -----------------------------------------------------------------

def app_root(required=True):
    """The target application's git checkout: LOOP_APP_ROOT, else the vcs_host provider's
    `repo_dir`, else `app.repo_dir` from stack.config. Never a baked-in path."""
    raw = os.environ.get("LOOP_APP_ROOT")
    if raw:
        path = os.path.abspath(os.path.expanduser(raw))
    else:
        st = stack()
        p = st.resolve(st.get("adapters.vcs_host.config.repo_dir")) or st.repo_dir()
        path = str(p) if p else None
    if path and os.path.isdir(os.path.join(path, ".git")):
        return path
    if not required:
        return None
    fail("checkout-failed" if path else "config-invalid",
         (f"{path} is not a git checkout" if path else "no target repo configured")
         + " — set app.repo_dir in stack.config.json (or LOOP_APP_ROOT)", code=2 if path else 3)


def target_branch():
    """The branch PRs must target: `--base` on the verb, else LOOP_PR_TARGET, else
    project.default_branch, else what the vcs_host adapter reports. Never a literal."""
    raw = (_BASE_OVERRIDE or os.environ.get("LOOP_PR_TARGET") or "").strip()
    if raw:
        return raw.replace("refs/heads/", "")
    configured = cfg("project.default_branch")
    if configured:
        return str(configured)
    return str(host("default_branch"))


def worktree_root():
    """Where read-only review worktrees land. Deliberately OUTSIDE the target repo checkout so a
    stray `git clean` or IDE indexer in the developer's tree never touches them, and vice versa.
    LOOP_PR_WORKTREE_ROOT, else integrations.pr_review.worktree_root, else a sibling directory of
    the target repo named after the project (the same shape the sandbox adapter uses)."""
    raw = os.environ.get("LOOP_PR_WORKTREE_ROOT") or cfg("integrations.pr_review.worktree_root")
    if raw:
        return os.path.abspath(os.path.expanduser(str(raw)))
    root = app_root()
    name = cfg("project.name") or os.path.basename(root)
    return os.path.join(os.path.dirname(root), f"{name}-review-worktrees")


def loop_pr_label():
    """LOOP_PR_LOOP_LABEL, else integrations.pr_review.loop_pr_label, else the default infra-ship
    stamps on every loop-opened PR. Compared lower-cased."""
    raw = (os.environ.get("LOOP_PR_LOOP_LABEL") or "").strip() \
        or cfg("integrations.pr_review.loop_pr_label", LOOP_PR_LABEL_DEFAULT) or LOOP_PR_LABEL_DEFAULT
    return str(raw).strip().lower()


def remote_name():
    """The remote PR refs are fetched from: LOOP_PR_REMOTE, else adapters.vcs_host.config.remote,
    else git's conventional name."""
    raw = (os.environ.get("LOOP_PR_REMOTE") or "").strip() or cfg("adapters.vcs_host.config.remote")
    return str(raw).strip() if raw and str(raw).strip() else REMOTE_DEFAULT


_GIT_CACHE = {}


def _git(args, cwd, timeout=180):
    try:
        return run(["git"] + args, cwd=cwd, check=False, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def has_remote(root):
    """True when the target checkout has the configured remote (see remote_name())."""
    key = ("remote", root)
    if key not in _GIT_CACHE:
        p = _git(["remote", "get-url", remote_name()], root)
        _GIT_CACHE[key] = bool(p and p.returncode == 0)
    return _GIT_CACHE[key]


def remote_heads(root):
    """{branch: sha} from one `git ls-remote --heads <remote>`, cached per process. One network
    round trip for the whole candidate list rather than one per PR."""
    key = ("heads", root)
    if key not in _GIT_CACHE:
        heads = {}
        if has_remote(root):
            p = _git(["ls-remote", "--heads", remote_name()], root, timeout=120)
            if p and p.returncode == 0:
                for line in p.stdout.splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                        heads[parts[1][len("refs/heads/"):]] = parts[0]
        _GIT_CACHE[key] = heads
    return _GIT_CACHE[key]


# The keys the feed honours in a provider's `raw` payload, flat or dotted (nested). A provider
# that surfaces any of these saves a git round trip; one that surfaces none still works, because
# git answers from the target checkout. Documented in SKILL.md under "What the feed reads from
# a provider's raw payload" — keep the two lists in step.
RAW_KEYS = {
    "head_commit": ("head_commit", "head_sha", "sha", "head.sha", "source.sha", "lastMergeSourceCommit.commitId"),
    "merge_base_commit": ("base_commit", "merge_base_commit", "merge_base", "merge_base_sha", "base.sha"),
    "merge_status": ("merge_status", "mergeable_state", "mergeStatus", "detailed_merge_status"),
    "merge_ref": ("merge_ref", "head_ref"),
}


def raw_get(raw, dotted):
    """A value from a raw payload by flat key or dotted path; None when any hop is missing."""
    cur = raw
    for part in str(dotted).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def raw_str(raw, field):
    """The first non-empty string among RAW_KEYS[field] in `raw`, or None."""
    raw = raw if isinstance(raw, dict) else {}
    for k in RAW_KEYS[field]:
        v = raw_get(raw, k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def resolve_head(branch, raw=None):
    """The PR's head commit. A provider that knows it says so in `raw` (RAW_KEYS["head_commit"]);
    otherwise git answers — the remote's branch tip when there is a remote, else the local one."""
    v = raw_str(raw, "head_commit")
    if v:
        return v
    if not branch:
        return None
    root = app_root(required=False)
    if not root:
        return None
    sha = remote_heads(root).get(branch)
    if sha:
        return sha
    p = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], root)
    return p.stdout.strip() if p and p.returncode == 0 and p.stdout.strip() else None


def base_ref(root, base):
    """The target branch tip as git knows it: <remote>/<base> when fetched, else <base>."""
    if not base:
        return None
    for cand in (f"refs/remotes/{remote_name()}/{base}", f"refs/heads/{base}"):
        p = _git(["rev-parse", "--verify", "--quiet", cand], root)
        if p and p.returncode == 0:
            return cand
    return None


def local_commit(root, sha):
    if not sha:
        return False
    p = _git(["cat-file", "-e", f"{sha}^{{commit}}"], root)
    return bool(p and p.returncode == 0)


def resolve_merge_base(base, head, raw=None):
    v = raw_str(raw, "merge_base_commit")
    if v:
        return v
    root = app_root(required=False)
    if not root or not head or not local_commit(root, head):
        return None
    ref = base_ref(root, base)
    if not ref:
        return None
    p = _git(["merge-base", ref, head], root)
    return p.stdout.strip() if p and p.returncode == 0 and p.stdout.strip() else None


def local_merge_status(base, head):
    """mergeable | conflicts | None (unknown) — computed with `git merge-tree` when both sides are
    local commits, so the health sweep needs no host-specific merge field."""
    root = app_root(required=False)
    if not root or not local_commit(root, head):
        return None
    ref = base_ref(root, base)
    if not ref:
        return None
    p = _git(["merge-tree", "--write-tree", "--quiet", ref, head], root)
    if p is None or p.returncode not in (0, 1):
        return None
    return "mergeable" if p.returncode == 0 else "conflicts"


def normalize_merge_status(raw):
    """A provider may report merge status under a few names (RAW_KEYS["merge_status"], or a
    boolean `mergeable`); collapse them to the enum. Unknown values are None, never a problem."""
    raw = raw if isinstance(raw, dict) else {}
    v = raw_str(raw, "merge_status")
    if v is None and "mergeable" in raw:
        m = raw.get("mergeable")
        v = "mergeable" if m is True else ("conflicts" if m is False else None)
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("conflicts", "conflict", "conflicting", "dirty", "cannot_be_merged"):
            return "conflicts"
        if low in ("mergeable", "clean", "succeeded", "can_be_merged", "unstable", "has_hooks"):
            return "mergeable"
    return None


# --- The seen-ledger -----------------------------------------------------------------------
# Keyed on PR id + HEAD COMMIT, never the PR id alone. If we keyed on the id, the loop would go
# permanently silent the moment an author pushed a fix in response to our findings — the single
# most likely way this whole thing quietly stops being useful.

def ledger_key(pr_id):
    return f"loops.pr_review.{pr_id}"


def _file_path():
    return os.path.abspath(LEDGER_FILE)


def _lock_path():
    return os.path.join(os.path.dirname(_file_path()), "pr-review-tick.lock")


def ledger_read_all():
    """Return {pr_id(str): entry}. Raises so callers can fail CLOSED rather than guess."""
    if LEDGER_BACKEND == "file":
        path = _file_path()
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            content = f.read().strip()
        if not content:
            return {}
        data = json.loads(content)
        # `[]`, `null`, `0` and `false` are falsy, and every caller's `or {}` turned them into an
        # empty ledger: every PR then read as never-reviewed — duplicate comments, rc 0. Refuse.
        if not isinstance(data, dict):
            raise RuntimeError(f"ledger top level is {type(data).__name__}, not an object")
        return data
    if LEDGER_BACKEND == "kb":
        # kb has no "list by prefix" verb, so the kb backend reads per-PR in ledger_get().
        # A full sweep isn't available; unreviewed() handles that by querying each candidate.
        return None
    raise RuntimeError(f"unknown ledger backend {LEDGER_BACKEND!r}")


def ledger_get(pr_id):
    if LEDGER_BACKEND == "file":
        return (ledger_read_all() or {}).get(str(pr_id))
    p = run([sys.executable, KB_CLI, "state", "--key", ledger_key(pr_id)], check=False)
    if p.returncode != 0:
        raise RuntimeError(f"kb state read failed: {(p.stderr or p.stdout).strip()[:300]}")
    try:
        rec = json.loads(p.stdout or "null")
    except json.JSONDecodeError:
        raise RuntimeError("kb state returned non-JSON")
    if not rec:
        return None
    value = rec.get("value") if isinstance(rec, dict) else None
    return json.loads(value) if isinstance(value, str) else value


def ledger_put(pr_id, entry):
    if LEDGER_BACKEND == "file":
        path = _file_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        current = ledger_read_all() or {}
        current[str(pr_id)] = entry
        # Write-then-rename: a crash mid-write leaves the old ledger intact rather than a
        # truncated file. A half-written ledger is indistinguishable from "never reviewed",
        # which means duplicate comments on someone's PR.
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(current, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return
    p = run([sys.executable, KB_CLI, "state", "--key", ledger_key(pr_id),
             "--set", json.dumps(entry), "--source", "dev-pr-feed"], check=False)
    if p.returncode != 0:
        raise RuntimeError(f"kb state write failed: {(p.stderr or p.stdout).strip()[:300]}")


def ledger_forget(pr_id):
    if LEDGER_BACKEND == "file":
        current = ledger_read_all() or {}
        current.pop(str(pr_id), None)
        path = _file_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(current, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return
    run([sys.executable, KB_CLI, "state", "--key", ledger_key(pr_id), "--delete"], check=False)


# --- The tick lock -------------------------------------------------------------------------

def tick_lock(timeout=0):
    """Kept for in-process use (the test suite); a flock held only as long as this process."""
    path = _lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path + ".flock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(f"{os.getpid()} {now()}\n"); fh.flush()
    return fh


def cmd_lock(a):
    """
    A tick lock that OUTLIVES a single process.

    A recipe tick is not one process — it is an agent making many separate calls over 15+ minutes.
    An flock held for the length of one `pr_feed.py` invocation is therefore useless as a tick
    lock: it is released the moment the probe exits, so two overlapping ticks both see "free" and
    both proceed. The lock has to be a record on disk with an owner and an expiry.

    The expiry is what keeps a crashed tick from blocking the loop forever: a lock older than its
    TTL is stale and gets taken. Default TTL is deliberately longer than the slowest review the
    per-tick budget allows.
    """
    path = _lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if a.release:
        try:
            os.remove(path)
            out({"status": "ok", "released": True})
        except FileNotFoundError:
            out({"status": "ok", "released": False, "message": "no lock was held"})

    held = None
    if os.path.exists(path):
        try:
            with open(path) as fh:
                held = json.load(fh)
        except (json.JSONDecodeError, OSError):
            held = None            # unreadable lock is treated as stale, not as a wall

    if held:
        expires = held.get("expires_at", "")
        stale = expires < now()
        if a.status:
            out({"status": "held" if not stale else "stale", "lock": held})
        if not stale:
            out({"status": "busy", "lock": held,
                 "message": f"another tick has held the lock since {held.get('acquired_at')} "
                            f"(expires {expires}) — declining rather than overlapping"}, 4)

    if a.status:
        out({"status": "free"})

    expires_at = (datetime.datetime.now(datetime.timezone.utc)
                  + datetime.timedelta(seconds=a.ttl)).isoformat(timespec="seconds")
    record = {"pid": os.getpid(), "acquired_at": now(), "expires_at": expires_at,
              "ttl_seconds": a.ttl, "stole_stale_lock": bool(held)}
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(record, fh, indent=2)
    os.replace(tmp, path)
    out({"status": "ok", "acquired": True, "lock": record})


# --- R9 global pause -----------------------------------------------------------------------

def pause_is_on():
    """
    True if shipping/outward actions are paused. FAILS CLOSED: if we cannot read the flag we
    report paused, because posting to real pull requests while the kill switch is unreadable is
    exactly the situation the kill switch exists to prevent.
    """
    if not os.path.exists(os.path.abspath(KB_CLI)):
        return True, "kb CLI not found — treating as paused (fail closed)"
    p = run_bounded([sys.executable, os.path.abspath(KB_CLI), "pause", "--status"], PAUSE_TIMEOUT)
    if p is None:
        return True, (f"pause flag timed out after {PAUSE_TIMEOUT}s — treating as paused "
                      "(fail closed); the brain is unreachable")
    if p.returncode != 0:
        return True, f"pause flag unreadable — treating as paused (fail closed): {(p.stderr or '').strip()[:200]}"
    try:
        rec = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        return True, "pause flag returned non-JSON — treating as paused (fail closed)"
    paused = rec.get("paused")
    if paused is None:
        return True, "pause flag absent from kb response — treating as paused (fail closed)"
    return bool(paused), "ok"


# --- Candidates ----------------------------------------------------------------------------

def current_identity():
    """The identity whose PRs count as "own": LOOP_PR_IDENTITY, else git's user.email / user.name
    in the target repo. Returned lower-cased; a PR's `author` is compared the same way.

    The vcs_host contract has no "who am I" operation, so the feed never asks the provider. On a
    host whose `author` is a login rather than an email, git's user.email will match nothing and
    the `own` scope yields an empty queue — set LOOP_PR_IDENTITY to the login. `list` reports the
    identity it applied so that mismatch is visible, not silent."""
    raw = (os.environ.get("LOOP_PR_IDENTITY") or "").strip()
    if raw:
        return raw.lower()
    root = app_root(required=False)
    if root:
        for key in ("user.email", "user.name"):
            p = _git(["config", key], root)
            if p and p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip().lower()
    return ""


def is_loop_pr(labels):
    """True when the PR carries the configured loop label (compared case-insensitively).
    `labels` is the adapter's normalized list of strings; a dict with a `labels` key or a list of
    {name} objects is tolerated so a raw provider payload can be asked the same question."""
    if isinstance(labels, dict):
        labels = labels.get("labels")
    want = loop_pr_label()
    for l in labels or []:
        name = l.get("name") if isinstance(l, dict) else l
        if isinstance(name, str) and name.strip().lower() == want:
            return True
    return False


def pr_row(pr):
    """One normalized PullRequest → the flat record every verb and the recipe read."""
    raw = pr.raw if isinstance(pr.raw, dict) else {}
    head = resolve_head(pr.branch, raw)
    return {
        "pr_id": pr.id,
        "title": pr.title or "",
        "author": pr.author or "",
        "source_branch": pr.branch,
        "target_branch": pr.base,
        "head_commit": head,
        "merge_base_commit": resolve_merge_base(pr.base, head, raw),
        "created": pr.created_at,
        "state": pr.state,
        "draft": bool(pr.draft),
        "loop_pr": is_loop_pr(pr.labels),
        "merge_status": normalize_merge_status(raw),
        # A provider that knows the ref holding the PR's merge result says so here; checkout
        # tries it before the common host conventions. None when the provider is silent.
        "merge_ref": raw_str(raw, "merge_ref"),
        "url": pr.url,
    }


def candidates():
    """Open, non-draft PRs targeting the configured branch, newest first."""
    target = target_branch()
    me = current_identity() if AUTHOR_SCOPE in ("own", "others") else None
    if AUTHOR_SCOPE in ("own", "others") and not me:
        fail("config-invalid", "could not resolve the runner's VCS-host identity, so the author scope "
             f"{AUTHOR_SCOPE!r} cannot be applied — set LOOP_PR_IDENTITY (or git user.email in the "
             "target repo) rather than guessing whose PRs these are", code=3)
    prs = host("list_prs", {"state": "open", "base": target, "draft": False}) or []
    rows = []
    for pr in prs:
        if getattr(pr, "state", "open") != "open" or getattr(pr, "draft", False):
            continue
        if (pr.base or "").replace("refs/heads/", "") != target:
            continue
        author = (pr.author or "").strip().lower()
        if AUTHOR_SCOPE == "own" and author != me:
            continue
        if AUTHOR_SCOPE == "others" and author == me:
            continue
        rows.append(pr_row(pr))
    rows.sort(key=lambda r: (r.get("created") or "", str(r["pr_id"])), reverse=True)
    return rows


def pr_detail(pr_id):
    """One PR by id regardless of the candidate filter, or None when the host has no such PR."""
    try:
        pr = _host("get_pr", str(pr_id))
    except HostError as e:
        if e.http_status == 404 or "no pr" in e.message.lower() or "not found" in e.message.lower():
            return None
        fail(e.failure_class, e.message, http_status=e.http_status)
    return pr_row(pr) if pr is not None else None


def changed_file_count(row, cap):
    """Files the PR changes against its merge base, or None when it cannot be measured.

    Counted locally with `git diff --name-only` when both commits are present; otherwise the
    adapter's unified diff is asked for and its file headers counted. Raises HostError."""
    root = app_root(required=False)
    base, head = row.get("merge_base_commit"), row.get("head_commit")
    if root and base and head and local_commit(root, base) and local_commit(root, head):
        p = _git(["diff", "--name-only", base, head], root)
        if p and p.returncode == 0:
            return sum(1 for ln in p.stdout.splitlines() if ln.strip())
    diff = _host("get_pr_diff", str(row["pr_id"])) or ""
    if not isinstance(diff, str):
        return None
    return sum(1 for ln in diff.splitlines() if ln.startswith("diff --git "))


def cmd_list(a):
    rows = candidates()
    out({"status": "ok", "count": len(rows), "filter": {
        "state": "open", "is_draft": False, "target": target_branch(),
        "author_scope": AUTHOR_SCOPE,
        "identity": current_identity() if AUTHOR_SCOPE in ("own", "others") else None,
        "loop_prs": "included" if INCLUDE_LOOP_PRS else "skipped-by-unreviewed"},
        "pull_requests": rows})


# --- Health --------------------------------------------------------------------------------

# Normalized merge statuses that mean "this PR cannot merge as it stands". None is "not known"
# and is reported as unknown, never as a problem.
MERGE_BLOCKED = {"conflicts": "merge conflicts with the target branch"}

# ci_cd.build_status → one of passed | failed | pending | unknown
def normalize_check(b):
    status = (getattr(b, "status", None) or "").lower()
    result = (getattr(b, "result", None) or "").lower()
    if result in ("succeeded", "success", "passed"):
        norm = "passed"
    elif result in ("failed", "failure", "error", "broken", "rejected"):
        norm = "failed"
    elif status in ("queued", "running", "pending", "in_progress", "inprogress"):
        norm = "pending"
    elif status == "completed" and result in ("canceled", "cancelled"):
        norm = "failed"
    else:
        norm = "unknown"
    return {"name": getattr(b, "id", None) or "build", "status": norm, "blocking": True,
            "build_id": getattr(b, "id", None), "url": getattr(b, "url", None)}


def ci_adapter():
    return adapter_get("ci_cd")


def build_checks(row):
    """CI status at the PR's head via the ci_cd adapter. Raises AdapterUnavailable when the role
    is absent (the sweep reports that once, not per PR) and RuntimeError for a per-PR failure."""
    head = row.get("head_commit")
    if not head:
        raise RuntimeError("no head commit to look up")
    try:
        b = ci_adapter().build_status(head)
    except AdapterUnavailable:
        raise
    except AdapterError as e:
        raise RuntimeError(f"{classify_host_error(e, 'build_status').failure_class}: {e.message[:200]}")
    except Exception as e:
        raise RuntimeError(f"ci_cd.build_status failed: {type(e).__name__}: {str(e)[:200]}")
    if b is None:
        return []
    return [normalize_check(b)]


def cmd_health(a):
    """Read-only PR health for an otherwise idle tick: which in-scope open PRs cannot merge
    (conflicts) or have a red build. Report-only by design — it never rebases, pushes, retries a
    build or comments; the PR-review loop never writes to a branch. One CI read per PR on top of
    the list, bounded by --budget-s: PRs not reached are named in `unchecked`, never dropped."""
    import time
    deadline = time.monotonic() + max(a.budget_s, 1)
    rows = candidates()
    attention, healthy, pending, unknown, unchecked, errors = [], [], [], [], [], []
    ci_unavailable = None
    for r in rows:
        if time.monotonic() >= deadline:
            unchecked.append(r["pr_id"])
            continue
        reasons = []
        ms = r.get("merge_status")
        if ms is None:
            ms = local_merge_status(r.get("target_branch"), r.get("head_commit"))
        if ms in MERGE_BLOCKED:
            reasons.append(MERGE_BLOCKED[ms])
        checks = None
        if ci_unavailable is None:
            try:
                checks = build_checks(r)
            except AdapterUnavailable as e:
                ci_unavailable = e.to_dict()["error"]
            except RuntimeError as e:
                errors.append({"pr_id": r["pr_id"], "error": str(e)[:240]})
        red = [c for c in (checks or []) if c["status"] == "failed"]
        for c in red:
            reasons.append(f"build '{c['name']}' failed" + (f" (build {c['build_id']})" if c.get("build_id") else ""))
        item = {"pr_id": r["pr_id"], "title": r.get("title"), "author": r.get("author"),
                "loop_pr": r.get("loop_pr"), "merge_status": ms, "url": r.get("url")}
        if reasons:
            attention.append(dict(item, reasons=reasons))
        elif checks is None or ms is None:
            unknown.append(r["pr_id"])
        elif any(c["status"] in ("pending", "unknown") for c in checks):
            pending.append(r["pr_id"])
        else:
            healthy.append(r["pr_id"])
    out({"status": "ok", "author_scope": AUTHOR_SCOPE, "checked": len(rows) - len(unchecked),
         "needs_attention": attention, "healthy": healthy, "ci_pending": pending,
         "unknown": unknown, "unchecked": unchecked, "errors": errors, "ci": ci_unavailable or "ok",
         "truncated": bool(unchecked), "budget_s": a.budget_s})


# --- Unreviewed ----------------------------------------------------------------------------

def cmd_unreviewed(a):
    rows = candidates()
    unreviewed, skipped, loop_skipped = [], [], []
    for r in rows:
        if r.get("loop_pr") and not INCLUDE_LOOP_PRS:
            loop_skipped.append({"pr_id": r["pr_id"], "title": r["title"],
                                 "reason": f"label {loop_pr_label()!r}: opened by a loop, reviewed in-loop"})
            continue
        try:
            entry = ledger_get(r["pr_id"])
        except Exception as e:
            # Fail closed. Reviewing everything because the ledger is down means duplicate
            # comments on real colleagues' PRs — worse than doing nothing this tick.
            fail("ledger-unreadable",
                 f"seen-ledger unreadable ({e}) — refusing to treat PRs as unreviewed",
                 code=3, backend=LEDGER_BACKEND)
        if entry is not None and not isinstance(entry, dict):
            # A malformed entry is an unreadable ledger, not an unreviewed PR — same fail-closed rule.
            fail("ledger-unreadable",
                 f"seen-ledger entry for PR {r['pr_id']} is {type(entry).__name__}, not an object",
                 code=3, backend=LEDGER_BACKEND)
        if entry is None:
            r["reason"] = "never-reviewed"
            unreviewed.append(r)
        elif not same_commit(entry.get("head_commit"), r["head_commit"]):
            r["reason"] = "new-commits-since-review"
            r["previously_reviewed_commit"] = entry.get("head_commit")
            unreviewed.append(r)
        else:
            skipped.append({"pr_id": r["pr_id"], "head_commit": r["head_commit"],
                            "reviewed_at": entry.get("reviewed_at"),
                            "verdict": entry.get("verdict")})
    # THE SIZE GATE (LOOP_PR_MAX_FILES). A review that cannot finish inside one tick is never
    # marked, and the queue is newest-first — so an oversized PR is re-selected FIRST every tick,
    # killed at the watchdog every tick, and blocks every older PR behind it while each kill is
    # charged the per-tick ceiling. A PR over the cap is listed under `too_large` instead of
    # queued: nothing is marked, so it comes straight back if a push shrinks it, and a human (or
    # a raised cap) decides. Only the PRs this call would actually return are measured.
    too_large, max_files = [], env_int("LOOP_PR_MAX_FILES")
    if max_files and unreviewed:
        kept = []
        for r in unreviewed:
            if a.limit and len(kept) >= a.limit:
                kept.append(r)
                continue
            try:
                n = changed_file_count(r, max_files)
            except HostError as e:
                # Fail OPEN here, deliberately: an unmeasured PR costs at worst one slow tick,
                # which the watchdog bounds. It never costs a comment.
                r["size_unknown"] = f"{e.failure_class}: {e.message[:160]}"
                kept.append(r)
                continue
            except Exception as e:
                # Same rule for a response shape we did not expect: that used to crash the whole
                # queue read with rc 1.
                r["size_unknown"] = f"unexpected response: {type(e).__name__}: {str(e)[:160]}"
                kept.append(r)
                continue
            if n is None:
                r["size_unknown"] = "the diff could not be measured"
                kept.append(r)
                continue
            if n > max_files:
                too_large.append({"pr_id": r["pr_id"], "title": r.get("title"), "author": r.get("author"),
                                  "head_commit": r.get("head_commit"), "changed_files_at_least": n,
                                  "max_changed_files": max_files,
                                  "reason": f"changes at least {n} files, over the {max_files}-file cap "
                                            "(pr_review_loop.max_changed_files_per_pr): a review this size "
                                            "cannot finish inside one tick. NOT reviewed, NOT marked — name "
                                            "it in the report as skipped (too-large)."})
                continue
            r["changed_files"] = n
            kept.append(r)
        unreviewed = kept
    if a.limit:
        unreviewed = unreviewed[: a.limit]
    out({"status": "ok", "backend": LEDGER_BACKEND, "count": len(unreviewed),
         "unreviewed": unreviewed, "already_reviewed": skipped, "loop_prs_skipped": loop_skipped,
         "too_large": too_large, "max_changed_files": max_files or None})


# --- Checkout / teardown -------------------------------------------------------------------

def worktree_path(pr_id):
    return os.path.join(worktree_root(), f"pr-{slug(pr_id)}")


def local_ref(pr_id):
    return f"{REF_NAMESPACE}{slug(pr_id)}"


def merge_ref_candidates(pr_id, source_branch, merge_ref=None):
    """Refs to try, in order, for "what would actually land": a provider-supplied merge ref,
    the two common host conventions for a PR's merge result, then the source branch itself
    (the fallback when the PR has conflicts and the host produced no merge commit)."""
    cands = []
    if isinstance(merge_ref, str) and merge_ref.strip():
        cands.append(merge_ref.strip())
    cands += [f"refs/pull/{pr_id}/merge", f"refs/merge-requests/{pr_id}/merge"]
    if source_branch:
        cands.append(f"refs/heads/{source_branch}")
    return cands


def cmd_checkout(a):
    """
    Read-only worktree at the PR head. No DB copy, no ports, no app start — a review reads code.

    With a remote, the host's merge ref (what would actually land) is fetched first, falling back
    to the source branch when the PR has merge conflicts and the host hasn't produced a merge
    commit. Without a remote (the git-local provider) the PR's head commit is pinned directly.
    """
    pr = next((r for r in candidates() if str(r["pr_id"]) == str(a.pr)), None)
    if pr is None:
        pr = pr_detail(a.pr)
        if pr is None:
            fail("api-unavailable", f"PR {a.pr} not found on the VCS host")

    root = app_root()
    path = worktree_path(a.pr)
    ref = local_ref(a.pr)
    if os.path.exists(path):
        # Idempotent: a crashed prior run left this behind. Reuse only if it is at the right
        # commit; otherwise tear it down so we never review stale code and call it fresh.
        p = run(["git", "rev-parse", "HEAD"], cwd=path, check=False)
        if p.returncode == 0 and pr.get("head_commit") and same_commit(p.stdout.strip(), pr["head_commit"]):
            out({"status": "ok", "reused": True, "worktree": path, "pr_id": a.pr,
                 "head_commit": pr["head_commit"], "diff_base": pr.get("merge_base_commit")})
        run(["git", "worktree", "remove", "--force", path], cwd=root, check=False)

    fetched = None
    remote = remote_name()
    if has_remote(root):
        for candidate_ref in merge_ref_candidates(a.pr, pr.get("source_branch"), pr.get("merge_ref")):
            p = run(["git", "fetch", "--no-tags", "--force", remote, f"{candidate_ref}:{ref}"],
                    cwd=root, check=False, timeout=600)
            if p.returncode == 0:
                fetched = candidate_ref
                break
        if fetched is None:
            fail("checkout-failed", f"could not fetch a merge ref or the source branch for PR {a.pr} from {remote}")
    else:
        head = pr.get("head_commit")
        if not head or not local_commit(root, head):
            fail("checkout-failed", f"PR {a.pr}: no remote named {remote!r} (LOOP_PR_REMOTE / "
                                    f"adapters.vcs_host.config.remote) and its head commit "
                                    f"{str(head)[:8] if head else '(unknown)'} is not in {root}")
        run(["git", "update-ref", ref, head], cwd=root, check=False)
        fetched = head

    os.makedirs(os.path.dirname(path), exist_ok=True)
    p = run(["git", "worktree", "add", "--detach", path, ref], cwd=root, check=False, timeout=600)
    if p.returncode != 0:
        fail("checkout-failed", f"git worktree add failed: {(p.stderr or p.stdout).strip()[:300]}")

    base = pr.get("merge_base_commit")
    if base and has_remote(root) and not local_commit(root, base):
        run(["git", "fetch", "--no-tags", remote, base], cwd=root, check=False, timeout=600)
    target = pr.get("target_branch") or target_branch()
    fallback_base = f"{remote}/{target}" if has_remote(root) else target
    out({"status": "ok", "reused": False, "worktree": path, "pr_id": a.pr,
         "fetched_ref": fetched, "head_commit": pr.get("head_commit"), "diff_base": base,
         "diff_command": f"git -C {path} diff {base}...HEAD" if base else f"git -C {path} diff {fallback_base}...HEAD",
         "note": "read-only review checkout; the developer's own checkout was not touched"})


def cmd_teardown(a):
    """Idempotent by design — the recipe calls this in a finally, including after a crash."""
    root = app_root(required=False)
    path = worktree_path(a.pr) if (root or os.environ.get("LOOP_PR_WORKTREE_ROOT")
                                   or cfg("integrations.pr_review.worktree_root")) else None
    removed = False
    if root is None:
        out({"status": "ok", "pr_id": a.pr, "worktree": path, "removed": False,
             "note": "no target repo configured; nothing to tear down"})
    if path and os.path.exists(path):
        run(["git", "worktree", "remove", "--force", path], cwd=root, check=False)
        removed = not os.path.exists(path)
    run(["git", "worktree", "prune"], cwd=root, check=False)
    run(["git", "update-ref", "-d", local_ref(a.pr)], cwd=root, check=False)
    out({"status": "ok", "pr_id": a.pr, "worktree": path, "removed": removed})


# --- Post ----------------------------------------------------------------------------------

def norm_path(path_):
    """Anchors compare on a repo-relative path; hosts differ on the leading slash."""
    if not path_:
        return None
    return str(path_).lstrip("/")


def build_comments(findings):
    """
    Turn dev-code-review findings into host review-comment payloads: {path, line, body}.

    Each comment anchors to file:line at the head commit the review ran against. A comment
    pinned to code the author already replaced points at the wrong line, which is worse than no
    comment at all — reviewers lose trust in the bot after one of those. The stale-iteration
    guard in cmd_post is what keeps the anchor honest.
    """
    # A withheld finding stays in review.json for the human and the tick report, but never
    # becomes a comment — see dev-loop-pr-review/references/reviewing-incoming-prs.md.
    return [build_comment(f) for f in findings if not f.get("withheld")]


def build_comment(f):
    """ONE finding -> ONE host comment payload {path, line, body}."""
    sev = (f.get("severity") or "").upper()
    body = [f"**{sev or 'FINDING'}: {f.get('title') or f.get('summary', 'Finding')}**", ""]
    if f.get("summary") and f.get("title"):
        body.append(f["summary"])
    if f.get("failure_scenario"):
        body += ["", f"**How it fails:** {f['failure_scenario']}"]
    if f.get("recommendation"):
        body += ["", f"**Suggested fix:** {f['recommendation']}"]
    body += ["", f"_{BOT_MARKER} · reply here or resolve the thread._"]
    path_ = norm_path(f.get("file"))
    line = f.get("line")
    try:
        line = int(line) if line not in (None, "") else None
    except (TypeError, ValueError):
        line = None
    return {"path": path_ if (path_ and line) else None,
            "line": line if (path_ and line) else None,
            "body": "\n".join(body)}


def _anchor(comment):
    return (norm_path(comment.get("path")), comment.get("line"))


def existing_bot_anchors(pr_id):
    """
    (path, line) of every comment WE already posted on this PR.

    A PR comes back for review whenever its author pushes, so the same finding will be re-derived
    on every tick until it is fixed. Posting it again each time is the exact spam this loop is
    built to avoid — and re-raising a thread a human already resolved is worse than duplication,
    it is arguing with them. So the anchor is skipped regardless of thread status.
    """
    anchors = set()
    for t in host("list_review_threads", str(pr_id)) or []:
        if not isinstance(t, dict):
            continue
        if BOT_MARKER not in (t.get("body") or ""):
            continue
        anchors.add((norm_path(t.get("path")), t.get("line")))
    return anchors


def cmd_post(a):
    """
    Findings → PR review comments. Dry-run (a byte-faithful preview) unless --live.

    THE LIVE PATH IS WRITE-AHEAD. Posting and recording used to be two commands — `post`, then a
    later `mark` — with a whole agent turn between them and a watchdog that can kill in the gap;
    and a failure on comment k>1 exited with no thread_ids at all. Either way comments sat on a
    real person's pull request with nothing recording them, and the next tick re-reviewed the PR
    and posted again. So, in THIS process, after every check that can refuse (closed, stale,
    paused, already posted) and BEFORE the first comment is sent, the PR is marked in the
    seen-ledger at the verified head commit (verdict "posting"). A kill from that point on costs
    a missed review — never a duplicate. The entry is rolled back only when the FIRST comment is
    provably refused (a 4xx, or a connection that never opened), because only then did nothing
    reach the PR.

    Two runner postures are also enforced here, not only in the runner's guard, so a session that
    calls this script directly gets the same answer: LOOP_PR_POSTING (a process armed dry-run
    turns --live into a preview) and LOOP_PR_MAX_THREADS (the per-PR volume cap).
    """
    with open(a.findings) as f:
        review = json.load(f)
    findings = review.get("findings", review if isinstance(review, list) else [])
    findings = [f for f in findings if isinstance(f, dict)]

    posture_note = None
    posture = (os.environ.get("LOOP_PR_POSTING") or "").strip()
    if a.live and posture and posture != "live":
        a.live = False
        posture_note = (f"--live was REFUSED: this process is armed LOOP_PR_POSTING={posture}, so this "
                        "call wrote a preview and posted nothing. Mark this PR --mode dry-run.")

    pr = next((r for r in candidates() if str(r["pr_id"]) == str(a.pr)), None)
    if pr is None:
        # Reviews take minutes; a PR can merge or close in that window. This is a normal race,
        # not an outage — the recipe must DROP it rather than retry, which is why it gets its
        # own class instead of api-unavailable.
        detail = pr_detail(a.pr) or {}
        state = detail.get("state", "unknown")
        fail("pr-closed",
             f"PR {a.pr} is no longer an open candidate (state: {state}) — it closed during the "
             "review. Dropping the findings rather than commenting on a merged PR.",
             pr_state=state)

    # Stale-iteration guard: the PR may have advanced between the review and this post. Posting
    # findings about code the author already replaced is noise, and the anchors would be wrong.
    # Compare by prefix: callers legitimately pass an abbreviated SHA, and treating a short sha
    # as different from its own full SHA would silently skip a PR that never moved.
    if a.reviewed_commit and not same_commit(pr["head_commit"], a.reviewed_commit):
        fail("stale-iteration",
             f"PR {a.pr} advanced from {a.reviewed_commit[:8]} to {str(pr['head_commit'])[:8]} "
             "during the review — abandoning the post so nothing anchors to replaced code",
             reviewed_commit=a.reviewed_commit, current_commit=pr["head_commit"])

    pairs = [(f, build_comment(f)) for f in findings if not f.get("withheld")]

    # Drop anything we have already said on this PR.
    already = existing_bot_anchors(a.pr)
    duplicates = [_anchor(c) for f, c in pairs if _anchor(c) in already]
    fresh = [(f, c) for f, c in pairs if _anchor(c) not in already]

    # The per-PR volume cap, on what would ACTUALLY post (after duplicates are dropped), most
    # severe first. Applied to previews too: a preview is byte-for-byte the live call.
    capped, cap = [], env_int("LOOP_PR_MAX_THREADS")
    if cap and len(fresh) > cap:
        order = sorted(range(len(fresh)),
                       key=lambda i: (SEVERITY_RANK.get(str(fresh[i][0].get("severity", "")).lower(), 4), i))
        keep = set(order[:cap])
        for i in order[cap:]:
            f = fresh[i][0]
            f["withheld"] = True
            f["withheld_reason"] = (f"volume cap (max_threads_per_pr={cap}): this PR had {len(fresh)} "
                                    "postable findings and a wall of comment threads is read by nobody. "
                                    "Kept in review.json and the tick report, not commented.")
            capped.append({"file": f.get("file"), "line": f.get("line"),
                           "severity": f.get("severity"), "title": f.get("title") or f.get("summary")})
        fresh = [fresh[i] for i in sorted(keep)]
    comments = [c for f, c in fresh]
    withheld = [f for f in findings if f.get("withheld")]
    summary = {"pr_id": a.pr, "head_commit": pr["head_commit"], "withheld_count": len(withheld),
               "duplicate_skipped": [{"file": d[0], "line": d[1]} for d in duplicates],
               "withheld": [{"file": f.get("file"), "line": f.get("line"),
                             "title": f.get("title"), "reason": f.get("withheld_reason")}
                            for f in withheld]}
    if capped:
        summary["volume_cap"] = {"max_threads_per_pr": cap, "withheld_for_volume": capped}
    if posture_note:
        summary["runner_posture"] = posture_note

    if not a.live:
        # Dry run writes the exact payload we would send. "Exact" matters: a preview that
        # differs from the live call teaches you nothing about what the live call will do.
        preview = os.path.abspath(a.out or f"pr-{slug(a.pr)}-comments.preview.json")
        os.makedirs(os.path.dirname(preview), exist_ok=True)
        with open(preview, "w") as f:
            json.dump({"pr_id": a.pr, "head_commit": pr["head_commit"],
                       "target": {"role": "vcs_host", "op": "post_review_comment", "id": str(a.pr)},
                       "comments": comments}, f, indent=2)
        out({"status": "ok", "mode": "dry-run", "thread_count": len(comments), **summary,
             "preview_file": preview, "would_post_to": pr["url"],
             "note": "nothing was posted; re-run with --live to post these exact payloads"})

    paused, why = pause_is_on()
    if paused:
        fail("paused", f"R9 global pause is on — not posting to PR {a.pr}. {why}", code=3)

    # ── already posted at this head? ──────────────────────────────────────────────────────
    # The ledger used to be read by `unreviewed` alone, so nothing mechanical stopped a second
    # live post to a PR already delivered at this exact commit: a retry, a second `post`, a PR
    # outside this tick's queue, a PR description saying "also post to <another id>". Refuse here.
    try:
        prior = ledger_get(a.pr)
    except Exception as e:
        fail("ledger-unreadable", f"seen-ledger unreadable ({e}) — refusing a live post that could "
             "not be recorded, and whose earlier delivery could not be ruled out", code=3)
    if (isinstance(prior, dict) and prior.get("mode") == "live"
            and same_commit(prior.get("head_commit"), pr["head_commit"])):
        fail("already-posted",
             f"PR {a.pr} is already recorded as posted LIVE at its current head "
             f"{str(pr['head_commit'])[:8]} (verdict {prior.get('verdict')!r}, {prior.get('reviewed_at')}). "
             "Posting again would duplicate threads on a real pull request. Nothing was posted. If a "
             "re-post is genuinely intended, a HUMAN runs `ledger --forget` for this PR first.",
             code=3, entry=prior)

    if not comments:
        out({"status": "ok", "mode": "live", "thread_ids": [], "thread_count": 0, "url": pr["url"],
             **summary, "note": "nothing new to post (every finding was withheld or already on the PR)"})

    # ── write-ahead: mark BEFORE the first comment leaves ──────────────────────────────────
    entry = {"pr_id": a.pr, "head_commit": pr["head_commit"],
             "reviewed_at": now(), "verdict": "posting", "findings": len(comments), "mode": "live",
             "reviewer": "dev-pr-feed", "write_ahead": True,
             "run_id": os.environ.get("LOOP_RUN_ID") or None,
             "reviewed_commit_verified": bool(a.reviewed_commit)}
    try:
        ledger_put(a.pr, entry)
    except Exception as e:
        fail("ledger-unreadable", f"could not record the post before making it ({e}) — refusing to "
             "post comments that nothing would remember. Nothing was posted.", code=3, entry=entry)

    posted, err = [], None
    for c in comments:
        try:
            r = _host("post_review_comment", str(a.pr), c["path"], c["line"], c["body"])
        except HostError as e:
            err = e
            break
        except Exception as e:                    # never exit with posted comments unrecorded
            err = HostError("api-unavailable", f"unexpected error posting a comment: {type(e).__name__}: {e}")
            break
        posted.append((r or {}).get("thread_id") if isinstance(r, dict) else None)

    base = {"mode": "live", "thread_ids": posted, "thread_count": len(posted), "url": pr["url"],
            **summary}
    if err is None:
        entry.update(verdict="posted", findings=len(posted), reviewed_at=now())
        try:
            ledger_put(a.pr, entry)
        except Exception:
            pass                                  # the write-ahead entry already protects the PR
        out({"status": "ok", **base, "ledger_marked": True, "ledger_entry": entry})

    if not posted and err.not_sent:
        # Provably nothing reached the PR: roll the write-ahead back, so the PR is exactly as
        # unreviewed as it was — the SKILL's "never mark a PR whose findings were not delivered".
        restored = True
        try:
            if prior is None:
                ledger_forget(a.pr)
            else:
                ledger_put(a.pr, prior)
        except Exception:
            restored = False
        fail(err.failure_class,
             f"{err.message} — nothing was posted to PR {a.pr}"
             + ("" if restored else "; the write-ahead ledger entry could NOT be rolled back, so the "
                "PR stays marked (a missed review, never a duplicate)"),
             http_status=err.http_status, ledger_restored=restored, thread_ids=[], thread_count=0)

    # Something may have landed. Keep the PR marked — a missed remainder, never a duplicate.
    entry.update(verdict="posted-partial" if posted else "post-unconfirmed",
                 findings=len(posted), reviewed_at=now())
    try:
        ledger_put(a.pr, entry)
    except Exception:
        pass
    out({"status": "partial", **base, "failure_class": err.failure_class, "http_status": err.http_status,
         "not_posted": len(comments) - len(posted), "delivery_unknown": not posted,
         "ledger_marked": True, "ledger_entry": entry,
         "message": (f"posting to PR {a.pr} stopped after {len(posted)} of {len(comments)} comment(s): "
                     f"{err.message}. The PR stays MARKED at this head so no later tick re-posts; do "
                     "NOT retry the post. "
                     + ("The first comment may or may not have landed — check the PR by hand."
                        if not posted else "The remaining findings are in review.json only."))}, 2)


# --- Mark / ledger -------------------------------------------------------------------------

def cmd_mark(a):
    # Verify the sha against the host before recording it. A typo'd or stale commit here is
    # invisible: the entry looks fine, but every later tick sees a mismatch and re-reviews the PR
    # forever. Better to refuse the write than to poison the ledger silently.
    if not a.force:
        live = next((r for r in candidates() if str(r["pr_id"]) == str(a.pr)), None)
        if live and live.get("head_commit") and not same_commit(live["head_commit"], a.commit):
            fail("stale-iteration",
                 f"refusing to mark PR {a.pr} at {a.commit} — the host says its head is "
                 f"{live['head_commit']}. Either the PR moved (re-review it) or the sha is a "
                 "typo; pass --force only if you know why they differ.",
                 host_head=live["head_commit"], supplied=a.commit)

    entry = {"pr_id": a.pr, "head_commit": a.commit,
             "reviewed_at": now(), "verdict": a.verdict, "findings": a.findings_count,
             "mode": a.mode, "reviewer": "dev-pr-feed"}
    try:
        ledger_put(a.pr, entry)
    except Exception as e:
        # Never silently swallow this. An unrecorded review means the next tick re-reviews and
        # re-posts the same findings on a real person's PR.
        fail("ledger-unreadable", f"could not record the review ({e}) — the next run would "
             "re-review this PR; fix the ledger before the next tick", code=3, entry=entry)
    out({"status": "ok", "backend": LEDGER_BACKEND, "recorded": entry})


def cmd_ledger(a):
    if a.forget is not None:
        ledger_forget(a.forget)
        out({"status": "ok", "forgot": a.forget})
    try:
        allrec = ledger_read_all()
    except Exception as e:
        fail("ledger-unreadable", str(e), code=3)
    if allrec is None:
        out({"status": "ok", "backend": LEDGER_BACKEND,
             "note": "the kb backend has no prefix scan; query a specific PR via `unreviewed`"})
    out({"status": "ok", "backend": LEDGER_BACKEND, "path": _file_path() if LEDGER_BACKEND == "file" else None,
         "count": len(allrec), "entries": allrec})


# --- CLI -----------------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description="pull-request surface for the loops (vcs_host adapter)")
    sub = p.add_subparsers(dest="verb", required=True)

    # Every verb that filters candidates takes --base: the branch PRs must target. Default
    # LOOP_PR_TARGET, else project.default_branch — never a literal branch name.
    base_opt = argparse.ArgumentParser(add_help=False)
    base_opt.add_argument("--base", default=None,
                          help="the target branch PRs must have (default: LOOP_PR_TARGET, else project.default_branch)")

    sub.add_parser("list", parents=[base_opt], help="open non-draft PRs targeting the base branch")

    h = sub.add_parser("health", parents=[base_opt], help="read-only: in-scope PRs with merge conflicts or a red build")
    h.add_argument("--budget-s", dest="budget_s", type=int, default=240,
                   help="stop reading per-PR build status after this many seconds (default 240)")

    u = sub.add_parser("unreviewed", parents=[base_opt], help="candidates minus those already reviewed at their head commit")
    u.add_argument("--limit", type=int, default=0)

    c = sub.add_parser("checkout", parents=[base_opt], help="read-only worktree at the PR head")
    c.add_argument("--pr", required=True, help="the PR id as the vcs_host adapter reports it")

    t = sub.add_parser("teardown", help="remove the review worktree (idempotent)")
    t.add_argument("--pr", required=True)

    po = sub.add_parser("post", parents=[base_opt], help="post findings as PR review comments (dry-run unless --live)")
    po.add_argument("--pr", required=True)
    po.add_argument("--findings", required=True, help="review.json from dev-code-review")
    po.add_argument("--reviewed-commit", dest="reviewed_commit",
                    help="the head commit the review was run against (enables the stale-iteration guard)")
    po.add_argument("--live", action="store_true", help="actually post; otherwise write a preview")
    po.add_argument("--out", help="where to write the dry-run preview")

    m = sub.add_parser("mark", parents=[base_opt], help="record the review in the seen-ledger")
    m.add_argument("--pr", required=True)
    m.add_argument("--commit", required=True)
    m.add_argument("--verdict", default="reviewed")
    m.add_argument("--findings-count", dest="findings_count", type=int, default=0)
    m.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    m.add_argument("--force", action="store_true",
                   help="record even when the sha doesn't match the PR's current head")

    lk = sub.add_parser("lock", help="acquire/release the tick lock; exits 4 if another tick holds it")
    lk.add_argument("--release", action="store_true", help="release the lock (call in a finally)")
    lk.add_argument("--status", action="store_true", help="report without acquiring")
    lk.add_argument("--ttl", type=int, default=2700,
                    help="seconds before a held lock is considered stale (default 45m)")

    lg = sub.add_parser("ledger", help="inspect or clear the seen-ledger")
    lg.add_argument("--forget", help="PR id to re-queue")

    a = p.parse_args(argv)
    global _BASE_OVERRIDE
    _BASE_OVERRIDE = (getattr(a, "base", None) or "").strip() or None
    # An unknown backend used to fall through to the kb path, and an unknown scope (OWN, "own ",
    # "") to scope `all` — both silently. The runner pins both; a bare call must not guess.
    if LEDGER_BACKEND not in ("file", "kb"):
        fail("config-invalid", f"LOOP_PR_LEDGER_BACKEND={LEDGER_BACKEND!r} — expected file or kb")
    if AUTHOR_SCOPE not in ("own", "all", "others"):
        fail("config-invalid", f"LOOP_PR_AUTHOR_SCOPE={AUTHOR_SCOPE!r} — expected own, all or others")
    return {"lock": cmd_lock, "list": cmd_list, "unreviewed": cmd_unreviewed, "health": cmd_health,
            "checkout": cmd_checkout, "teardown": cmd_teardown, "post": cmd_post, "mark": cmd_mark,
            "ledger": cmd_ledger}[a.verb](a)


if __name__ == "__main__":
    main()
