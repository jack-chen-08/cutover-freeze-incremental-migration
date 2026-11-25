#!/usr/bin/env python3
"""Final incremental migration for the member-space cutover night.

    scripts/cutover_incremental.py --dry-run
    scripts/cutover_incremental.py --tables contacts,registrations --since 2026-11-24T00:00:00Z
    scripts/cutover_incremental.py --batch-id night-01 --batch-size 5000

Behaviour (see docs/runbook.md for the operational steps):

* Tables run in dependency order — organisations and sites first, knowledge base last.
* A row is in scope when ``updated_at > --since`` and, if a freeze point is known,
  ``updated_at <= freeze_at``.  ``--since`` defaults to the watermark stored in the
  target's ``migration_state`` (i.e. "the last full migration").
* Writes are ``INSERT ... ON CONFLICT (id) DO UPDATE`` so a batch can be replayed.
  The ``migration_batches`` ledger short-circuits a whole table when the same batch id
  and checksum were already applied.
* Soft-deleted rows (``deleted_at`` not null) are migrated like any other row, so the
  target keeps the same tombstones.
* After the data lands: sequences that back the legacy integer columns are re-synced
  and the redundant counters (``member_companies.contact_count``,
  ``activities.registration_count``) are recomputed from the target itself.
* Every run appends a log line per table with row count and duration, plus the total.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row, tuple_row

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "evidence" / "cutover_incremental.log"

# Dependency order: a table only ever references tables listed before it.
TABLES: dict[str, list[str]] = {
    "orgs": ["id", "org_id", "site_id", "name", "status", "created_at", "created_by", "updated_at", "deleted_at"],
    "sites": ["id", "org_id", "site_id", "name", "city", "created_at", "created_by", "updated_at", "deleted_at"],
    "roles": ["id", "org_id", "site_id", "name", "permissions", "created_at", "created_by", "updated_at", "deleted_at"],
    "users": ["id", "org_id", "site_id", "email", "display_name", "legacy_no", "created_at", "created_by", "updated_at", "deleted_at"],
    "user_roles": ["id", "org_id", "site_id", "user_id", "role_id", "created_at", "created_by", "updated_at", "deleted_at"],
    "member_companies": ["id", "org_id", "site_id", "name", "industry", "tax_no", "employee_count", "contact_count", "legacy_no", "created_at", "created_by", "updated_at", "deleted_at"],
    "contacts": ["id", "org_id", "site_id", "company_id", "full_name", "title", "phone", "legacy_no", "created_at", "created_by", "updated_at", "deleted_at"],
    "tags": ["id", "org_id", "site_id", "label", "created_at", "created_by", "updated_at", "deleted_at"],
    "tag_links": ["id", "org_id", "site_id", "tag_id", "company_id", "created_at", "created_by", "updated_at", "deleted_at"],
    "activities": ["id", "org_id", "site_id", "title", "starts_at", "registration_count", "created_at", "created_by", "updated_at", "deleted_at"],
    "registrations": ["id", "org_id", "site_id", "activity_id", "contact_id", "status", "created_at", "created_by", "updated_at", "deleted_at"],
    "opportunities": ["id", "org_id", "site_id", "company_id", "title", "amount_cents", "stage", "created_at", "created_by", "updated_at", "deleted_at"],
    "kb_articles": ["id", "org_id", "site_id", "slug", "title", "body", "created_at", "created_by", "updated_at", "deleted_at"],
}

# Legacy integer columns that still have a sequence behind them on the new schema.
SEQUENCES = {
    "users": ("users_legacy_no_seq", "legacy_no"),
    "member_companies": ("member_companies_legacy_no_seq", "legacy_no"),
    "contacts": ("contacts_legacy_no_seq", "legacy_no"),
}

# Redundant counters recomputed from the target after the data lands.
COUNTERS = {
    "member_companies": (
        "UPDATE member_companies c SET contact_count = sub.n "
        "FROM (SELECT company_id, count(*) AS n FROM contacts WHERE deleted_at IS NULL "
        "      GROUP BY company_id) sub WHERE c.id = sub.company_id "
        "  AND c.contact_count <> sub.n",
    ),
    "activities": (
        "UPDATE activities a SET registration_count = sub.n "
        "FROM (SELECT activity_id, count(*) AS n FROM registrations WHERE deleted_at IS NULL "
        "      GROUP BY activity_id) sub WHERE a.id = sub.activity_id "
        "  AND a.registration_count <> sub.n",
    ),
}


def log(msg: str, path: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def connect(role: str, *, read_only: bool = False) -> psycopg.Connection:
    """Open the source (trial run) or target (new production) database.

    Hosts, ports and credentials come from the environment; the defaults match
    docker-compose.yml so the drill runs without exporting anything.
    """
    prefix = "SOURCE" if role == "source" else "TARGET"
    defaults = {"SOURCE": ("55432", "source"), "TARGET": ("55435", "target")}[prefix]
    conn = psycopg.connect(
        host=os.environ.get(f"{prefix}_HOST", "127.0.0.1"),
        port=os.environ.get(f"{prefix}_PORT", defaults[0]),
        user=os.environ.get("PGUSER", "app"),
        password=os.environ.get("PGPASSWORD", "app_pw_change_me"),
        dbname=os.environ.get(f"{prefix}_DB", defaults[1]),
        autocommit=False,
    )
    if read_only:
        conn.read_only = True
    return conn


def columns_sql(table: str) -> str:
    return ", ".join(TABLES[table])


def get_state(target: psycopg.Connection, key: str) -> str | None:
    with target.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT value FROM migration_state WHERE key = %s", (key,))
        row = cur.fetchone()
    return row[0] if row else None


def set_state(target: psycopg.Connection, key: str, value: str) -> None:
    with target.cursor() as cur:
        cur.execute(
            "INSERT INTO migration_state (key, value, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, value),
        )


def watermark(target: psycopg.Connection, table: str) -> tuple[datetime | None, str | None]:
    """Last applied (updated_at, id) for a table, so batches can resume."""
    raw_ts = get_state(target, f"watermark:{table}:ts")
    raw_id = get_state(target, f"watermark:{table}:id")
    if not raw_ts:
        return None, None
    return datetime.fromisoformat(raw_ts), raw_id


def pending_rows(
    source: psycopg.Connection,
    table: str,
    since: datetime | None,
    until: datetime | None,
    wm_ts: datetime | None,
    wm_id: str | None,
    limit: int,
) -> list[dict]:
    """Keyset page ordered by (updated_at, id).

    ``since`` is the cutover start of interest (default: last full migration),
    ``until`` is the freeze point — nothing written after the freeze belongs to this run.
    """
    sql = f"SELECT {columns_sql(table)} FROM {table} WHERE 1 = 1"
    params: list = []
    low = max([d for d in (since, wm_ts) if d is not None], default=None)
    if low is not None:
        sql += " AND (updated_at, id) > (%s, %s)"
        params += [low, wm_id or "00000000-0000-0000-0000-000000000000"]
    if until is not None:
        sql += " AND updated_at <= %s"
        params.append(until)
    sql += " ORDER BY updated_at, id LIMIT %s"
    params.append(limit)
    with source.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def count_pending(
    source: psycopg.Connection,
    table: str,
    since: datetime | None,
    until: datetime | None,
    wm_ts: datetime | None,
    wm_id: str | None,
) -> int:
    sql = f"SELECT count(*) FROM {table} WHERE 1 = 1"
    params: list = []
    low = max([d for d in (since, wm_ts) if d is not None], default=None)
    if low is not None:
        sql += " AND (updated_at, id) > (%s, %s)"
        params += [low, wm_id or "00000000-0000-0000-0000-000000000000"]
    if until is not None:
        sql += " AND updated_at <= %s"
        params.append(until)
    with source.cursor(row_factory=tuple_row) as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()[0])


def rows_checksum(rows: list[dict]) -> str:
    h = hashlib.md5()
    for row in sorted(rows, key=lambda r: str(r["id"])):
        h.update("|".join("" if row[c] is None else str(row[c]) for c in row).encode())
    return h.hexdigest()


def table_checksum(
    conn: psycopg.Connection, table: str, cols: list[str] | None = None
) -> tuple[int, str]:
    cols = cols or TABLES[table]
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        count = int(cur.fetchone()[0])
        cur.execute(
            "SELECT coalesce(md5(string_agg(h, '' ORDER BY h)), '') FROM ("
            "  SELECT md5(concat_ws('|', " + ", ".join(f"{c}::text" for c in cols) + f")) AS h FROM {table}) s"
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
    if not rows:
        return
    cols = TABLES[table]
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
    with target.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            [[row[c] for c in cols] for row in rows],
        )


def record_batch(target: psycopg.Connection, batch_id: str, table: str, n: int, ck: str, ms: int) -> None:
    with target.cursor() as cur:
        cur.execute(
            "INSERT INTO migration_batches (batch_id, table_name, row_count, checksum, duration_ms) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (batch_id, table_name) DO NOTHING",
            (batch_id, table, n, ck, ms),
        )


def sync_sequences(target: psycopg.Connection) -> list[str]:
    notes = []
    for table, (seq, col) in SEQUENCES.items():
        with target.cursor(row_factory=tuple_row) as cur:
            cur.execute(f"SELECT coalesce(max({col}), 0) FROM {table}")
            top = int(cur.fetchone()[0])
            cur.execute("SELECT setval(%s, %s, %s)", (seq, max(top, 1), top > 0))
        notes.append(f"{table}.{col} sequence -> {top}")
    return notes


def recompute_counters(target: psycopg.Connection) -> list[str]:
    notes = []
    for table, (stmt,) in COUNTERS.items():
        with target.cursor() as cur:
            cur.execute(stmt)
            notes.append(f"{table}: {cur.rowcount} redundant counter rows realigned")
    return notes


def resolve_since(target: psycopg.Connection, raw: str | None) -> datetime | None:
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    last_full = get_state(target, "last_full_migration_at")
    return datetime.fromisoformat(last_full) if last_full else None


def resolve_freeze_at(target: psycopg.Connection, raw: str | None) -> datetime | None:
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    recorded = get_state(target, "freeze_at")
    return datetime.fromisoformat(recorded) if recorded else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cutover-night incremental migration")
    ap.add_argument("--dry-run", action="store_true", help="only print pending rows per table")
    ap.add_argument("--tables", help="comma separated subset, default: every table in dependency order")
    ap.add_argument("--since", help="ISO timestamp; defaults to last_full_migration_at on the target")
    ap.add_argument("--freeze-at", dest="freeze_at", help="ISO timestamp; defaults to freeze_at on the target")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--batch-id", default=datetime.now(timezone.utc).strftime("batch-%Y%m%dT%H%M%S"))
    ap.add_argument("--record-freeze-at", dest="record_freeze_at",
                    help="store the freeze point in migration_state and exit")
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    args = ap.parse_args(argv)

    logpath = Path(args.log)
    if args.record_freeze_at:
        stamp = datetime.fromisoformat(args.record_freeze_at.replace("Z", "+00:00"))
        target = connect("target")
        set_state(target, "freeze_at", stamp.isoformat())
        target.commit()
        target.close()
        print(f"freeze_at recorded = {stamp.isoformat()}")
        return 0

    tables = [t.strip() for t in args.tables.split(",")] if args.tables else list(TABLES)
    unknown = [t for t in tables if t not in TABLES]
    if unknown:
        print(f"unknown table(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    ordered = [t for t in TABLES if t in tables]

    source = connect("source")
    target = connect("target", read_only=args.dry_run)
    since = resolve_since(target, args.since)
    freeze_at = resolve_freeze_at(target, args.freeze_at)

    log(f"run start — batch={args.batch_id} tables={','.join(ordered)}", logpath)
    log(f"since={since.isoformat() if since else 'beginning'}  freeze_at={freeze_at.isoformat() if freeze_at else 'not recorded'}", logpath)

    if args.dry_run:
        total = 0
        log("DRY-RUN — counting only, target connection is READ ONLY", logpath)
        for table in ordered:
            wm_ts, wm_id = watermark(target, table)
            n = count_pending(source, table, since, freeze_at, wm_ts, wm_id)
            total += n
            log(f"  {table:<18} pending={n}", logpath)
        log(f"  {'TOTAL':<18} pending={total}", logpath)
        source.close()
        target.close()
        return 0

    grand = 0
    started = time.perf_counter()
    for table in ordered:
        t0 = time.perf_counter()
        wm_ts, wm_id = watermark(target, table)
        applied = 0
        last_seen: dict | None = None
        while True:
            rows = pending_rows(source, table, since, freeze_at, wm_ts, wm_id, args.batch_size)
            if not rows:
                break
            checksum = rows_checksum(rows)
            if ledger_hit(target, args.batch_id, table, checksum):
                log(f"  [{table}] batch already applied (ledger hit) — stopping", logpath)
                break
            upsert_rows(target, table, rows)
            last_seen = rows[-1]
            wm_ts, wm_id = last_seen["updated_at"], str(last_seen["id"])
            applied += len(rows)
            target.commit()
        if last_seen is not None:
            ms = int((time.perf_counter() - t0) * 1000)
            record_batch(target, args.batch_id, table, applied, rows_checksum([last_seen]), ms)
            set_state(target, f"watermark:{table}:ts", last_seen["updated_at"].isoformat())
            set_state(target, f"watermark:{table}:id", str(last_seen["id"]))
            target.commit()
        grand += applied
        duration = time.perf_counter() - t0
        log(f"  {table:<18} applied={applied:<6} duration={duration:.3f}s", logpath)

    for note in sync_sequences(target):
        log(f"  sequence sync — {note}", logpath)
    for note in recompute_counters(target):
        log(f"  counter recompute — {note}", logpath)
    target.commit()

    if freeze_at is not None:
        set_state(target, "last_incremental_migration_at", datetime.now(timezone.utc).isoformat())
        set_state(target, "last_incremental_freeze_at", freeze_at.isoformat())
        target.commit()

    total_duration = time.perf_counter() - started
    log(f"  TOTAL applied={grand} duration={total_duration:.3f}s", logpath)
    log("run end", logpath)
    source.close()
    target.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
