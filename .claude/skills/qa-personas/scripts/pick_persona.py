#!/usr/bin/env python3
"""pick_persona — choose the QA-tester persona(s) whose lens best matches a bug / surface / feature.

Personas come from the pack the target repo configures: `personas_file` in
`.claude/loops/stack.config.json` (default `.claude/loops/personas.json`, a neutral sample). The pack
format is `references/pack-format.md`. Reproducing or exploring AS the right persona surfaces the
failures that persona hits first — a phone-first member catches a tap-target bug a desk admin never
would. Selection is deterministic and token-free: keyword overlap scored lenses x3, role/label/device x2,
goals/habits/profile x1.

Usage:
  pick_persona.py --query "invoice total won't update on a phone in the field" [--k 3] [--full]
  pick_persona.py --tier <tier-id|alias> [--k 5]     # tiers come from the pack's engagement_tiers
  pick_persona.py --role member                      # one of the pack's roles
  pick_persona.py --lens mobile                      # substring match on the persona's lenses
  pick_persona.py --id 2 --full                      # one persona's full role-play profile
  pick_persona.py --spread [--full]                  # one persona per engagement tier (explore)
  pick_persona.py --list                             # one-line summary of every persona in the pack
  pick_persona.py --tiers                            # the pack's roles, tiers (with aliases) and overlays
  pick_persona.py --pack path/to/pack.json ...       # use another pack (else personas_file from stack.config)

Prints JSON on stdout. `--full` adds goals / habits / lenses / standards / what_broken_looks_like and
`profile_markdown` (the role to adopt). Exit 0 on success — including an EMPTY result, which the caller
must treat as a `needs-info` stop rather than force a weak match. Exit 2 when the pack cannot be found,
parsed or fails shape validation (the error is a JSON object on stderr).
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
# <repo>/.claude/skills/qa-personas/scripts/pick_persona.py → the repo root is four levels up.
REPO_ROOT = os.environ.get("LOOPS_REPO_ROOT") or os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
DEFAULT_PACK_REL = os.path.join(".claude", "loops", "personas.json")
STOP = set("a an the to of for on in is are it this that with and or but you your we our they "
           "their from has have had will would can cant does not no when where how what which who "
           "page user users feature bug issue".split())
PROFILE_FIELDS = ("summary", "device", "goals", "habits", "lenses", "standards", "what_broken_looks_like", "profile_markdown")


def die(msg, **extra):
    sys.stderr.write(json.dumps({"ok": False, "error": msg, **extra}) + "\n")
    sys.exit(2)


def _cfg(key, default=None):
    """Read a stack.config key through lib.adapters.config when the package is importable."""
    try:
        if REPO_ROOT not in sys.path:
            sys.path.insert(0, REPO_ROOT)
        from lib.adapters.config import load_config  # type: ignore
        return load_config().get(key, default)
    except Exception:
        return default


def resolve_pack_path(explicit):
    if explicit:
        return explicit
    configured = _cfg("personas_file", DEFAULT_PACK_REL) or DEFAULT_PACK_REL
    return configured if os.path.isabs(configured) else os.path.join(REPO_ROOT, configured)


def load_pack(path):
    try:
        with open(path, encoding="utf-8") as fh:
            pack = json.load(fh)
    except FileNotFoundError:
        die("persona pack not found; set personas_file in .claude/loops/stack.config.json or pass --pack", path=path)
    except json.JSONDecodeError as e:
        die("persona pack is not valid JSON: %s" % e, path=path)
    problems = validate_pack(pack)
    if problems:
        die("persona pack failed validation", path=path, problems=problems)
    return pack


def _as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def tier_ids(pack):
    out = []
    for t in pack.get("engagement_tiers") or []:
        out.append(t["id"] if isinstance(t, dict) else str(t))
    return out


def validate_pack(pack):
    """Shape check against references/pack-format.md. Returns a list of problems (empty = valid)."""
    problems = []
    if not isinstance(pack, dict):
        return ["pack must be a JSON object"]
    roles = pack.get("roles")
    tiers = pack.get("engagement_tiers")
    personas = pack.get("personas")
    if not isinstance(roles, list) or not roles:
        problems.append("roles: non-empty list required")
    if not isinstance(tiers, list) or not tiers:
        problems.append("engagement_tiers: non-empty list required")
    if not isinstance(personas, list) or not personas:
        problems.append("personas: non-empty list required")
    if problems:
        return problems
    role_set = {str(r).lower() for r in roles}
    tier_set = {t.lower() for t in tier_ids(pack)}
    seen = set()
    for i, p in enumerate(personas):
        where = "personas[%d]" % i
        if not isinstance(p, dict):
            problems.append(where + ": must be an object"); continue
        pid = p.get("id")
        if pid is None:
            problems.append(where + ": id required")
        elif pid in seen:
            problems.append(where + ": duplicate id %r" % pid)
        seen.add(pid)
        if str(p.get("role", "")).lower() not in role_set:
            problems.append(where + ": role %r is not one of the pack's roles" % p.get("role"))
        if str(p.get("tier", "")).lower() not in tier_set:
            problems.append(where + ": tier %r is not one of the pack's engagement_tiers" % p.get("tier"))
        for key in ("goals", "habits", "lenses"):
            if not _as_list(p.get(key)):
                problems.append(where + ": %s required (non-empty list)" % key)
    return problems


def tier_alias_map(pack):
    """alias (lower) → canonical tier id, from the pack's engagement_tiers."""
    out = {}
    for t in pack.get("engagement_tiers") or []:
        if isinstance(t, dict):
            tid = str(t["id"])
            out[tid.lower()] = tid
            for a in _as_list(t.get("aliases")):
                out[str(a).lower()] = tid
            if t.get("label"):
                out[str(t["label"]).lower()] = tid
        else:
            out[str(t).lower()] = str(t)
    return out


def toks(s):
    if isinstance(s, (list, tuple)):
        s = " ".join(str(x) for x in s)
    return {w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if w and w not in STOP and len(w) > 2}


def glossary_expand(q):
    """Add the target product's own nouns to a query via conventions.domain_glossary (optional)."""
    conv_file = _cfg("stack.conventions_file")
    if not conv_file:
        return q
    path = conv_file if os.path.isabs(conv_file) else os.path.join(REPO_ROOT, conv_file)
    try:
        with open(path, encoding="utf-8") as fh:
            glossary = json.load(fh).get("domain_glossary") or {}
    except Exception:
        return q
    if not isinstance(glossary, dict):
        return q
    extra = set()
    for generic, product in glossary.items():
        both = toks(generic) | toks(product)
        if q & both:
            extra |= both
    return q | extra


def slim(p, full):
    out = {k: p.get(k) for k in ("id", "label", "role", "tier")}
    out["lenses"] = _as_list(p.get("lenses"))
    if full:
        for k in PROFILE_FIELDS:
            if k in p and k != "lenses":
                out[k] = p[k]
    return out


def score_fn(q):
    def score(p):
        lens = toks(p.get("lenses"))
        role = toks(" ".join([str(p.get("role", "")), str(p.get("label", "")), str(p.get("device", "")), str(p.get("tier", ""))]))
        prof = toks(" ".join([
            str(p.get("summary", "")), str(p.get("standards", "")), str(p.get("profile_markdown", "")),
            " ".join(_as_list(p.get("goals"))), " ".join(_as_list(p.get("habits"))),
            " ".join(_as_list(p.get("what_broken_looks_like"))),
        ]))
        return 3 * len(q & lens) + 2 * len(q & role) + 1 * len(q & prof)
    return score


def main():
    ap = argparse.ArgumentParser(description="Select QA-tester persona(s) from the configured pack.")
    ap.add_argument("--query", help="the bug / surface / feature in plain words")
    ap.add_argument("--tier", "--engagement", dest="tier", help="engagement tier id or alias (from the pack)")
    ap.add_argument("--role", help="one of the pack's roles")
    ap.add_argument("--lens", help="substring match on a persona's lenses")
    ap.add_argument("--id", help="one persona by id")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--full", action="store_true", help="include the full profile (the role to adopt)")
    ap.add_argument("--list", action="store_true", help="every persona, one line each")
    ap.add_argument("--spread", action="store_true", help="one persona per engagement tier (for explore walks)")
    ap.add_argument("--tiers", action="store_true", help="print the pack's roles, tiers and overlays")
    ap.add_argument("--pack", help="pack file (default: personas_file in stack.config.json)")
    ap.add_argument("--no-glossary", action="store_true", help="do not expand the query via conventions.domain_glossary")
    a = ap.parse_args()

    pack_path = resolve_pack_path(a.pack)
    pack = load_pack(pack_path)
    ps = pack["personas"]
    aliases = tier_alias_map(pack)

    if a.tiers:
        print(json.dumps({
            "pack": pack_path,
            "roles": pack.get("roles"),
            "engagement_tiers": pack.get("engagement_tiers"),
            "overlays": [{"id": o.get("id"), "label": o.get("label")} for o in pack.get("overlays") or []],
            "count": len(ps),
        }, indent=2)); return
    if a.list:
        print(json.dumps([slim(p, False) for p in ps], indent=2)); return
    if a.id is not None:
        hit = [p for p in ps if str(p.get("id")) == str(a.id)]
        print(json.dumps([slim(p, a.full) for p in hit], indent=2)); return

    pool = ps
    if a.tier:
        target = aliases.get(a.tier.lower())
        if target is None:
            die("unknown tier %r; the pack's tiers are %s" % (a.tier, tier_ids(pack)), path=pack_path)
        pool = [p for p in pool if str(p.get("tier", "")).lower() == target.lower()]
    if a.role:
        pool = [p for p in pool if str(p.get("role", "")).lower() == a.role.lower()]
    if a.lens:
        pool = [p for p in pool if a.lens.lower() in " ".join(_as_list(p.get("lenses"))).lower()]

    if a.spread:
        q = toks(a.query) if a.query else set()
        score = score_fn(q)
        out = []
        for tid in tier_ids(pack):
            cands = [p for p in pool if str(p.get("tier", "")).lower() == tid.lower()]
            if not cands:
                continue
            best = max(cands, key=score) if q else cands[0]
            out.append({**slim(best, a.full), "score": score(best)} if q else slim(best, a.full))
        print(json.dumps(out, indent=2)); return

    if a.query:
        q = toks(a.query)
        if not a.no_glossary:
            q = glossary_expand(q)
        score = score_fn(q)
        ranked = sorted(pool, key=score, reverse=True)
        out = [{**slim(p, a.full), "score": score(p)} for p in ranked[: a.k] if score(p) > 0]
    else:
        out = [slim(p, a.full) for p in pool[: a.k]]
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
