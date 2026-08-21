# Alembic migration environment for Hive (issue #14).
#
# Reads the database URL from the environment (ALEMBIC_DATABASE_URL, falling
# back to the same DATABASE_URL the app uses) and imports every model so
# autogenerate can diff against ``database.Base``.
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make ``backend`` importable (models import ``from database import Base``).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Alembic Config object (alembic.ini in this directory).
config = context.config

# Interpret the config file for Python logging when present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    """Raw resolved URL: ALEMBIC_DATABASE_URL, then DATABASE_URL, then default."""
    return (
        os.getenv("ALEMBIC_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or "sqlite+aiosqlite:///./agent_marketplace.db"
    )


def _to_sync(url: str) -> str:
    """Translate async driver schemes to sync equivalents for Alembic."""
    url = url.replace("+aiosqlite", "").replace("postgresql+asyncpg", "postgresql")
    if url.startswith("postgres://"):  # legacy SQLAlchemy 1.x scheme
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def _to_async(url: str) -> str:
    """Translate sync schemes to async equivalents for database.py's engine."""
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


# The app's ``database`` module builds an async engine from DATABASE_URL at
# import time, so it must always see an async-driver URL — even when the
# operator handed Alembic a sync one via ALEMBIC_DATABASE_URL. Nothing
# connects at import time; we only need ``Base.metadata`` registered.
os.environ["DATABASE_URL"] = _to_async(_to_sync(_resolve_url()))

from database import Base  # noqa: E402
import models  # noqa: E402,F401  — registers all tables on Base.metadata

# The single MetaData object describing the current schema.
target_metadata = Base.metadata

# Sync URL Alembic itself connects with.
_SYNC_URL = _to_sync(_resolve_url())


def run_migrations_offline() -> None:
    """Configure the context with just a URL (no DBAPI required)."""
    context.configure(
        url=_SYNC_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Create an Engine and associate a connection with the context."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _SYNC_URL
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
