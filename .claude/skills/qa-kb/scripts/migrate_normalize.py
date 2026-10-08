#!/usr/bin/env python3
"""migrate_normalize — fold the brain's mixed-case (kind, area) values onto the canonical
normalized form that kb.py now writes.

WHY THIS EXISTS
    kb.py normalizes `area`/`kind` on write AND read (`"Workspace Settings"` -> `workspace-settings`),
    and its reads also match anything that *normalizes* to the requested value — so the brain
    stays usable before this runs. But the rows themselves are still split across spellings
    ('Invoices' and 'invoices', 'Tasks' and 'tasks'), which means:
      * `UNIQUE (kind, area, key)` on app_map is enforced per-spelling, so the same selector
        can exist twice with different values, and a reader can get the stale one;
      * every read pays a normalizing sequential scan;
      * `decay`'s stale_app_map report double-counts an area.
    This script folds them together, once.

SAFETY
    Dry-run by DEFAULT. `--apply` is the only thing that writes, and it runs as ONE
    transaction (all-or-nothing). The only destructive act is deleting the LOSER of a
    normalization collision — the row whose (kind, area, key) would become a duplicate —
    and the winner is always the one with the newer `updated_at`. Every drop is reported
    explicitly (id, key, kept, dropped, value preview) and ledgered into kb_writes.

    Idempotent: after `--apply`, a re-run finds nothing to do.

TWO SECTIONS
    1. (kind, area) normalization — the fold described above, the only destructive part.
    2. hash backfill — fills `fix_lessons.symptom_hash` / `expectations.statement_hash` on rows
       written before kb.py's exact-duplicate guard existed, so a retry of one of THOSE rows
       bumps instead of cloning (it is the guard that works with no embedder). Pure derivation:
       it computes kb.text_hash() over the row's own text and writes it to the hash column —
       no text is changed and no row is deleted. It also REPORTS the exact-duplicate groups the
       hashes make visible; collapsing those is a human's decision, never this script's.

Usage
    python3 migrate_normalize.py                       # print the plan, change nothing
    python3 migrate_normalize.py --store app_map       # plan for one store
    python3 migrate_normalize.py --apply                # execute the plan
    python3 migrate_normalize.py --apply --yes          # skip the interactive confirmation
    python3 migrate_normalize.py --section hashes       # ONLY the (non-destructive) hash backfill
    python3 migrate_normalize.py --section hashes --apply --yes
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import kb  # noqa: E402  — same DSN resolution, same connect(), and the SAME norm functions

AREA_ONLY_STORES = ["expectations", "flakiness", "fix_lessons"]
ALL_STORES = ["app_map"] + AREA_ONLY_STORES


def _preview(value, n=80):
    try:
        return json.dumps(value)[:n]
    except (TypeError, ValueError):
        return str(value)[:n]


def plan_app_map(cur):
    """app_map is the only store where normalization can COLLIDE: UNIQUE (kind, area, key).

    Group every row by the target it would normalize to. A group of >1 means two spellings
    of the same logical row exist; the newest `updated_at` wins and the rest are dropped."""
    cur.execute("""SELECT id, kind, area, key, value, source, ticket, confidence,
                          last_confirmed_at, updated_at, version
                   FROM app_map ORDER BY id""")
    rows = [dict(r) for r in cur.fetchall()]
    groups = {}
    for r in rows:
        target = (kb.norm_kind(r["kind"]), kb.norm_area(r["area"]), r["key"])
        groups.setdefault(target, []).append(r)

    updates, merges, delete_ids = [], [], []
    for (nkind, narea, key), members in sorted(groups.items()):
        # Newest wins. `updated_at` is NOT NULL, so this ordering is total once id breaks ties.
        members = sorted(members, key=lambda r: (r["updated_at"], r["id"]), reverse=True)
        keeper, losers = members[0], members[1:]
        if losers:
            merges.append({
                "target": {"kind": nkind, "area": narea, "key": key},
                "kept": {"id": keeper["id"], "kind": keeper["kind"], "area": keeper["area"],
                         "updated_at": keeper["updated_at"], "source": keeper["source"],
                         "version": keeper["version"], "value_preview": _preview(keeper["value"])},
                "dropped": [{"id": l["id"], "kind": l["kind"], "area": l["area"],
                             "updated_at": l["updated_at"], "source": l["source"],
                             "version": l["version"], "value_preview": _preview(l["value"]),
                             "value_differs": l["value"] != keeper["value"]} for l in losers],
            })
            delete_ids.extend(l["id"] for l in losers)
        if keeper["kind"] != nkind or keeper["area"] != narea:
            updates.append({"id": keeper["id"], "key": key,
                            "from": {"kind": keeper["kind"], "area": keeper["area"]},
                            "to": {"kind": nkind, "area": narea}})

    return {
        "rows_total": len(rows),
        "updates": updates, "update_count": len(updates),
        "merges": merges, "merge_count": len(merges),
        "delete_ids": delete_ids, "delete_count": len(delete_ids),
        "unknown_kinds": sorted(set(
            kb.norm_kind(r["kind"]) for r in rows if kb.norm_kind(r["kind"]) not in kb.KNOWN_KINDS)),
    }


def plan_area_only(cur, table):
    """expectations / flakiness / fix_lessons: `area` is not part of any unique key, so
    normalizing it can never collide — every change is a plain in-place UPDATE."""
    cur.execute("SELECT id, area FROM {} ORDER BY id".format(table))
    rows = [dict(r) for r in cur.fetchall()]
    updates = [{"id": r["id"], "from": {"area": r["area"]}, "to": {"area": kb.norm_area(r["area"])}}
               for r in rows if r["area"] != kb.norm_area(r["area"])]
    return {"rows_total": len(rows), "updates": updates, "update_count": len(updates),
            "merges": [], "merge_count": 0, "delete_ids": [], "delete_count": 0}


def build_plan(cur, stores):
    plan = {}
    for store in stores:
        plan[store] = plan_app_map(cur) if store == "app_map" else plan_area_only(cur, store)
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — hash backfill (separate from the (kind, area) normalization above)
# ─────────────────────────────────────────────────────────────────────────────
# kb.py's exact-duplicate guard matches on (normalized area, sha256 of the normalized text).
# Rows written before that column existed carry NULL, so they can never be hash-matched and a
# retry of one of them would insert a clone. This section fills them in. It is pure derivation
# — no row is deleted, no text is changed — but it still runs dry by default and inside the
# same one transaction as section 1.
HASHED = [("fix_lessons", "symptom", "symptom_hash"),
          ("expectations", "statement", "statement_hash")]


def _has_col(cur, table, col):
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema()
                     AND table_name = %s AND column_name = %s""", (table, col))
    return cur.fetchone() is not None


def plan_hashes(cur, stores):
    """Rows whose stored hash does not equal kb.text_hash(<their own text>) — NULLs from before
    the column existed, plus anything a text edit left stale. Also reports the duplicate GROUPS
    the backfill makes visible: rows sharing (normalized area, hash) are exact duplicates that
    the guard would now collapse into one bump, which is a human's call, not this script's."""
    out = {}
    for table, text_col, hash_col in HASHED:
        if table not in stores:
            continue
        if not _has_col(cur, table, hash_col):
            out[table] = {"available": False, "rows_total": 0, "updates": [], "update_count": 0,
                          "duplicate_groups": [],
                          "reason": "{}.{} does not exist — apply scripts/schema.sql first".format(
                              table, hash_col)}
            continue
        cur.execute("SELECT id, area, {} AS text, {} AS stored FROM {} ORDER BY id".format(
                        text_col, hash_col, table))
        rows = [dict(r) for r in cur.fetchall()]
        updates, groups = [], {}
        for r in rows:
            digest = kb.text_hash(r["text"])
            groups.setdefault((kb.norm_area(r["area"]), digest), []).append(r["id"])
            if r["stored"] != digest:
                updates.append({"id": r["id"], "area": r["area"], "hash": digest,
                                "was": r["stored"], "text_preview": (r["text"] or "")[:80]})
        dups = [{"area": a, "hash": h, "ids": ids} for (a, h), ids in sorted(groups.items())
                if len(ids) > 1]
        out[table] = {"available": True, "rows_total": len(rows), "updates": updates,
                      "update_count": len(updates), "duplicate_groups": dups}
    return out


def apply_hashes(cur, hplan):
    """One UPDATE per row, then ONE ledger row per table. Never touches the text itself."""
    applied = {}
    for table, text_col, hash_col in HASHED:
        p = hplan.get(table)
        if not p or not p.get("available") or not p["updates"]:
            continue
        ids = []
        for u in p["updates"]:
            cur.execute("UPDATE {} SET {} = %s WHERE id = %s RETURNING id".format(table, hash_col),
                        (u["hash"], u["id"]))
            got = cur.fetchone()
            if got:
                ids.append(got["id"])
        cur.execute(
            """INSERT INTO kb_writes(verb, store, action, row_id, source, summary)
               VALUES('migrate_normalize', %s, 'hash_backfill', NULL, 'migrate_normalize', %s)""",
            (table, "backfilled {} on {} row(s)".format(hash_col, len(ids))))
        applied[table] = {"updated": ids, "updated_count": len(ids)}
    return applied


def hash_totals(hplan):
    return {"hash_updates": sum(p.get("update_count", 0) for p in hplan.values()),
            "duplicate_groups": sum(len(p.get("duplicate_groups", [])) for p in hplan.values()),
            "unavailable": sorted(t for t, p in hplan.items() if not p.get("available"))}


def apply_plan(cur, plan):
    """One transaction. Deletes first so the surviving row can take the target key without
    tripping UNIQUE (kind, area, key)."""
    applied = {}
    for store, p in sorted(plan.items()):
        deleted, updated = [], []
        if p["delete_ids"]:
            # Ledger the destructive half BEFORE it happens, so the audit row names a real id.
            for merge in p["merges"]:
                for d in merge["dropped"]:
                    cur.execute(
                        """INSERT INTO kb_writes(verb, store, action, row_id, source, area, summary)
                           VALUES('migrate_normalize', %s, 'merge_delete', %s, 'migrate_normalize',
                                  %s, %s)""",
                        (store, d["id"], merge["target"]["area"],
                         "merged into #{} as {}/{}/{}".format(
                             merge["kept"]["id"], merge["target"]["kind"],
                             merge["target"]["area"], merge["target"]["key"])[:200]))
            cur.execute("DELETE FROM {} WHERE id = ANY(%s) RETURNING id".format(store),
                        (p["delete_ids"],))
            deleted = [r["id"] for r in cur.fetchall()]

        for u in p["updates"]:
            if store == "app_map":
                cur.execute("UPDATE app_map SET kind=%s, area=%s WHERE id=%s RETURNING id",
                            (u["to"]["kind"], u["to"]["area"], u["id"]))
            else:
                cur.execute("UPDATE {} SET area=%s WHERE id=%s RETURNING id".format(store),
                            (u["to"]["area"], u["id"]))
            got = cur.fetchone()
            if got:
                updated.append(got["id"])

        if updated or deleted:
            cur.execute(
                """INSERT INTO kb_writes(verb, store, action, row_id, source, summary)
                   VALUES('migrate_normalize', %s, 'normalize', NULL, 'migrate_normalize', %s)""",
                (store, "normalized {} row(s), merged away {}".format(len(updated), len(deleted))))
        applied[store] = {"updated": updated, "updated_count": len(updated),
                          "deleted": deleted, "deleted_count": len(deleted)}
    return applied


def totals(plan):
    """Safe on an empty plan — `--section hashes` skips section 1 entirely."""
    return {"updates": sum(p["update_count"] for p in plan.values()),
            "deletes": sum(p["delete_count"] for p in plan.values()),
            "merges": sum(p["merge_count"] for p in plan.values())}


def human_summary(plan, dry_run, hplan=None, out=sys.stderr):
    t = totals(plan)
    print("== migrate_normalize {} ==".format("PLAN (dry run)" if dry_run else "APPLIED"), file=out)
    if plan:
        print("  -- (kind, area) normalization --", file=out)
    for store, p in sorted(plan.items()):
        print("  {:<13} {:>4} rows  {:>3} to normalize  {:>3} merge-deletes".format(
            store, p["rows_total"], p["update_count"], p["delete_count"]), file=out)
        for m in p["merges"]:
            tgt = m["target"]
            print("      MERGE {}/{}/{}: keep #{} ({}/{}), drop {}".format(
                tgt["kind"], tgt["area"], tgt["key"], m["kept"]["id"],
                m["kept"]["kind"], m["kept"]["area"],
                ", ".join("#{}{}".format(d["id"], " VALUE DIFFERS" if d["value_differs"] else "")
                          for d in m["dropped"])), file=out)
    if plan:
        print("  TOTAL: {} updates, {} deletes across {} merge groups".format(
            t["updates"], t["deletes"], t["merges"]), file=out)
    if hplan:
        ht = hash_totals(hplan)
        print("  -- hash backfill --", file=out)
        for table, p in sorted(hplan.items()):
            if not p.get("available"):
                print("  {:<13} UNAVAILABLE — {}".format(table, p.get("reason")), file=out)
                continue
            print("  {:<13} {:>4} rows  {:>3} hashes to backfill  {:>3} exact-duplicate group(s)"
                  .format(table, p["rows_total"], p["update_count"], len(p["duplicate_groups"])),
                  file=out)
            for g in p["duplicate_groups"]:
                print("      DUPLICATES in '{}': ids {} share one normalized text "
                      "(the guard will now bump instead of cloning; merging them is a human call)"
                      .format(g["area"], g["ids"]), file=out)
        print("  TOTAL: {} hashes to backfill".format(ht["hash_updates"]), file=out)
    if dry_run and (t["updates"] or t["deletes"] or (hplan and hash_totals(hplan)["hash_updates"])):
        print("  (nothing was changed — re-run with --apply)", file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="migrate_normalize",
        description="Normalize app_map/expectations/flakiness/fix_lessons (kind, area). "
                    "Dry-run by default; --apply executes.")
    ap.add_argument("--apply", action="store_true", help="execute the plan (default: dry run)")
    ap.add_argument("--yes", action="store_true", help="skip the --apply confirmation prompt")
    ap.add_argument("--store", choices=ALL_STORES + ["all"], default="all")
    ap.add_argument("--section", choices=["all", "normalize", "hashes"], default="all",
                    help="which section to plan/apply: 'normalize' is the (kind, area) fold "
                         "(the only destructive one), 'hashes' is the pure derived-column "
                         "backfill, 'all' (default) does both")
    ap.add_argument("--db", help="override DSN (else kb.py's config/env resolution)")
    args = ap.parse_args(argv)

    stores = ALL_STORES if args.store == "all" else [args.store]
    dsn = kb.resolve_dsn(args.db)   # the one resolver: --db > KB_DATABASE_URL > kb.config

    conn = None
    try:
        conn = kb.connect(dsn)
        with conn.cursor() as cur:
            plan = build_plan(cur, stores) if args.section in ("all", "normalize") else {}
            t = totals(plan)
            hplan = plan_hashes(cur, stores) if args.section in ("all", "hashes") else {}
            ht = hash_totals(hplan)
            t = dict(t, **ht)

            if not args.apply:
                conn.rollback()
                out = {"ok": True, "dry_run": True, "generated_at": kb._now_iso(),
                       "stores": plan, "hash_backfill": hplan, "totals": t,
                       "note": "nothing changed; re-run with --apply to execute"}
                print(json.dumps(out, default=str))
                human_summary(plan, dry_run=True, hplan=hplan)
                return 0

            if not args.yes and sys.stdin.isatty():
                human_summary(plan, dry_run=True, hplan=hplan)
                ans = input("Apply {} updates, {} DELETES and {} hash backfills? [y/N] ".format(
                    t["updates"], t["deletes"], t["hash_updates"])).strip().lower()
                if ans not in ("y", "yes"):
                    conn.rollback()
                    print(json.dumps({"ok": True, "dry_run": True, "aborted": "user declined",
                                      "stores": plan, "hash_backfill": hplan, "totals": t},
                                     default=str))
                    return 0

            applied = apply_plan(cur, plan)
            hash_applied = apply_hashes(cur, hplan)
            # Self-check INSIDE the transaction: recompute; an idempotent migration leaves nothing.
            residual = build_plan(cur, stores) if plan else {}
            residual_totals = totals(residual)
            residual_hash = hash_totals(plan_hashes(cur, stores) if hplan else {})
            residual_totals = dict(residual_totals, **residual_hash)
            if (residual_totals["updates"] or residual_totals["deletes"]
                    or residual_totals["hash_updates"]):
                conn.rollback()
                return kb.fail("migration_not_idempotent — rolled back",
                               residual=residual_totals, stores=residual)
        conn.commit()
        out = {"ok": True, "dry_run": False, "generated_at": kb._now_iso(),
               "stores": plan, "hash_backfill": hplan, "totals": t, "applied": applied,
               "hash_applied": hash_applied, "residual": residual_totals}
        print(json.dumps(out, default=str))
        human_summary(plan, dry_run=False, hplan=hplan)
        return 0
    except Exception as e:  # noqa: BLE001
        if conn:
            conn.rollback()
        return kb.fail("{}: {}".format(type(e).__name__, e))
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
