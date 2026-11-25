# cutover-freeze-incremental-migration

Cutover-night package for the member-space platform: put the service into a **read-only
freeze window**, run the **final incremental migration** into the new production database,
**realign sequences and redundant counters**, and only open the gate when a **reconciliation
script reports zero differences**.

Everything below is reproducible on two local PostgreSQL 16 instances — the trial-run
source (`:55432`) and the new production target (`:55435`) — with fictional data only
(1,000 member companies / 5,000 contacts plus supporting rows).

```bash
docker compose up -d
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m scripts.run_drill          # full rehearsal  -> evidence/drill_run.log
.venv/bin/python -m scripts.run_freeze_drill   # freeze window   -> evidence/freeze_run.log
.venv/bin/python -m pytest -q                  # 14 tests
```

## Acceptance criteria → where the proof lives

| Criterion | Implementation | Evidence (real run, this repo) |
| --- | --- | --- |
| **1.** `--dry-run` prints pending rows per table and leaves the target untouched | `scripts/cutover_incremental.py --dry-run` — target connection opened `READ ONLY` (`connect(role, read_only=True)`), counting only | `evidence/drill_run.log` §step 4: per-table `pending=`, `TOTAL pending=157`; checksums/counts before == after; `tests/test_cutover_incremental.py::test_criterion1_dry_run_is_read_only` proves an INSERT is refused by the server |
| **2.** Same batch twice → same rows and checksums | `migration_batches` ledger (`batch_id, table, rows, checksum, duration_ms`) short-circuits a known batch; row-level `INSERT … ON CONFLICT (id) DO UPDATE` is the second line of defence | `evidence/drill_run.log` §step 6: replay of `inc-01` → `applied=0`, per-table checksums identical; `test_criterion2_replay_same_batch_is_idempotent` |
| **3.** Soft-deleted rows carry identical `deleted_at` | `deleted_at` is a normal migrated column; deleted rows are **not** filtered out | §step 8: member_companies 63/63, contacts 278/278, registrations 80/80, activities 2/2 — `deleted_at` matched row by row; `test_criterion3_soft_deletes_keep_deleted_at` |
| **4.** 1,000 companies / 5,000 contacts, total **and** per-table duration | one line per table with `applied=` + `duration=` and a `TOTAL … duration=` line | §step 5: full load `TOTAL applied=8443 duration=1.515s` (companies 0.164s, contacts 0.775s), incremental `TOTAL applied=157 duration=0.130s`; wall clock 8.7s |
| **5.** Freeze window: writes → 503 + maintenance notice, reads → 200 | two independent layers: gateway snippet `gateway/nginx-freeze.conf` (`map` on request method) and app middleware `app/main.py` (`FREEZE_ENABLED`) | `evidence/freeze_run.log`: real HTTP, `GET /health`, `/api/v1/companies`, `/api/v1/contacts`, `/api/v1/freeze/status` all 200; `POST/PUT/PATCH/DELETE` all **503** with `service_temporarily_frozen` + notice + `Retry-After: 3600`; after lifting the flag `POST` → **201** |

## Ticket scope (beyond the five criteria)

| Scope item | Where |
| --- | --- |
| ② Incremental filter `updated_at > last migration point` **and** `≤ freeze_at` | `pending_rows()` — keyset on `(updated_at, id)`, hard upper bound `until`; `--record-freeze-at` / `--freeze-at` store and honour the freeze point in `migration_state`. Test: `test_freeze_upper_bound_excludes_later_rows` |
| ③ Eight dependency-ordered business tables | `TABLES` in `scripts/cutover_incremental.py`: orgs → sites → roles → users → user_roles → member_companies → contacts → tags → tag_links → activities → registrations → opportunities → kb_articles. Order is asserted by `test_dependency_order_puts_parents_first` |
| ④ Sequences synced + redundant counters recomputed | `sync_sequences()` / `recompute_counters()` — run after data load: `member_companies.contact_count`, `activities.registration_count` recomputed from live child rows; drill §step 9 shows `sum(registration_count)=1920 == live registrations=1920` and sequence `last_value ≥ max(legacy_no)` |
| ⑤ Reconciliation script, zero difference to release | `scripts/cutover_verify.py` — per-table row count + order-independent checksum over business columns, derived counters checked separately against the recompute, exits 1 on any difference. Drill §step 7: `RELEASE GATE: PASS — 13 tables, 0 differences` |
| ① Gateway freeze snippet + recorded `freeze_at` | `gateway/nginx-freeze.conf` (map block + server block + deploy/lift commands), `freeze_at` persisted via `--record-freeze-at` and printed in `evidence/freeze_run.log` |
| Script name / flags | `scripts/cutover_incremental.py` with `--dry-run`, `--tables`, `--since`, `--freeze-at`, `--record-freeze-at`, `--batch-id`, `--batch-size`, `--log` |
| Two local PostgreSQL 16 instances | `docker-compose.yml` — separate containers `ms-cutover-source` / `ms-cutover-target` |
| Data-step runbook | `docs/runbook.md` — 7 sections, every step with a read-back criterion and rollback row |
| Schema changes under Alembic | `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_baseline.py` execute `migrations/sql/0001_schema.sql` (one DDL source for Alembic, drill and psql) |

## Design notes

* **Ids and audit columns.** Every business table has `id UUID`, `org_id`, `site_id`,
  `created_at`, `created_by`, `updated_at`, `deleted_at`; `users`, `member_companies`,
  `contacts` additionally keep the legacy integer `legacy_no` with its sequence.
* **Idempotent by construction.** Ledger first, upsert second, checksum recorded per batch;
  re-running is always safe and never double-counts.
* **Target is disposable, source is never written.** `--dry-run` uses a READ ONLY
  transaction; the migration only ever reads the source. Rollback = drop and rebuild the
  target.
* **Frozen means frozen at two layers.** The gateway answers 503 even if the app is
  restarted or a deployment slips through; the app answers 503 even if the gateway config
  has not been reloaded yet. Same message text so users never see two different notices.
* **Fictional data only.** No customer names, no real tax numbers, phone numbers are masked
  (`+1****000001`). Credentials come from environment variables / `.env` (see `.env.example`).

## Layout

```
app/                 freeze middleware + read/write endpoints (FastAPI, SQLAlchemy 2)
scripts/             cutover_incremental.py, cutover_verify.py, prepare_drill.py,
                     run_drill.py, run_freeze_drill.py
gateway/             nginx freeze snippet
migrations/          sql/0001_schema.sql, alembic env + baseline revision
docs/runbook.md      cutover-night operating procedure
evidence/            drill_run.log, freeze_run.log, pytest.txt (real outputs)
tests/               pytest gate: 5 freeze tests + 9 migration tests
```

## Environment

`SOURCE_HOST/PORT/DB`, `TARGET_HOST/PORT/DB`, `PGUSER`, `PGPASSWORD`, `FREEZE_ENABLED`,
`FREEZE_MESSAGE` — all optional, defaults match `docker-compose.yml`. No credential is
committed; `.env` is git-ignored.
