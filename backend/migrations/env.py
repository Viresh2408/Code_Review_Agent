"""
Alembic environment configuration.

Reads the database URL from the same Settings object as the app,
so there's a single source of truth for DB connection strings.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ── Load app models so Alembic can detect schema changes ─────────────────────
# Import Base BEFORE importing models so the mapper registry is populated
from app.db.base import Base  # noqa: F401
from app.db.models import (  # noqa: F401
    DebtScore,
    Finding,
    Installation,
    PullRequest,
    Repo,
    Review,
)

config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use the sync DB URL (Alembic doesn't support asyncpg natively)
sync_url = os.environ.get(
    "SYNC_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/codereview",
)
config.set_main_option("sqlalchemy.url", sync_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generate SQL without a live connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (apply directly to the database)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
