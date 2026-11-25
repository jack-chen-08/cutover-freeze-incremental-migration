#!/usr/bin/env python3
"""Prepare the fictional drill data: create the schema on both databases and seed
1,000 companies plus 5,000 contacts into the source database.

All data is fictional and generated deterministically from --seed, so the drill is
reproducible. Roughly 6% of rows are soft-deleted and 10% carry a later updated_at,
which is what the incremental migration has to pick up.
"""
from __future__ import annotations

import argparse
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SQL_DIR = Path(__file__).resolve().parent.parent / "migrations" / "sql"
BASE_TIME = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
INDUSTRIES = ["manufacturing", "logistics", "retail", "software", "construction",
              "finance", "healthcare", "education", "energy", "agriculture"]


def connect(dbname: str) -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "55432")),
        user=os.environ.get("PGUSER", "app"),
        password=os.environ.get("PGPASSWORD", "app_pw_change_me"),
        dbname=os.environ.get(f"{dbname.upper()}_DBNAME", dbname),
        row_factory=dict_row,
    )


def apply_schema(conn: psycopg.Connection) -> None:
    sql = (SQL_DIR / "0001_init.sql").read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def seed(conn: psycopg.Connection, n_companies: int, n_contacts: int, seed_value: int) -> dict:
    rng = random.Random(seed_value)
    org_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    site_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    actor = uuid.UUID("33333333-3333-3333-3333-333333333333")

    companies = []
    for i in range(n_companies):
        created = BASE_TIME + timedelta(minutes=i)
        updated = created + timedelta(minutes=rng.randint(0, 600))
        deleted = None
        if rng.random() < 0.06:
            deleted = created + timedelta(hours=2)
            updated = max(updated, deleted)
        companies.append((
            str(uuid.UUID(int=rng.getrandbits(128))),
            org_id, site_id,
            f"Fictional Company {i:04d}",
            f"TAX{i:08d}",
            INDUSTRIES[i % len(INDUSTRIES)],
            rng.randint(3, 400),
            "active" if deleted is None else "inactive",
            created, actor, updated, deleted,
        ))

    contacts = []
    for i in range(n_contacts):
        created = BASE_TIME + timedelta(minutes=i)
        updated = created + timedelta(minutes=rng.randint(0, 600))
        deleted = None
        if rng.random() < 0.06:
            deleted = created + timedelta(hours=2)
            updated = max(updated, deleted)
        contacts.append((
            str(uuid.UUID(int=rng.getrandbits(128))),
            org_id, site_id,
            companies[i % n_companies][0],
            f"Fictional Contact {i:05d}",
            f"1380000{i:04d}",
            f"contact{i:05d}@example.test",
            ["manager", "director", "engineer", "buyer"][i % 4],
            created, actor, updated, deleted,
        ))

    with conn.cursor() as cur:
        cur.execute("TRUNCATE companies, contacts, migration_state, migration_batches")
        cur.executemany(
            "INSERT INTO companies (id, org_id, site_id, name, tax_no, industry, "
            "employee_count, status, created_at, created_by, updated_at, deleted_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", companies,
        )
        cur.executemany(
            "INSERT INTO contacts (id, org_id, site_id, company_id, full_name, phone, email, "
            "title, created_at, created_by, updated_at, deleted_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", contacts,
        )
    conn.commit()
    return {
        "companies": n_companies,
        "contacts": n_contacts,
        "companies_soft_deleted": sum(1 for c in companies if c[-1] is not None),
        "contacts_soft_deleted": sum(1 for c in contacts if c[-1] is not None),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--companies", type=int, default=1000)
    ap.add_argument("--contacts", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261125)
    args = ap.parse_args()

    with connect("source") as source:
        apply_schema(source)
        stats = seed(source, args.companies, args.contacts, args.seed)
    with connect("target") as target:
        apply_schema(target)
        with target.cursor() as cur:
            cur.execute("TRUNCATE companies, contacts, migration_state, migration_batches")
        target.commit()

    print("drill data ready: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
