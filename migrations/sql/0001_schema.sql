-- Member-space schema, created identically on the trial-run source and the new
-- production target. Every business table carries the platform-wide audit columns:
--   id (UUID PK), org_id, site_id, created_at, created_by, updated_at, deleted_at
-- Tables are listed here in the dependency order used by the incremental script:
--   orgs -> sites -> roles -> users -> user_roles -> member_companies -> contacts
--   -> tags -> tag_links -> activities -> registrations -> opportunities -> kb_articles

CREATE TABLE IF NOT EXISTS orgs (
    id           UUID PRIMARY KEY,
    org_id       UUID,
    site_id      UUID,
    name         TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   UUID,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sites (
    id           UUID PRIMARY KEY,
    org_id       UUID        NOT NULL REFERENCES orgs (id),
    site_id      UUID,
    name         TEXT        NOT NULL,
    city         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   UUID,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS roles (
    id           UUID PRIMARY KEY,
    org_id       UUID,
    site_id      UUID,
    name         TEXT        NOT NULL,
    permissions  TEXT[]      NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   UUID,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS users (
    id           UUID PRIMARY KEY,
    org_id       UUID        NOT NULL REFERENCES orgs (id),
    site_id      UUID        REFERENCES sites (id),
    email        TEXT        NOT NULL,
    display_name TEXT        NOT NULL,
    legacy_no    BIGINT      NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   UUID,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS users_org_email_key ON users (org_id, email);
CREATE SEQUENCE IF NOT EXISTS users_legacy_no_seq OWNED BY users.legacy_no;

CREATE TABLE IF NOT EXISTS user_roles (
    id           UUID PRIMARY KEY,
    org_id       UUID        NOT NULL,
    site_id      UUID,
    user_id      UUID        NOT NULL REFERENCES users (id),
    role_id      UUID        NOT NULL REFERENCES roles (id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   UUID,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS member_companies (
    id             UUID PRIMARY KEY,
    org_id         UUID        NOT NULL REFERENCES orgs (id),
    site_id        UUID        REFERENCES sites (id),
    name           TEXT        NOT NULL,
    industry       TEXT,
    tax_no         TEXT,
    employee_count INTEGER     NOT NULL DEFAULT 0,
    contact_count  INTEGER     NOT NULL DEFAULT 0,   -- redundant counter, recomputed on cutover
    legacy_no      BIGINT      NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by     UUID,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ
);
CREATE SEQUENCE IF NOT EXISTS member_companies_legacy_no_seq OWNED BY member_companies.legacy_no;

CREATE TABLE IF NOT EXISTS contacts (
    id           UUID PRIMARY KEY,
    org_id       UUID        NOT NULL REFERENCES orgs (id),
    site_id      UUID,
    company_id   UUID        NOT NULL REFERENCES member_companies (id),
    full_name    TEXT        NOT NULL,
    title        TEXT,
    phone        TEXT,
    legacy_no    BIGINT      NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   UUID,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);
CREATE SEQUENCE IF NOT EXISTS contacts_legacy_no_seq OWNED BY contacts.legacy_no;

CREATE TABLE IF NOT EXISTS tags (
    id           UUID PRIMARY KEY,
    org_id       UUID        NOT NULL,
    site_id      UUID,
    label        TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   UUID,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tag_links (
    id           UUID PRIMARY KEY,
    org_id       UUID        NOT NULL,
    site_id      UUID,
    tag_id       UUID        NOT NULL REFERENCES tags (id),
    company_id   UUID        NOT NULL REFERENCES member_companies (id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   UUID,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS activities (
    id                 UUID PRIMARY KEY,
    org_id             UUID        NOT NULL,
    site_id            UUID,
    title              TEXT        NOT NULL,
    starts_at          TIMESTAMPTZ NOT NULL,
    registration_count INTEGER     NOT NULL DEFAULT 0,  -- redundant counter, recomputed on cutover
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by         UUID,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS registrations (
    id           UUID PRIMARY KEY,
    org_id       UUID        NOT NULL,
    site_id      UUID,
    activity_id  UUID        NOT NULL REFERENCES activities (id),
    contact_id   UUID        NOT NULL REFERENCES contacts (id),
    status       TEXT        NOT NULL DEFAULT 'confirmed',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   UUID,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS opportunities (
    id            UUID PRIMARY KEY,
    org_id        UUID        NOT NULL,
    site_id       UUID,
    company_id    UUID        REFERENCES member_companies (id),
    title         TEXT        NOT NULL,
    amount_cents  BIGINT      NOT NULL DEFAULT 0,   -- every amount is an integer in cents
    stage         TEXT        NOT NULL DEFAULT 'new',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    UUID,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS kb_articles (
    id           UUID PRIMARY KEY,
    org_id       UUID        NOT NULL,
    site_id      UUID,
    slug         TEXT        NOT NULL,
    title        TEXT        NOT NULL,
    body         TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   UUID,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);

-- Bookkeeping for the cutover itself (target side only, but created on both so the
-- schema stays identical).
CREATE TABLE IF NOT EXISTS migration_batches (
    id           BIGSERIAL PRIMARY KEY,
    batch_id     TEXT        NOT NULL,
    table_name   TEXT        NOT NULL,
    row_count    INTEGER     NOT NULL,
    checksum     TEXT        NOT NULL,
    duration_ms  INTEGER     NOT NULL DEFAULT 0,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (batch_id, table_name)
);

CREATE TABLE IF NOT EXISTS migration_state (
    key          TEXT PRIMARY KEY,
    value        TEXT        NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent column additions (keeps an existing drill database in step with the file).
ALTER TABLE member_companies ADD COLUMN IF NOT EXISTS tax_no TEXT;

-- The legacy_no columns are filled by the application layer, so the sequences must also
-- serve as column defaults (keeps hand-written INSERTs and the API in agreement).
ALTER TABLE users            ALTER COLUMN legacy_no SET DEFAULT nextval('users_legacy_no_seq');
ALTER TABLE member_companies ALTER COLUMN legacy_no SET DEFAULT nextval('member_companies_legacy_no_seq');
ALTER TABLE contacts         ALTER COLUMN legacy_no SET DEFAULT nextval('contacts_legacy_no_seq');
