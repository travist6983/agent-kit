#!/usr/bin/env python3
"""Emit a contract-shaped seam file deterministically. The agent supplies VALUES, never syntax.

WHY THIS EXISTS. The graph audit measured a meaningful slice of every run (minutes, well over
a tenth of active time) going into contract-shaped JSON — and almost none of it was thinking.
It was composing heredocs by hand, running `contracts/validate.py`, being rejected for a key
the schema does not allow, editing the file, re-validating, then micro-editing it several
more times. One run spent minutes repairing the brain after a malformed write merged into
the wrong row. That is an agent hand-serialising a schema it already has.

This script closes that loop:
  * builds the object from typed --set / --set-json arguments
  * stamps `timing` (and `attempt` / `retry_reason`) without being asked
  * VALIDATES AGAINST THE CONTRACT BEFORE WRITING — a violation never lands on disk
  * fails loudly and says exactly which key is wrong

Usage:
  # values on the command line (scalars are type-coerced from the schema)
  python3 contracts/emit.py --contract contracts/qa-verify.json \\
      --out <evidence.root>/PROJ-123/verify.json \\
      --set verdict=pass --set confidence=90 --set masking_suspected=false \\
      --set-json must_haves='[{"check":"replay","result":"pass"}]' \\
      --started-at 2026-01-01T00:00:00Z

  # or hand it a whole object and let it stamp + validate + write
  python3 contracts/emit.py --contract contracts/qa-verify.json \\
      --out .../verify.json --from-json - <<'JSON'
  { "verdict": "pass", "confidence": 90, "masking_suspected": false }
  JSON

Exit codes:
  0  written and contract-valid
  1  CONTRACT VIOLATION — nothing was written; stderr names the offending key
  2  bad usage / unreadable contract

--dry-run prints the object it would write and validates it, without touching the file.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VALIDATE = os.path.join(HERE, "validate.py")


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def schema_type(contract, key):
    props = contract.get("properties") or {}
    spec = props.get(key)
    if not isinstance(spec, dict):
        return None
    return spec.get("type")


def coerce(raw, want):
    """Coerce a command-line string to the type the CONTRACT declares. This is the whole
    trick: the agent types `confidence=90` and the schema decides it is an integer, so a
    string-vs-int contract rejection can no longer happen."""
    if want == "integer":
        return int(raw)
    if want == "number":
        f = float(raw)
        return int(f) if f.is_integer() else f
    if want == "boolean":
        low = raw.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        raise ValueError(f"{raw!r} is not a boolean")
    if want in ("array", "object"):
        return json.loads(raw)
    if want is None:
        # Unknown key. Leave it a string; the contract will reject it if it is not allowed,
        # which is the correct outcome and a clearer error than guessing.
        return raw
    return raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="scalar field; type-coerced from the contract")
    ap.add_argument("--set-json", action="append", default=[], metavar="KEY=JSON",
                    help="field whose value is raw JSON (arrays/objects)")
    ap.add_argument("--from-json", metavar="PATH|-",
                    help="read the whole object from a file or stdin, then stamp + validate")
    ap.add_argument("--started-at", help="ISO-8601 UTC; defaults to now (a zero-length stage)")
    ap.add_argument("--ended-at", help="ISO-8601 UTC; defaults to now")
    ap.add_argument("--attempt", type=int)
    ap.add_argument("--retry-reason")
    ap.add_argument("--no-timing", action="store_true", help="skip the timing stamp")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    try:
        contract = json.load(open(a.contract))
    except Exception as e:
        print(f"emit: cannot read contract {a.contract}: {e}", file=sys.stderr)
        return 2

    obj = {}
    if a.from_json:
        try:
            src = sys.stdin.read() if a.from_json == "-" else open(a.from_json).read()
            obj = json.loads(src)
        except Exception as e:
            print(f"emit: --from-json is not valid JSON: {e}", file=sys.stderr)
            return 2
        if not isinstance(obj, dict):
            print(f"emit: --from-json must be an object, got {type(obj).__name__}", file=sys.stderr)
            return 2

    for pair in a.set:
        if "=" not in pair:
            print(f"emit: --set needs KEY=VALUE, got {pair!r}", file=sys.stderr)
            return 2
        k, _, v = pair.partition("=")
        try:
            obj[k] = coerce(v, schema_type(contract, k))
        except Exception as e:
            print(f"emit: --set {k}: {e}", file=sys.stderr)
            return 2

    for pair in a.set_json:
        if "=" not in pair:
            print(f"emit: --set-json needs KEY=JSON, got {pair!r}", file=sys.stderr)
            return 2
        k, _, v = pair.partition("=")
        try:
            obj[k] = json.loads(v)
        except Exception as e:
            print(f"emit: --set-json {k} is not valid JSON: {e}", file=sys.stderr)
            return 2

    # ---- stamps the agent should never have to remember ------------------------------
    allows = contract.get("properties") or {}
    extra_ok = contract.get("additionalProperties", True) is not False

    if a.attempt is not None and ("attempt" in allows or extra_ok):
        obj["attempt"] = a.attempt
    if a.retry_reason and ("retry_reason" in allows or extra_ok):
        obj["retry_reason"] = a.retry_reason

    if not a.no_timing and ("timing" in allows or extra_ok):
        started = a.started_at or utcnow()
        ended = a.ended_at or utcnow()
        t = {"started_at": started, "ended_at": ended}
        try:
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            d = (datetime.datetime.strptime(ended, fmt)
                 - datetime.datetime.strptime(started, fmt)).total_seconds()
            if d >= 0:
                t["duration_s"] = d
        except Exception:
            pass  # a non-standard timestamp still records start/end; duration is optional
        obj["timing"] = t

    # ---- validate BEFORE writing -------------------------------------------------------
    tmp = a.out + ".emit-tmp"
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    try:
        r = subprocess.run([sys.executable, VALIDATE, a.contract, tmp],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        os.unlink(tmp)
        print(f"emit: could not run validate.py: {e}", file=sys.stderr)
        return 2

    if r.returncode != 0:
        os.unlink(tmp)
        sys.stderr.write(r.stdout + r.stderr)
        print(f"\nemit: CONTRACT VIOLATION — {a.out} was NOT written.", file=sys.stderr)
        print("emit: fix the offending key above and re-run. Nothing landed on disk, so "
              "there is no half-written artifact for a downstream node to read.",
              file=sys.stderr)
        return 1

    if a.dry_run:
        os.unlink(tmp)
        print(json.dumps(obj, indent=2, ensure_ascii=False))
        print(f"\nemit: DRY RUN — contract-valid against {os.path.basename(a.contract)}; "
              f"would write {a.out}")
        return 0

    os.replace(tmp, a.out)
    print(f"emit: wrote {a.out} — contract-valid against {os.path.basename(a.contract)}"
          + (f" (attempt {obj['attempt']})" if "attempt" in obj else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"emit: crashed: {e}", file=sys.stderr)
        sys.exit(2)
