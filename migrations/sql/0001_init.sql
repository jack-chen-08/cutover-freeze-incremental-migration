-- Schema shared by source and target databases.
-- Unified conventions: every business table carries id (UUID), org_id, site_id,
-- created_at, created_by, updated_at, deleted_at (soft delete). Money is integer cents.

CREATE TABLE IF NOT EXISTS companies (
    id              UUID PRIMARY KEY,
    org_id          UUID        NOT NULL,
    site_id         UUID        NOT NULL,
    name            TEXT        NOT NULL,
    tax_no          TEXT,
    industry        TEXT,
    employee_count  INTEGER     NOT NULL DEFAULT 0,
    status          TEXT        NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL,
    created_by      UUID,
    updated_at      TIMESTAMPTZ NOT NULL,
    deleted_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS contacts (
    id          UUID PRIMARY KEY,
    org_id      UUID        NOT NULL,
    site_id     UUID        NOT NULL,
    company_id  UUID        NOT NULL,
    full_name   TEXT        NOT NULL,
    phone       TEXT,
    email       TEXT,
    title       TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    created_by  UUID,
    updated_at  TIMESTAMPTZ NOT NULL,
    deleted_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_companies_updated_at ON companies (updated_at, id);
CREATE INDEX IF NOT EXISTS idx_contacts_updated_at  ON contacts  (updated_at, id);
CREATE INDEX IF NOT EXISTS idx_contacts_company     ON contacts  (company_id);

-- Target-only bookkeeping tables (harmless when created on the source database).
CREATE TABLE IF NOT EXISTS migration_state (
    table_name      TEXT PRIMARY KEY,
    last_updated_at TIMESTAMPTZ,
    last_id         UUID,
    updated_ts      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS migration_batches (
    batch_id    TEXT        NOT NULL,
    table_name  TEXT        NOT NULL,
    row_count   INTEGER     NOT NULL,
    checksum    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, table_name)
);
