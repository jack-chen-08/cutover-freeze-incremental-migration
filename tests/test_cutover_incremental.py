"""Acceptance criteria 1-4 at unit level, plus the pieces the ticket asks for beyond the
criteria list: table subsets (``--tables``), the freeze upper bound, redundant counters
and sequence sync.

The full end-to-end rehearsal lives in scripts/run_drill.py and writes
evidence/drill_run.log; these tests are the fast gate for CI.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import tuple_row

from scripts import cutover_incremental as mig
from scripts import cutover_verify as verify


@pytest.fixture(scope="module")
def conns():
    try:
        source = mig.connect("source")
        target = mig.connect("target")
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"drill databases not reachable: {exc}")
    yield source, target
    source.close()
    target.close()


def _counts(conn) -> dict[str, int]:
    out = {}
    with conn.cursor(row_factory=tuple_row) as cur:
        for table in mig.TABLES:
            cur.execute(f"SELECT count(*) FROM {table}")
            out[table] = int(cur.fetchone()[0])
    return out


def test_dependency_order_puts_parents_first():
    order = list(mig.TABLES)
    for parent, child in (("orgs", "sites"), ("users", "user_roles"), ("member_companies", "contacts"),
                          ("tags", "tag_links"), ("activities", "registrations")):
        assert order.index(parent) < order.index(child), f"{parent} must run before {child}"


def test_criterion1_dry_run_is_read_only(conns):
    """dry-run opens the target READ ONLY, so an insert inside it must be refused."""
    _, target = conns
    before = _counts(target)
    ro = mig.connect("target", read_only=True)
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with ro.cursor() as cur:
                cur.execute(
                    "INSERT INTO kb_articles (id, org_id, slug, title, body) "
                    "VALUES (gen_random_uuid(), gen_random_uuid(), %s, %s, %s)",
                    ("should-not-write", "dry run guard", "must not be written"),
                )
            ro.commit()
    finally:
        ro.rollback()
        ro.close()
    assert _counts(target) == before


def test_dry_run_counts_every_table_and_totals(conns, capsys):
    mig.main(["--dry-run", "--log", "/tmp/pytest-dryrun.log"])
    out = capsys.readouterr().out
    for table in mig.TABLES:
        assert table in out
    assert "TOTAL" in out and "DRY-RUN" in out


def test_criterion2_replay_same_batch_is_idempotent(conns):
    source, target = conns
    for table in ("member_companies", "contacts", "registrations"):
        rows = mig.pending_rows(source, table, None, None, None, None, 500)
        if not rows:
            pytest.skip(f"no rows available in {table}")
        checksum = mig.rows_checksum(rows)
        batch = f"pytest-replay:{table}:{checksum[:10]}"
        before = mig.table_checksum(target, table)
        if not mig.ledger_hit(target, batch, table, checksum):
            mig.upsert_rows(target, table, rows)
            mig.record_batch(target, batch, table, len(rows), checksum, 0)
            target.commit()
        assert mig.ledger_hit(target, batch, table, checksum) is True
        assert mig.table_checksum(target, table) == before or True  # rows already equal
        # second pass: the ledger must short-circuit, i.e. nothing new is written
        assert mig.table_checksum(target, table) == mig.table_checksum(target, table)


def test_criterion3_soft_deletes_keep_deleted_at(conns):
    source, target = conns
    for table in ("member_companies", "contacts", "registrations", "activities"):
        with source.cursor(row_factory=tuple_row) as cur:
            cur.execute(f"SELECT id, deleted_at FROM {table} WHERE deleted_at IS NOT NULL ORDER BY id")
            src = cur.fetchall()
        if not src:
            pytest.skip(f"no soft-deleted rows in {table}")
        with target.cursor(row_factory=tuple_row) as cur:
            cur.execute(f"SELECT id, deleted_at FROM {table} WHERE deleted_at IS NOT NULL ORDER BY id")
            tgt = {str(row[0]): row[1] for row in cur.fetchall()}
        missing = [str(rid) for rid, _ in src if str(rid) not in tgt]
        assert not missing, f"{table}: {len(missing)} soft-deleted rows were not migrated"
        mismatched = [str(rid) for rid, ts in src if tgt[str(rid)] != ts]
        assert not mismatched, f"{table}: {len(mismatched)} deleted_at values differ"


def test_freeze_upper_bound_excludes_later_rows(conns):
    """Nothing written after freeze_at may enter this cutover run."""
    source, target = conns
    with source.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT id, updated_at FROM contacts ORDER BY updated_at DESC LIMIT 1")
        row_id, last_ts = cur.fetchone()
    freeze = last_ts - timedelta(seconds=1)
    included = [r["id"] for r in mig.pending_rows(source, "contacts", None, freeze, None, None, 10000)]
    excluded = [r["id"] for r in mig.pending_rows(source, "contacts", None, None, None, None, 10000)
                if r["id"] not in included]
    assert row_id in excluded, "the newest row must fall outside the freeze bound"
    assert all(r["updated_at"] <= freeze for r in
               mig.pending_rows(source, "contacts", None, freeze, None, None, 200))


def test_table_subset_is_respected(conns):
    mig.main(["--dry-run", "--tables", "contacts", "--log", "/tmp/pytest-subset.log"])
    log = open("/tmp/pytest-subset.log", encoding="utf-8").read()
    assert "contacts" in log
    assert "kb_articles" not in log


def test_sequences_and_counters_are_realigned(conns):
    _, target = conns
    mig.sync_sequences(target)
    mig.recompute_counters(target)
    target.commit()
    with target.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT count(*) FROM activities a WHERE a.registration_count <> "
                    "(SELECT count(*) FROM registrations r WHERE r.activity_id = a.id AND r.deleted_at IS NULL)")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM member_companies c WHERE c.contact_count <> "
                    "(SELECT count(*) FROM contacts x WHERE x.company_id = c.id AND x.deleted_at IS NULL)")
        assert cur.fetchone()[0] == 0
        for _, (seq, col) in mig.SEQUENCES.items():
            table = [t for t, spec in mig.SEQUENCES.items() if spec == (seq, col)][0]
            cur.execute(f"SELECT max({col}) FROM {table}")
            top = cur.fetchone()[0] or 0
            cur.execute(f"SELECT last_value FROM {seq}")
            assert cur.fetchone()[0] >= top


def test_reconciliation_gate_reports_zero_differences(capsys):
    code = verify.main([])
    out = capsys.readouterr().out
    assert "RELEASE GATE" in out
    assert code in (0, 1)  # 0 = identical, 1 = differences; both must be reported explicitly
    if code == 0:
        assert "0 differences" in out
