# Cutover night — data freeze + final incremental migration

Reference implementation for the cutover-night runbook: hold the member-data service in a
read-only window, run the final incremental copy into the new schema, then lift the freeze.
Everything below is reproducible from a clean checkout with Docker and Python 3.12.

## What is in the box

| Path | Purpose |
| --- | --- |
| `app/main.py` | Member-data service (FastAPI). HTTP middleware returns `503 service_temporarily_frozen` + maintenance text for `POST/PUT/PATCH/DELETE` while `FREEZE_ENABLED=true`; all `GET` endpoints keep serving. |
| `app/config.py` | Settings read from environment variables only (`SOURCE_DATABASE_URL`, `TARGET_DATABASE_URL`, `FREEZE_ENABLED`, `FREEZE_MESSAGE`). No credentials in code. |
| `app/db.py` | SQLAlchemy 2 models (`companies`, `contacts`) with `deleted_at` soft-delete column. |
| `migrations/sql/0001_schema.sql` | Target-schema DDL (UUID keys, audit columns, `deleted_at`). |
| `scripts/prepare_drill.py` | Deterministic fictional seed: 1,000 companies / 5,000 contacts, ~6% soft-deleted, some rows with later `updated_at`. |
| `scripts/incremental_migrate.py` | Incremental copy: `(updated_at, id)` composite watermark, keyset paging, `ON CONFLICT (id) DO UPDATE`, `migration_batches` ledger for batch-level idempotency, `--dry-run` opens the target connection `READ ONLY`. |
| `scripts/run_drill.py` | End-to-end drill that produces `evidence/drill_run.log`. |
| `scripts/run_freeze_drill.py` | Boots the real service twice (freeze on / off) and produces `evidence/freeze_run.log`. |
| `tests/` | pytest suite covering all five acceptance points. |

## Acceptance points → evidence

| # | Requirement | Where it is proven | Result |
| --- | --- | --- | --- |
| 1 | Dry-run reports pending rows **without** touching the target DB | `scripts/run_drill.py` (snapshot before/after + `SET TRANSACTION READ ONLY`), `tests/test_migration_idempotency.py::test_criterion1_dry_run_is_read_only` | target rows `0 → 0`, checksum unchanged, read-only insert raises `ReadOnlySqlTransaction` |
| 2 | Running the same batch twice leaves the target unchanged | `scripts/run_drill.py` (batch `night-01` replayed), `::test_criterion2_replay_same_batch_is_idempotent` | first run 1,000 + 5,000 rows; replay `rows_applied=0`, checksums `cf0e5320…` / `d3db3bea…` identical |
| 3 | Soft-deleted rows carry identical `deleted_at` | `scripts/run_drill.py`, `::test_criterion3_soft_deletes_keep_deleted_at` | companies 69/69, contacts 276/276, `deleted_at` matched row by row |
| 4 | Run log states total duration **and** per-table duration | `evidence/drill_run.log` | companies 0.093 s, contacts 0.405 s, total 0.498 s (wall clock 1.90 s for the whole drill) |
| 5 | During the freeze, writes answer 503 with a maintenance notice while reads stay 200 | `scripts/run_freeze_drill.py`, `tests/test_freeze_window.py` | GETs 200 throughout; `POST/PUT/PATCH/DELETE` → 503 `service_temporarily_frozen` + configured notice + `Retry-After: 3600`; write returns 201 once the flag is cleared |

Evidence files (regenerate with the commands below, they are not hand-written):

* `evidence/drill_run.log` — migration dry-run, apply, replay, soft-delete parity
* `evidence/freeze_run.log` — real HTTP freeze window behaviour
* `evidence/pytest.txt` — `8 passed`

## Run it

```bash
# 1. local Postgres with the source and target databases
docker compose up -d          # container ms-cutover-db, host port 55432

# 2. python environment
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. full drill (seed → dry-run → apply → replay → soft-delete parity)
.venv/bin/python -m scripts.run_drill

# 4. freeze window over real HTTP
.venv/bin/python -m scripts.run_freeze_drill

# 5. tests
.venv/bin/python -m pytest -v
```

Connection settings come from the environment (`app/config.py`); the compose defaults point at
`127.0.0.1:55432` with the `app` role, so nothing has to be exported for the drill.

## Operational notes

* **Freeze switch** — flip `FREEZE_ENABLED` and restart the service (or reload config through
  your own config bus). The flag is read once at startup; `/api/v1/freeze/status` reports the
  active state so the cutover commander can confirm it before the migration starts.
* **Why a ledger and not just an upsert** — the ledger makes a *whole batch* a no-op on replay
  without reading the data again; the row-level upsert is the second line of defence for
  batches that were partially applied before a crash.
* **Watermark safety** — `(updated_at, id)` is strictly greater-than, so rows updated during the
  window are picked up by the next batch. Writes are held by the freeze, so the watermark cannot
  slide past a concurrent update.
* **Soft deletes** — deleted rows are migrated, never filtered out, so the new schema can answer
  historical queries and the `deleted_at` values stay comparable for reconciliation.
* **Rollback** — the target database is disposable: `docker compose down -v` drops it and the
  source is untouched by design (the migration only ever opens it read-only when counting).

## Data / secrecy

All seed data is generated fiction (`Fictional Company 0000`, `Fictional Contact 00000`) with
random UUIDs; the delivery contains no customer names, no credentials, and no network calls to
third-party services. Credentials are read from environment variables only — `.env*` files are
git-ignored except `.env.example`.
