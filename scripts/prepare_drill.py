#!/usr/bin/env python3
"""Build the drill data set on the trial-run source, deterministically.

    scripts/prepare_drill.py --phase seed      # 1,000 member companies / 5,000 contacts + the rest
    scripts/prepare_drill.py --phase changes   # writes that happen after the full migration

``--phase changes`` is what makes the incremental pass meaningful: rows get a newer
``updated_at``, some rows are soft-deleted, and a few rows are brand new.
"""
from __future__ import annotations

import argparse
import random
import uuid
from datetime import datetime, timedelta, timezone

from scripts.cutover_incremental import TABLES, connect

BASE_TIME = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
NAMESPACE = uuid.UUID("6f1c9b0a-3c1e-4f39-9a2f-0d0c1f3b7a55")
INDUSTRIES = ["manufacturing", "logistics", "retail", "software", "construction",
              "finance", "healthcare", "education", "energy", "agriculture"]
TITLES = ["manager", "director", "engineer", "buyer"]


def sid(*parts: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, "|".join(parts))


def truncate(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(reversed(list(TABLES))) + " RESTART IDENTITY CASCADE")
        cur.execute("TRUNCATE migration_batches RESTART IDENTITY")
        cur.execute("TRUNCATE migration_state")
    conn.commit()


def seed(conn, *, companies: int = 1000, contacts: int = 5000, rng: random.Random) -> dict:
    org = sid("org", "1")
    site_a, site_b = sid("site", "a"), sid("site", "b")
    roles = {name: sid("role", name) for name in ("admin", "sales", "member")}
    users = [sid("user", str(i)) for i in range(12)]
    tags = {label: sid("tag", label) for label in
            ("vip", "manufacturing", "logistics", "startup", "investor")}
    company_ids = [sid("company", str(i)) for i in range(companies)]
    contact_ids = [sid("contact", str(i)) for i in range(contacts)]
    activity_ids = [sid("activity", str(i)) for i in range(8)]
    kb_ids = [sid("kb", str(i)) for i in range(30)]

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO orgs (id, org_id, site_id, name, status, created_at, created_by, updated_at) "
            "VALUES (%s, %s, NULL, %s, 'active', %s, NULL, %s)",
            (org, org, "Fictional Member Space Org", BASE_TIME, BASE_TIME),
        )
        for name, site_id in (("Fictional Site A", site_a), ("Fictional Site B", site_b)):
            cur.execute(
                "INSERT INTO sites (id, org_id, site_id, name, city, created_at, created_by, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, NULL, %s)",
                (site_id, org, site_id, name, "Fictional City", BASE_TIME, BASE_TIME),
            )
        for name, role_id in roles.items():
            cur.execute(
                "INSERT INTO roles (id, org_id, site_id, name, permissions, created_at, created_by, updated_at) "
                "VALUES (%s, %s, NULL, %s, %s, %s, NULL, %s)",
                (role_id, org, name, [f"{name}:read", f"{name}:write"], BASE_TIME, BASE_TIME),
            )
        for i, user_id in enumerate(users):
            cur.execute(
                "INSERT INTO users (id, org_id, site_id, email, display_name, legacy_no, "
                "created_at, created_by, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s)",
                (user_id, org, site_a if i % 2 == 0 else site_b,
                 f"user{i:02d}@example.invalid", f"Fictional User {i:02d}", 1000 + i,
                 BASE_TIME, BASE_TIME),
            )
            cur.execute(
                "INSERT INTO user_roles (id, org_id, site_id, user_id, role_id, created_at, updated_at) "
                "VALUES (%s, %s, NULL, %s, %s, %s, %s)",
                (sid("user_role", str(i)), org, user_id,
                 roles["admin"] if i == 0 else roles["member"], BASE_TIME, BASE_TIME),
            )
        for i, cid in enumerate(company_ids):
            deleted = BASE_TIME + timedelta(minutes=i) if i % 16 == 0 else None
            cur.execute(
                "INSERT INTO member_companies (id, org_id, site_id, name, industry, employee_count, "
                "contact_count, legacy_no, created_at, created_by, updated_at, deleted_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (cid, org, site_a if i % 2 == 0 else site_b, f"Fictional Company {i:04d}",
                 INDUSTRIES[i % len(INDUSTRIES)], rng.randint(5, 400), 0, 20000 + i,
                 BASE_TIME + timedelta(seconds=i), users[i % len(users)],
                 BASE_TIME + timedelta(seconds=i), deleted),
            )
        for i, contact_id in enumerate(contact_ids):
            company = company_ids[i % companies]
            deleted = BASE_TIME + timedelta(hours=1, seconds=i) if i % 18 == 0 else None
            cur.execute(
                "INSERT INTO contacts (id, org_id, site_id, company_id, full_name, title, phone, "
                "legacy_no, created_at, created_by, updated_at, deleted_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (contact_id, org, site_a if i % 2 == 0 else site_b, company,
                 f"Fictional Contact {i:05d}", TITLES[i % len(TITLES)],
                 f"+1000000{i:04d}", 500000 + i, BASE_TIME + timedelta(seconds=i),
                 users[i % len(users)], BASE_TIME + timedelta(seconds=i), deleted),
            )
        for label, tag_id in tags.items():
            cur.execute(
                "INSERT INTO tags (id, org_id, site_id, label, created_at, updated_at) "
                "VALUES (%s, %s, NULL, %s, %s, %s)",
                (tag_id, org, label, BASE_TIME, BASE_TIME),
            )
        for i in range(0, companies, 4):
            tag_id = tags[INDUSTRIES[i % len(INDUSTRIES)]] if INDUSTRIES[i % len(INDUSTRIES)] in tags else tags["vip"]
            cur.execute(
                "INSERT INTO tag_links (id, org_id, site_id, tag_id, company_id, created_at, updated_at) "
                "VALUES (%s, %s, NULL, %s, %s, %s, %s)",
                (sid("tag_link", str(i)), org, tag_id, company_ids[i], BASE_TIME, BASE_TIME),
            )
        for i, activity_id in enumerate(activity_ids):
            cur.execute(
                "INSERT INTO activities (id, org_id, site_id, title, starts_at, registration_count, "
                "created_at, created_by, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (activity_id, org, site_a, f"Fictional Activity {i:02d}",
                 BASE_TIME + timedelta(days=7 + i), 0, BASE_TIME, users[i % len(users)], BASE_TIME),
            )
        for i, contact_id in enumerate(contact_ids[:2000]):
            activity_id = activity_ids[i % len(activity_ids)]
            deleted = BASE_TIME + timedelta(hours=2, seconds=i) if i % 40 == 0 else None
            cur.execute(
                "INSERT INTO registrations (id, org_id, site_id, activity_id, contact_id, status, "
                "created_at, created_by, updated_at, deleted_at) "
                "VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s)",
                (sid("registration", str(i)), org, activity_id, contact_id, "confirmed",
                 BASE_TIME + timedelta(seconds=i), users[i % len(users)],
                 BASE_TIME + timedelta(seconds=i), deleted),
            )
        for i in range(120):
            cur.execute(
                "INSERT INTO opportunities (id, org_id, site_id, company_id, title, amount_cents, stage, "
                "created_at, created_by, updated_at) "
                "VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s)",
                (sid("opportunity", str(i)), org, company_ids[i % companies],
                 f"Fictional Opportunity {i:03d}", rng.randint(100_000, 9_000_000),
                 ("new", "contacted", "won")[i % 3], BASE_TIME, users[i % len(users)], BASE_TIME),
            )
        for i, kb_id in enumerate(kb_ids):
            cur.execute(
                "INSERT INTO kb_articles (id, org_id, site_id, slug, title, body, created_at, updated_at) "
                "VALUES (%s, %s, NULL, %s, %s, %s, %s, %s)",
                (kb_id, org, f"fictional-article-{i:02d}", f"Fictional Article {i:02d}",
                 "Body text generated for the cutover drill.", BASE_TIME, BASE_TIME),
            )
    conn.commit()
    return {"companies": companies, "contacts": contacts}


def changes(conn, rng: random.Random) -> dict:
    """Write activity that happens between the full migration and the freeze."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    touched = {"member_companies": 40, "contacts": 60}
    with conn.cursor() as cur:
        for table, n in touched.items():
            if table == "member_companies":
                cur.execute(
                    "SELECT id FROM member_companies WHERE deleted_at IS NULL ORDER BY id LIMIT %s", (n,))
            else:
                cur.execute(
                    "SELECT id FROM contacts WHERE deleted_at IS NULL ORDER BY id LIMIT %s", (n,))
            ids = [r[0] for r in cur.fetchall()]
            cur.executemany(
                f"UPDATE {table} SET updated_at = %s, created_by = %s WHERE id = %s",
                [(now, sid("user", str(i % 12)), rid) for i, rid in enumerate(ids)],
            )
        # new rows written during the trial run
        org = sid("org", "1")
        cur.execute(
            "SELECT id FROM sites ORDER BY id LIMIT 2")
        site_ids = [r[0] for r in cur.fetchall()]
        for i in range(25):
            cur.execute(
                "INSERT INTO contacts (id, org_id, site_id, company_id, full_name, title, phone, legacy_no, "
                "created_at, created_by, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (sid("contact", f"new-{i}"), org, site_ids[i % len(site_ids)],
                 sid("company", str(i)), f"Fictional Contact NEW{i:03d}", "buyer",
                 f"+1000009{i:04d}", 600000 + i, now, sid("user", "1"), now),
            )
        # soft deletes that happen during the trial run
        cur.execute("SELECT id FROM registrations WHERE deleted_at IS NULL ORDER BY id LIMIT 30")
        reg_ids = [r[0] for r in cur.fetchall()]
        cur.executemany("UPDATE registrations SET deleted_at = %s, updated_at = %s WHERE id = %s",
                        [(now, now, rid) for rid in reg_ids])
        cur.execute("SELECT id FROM activities ORDER BY id LIMIT 2")
        act_ids = [r[0] for r in cur.fetchall()]
        cur.executemany("UPDATE activities SET deleted_at = %s, updated_at = %s WHERE id = %s",
                        [(now, now, rid) for rid in act_ids])
        # drift the redundant counters so the recompute step has real work to do
        cur.execute("UPDATE activities SET registration_count = registration_count + 7")
        cur.execute("UPDATE member_companies SET contact_count = contact_count + 3")
    conn.commit()
    return {"updated": touched, "inserted_contacts": 25,
            "soft_deleted_registrations": len(reg_ids), "soft_deleted_activities": len(act_ids)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Prepare the cutover drill data set")
    ap.add_argument("--phase", choices=("seed", "changes"), default="seed")
    ap.add_argument("--companies", type=int, default=1000)
    ap.add_argument("--contacts", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261125)
    ap.add_argument("--truncate", action="store_true", help="wipe both sides first (seed phase only)")
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    source = connect("source")
    try:
        if args.truncate:
            truncate(source)
            target = connect("target")
            truncate(target)
            target.close()
        if args.phase == "seed":
            info = seed(source, companies=args.companies, contacts=args.contacts, rng=rng)
            print(f"    drill data ready: companies={info['companies']}, contacts={info['contacts']}")
        else:
            info = changes(source, rng)
            print(f"    trial-run activity applied: {info}")
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
