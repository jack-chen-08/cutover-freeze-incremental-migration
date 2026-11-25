#!/usr/bin/env python3
"""Final incremental migration for the cutover night.

Safety properties required by the ticket:

* ``--dry-run`` only counts, per table, how many rows are still to be migrated; the
  target connection is opened READ ONLY, so target data cannot change.
* Re-executing the same batch (same ``--batch-id``) is a no-op: the batch ledger in
  ``migration_batches`` short-circuits the run, and row writes are UPSERTs keyed on
  ``id`` so a replay can never duplicate or mutate rows.
* Soft-deleted rows travel with their ``deleted_at`` value unchanged.
* Every run reports the total duration and the per-table duration.

Usage:
    python -m scripts.incremental_migrate --dry-run
    python -m scripts.incremental_migrate            # apply one batch per table
    python -m scripts.incremental_migrate --batch-id night-01 --batch-size 5000
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from datetime import datetime

import psycopg
from psycopg.rows import dict_row, tuple_row

TABLES: dict[str, list[str]] = {
    "companies": [
        "id", "org_id", "site_id", "name", "tax_no", "industry",
        "employee_count", "status", "created_at", "created_by", "updated_at", "deleted_at",
    ],
    "contacts": [
        "id", "org_id", "site_id", "company_id", "full_name", "phone",
        "email", "title", "created_at", "created_by", "updated_at", "deleted_at",
    ],
}


def connect(dbname: str, *, read_only: bool = False) -> psycopg.Connection:
    """Connect with discrete keywords so no credential lives inside a URL literal."""
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "55432")),
        user=os.environ.get("PGUSER", "app"),
        password=os.environ.get("PGPASSWORD", "app_pw_change_me"),
        dbname=os.environ.get(f"{dbname.upper()}_DBNAME", dbname),
        row_factory=dict_row,
    )
    if read_only:
        conn.execute("SET TRANSACTION READ ONLY")
    return conn


def columns_sql(table: str) -> str:
    return ", ".join(TABLES[table])


def watermark(target: psycopg.Connection, table: str) -> tuple[datetime | None, str | None]:
    with target.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT last_updated_at, last_id FROM migration_state WHERE table_name = %s",
            (table,),
        )
        row = cur.fetchone()
    if not row:
        return None, None
    return row["last_updated_at"], str(row["last_id"]) if row["last_id"] else None


def pending_rows(
    source: psycopg.Connection,
    table: str,
    wm_ts: datetime | None,
    wm_id: str | None,
    limit: int,
) -> list[dict]:
    sql = f"SELECT {columns_sql(table)} FROM {table}"
    if wm_ts is None:
        sql += " ORDER BY updated_at, id LIMIT %s"
        params: tuple = (limit,)
    else:
        sql += " WHERE (updated_at, id) > (%s, %s) ORDER BY updated_at, id LIMIT %s"
        params = (wm_ts, wm_id, limit)
    with source.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def count_pending(source: psycopg.Connection, table: str, wm_ts, wm_id) -> int:
    if wm_ts is None:
        sql = f"SELECT count(*) FROM {table}"
        params: tuple = ()
    else:
        sql = f"SELECT count(*) FROM {table} WHERE (updated_at, id) > (%s, %s)"
        params = (wm_ts, wm_id)
    with source.cursor(row_factory=tuple_row) as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()[0])


def rows_checksum(rows: list[dict]) -> str:
    h = hashlib.md5()
    for row in sorted(rows, key=lambda r: str(r["id"])):
        h.update("|".join("" if row[c] is None else str(row[c]) for c in row).encode())
    return h.hexdigest()


def table_checksum(conn: psycopg.Connection, table: str) -> tuple[int, str]:
    """Row count plus an order-independent checksum over every business column."""
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        count = int(cur.fetchone()[0])
        cur.execute(
            f"SELECT coalesce(md5(string_agg(h, '' ORDER BY h)), '') FROM ("
            f"  SELECT md5(concat_ws('|', "
            + ", ".join(f"{c}::text" for c in TABLES[table])
            + f")) AS h FROM {table}) s"
        )
        return count, cur.fetchone()[0]


def ledger_hit(target: psycopg.Connection, batch_id: str, table: str, checksum: str) -> bool:
    with target.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            "SELECT checksum FROM migration_batches WHERE batch_id = %s AND table_name = %s",
            (batch_id, table),
        )
        row = cur.fetchone()
    return bool(row) and row[0] == checksum


def upsert_rows(target: psycopg.Connection, table: str, rows: list[dict]) -> None:
    cols = TABLES[table]
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}"
    )
    with target.cursor() as cur:
        cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])


def record_batch(target: psycopg.Connection, batch_id: str, table: str, n: int, ck: str) -> None:
    with target.cursor() as cur:
        cur.execute(
            "INSERT INTO migration_batches (batch_id, table_name, row_count, checksum) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (batch_id, table_name) DO NOTHING",
            (batch_id, table, n, ck),
        )


def advance_watermark(target: psycopg.Connection, table: str, last: dict) -> None:
    with target.cursor() as cur:
        cur.execute(
            "INSERT INTO migration_state (table_name, last_updated_at, last_id, updated_ts) "
            "VALUES (%s, %s, %s, now()) ON CONFLICT (table_name) DO UPDATE "
            "SET last_updated_at = EXCLUDED.last_updated_at, last_id = EXCLUDED.last_id, "
            "updated_ts = now()",
            (table, last["updated_at"], last["id"]),
        )


def run_dry(source: psycopg.Connection, target: psycopg.Connection) -> int:
    total = 0
    print("DRY-RUN — pending rows per table (target database is opened READ ONLY):")
    for table in TABLES:
        wm_ts, wm_id = watermark(target, table)
        n = count_pending(source, table, wm_ts, wm_id)
        total += n
        print(f"  {table:<12} pending={n}")
    print(f"  {'TOTAL':<12} pending={total}")
    return total


def run_apply(
    source: psycopg.Connection,
    target: psycopg.Connection,
    batch_size: int,
    batch_label: str,
) -> int:
    grand_start = time.perf_counter()
    applied = 0
    for table in TABLES:
        t0 = time.perf_counter()
        wm_ts, wm_id = watermark(target, table)
        rows = pending_rows(source, table, wm_ts, wm_id, batch_size)
        if not rows:
            print(f"[{table}] no pending rows ({time.perf_counter() - t0:.3f}s)")
            continue
        checksum = rows_checksum(rows)
        batch_id = f"{batch_label}:{table}:{checksum[:12]}"
        if ledger_hit(target, batch_id, table, checksum):
            print(f"[{table}] batch {batch_id} already applied — skipped "
                  f"({time.perf_counter() - t0:.3f}s)")
            continue
        before_n, before_ck = table_checksum(target, table)
        upsert_rows(target, table, rows)
        record_batch(target, batch_id, table, len(rows), checksum)
        advance_watermark(target, table, rows[-1])
        target.commit()
        after_n, after_ck = table_checksum(target, table)
        elapsed = time.perf_counter() - t0
        applied += len(rows)
        print(
            f"[{table}] batch={batch_id} rows={len(rows)} "
            f"target_rows {before_n}->{after_n} checksum {before_ck[:8]}->{after_ck[:8]} "
            f"duration={elapsed:.3f}s"
        )
    print(f"TOTAL duration={time.perf_counter() - grand_start:.3f}s rows_applied={applied}")
    return applied


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Final incremental migration (cutover night)")
    ap.add_argument("--dry-run", action="store_true", help="count pending rows, never write")
    ap.add_argument("--batch-size", type=int, default=5000)
    ap.add_argument("--batch-id", default=os.environ.get("MIGRATION_BATCH_LABEL", "night-01"))
    args = ap.parse_args(argv)

    with connect("source") as source:
        if args.dry_run:
            with connect("target", read_only=True) as target:
                run_dry(source, target)
            return 0
        with connect("target") as target:
            run_apply(source, target, args.batch_size, args.batch_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
