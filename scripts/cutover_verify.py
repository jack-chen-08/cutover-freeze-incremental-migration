#!/usr/bin/env python3
"""Independent reconciliation between the trial-run source and the new production target.

Compares, per table, the row count and an order-independent checksum over every
business column.  Any difference is a release blocker: the script exits non-zero
(``--gate`` semantics are built in — zero difference is the only pass).

    scripts/cutover_verify.py                 # all tables
    scripts/cutover_verify.py --tables contacts,activities
    scripts/cutover_verify.py --json          # machine readable summary
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.cutover_incremental import TABLES, connect, table_checksum

# Columns that are *derived* on the target by the cutover itself (redundant counters
# recomputed in step 4 of the runbook).  They are compared separately: the target value
# must equal the live count, not whatever the trial-run source drifted to.
DERIVED = {
    "member_companies": ["contact_count"],
    "activities": ["registration_count"],
}


def compare_columns(table: str) -> list[str]:
    return [c for c in TABLES[table] if c not in DERIVED.get(table, [])]


def derived_check(conn, table: str) -> tuple[int, str]:
    """Recompute the derived counters inside the database and report mismatches."""
    if table == "member_companies":
        sql = ("SELECT count(*) FROM member_companies c WHERE c.contact_count <> "
               "(SELECT count(*) FROM contacts x WHERE x.company_id = c.id AND x.deleted_at IS NULL)")
    elif table == "activities":
        sql = ("SELECT count(*) FROM activities a WHERE a.registration_count <> "
               "(SELECT count(*) FROM registrations r WHERE r.activity_id = a.id AND r.deleted_at IS NULL)")
    else:
        return 0, ""
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0], "recomputed"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cutover reconciliation gate")
    ap.add_argument("--tables", help="comma separated subset")
    ap.add_argument("--json", action="store_true", help="print a JSON summary only")
    args = ap.parse_args(argv)

    tables = [t.strip() for t in args.tables.split(",")] if args.tables else list(TABLES)
    unknown = [t for t in tables if t not in TABLES]
    if unknown:
        print(f"unknown table(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    source = connect("source")
    target = connect("target")
    rows, diffs = [], 0
    try:
        for table in tables:
            cols = compare_columns(table)
            s_count, s_ck = table_checksum(source, table, cols)
            t_count, t_ck = table_checksum(target, table, cols)
            derived_bad, _ = derived_check(target, table)
            delta = t_count - s_count
            same = (s_ck == t_ck)
            if not same or delta or derived_bad:
                diffs += 1
            rows.append({
                "table": table,
                "source_rows": s_count,
                "target_rows": t_count,
                "row_delta": delta,
                "source_checksum": s_ck,
                "target_checksum": t_ck,
                "derived_mismatches": derived_bad,
                "compared_columns": len(cols),
                "match": same and delta == 0 and derived_bad == 0,
            })
    finally:
        source.close()
        target.close()

    if args.json:
        print(json.dumps({"tables": rows, "differences": diffs, "passed": diffs == 0},
                         ensure_ascii=False, indent=1))
    else:
        print(f"{'table':<18}{'source':>8}{'target':>8}{'delta':>8}  checksum")
        for r in rows:
            flag = "ok" if r["match"] else "DIFF"
            print(f"{r['table']:<18}{r['source_rows']:>8}{r['target_rows']:>8}{r['row_delta']:>+8}  "
                  f"{r['source_checksum'][:12] or '-'} / {r['target_checksum'][:12] or '-'}  {flag}")
        print()
        if diffs == 0:
            print(f"RELEASE GATE: PASS — {len(rows)} tables, 0 differences（差异为 0，放行）")
        else:
            print(f"RELEASE GATE: FAIL — {diffs} table(s) differ, cutover must not proceed（差异非 0，拦住放行）")
    return 0 if diffs == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
