#!/usr/bin/env python3
"""Prove the replay oracle can actually sign in — at PROVISION time, not at verify.

WHY THIS EXISTS. The replay oracle (qa-repro / qa-verify) signs in with the test credentials the
`auth_provider` adapter supplies for a role. Test credentials are routinely minted for a shared
environment (staging) while the sandbox runs on localhost against a different identity store and
a different database — so a user that exists remotely need not exist in the sandbox at all. An
oracle that cannot sign in exits 2 = BLOCKED; `qa-verify` then returns `blocked`, and `dev-pre-pr`
blocks the ship. That is the correct outcome, but discovering it at the verify stage costs the whole
front half of a run first.

This preflight moves that discovery to minute one and names the actual cause. It is static by
default (no app required); `--live` additionally attempts a real sign-in through the oracle.

Everything it knows about credentials comes from `adapters.auth_provider` in stack.config.json:
the provider, the role names (`config.roles`), and the variable naming (`config.credential_env_pattern`
/ the provider's own `var_names`). Nothing is hard-coded.

Usage:
  python3 oracle_preflight.py --app-url <sandbox urls.app> [--roles owner,admin] \\
      [--env-file <one credential file to check instead of the configured ones>] \\
      [--live --oracle <evidence>/<KEY>/replay-oracle.sh] [--config <stack.config.json>]

Exit codes:
  0  ok        — credentials present for every role the run needs; target is not production
  1  BLOCKED   — the oracle will exit 2 (credentials-missing / credentials-unusable /
                 adapter-unavailable:auth_provider); the run should stop here, not at verify
  2  unreadable / bad usage (fail closed)
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]                              # .claude/skills/infra-worktree/scripts → repo
for _p in (str(_REPO_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.adapters import AdapterError, AdapterUnavailable, get, load_config  # noqa: E402
from lib.adapters.providers.env_file_auth import parse_env_file  # noqa: E402
from role_overrides import env_overrides, role_config  # noqa: E402

PROD_ENV_NAMES = ("prod", "production")


def host_of(url):
    try:
        return (urlparse(str(url)).hostname or "").lower()
    except Exception:
        return ""


def is_local(host):
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def var_names(auth, auth_cfg, role):
    """The variable names the oracle will look for — from the provider when it can say, else rendered
    from credential_env_pattern (E2E_{ROLE}_{FIELD})."""
    fn = getattr(auth, "var_names", None)
    if callable(fn):
        try:
            return fn(role)
        except Exception:
            pass
    pattern = str(auth_cfg.get("credential_env_pattern") or "E2E_{ROLE}_{FIELD}")
    r = str(role).upper().replace("-", "_")
    return {"username": pattern.format(ROLE=r, FIELD="USERNAME"),
            "password": pattern.format(ROLE=r, FIELD="PASSWORD")}


def prod_hosts(stack):
    hosts = set()
    envs = stack.get("app.environments") or {}
    for name, env in envs.items():
        if str(name).lower() in PROD_ENV_NAMES and isinstance(env, dict) and env.get("url"):
            hosts.add(host_of(env["url"]))
    return {h for h in hosts if h}


def credential_files(stack, auth_cfg, override_file):
    if override_file:
        return [Path(override_file)]
    out = []
    for name in ("env_file", "local_env_file"):
        p = stack.resolve(auth_cfg.get(name)) if auth_cfg.get(name) else None
        if p is not None:
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-url", required=True, help="the sandbox's urls.app")
    ap.add_argument("--roles", default="",
                    help="comma list of roles the run needs credentials for "
                         "(default: the first of adapters.auth_provider.config.roles)")
    ap.add_argument("--env-file", help="check ONE credential file instead of the configured pair "
                                       "(what the recipe will pass the oracle as E2E_ENV_FILE)")
    ap.add_argument("--live", action="store_true",
                    help="also attempt a real sign-in through the oracle (needs the app up)")
    ap.add_argument("--oracle", help="path to replay-oracle.sh, for --live")
    ap.add_argument("--config", help="an alternate stack.config.json")
    a = ap.parse_args()

    stack = load_config(a.config) if a.config else load_config()
    auth_cfg = role_config(stack, "auth_provider")      # file values + LOOPS_CFG__…__CONFIG__* overrides
    configured_roles = [str(r) for r in (auth_cfg.get("roles") or []) if str(r).strip()]
    roles = [r.strip().lower() for r in a.roles.split(",") if r.strip()] or \
            ([configured_roles[0].lower()] if configured_roles else ["owner"])

    problems, warnings, checked = [], [], {}
    env_desc = a.env_file or " + ".join(str(p) for p in credential_files(stack, auth_cfg, None)) or "the environment"

    # ---- the file itself (only when one file was named: the configured pair may be partial) -------
    if a.env_file and not os.path.isfile(a.env_file):
        print(json.dumps({
            "ok": False, "status": "credentials-missing", "next_action": "escalate",
            "problems": [f"no credential file at {a.env_file} — the replay oracle signs in from this "
                         "file; without it every signed-in oracle run exits 2 (BLOCKED) and qa-verify "
                         "can never return a verdict."],
            "remedy": "point E2E_ENV_FILE at a file that exists, or create the files named by "
                      "adapters.auth_provider.config.env_file / local_env_file.",
        }, indent=1))
        return 1

    # ---- the adapter -----------------------------------------------------------------------------
    override = env_overrides("auth_provider")
    if a.env_file:
        override.update({"env_file": a.env_file, "local_env_file": None, "process_env": False})
    try:
        auth = get("auth_provider", stack=stack, config_override=override or None)
    except AdapterError as e:
        print(json.dumps({"ok": False, "status": "unreadable", "next_action": "escalate",
                          "problems": [f"auth_provider adapter could not load: {e.message}"]}, indent=1))
        return 2

    # ---- credentials per role ----------------------------------------------------------------------
    unknown = [r for r in roles if configured_roles and r not in {c.lower() for c in configured_roles}]
    for r in unknown:
        warnings.append(f"role {r!r} is not in adapters.auth_provider.config.roles "
                        f"({', '.join(configured_roles)}); checking it anyway.")
    for role in roles:
        names = var_names(auth, auth_cfg, role)
        try:
            creds = auth.credentials(role)
            checked[role] = {"username": True, "password": True, "variables": names,
                             "extra": sorted(creds.extra)}
        except AdapterUnavailable as e:
            print(json.dumps({
                "ok": False, "status": "blocked", "next_action": "escalate",
                "error": "adapter-unavailable:%s" % e.role, "detail": e.message, "reason": e.reason,
                "problems": [f"the auth_provider adapter is {e.reason}; the oracle has no way to sign in "
                             "as any role. Configure adapters.auth_provider or run the oracle with "
                             "ROLE=none for a surface that needs no sign-in."],
            }, indent=1))
            return 1
        except AdapterError as e:
            missing = list(e.details.get("missing") or names.values())
            checked[role] = {"username": names["username"] not in missing,
                             "password": names["password"] not in missing,
                             "variables": names, "missing": missing}
            problems.append(f"{role}: {e.message} — the oracle run as ROLE={role} will exit 2 (BLOCKED) "
                            f"with no {' / '.join(missing)}.")

    # ---- production target: the oracle refuses it anyway ---------------------------------------------
    target = host_of(a.app_url)
    prod = prod_hosts(stack)
    if target and target in prod:
        problems.append(
            f"app_url points at PRODUCTION ({target}, app.environments). The oracle refuses prod hosts "
            "unless ALLOW_PROD=1 and the script is marked read-only (infra-prod-verify only).")

    # ---- the scope trap: credentials minted for a different host --------------------------------------
    # HOST MISMATCH IS A WARNING, NOT A BLOCK — and the distinction is mechanical. The oracle takes
    # its target from APP_URL and reads only the per-role username/password; a recorded base URL
    # says which environment the credentials were MINTED for. That is a strong hint that the
    # identity store differs (a user minted in the remote store need not exist locally), not a
    # mechanism that fails. Blocking on a heuristic manufactures false escalations, which is how a
    # gate stops being trusted. The conclusive check is --live, below.
    prefix = str(getattr(auth, "prefix", None) or auth_cfg.get("prefix") or "E2E").rstrip("_")
    merged = {}
    for p in credential_files(stack, auth_cfg, a.env_file):
        try:
            merged.update(parse_env_file(p))
        except Exception as e:
            return _unreadable(f"cannot read {p}: {e}")
    cred_url = merged.get(f"{prefix}_BASE_URL", "")
    for role in roles:
        extra = (checked.get(role) or {}).get("extra") or []
        if "base_url" in extra and not cred_url:
            try:
                cred_url = auth.credentials(role).extra.get("base_url", "")
            except AdapterError:
                pass
    cred_host = host_of(cred_url) if cred_url else ""
    env_label = merged.get(f"{prefix}_ENV", "")
    if cred_host and target and cred_host != target:
        detail = (f"credentials in {env_desc} were minted for {cred_host}"
                  + (f" ({prefix}_ENV={env_label})" if env_label else "") + f", but this sandbox is {target}.")
        if is_local(target) and not is_local(cred_host):
            warnings.append(
                detail + " The oracle will still TRY them (it only reads the username/password and "
                "drives APP_URL), but a user that exists in the remote identity store need not exist "
                "in the sandbox's local identity provider, which rejects the sign-in outright — and no "
                "amount of URL-fixing solves an account problem. Run with --live to settle it.")
        else:
            warnings.append(detail + " Verify this is intended.")

    # ---- optional live proof ----------------------------------------------------------------------------
    live = None
    if a.live:
        if not a.oracle or not os.path.isfile(a.oracle):
            warnings.append("--live requested but --oracle path is missing; skipped the live sign-in "
                            "and fell back to static checks only.")
        else:
            env = {**os.environ, "APP_URL": a.app_url, "ROLE": roles[0]}
            if a.env_file:
                env["E2E_ENV_FILE"] = a.env_file
            try:
                r = subprocess.run(["bash", a.oracle], env=env, capture_output=True, text=True, timeout=180)
                live = {"exit": r.returncode, "tail": (r.stdout + r.stderr)[-400:]}
                if r.returncode == 2:
                    problems.append("live sign-in FAILED (oracle exit 2 = BLOCKED). This is not a verdict "
                                    "about any fix — it means the evaluator cannot run at all.")
            except subprocess.TimeoutExpired:
                warnings.append("live sign-in timed out after 180s; treat as unproven.")
            except Exception as e:
                warnings.append(f"live sign-in could not run: {e}")

    ok = not problems
    print(json.dumps({
        "ok": ok,
        "status": "ok" if ok else "credentials-unusable",
        "next_action": "continue" if ok else "escalate",
        "auth_provider": getattr(auth, "provider_name", None) or type(auth).__name__,
        "credential_source": env_desc,
        "app_url": a.app_url,
        "credential_host": cred_host or None,
        "target_host": target or None,
        "roles_checked": checked,
        "problems": problems,
        "warnings": warnings,
        "live": live,
        "remedy": None if ok else (
            "Supply credentials for THIS sandbox's identity provider (the variables named in "
            "roles_checked, in the files named by adapters.auth_provider.config or the process "
            "environment) before the loop runs a browser step. Escalating here costs one minute; "
            "discovering it at qa-verify costs the whole front half of the run and produces a "
            "`blocked` verdict that says nothing about the fix."),
    }, indent=1))
    return 0 if ok else 1


def _unreadable(msg):
    print(json.dumps({"ok": False, "status": "unreadable", "next_action": "escalate",
                      "problems": [msg]}, indent=1))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # fail closed
        print(json.dumps({"ok": False, "status": "unreadable", "next_action": "escalate",
                          "problems": [f"oracle_preflight crashed: {e}"]}, indent=1))
        sys.exit(2)
