"""Alembic environment — the target database URL comes from the environment.

The project keeps its DDL in ``migrations/sql/*.sql`` so the very same statements are
used by the drill, by Alembic and by a hand-run psql session; Alembic revisions simply
execute those files. ``CUTOVER_DB_ROLE=source`` points Alembic at the trial-run database
when the schema has to exist on both sides.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

role = os.environ.get("CUTOVER_DB_ROLE", "target")
prefix = "SOURCE" if role == "source" else "TARGET"
default_port, default_db = ("55432", "source") if role == "source" else ("55435", "target")
url = (
    f"postgresql+psycopg://{os.environ.get('PGUSER', 'app')}:"
    f"{os.environ.get('PGPASSWORD', 'app_pw_change_me')}@"
    f"{os.environ.get(prefix + '_HOST', '127.0.0.1')}:"
    f"{os.environ.get(prefix + '_PORT', default_port)}/"
    f"{os.environ.get(prefix + '_DB', default_db)}"
)
config.set_main_option("sqlalchemy.url", url)

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
