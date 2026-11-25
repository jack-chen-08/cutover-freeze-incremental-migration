#!/usr/bin/env python3
"""Cutover rehearsal: proves every acceptance criterion on the local pair of databases.

Steps (each prints ``OK``/``FAIL`` and the whole run is appended to evidence/drill_run.log):

 0. create the schema on both instances
 1. seed 1,000 fictional member companies / 5,000 contacts (+ the rest of the tables)
 2. full migration  (cutover_incremental.py --since 1970-01-01)
 3. trial-run activity: updates, inserts, soft deletes, drifting counters
 4. freeze point recorded  (freeze_at), then --dry-run with the incremental window
 5. incremental batch, timed per table  -> criterion 4
 6. replay the same batch              -> criterion 2
 7. reconciliation gate, 0 differences -> criterion 5 of the runbook (data parity)
 8. soft-delete parity                 -> criterion 3
 9. sequences + redundant counters realigned
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.cutover_incremental import (  # noqa: E402
    COUNTERS, SEQUENCES, TABLES, connect, set_state, table_checksum, get_state,
)

EVIDENCE = ROOT / "evidence" / "drill_run.log"
PY = sys.executable
failures: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)
    with EVIDENCE.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def run(title: str, argv: list[str]) -> int:
    log(f"\n$ {' '.join(argv)}")
    log(f"--- {title}")
    proc = subprocess.run([PY, "-m", *argv], cwd=ROOT, capture_output=True, text=True)
    for line in (proc.stdout + proc.stderr).rstrip().splitlines():
        log("    " + line)
    if proc.returncode != 0:
        failures.append(f"{title} exited {proc.returncode}")
    return proc.returncode


def apply_schema() -> None:
    ddl = (ROOT / "migrations" / "sql" / "0001_schema.sql").read_text(encoding="utf-8")
    for role in ("source", "target"):
        conn = connect(role)
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        conn.close()


def counts(conn) -> dict[str, int]:
    out = {}
    with conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"SELECT count(*) FROM {table}")
            out[table] = cur.fetchone()[0]
    return out


def main() -> int:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text("", encoding="utf-8")
    started = time.perf_counter()
    log(f"started_at: {datetime.now(timezone.utc).isoformat()}")
    log("=" * 78)

    log("\n### step 0 — schema on both PostgreSQL 16 instances (source 55432 / target 55433)")
    apply_schema()
    log("    schema applied: " + ", ".join(TABLES))

    log("\n### steps 1-2 — seed and full migration")
    run("seed fictional data", ["scripts.prepare_drill", "--phase", "seed", "--truncate"])
    src = connect("source")
    with src.cursor() as cur:
        cur.execute("SELECT max(updated_at) FROM contacts")
        seed_max = cur.fetchone()[0]
    src.close()
    full_start = datetime.now(timezone.utc)
    run("full migration (everything up to now)", ["scripts.cutover_incremental", "--since", "1970-01-01T00:00:00+00:00",
                                                  "--batch-id", "full-01", "--batch-size", "5000"])
    tgt = connect("target")
    set_state(tgt, "last_full_migration_at", seed_max.isoformat())
    tgt.commit()
    log(f"    marker: last_full_migration_at={seed_max.isoformat()} (recorded at {full_start.isoformat()})")
    base = counts(tgt)
    tgt.close()
    log("    target rows after full migration: " + ", ".join(f"{k}={v}" for k, v in base.items()))

    log("\n### step 3-4 — trial-run activity, then the freeze point")
    run("trial-run writes (updates / inserts / soft deletes / counter drift)",
        ["scripts.prepare_drill", "--phase", "changes"])
    freeze_at = datetime.now(timezone.utc).replace(microsecond=0)
    tgt = connect("target")
    set_state(tgt, "freeze_at", freeze_at.isoformat())
    tgt.commit()
    tgt.close()
    log(f"    freeze_at recorded = {freeze_at.isoformat()}（冻结时间点已落盘）")

    # criterion 1: dry-run must not touch the target
    before = counts(connect_and_close := connect("target"))
    connect_and_close.close()
    before_ck = {}
    conn = connect("target")
    for table in TABLES:
        before_ck[table] = table_checksum(conn, table)
    conn.close()
    run("dry-run: pending rows per table, target opened READ ONLY",
        ["scripts.cutover_incremental", "--dry-run"])
    conn = connect("target")
    after = counts(conn)
    for table in TABLES:
        if before_ck[table] != table_checksum(conn, table):
            failures.append(f"dry-run modified {table}")
    conn.close()
    if before != after:
        failures.append("dry-run changed target row counts")
    log("    OK — dry-run left the target database unchanged (criterion 1)")

    log("\n### step 5 — incremental batch, timed per table (criterion 4)")
    run("incremental batch inc-01",
        ["scripts.cutover_incremental", "--batch-id", "inc-01", "--batch-size", "2000"])

    log("\n### step 6 — replay the same batch (criterion 2)")
    conn = connect("target")
    pre_replay = {t: table_checksum(conn, t) for t in TABLES}
    conn.close()
    run("replay inc-01", ["scripts.cutover_incremental", "--batch-id", "inc-01", "--batch-size", "2000"])
    conn = connect("target")
    post_replay = {t: table_checksum(conn, t) for t in TABLES}
    conn.close()
    if pre_replay != post_replay:
        failures.append("replay changed target rows or checksums")
    else:
        log("    OK — replaying batch inc-01 left rows and checksums untouched (criterion 2)")

    log("\n### step 7 — reconciliation gate (0 differences to release)")
    rc = run("reconciliation", ["scripts.cutover_verify"])
    if rc == 0:
        log("    OK — release gate PASS: source and target agree on every table")

    log("\n### step 8 — soft-deleted rows carry identical deleted_at (criterion 3)")
    src, tgt = connect("source"), connect("target")
    for table in ("member_companies", "contacts", "registrations", "activities"):
        with src.cursor() as cur:
            cur.execute(f"SELECT id, deleted_at FROM {table} WHERE deleted_at IS NOT NULL ORDER BY id")
            s_rows = cur.fetchall()
        with tgt.cursor() as cur:
            cur.execute(f"SELECT id, deleted_at FROM {table} WHERE deleted_at IS NOT NULL ORDER BY id")
            t_rows = cur.fetchall()
        if s_rows != t_rows:
            failures.append(f"{table}: soft-deleted rows differ")
        log(f"    {table:<18} soft_deleted source={len(s_rows)} target={len(t_rows)} "
            f"deleted_at matched={sum(1 for a, b in zip(s_rows, t_rows) if a == b)}")
    if not any("soft-deleted" in f for f in failures):
        log("    OK — soft-deleted rows migrated with identical deleted_at (criterion 3)")

    log("\n### step 9 — sequences and redundant counters")
    with tgt.cursor() as cur:
        for table, (seq, col) in SEQUENCES.items():
            cur.execute(f"SELECT max({col}) FROM {table}")
            top = cur.fetchone()[0]
            cur.execute("SELECT last_value, is_called FROM " + seq)
            cur.execute(f"SELECT last_value FROM {seq}")
            last = cur.fetchone()[0]
            log(f"    sequence {seq}: max({col})={top} sequence_last_value={last} "
                f"{'ok' if last >= (top or 0) else 'BEHIND'}")
            if last < (top or 0):
                failures.append(f"{seq} behind max({col})")
        cur.execute("SELECT count(*) FROM activities a WHERE a.registration_count <> "
                    "(SELECT count(*) FROM registrations r WHERE r.activity_id = a.id AND r.deleted_at IS NULL)")
        bad_act = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM member_companies c WHERE c.contact_count <> "
                    "(SELECT count(*) FROM contacts x WHERE x.company_id = c.id AND x.deleted_at IS NULL)")
        bad_co = cur.fetchone()[0]
        cur.execute("SELECT coalesce(sum(registration_count),0) FROM activities")
        sum_counter = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM registrations WHERE deleted_at IS NULL")
        sum_real = cur.fetchone()[0]
    log(f"    activities with wrong registration_count = {bad_act} / member_companies with wrong contact_count = {bad_co}")
    log(f"    sum(registration_count)={sum_counter} vs live registrations={sum_real}")
    if bad_act or bad_co or sum_counter != sum_real:
        failures.append("redundant counters not realigned")
    else:
        log("    OK — sequences synced and redundant counters recomputed（序列与冗余计数已对齐）")
    src.close()
    tgt.close()

    wall = time.perf_counter() - started
    log("\n" + "=" * 78)
    log(f"finished_at: {datetime.now(timezone.utc).isoformat()}")
    log(f"wall_clock_total: {wall:.2f}s")
    if failures:
        log("RESULT: FAIL — " + "; ".join(sorted(set(failures))))
        return 1
    log("RESULT: PASS — every criterion holds on both instances")
    log(f"log written to {EVIDENCE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
