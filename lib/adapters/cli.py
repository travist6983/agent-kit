#!/usr/bin/env python3
"""Shell entry point for the adapter layer.

    python3 lib/adapters/cli.py <role> <op> [--json '{...}'] [key=value ...] [positional ...]
    python3 lib/adapters/cli.py --list
    python3 lib/adapters/cli.py --config path/to/stack.config.json issue_tracker fetch_ticket PROJ-1

Prints exactly ONE JSON object:
    ok   → {"ok": true, "role": ..., "op": ..., "result": <value>}
    fail → {"ok": false, "error": "...", ...}
Exit codes: 0 ok · 1 operation error · 2 usage · 3 adapter unavailable (provider none / op unsupported).

`key=value` values parse as JSON when they can (`draft=true`, `labels=["a","b"]`, `line=12`), else
they are strings. `--json` supplies kwargs as one object; bare words become positional args.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
for p in (str(_REPO_ROOT), str(_HERE.parents[1])):
    if p not in sys.path:
        sys.path.insert(0, p)

from lib.adapters import (  # noqa: E402
    OPS, AdapterError, AdapterUnavailable, UnknownProviderError, get, known_providers, load_config,
)

EXIT_OK, EXIT_OP_ERROR, EXIT_USAGE, EXIT_UNAVAILABLE = 0, 1, 2, 3

USAGE = __doc__.strip()


def _emit(obj: Any, code: int) -> int:
    sys.stdout.write(json.dumps(obj, default=_default, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return code


def _default(o: Any) -> Any:
    if hasattr(o, "to_dict"):
        return o.to_dict()
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def _normalize(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def parse_args(argv: List[str]) -> Tuple[Dict[str, Any], List[Any], Dict[str, Any]]:
    """→ (options, positional, kwargs). Raises SystemExit(2) via ValueError on misuse."""
    opts: Dict[str, Any] = {"list": False, "config": None, "role": None, "op": None}
    positional: List[Any] = []
    kwargs: Dict[str, Any] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            opts["help"] = True
        elif a == "--list":
            opts["list"] = True
        elif a == "--config":
            i += 1
            if i >= len(argv):
                raise ValueError("--config needs a path")
            opts["config"] = argv[i]
        elif a.startswith("--config="):
            opts["config"] = a.split("=", 1)[1]
        elif a == "--json":
            i += 1
            if i >= len(argv):
                raise ValueError("--json needs a JSON object")
            try:
                obj = json.loads(argv[i])
            except ValueError as e:
                raise ValueError("--json is not valid JSON: %s" % e)
            if not isinstance(obj, dict):
                raise ValueError("--json must be a JSON object")
            kwargs.update(obj)
        elif a.startswith("--json="):
            obj = json.loads(a.split("=", 1)[1])
            if not isinstance(obj, dict):
                raise ValueError("--json must be a JSON object")
            kwargs.update(obj)
        elif opts["role"] is None:
            opts["role"] = a
        elif opts["op"] is None:
            opts["op"] = a
        else:
            k, eq, v = a.partition("=")
            if eq and k.isidentifier():
                kwargs[k] = _parse_value(v)
            else:
                positional.append(a)
        i += 1
    return opts, positional, kwargs


def do_list(config_path: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": True, "roles": {}, "providers": known_providers()}
    try:
        stack = load_config(config_path) if config_path else load_config()
        out["config"] = str(stack.path)
    except (FileNotFoundError, ValueError) as e:
        stack = None
        out["config_error"] = str(e)
    for role, ops in OPS.items():
        entry: Dict[str, Any] = {"ops": list(ops)}
        if stack is not None:
            entry["provider"] = stack.adapter_spec(role)["provider"]
        out["roles"][role] = entry
    return out


def main(argv: List[str]) -> int:
    try:
        opts, positional, kwargs = parse_args(argv)
    except ValueError as e:
        return _emit({"ok": False, "error": "usage: %s" % e, "usage": USAGE}, EXIT_USAGE)
    if opts.get("help"):
        return _emit({"ok": True, "usage": USAGE, "roles": {r: list(o) for r, o in OPS.items()}}, EXIT_OK)
    if opts["list"]:
        return _emit(do_list(opts["config"]), EXIT_OK)
    role, op = opts["role"], opts["op"]
    if not role or not op:
        return _emit({"ok": False, "error": "usage: <role> <op> required", "usage": USAGE,
                      "roles": list(OPS)}, EXIT_USAGE)
    if role not in OPS:
        return _emit({"ok": False, "error": "usage: unknown role %r" % role, "roles": list(OPS)}, EXIT_USAGE)
    if op not in OPS[role]:
        return _emit({"ok": False, "error": "usage: unknown op %r for role %s" % (op, role),
                      "ops": list(OPS[role])}, EXIT_USAGE)
    try:
        stack = load_config(opts["config"]) if opts["config"] else load_config()
        adapter = get(role, stack=stack if opts["config"] else None)
        result = getattr(adapter, op)(*positional, **kwargs)
        return _emit({"ok": True, "role": role, "op": op, "result": _normalize(result)}, EXIT_OK)
    except AdapterUnavailable as e:
        return _emit(e.to_dict(), EXIT_UNAVAILABLE)
    except UnknownProviderError as e:
        d = e.to_dict()
        d["role"] = role
        return _emit(d, EXIT_OP_ERROR)
    except AdapterError as e:
        d = e.to_dict()
        d.update({"role": role, "op": op})
        return _emit(d, EXIT_OP_ERROR)
    except TypeError as e:
        # wrong arguments for the op — a usage error, with the signature to help
        import inspect
        try:
            sig = str(inspect.signature(getattr(get(role, stack=load_config(opts["config"]) if opts["config"] else None), op)))
        except Exception:
            sig = "?"
        return _emit({"ok": False, "error": "usage: %s" % e, "role": role, "op": op, "signature": op + sig}, EXIT_USAGE)
    except (FileNotFoundError, ValueError, KeyError) as e:
        return _emit({"ok": False, "error": "%s: %s" % (type(e).__name__, e), "role": role, "op": op}, EXIT_OP_ERROR)
    except Exception as e:  # noqa: BLE001 - last resort: still one JSON object
        return _emit({"ok": False, "error": "%s: %s" % (type(e).__name__, e), "role": role, "op": op}, EXIT_OP_ERROR)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
