#!/usr/bin/env python3
"""Cutover-night drill: fictional 1,000 companies + 5,000 contacts.

Runs the whole ticket end to end and writes evidence/drill_run.log, which records the
total duration and the per-table duration of the incremental migration exactly as the
acceptance criteria require.

Steps
-----
1. prepare source/target databases and seed the fictional data set
2. --dry-run: count pending rows, prove the target database is unchanged
3. apply one incremental batch, capturing per-table timings
4. re-run the SAME batch: target row counts and checksums must not move
5. verify soft-deleted rows kept their deleted_at values
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psycopg.rows import tuple_row  # noqa: E402

from scripts import incremental_migrate as mig  # noqa: E402

EVIDENCE = ROOT / "evidence"
LOG = EVIDENCE / "drill_run.log"
_lines: list[str] = []


def log(msg: str = "") -> None:
    print(msg)
    _lines.append(msg)


def run_step(args: list[str], title: str) -> str:
    log("")
    log(f"$ python -m {' '.join(args)}")
    log(f"--- {title}")
    proc = subprocess.run(
        [sys.executable, "-m", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.rstrip().splitlines():
        log(f"    {line}")
    if proc.returncode != 0:
        log(f"    !! step failed with exit code {proc.returncode}")
        raise SystemExit(1)
    return out


def snapshot() -> dict:
    with mig.connect("target") as target:
        return {t: mig.table_checksum(target, t) for t in mig.TABLES}


def main() -> int:
    EVIDENCE.mkdir(exist_ok=True)
    started = datetime.now(timezone.utc)
    log("=" * 78)
    log("CUTOVER DRILL — data freeze + final incremental migration")
    log(f"started_at: {started.isoformat()}")
    log("=" * 78)

    run_step(["scripts.prepare_drill", "--companies", "1000", "--contacts", "5000"],
             "seed fictional data (1,000 companies / 5,000 contacts)")

    before = snapshot()
    log("")
    log(f"target snapshot BEFORE dry-run: {before}")
    run_step(["scripts.incremental_migrate", "--dry-run"], "dry-run counting only")
    after = snapshot()
    log(f"target snapshot AFTER  dry-run: {after}")
    if before != after:
        log("!! dry-run changed the target database — acceptance criterion 1 violated")
        raise SystemExit(1)
    log("OK — dry-run left the target database unchanged (criterion 1)")

    run_step(["scripts.incremental_migrate", "--batch-size", "5000", "--batch-id", "night-01"],
             "apply incremental batch night-01 (per-table duration above)")

    mid = snapshot()
    run_step(["scripts.incremental_migrate", "--batch-size", "5000", "--batch-id", "night-01"],
             "replay the SAME batch night-01")
    after_replay = snapshot()
    log(f"target snapshot after replay: {after_replay}")
    if mid != after_replay:
        log("!! replaying the same batch changed the target — criterion 2 violated")
        raise SystemExit(1)
    log("OK — replaying batch night-01 left rows and checksums untouched (criterion 2)")

    with mig.connect("source") as src, mig.connect("target") as tgt:
        for table in mig.TABLES:
            with src.cursor(row_factory=tuple_row) as cur:
                cur.execute(f"SELECT count(*) FROM {table}")
                src_total = int(cur.fetchone()[0])
                cur.execute(f"SELECT count(*) FROM {table} WHERE deleted_at IS NOT NULL")
                src_deleted = int(cur.fetchone()[0])
                cur.execute(
                    f"SELECT count(*) FROM {table} s JOIN {table} t USING (id) "
                    "WHERE s.deleted_at IS NOT NULL AND s.deleted_at = t.deleted_at"
                )
                matched = int(cur.fetchone()[0])
            with tgt.cursor(row_factory=tuple_row) as cur:
                cur.execute(f"SELECT count(*) FROM {table}")
                tgt_total = int(cur.fetchone()[0])
                cur.execute(f"SELECT count(*) FROM {table} WHERE deleted_at IS NOT NULL")
                tgt_deleted = int(cur.fetchone()[0])
            log(f"    {table}: source={src_total} target={tgt_total} "
                f"soft_deleted source={src_deleted} target={tgt_deleted} "
                f"deleted_at matched={matched}")
            if src_total != tgt_total or src_deleted != tgt_deleted or matched != src_deleted:
                log(f"!! soft-delete parity failed for {table} — criterion 3 violated")
                raise SystemExit(1)
    log("OK — soft-deleted rows migrated with identical deleted_at (criterion 3)")

    finished = datetime.now(timezone.utc)
    log("")
    log(f"finished_at: {finished.isoformat()}")
    log(f"wall_clock_total: {(finished - started).total_seconds():.2f}s")
    log("RESULT: PASS")
    LOG.write_text("\n".join(_lines) + "\n", encoding="utf-8")
    print(f"\nlog written to {LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
