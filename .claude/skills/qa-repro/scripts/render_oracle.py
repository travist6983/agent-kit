#!/usr/bin/env python3
"""render_oracle.py — copy qa-repro's oracle templates into an evidence dir with the CONFIG block rendered
from stack.config (the auth_provider adapter's `login` recipe + `roles`, the credential files, and the
production host). The emitted oracle is self-contained: qa-verify and infra-prod-verify run it from the
evidence dir with no access to this repo's config.

Usage:
  render_oracle.py <KEY> <evidence-dir> [--pure-backend] [--config PATH]

Writes <evidence-dir>/replay-oracle.sh (+x) and, unless --pure-backend, <evidence-dir>/replay-grader.js.
Prints ONE JSON object: {"oracle", "grader", "rendered": {...}, "warnings": [...]}.
Exit 0 rendered · 2 usage / config error (a missing login selector is an error, never a guess).

Only the `CFG_* = '…'   # @cfg <dotted>` lines in the template change; the FILL blocks, the exit contract and
the `blocked "FILL 1 not written"` sentinel are untouched — the rendered copy is still structurally BLOCKED
until qa-repro fills it. An env var set when the oracle runs (ORACLE_ROLES, LOGIN_PATH, PROD_HOSTS, …) still
overrides the rendered value, so a recipe can point one copy at another environment.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
ASSETS = SKILL_DIR / "assets"
ORACLE_TEMPLATE = ASSETS / "replay-oracle.template.sh"
GRADER_TEMPLATE = ASSETS / "replay-grader.template.js"


def _repo_root() -> Path:
    env = os.environ.get("LOOPS_REPO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return SKILL_DIR.parents[2]  # <repo>/.claude/skills/qa-repro → <repo>


sys.path.insert(0, str(_repo_root()))
from lib.adapters.config import load_config  # noqa: E402

CFG_LINE = re.compile(r"^(?P<name>CFG_[A-Z_]+)='(?P<value>(?:[^']|'\\'')*)'(?P<rest>\s+# @cfg .*)$")

FIELD_KEYS = {
    "CFG_LOGIN_PATH": ("path", True),
    "CFG_USERNAME_SELECTOR": ("username_selector", True),
    "CFG_PASSWORD_SELECTOR": ("password_selector", True),
    "CFG_SUBMIT_SELECTOR": ("submit_selector", False),
    "CFG_FAILURE_REGEX": ("failure_regex", False),
    "CFG_SIGNED_IN_LANDMARK": ("signed_in_landmark", True),
}


def sq(value: str) -> str:
    """A value as the body of a single-quoted shell literal."""
    return str(value).replace("'", "'\\''")


def render_values(stack, warnings: list) -> dict:
    spec = stack.adapter_spec("auth_provider")
    provider = spec["provider"] or "none"
    acfg = spec["config"]
    values = {}

    if provider == "none":
        values["CFG_ROLES"] = ""
        warnings.append("auth_provider is none: the oracle accepts ROLE=none only")
    else:
        roles = acfg.get("roles") or []
        if not isinstance(roles, list) or not roles:
            raise SystemExit("adapters.auth_provider.config.roles is missing or empty (the oracle needs the role vocabulary)")
        values["CFG_ROLES"] = " ".join(str(r).strip().lower() for r in roles if str(r).strip())

    login = acfg.get("login") or {}
    for cfg_name, (key, required) in FIELD_KEYS.items():
        val = login.get(key)
        if val in (None, ""):
            if required and provider != "none":
                raise SystemExit("adapters.auth_provider.config.login.%s is missing — the oracle will not guess a selector" % key)
            val = ""
        values[cfg_name] = str(val)

    pattern = acfg.get("credential_env_pattern")
    if not pattern:
        pattern = "%s_{ROLE}_{FIELD}" % str(acfg.get("prefix") or "E2E").rstrip("_")
    if "{ROLE}" not in pattern or "{FIELD}" not in pattern:
        raise SystemExit("adapters.auth_provider.config.credential_env_pattern must contain {ROLE} and {FIELD}")
    values["CFG_CRED_PATTERN"] = pattern

    pre_auth = str(acfg.get("pre_auth") or "none")
    if pre_auth not in ("none", "sso_human_once"):
        warnings.append("pre_auth=%s is not supported by the replay oracle; a signed-in ROLE will be BLOCKED" % pre_auth)
    values["CFG_PRE_AUTH"] = pre_auth

    for cfg_name, key in (("CFG_ENV_FILE", "env_file"), ("CFG_LOCAL_ENV_FILE", "local_env_file")):
        p = stack.resolve(acfg.get(key)) if acfg.get(key) else None
        values[cfg_name] = str(p) if p else ""
        if p and not p.exists():
            warnings.append("%s %s does not exist yet (fine if CI injects the credentials via the environment)" % (key, p))

    prod_url = stack.get("app.environments.prod.url")
    host = urlparse(str(prod_url)).hostname if prod_url else None
    values["CFG_PROD_HOSTS"] = host or ""
    if not host:
        warnings.append("app.environments.prod.url is not set: no host is treated as production by this oracle (set PROD_HOSTS at run time to refuse one)")
    return values


def render_template(src: str, values: dict, key: str) -> str:
    out, seen = [], set()
    for line in src.splitlines(keepends=True):
        m = CFG_LINE.match(line.rstrip("\n"))
        if m and m.group("name") in values:
            seen.add(m.group("name"))
            line = "%s='%s'%s\n" % (m.group("name"), sq(values[m.group("name")]), m.group("rest"))
        out.append(line)
    missing = set(values) - seen
    if missing:
        raise SystemExit("template lacks CONFIG lines for: %s" % ", ".join(sorted(missing)))
    text = "".join(out)
    if 'blocked "FILL 1 not written"' not in text:
        raise SystemExit("template FILL 1 sentinel missing — refusing to render an oracle that could pass unfilled")
    return text.replace("<KEY>", key, 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("key", help="the issue key (e.g. PROJ-123)")
    ap.add_argument("evidence_dir", help="<evidence.root>/<KEY>")
    ap.add_argument("--pure-backend", action="store_true", help="no browser surface: render the oracle only, no grader")
    ap.add_argument("--config", help="an alternative stack.config.json (tests)")
    args = ap.parse_args(argv)

    pattern = None
    try:
        stack = load_config(args.config) if args.config else load_config()
        pattern = stack.get("project.ticket_key_pattern")
    except FileNotFoundError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    if pattern and not re.match(pattern, args.key):
        print(json.dumps({"ok": False, "error": "key %r does not match project.ticket_key_pattern %r" % (args.key, pattern)}))
        return 2

    warnings: list = []
    try:
        values = render_values(stack, warnings)
    except SystemExit as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2

    dest = Path(args.evidence_dir).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    oracle = dest / "replay-oracle.sh"
    oracle.write_text(render_template(ORACLE_TEMPLATE.read_text(encoding="utf-8"), values, args.key), encoding="utf-8")
    oracle.chmod(oracle.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    grader = None
    if not args.pure_backend:
        grader = dest / "replay-grader.js"
        shutil.copyfile(GRADER_TEMPLATE, grader)
        grader.write_text(grader.read_text(encoding="utf-8").replace("<KEY>", args.key, 1), encoding="utf-8")

    rendered = {k.lower().replace("cfg_", ""): v for k, v in values.items()}
    print(json.dumps({"ok": True, "oracle": str(oracle), "grader": str(grader) if grader else None,
                      "rendered": rendered, "warnings": warnings}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
