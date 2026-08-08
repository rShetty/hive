"""Database configuration and session management.

Supports both SQLite (dev default) and PostgreSQL (prod). The backend
auto-detects the dialect from DATABASE_URL and configures the engine
appropriately:

  * SQLite → NullPool, PRAGMA-based auto-migration
  * Postgres → AsyncAdaptedQueuePool, information_schema-based auto-migration

Row-level locking via ``SELECT … FOR UPDATE`` is available through the
``lock_for_update()`` helper. On SQLite this is a no-op (SQLite serialises
writes via database-level locking); on Postgres it acquires a row lock.
"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import text
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agent_marketplace.db")

IS_POSTGRES = DATABASE_URL.startswith(("postgresql", "postgres"))
IS_SQLITE = DATABASE_URL.startswith("sqlite")

# Postgres uses connection pooling; SQLite uses NullPool (file-based, no
# benefit from pooling and can cause "database is locked" under concurrency).
_engine_kwargs: dict = {"echo": False}
if IS_SQLITE:
    _engine_kwargs["poolclass"] = __import__(
        "sqlalchemy.pool", fromlist=["NullPool"]
    ).NullPool

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


async def get_db():
    """Dependency for getting async database sessions."""
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def lock_for_update(query):
    """Apply ``SELECT … FOR UPDATE`` to a SQLAlchemy query.

    On Postgres this acquires a row-level lock, preventing concurrent
    transactions from modifying the row until the current transaction
    commits. On SQLite it is a no-op — SQLite serialises writes via
    database-level locking, and ``FOR UPDATE`` is a syntax error.
    """
    if IS_POSTGRES:
        return query.with_for_update()
    return query


async def init_db():
    """Initialize database tables and migrate any missing columns.

    ``create_all`` only creates tables that do not yet exist — it does NOT add
    columns to existing tables. To keep the app working e2e without manual
    migrations, we also ALTER existing tables to add any columns the models
    declare but the database is missing.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if IS_SQLITE:
        await _migrate_sqlite()
    else:
        await _migrate_postgres()


async def _migrate_sqlite():
    """SQLite auto-migration via PRAGMA table_info."""
    def _add_missing_columns(dbapi_conn):
        for table_name, table in Base.metadata.tables.items():
            rows = dbapi_conn.execute(
                text(f"PRAGMA table_info({table_name})")
            ).fetchall()
            existing = {row[1] for row in rows}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = str(column.type)
                nullability = "" if column.nullable else "NOT NULL"
                default = ""
                if column.default is not None and not callable(column.default.arg):
                    default = f"DEFAULT {column.default.arg}"
                dbapi_conn.execute(
                    text(
                        f"ALTER TABLE {table_name} ADD COLUMN "
                        f"{column.name} {col_type} {nullability} {default}"
                    )
                )

    async with engine.begin() as conn:
        await conn.run_sync(_add_missing_columns)


async def _migrate_postgres():
    """Postgres auto-migration via information_schema."""
    def _add_missing_columns(dbapi_conn):
        for table_name, table in Base.metadata.tables.items():
            rows = dbapi_conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :tbl",
                ),
                {"tbl": table_name},
            ).fetchall()
            existing = {row[0] for row in rows}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = str(column.type)
                nullability = "" if column.nullable else "NOT NULL"
                default = ""
                if column.default is not None and not callable(column.default.arg):
                    default = f"DEFAULT {column.default.arg}"
                dbapi_conn.execute(
                    text(
                        f'ALTER TABLE "{table_name}" ADD COLUMN '
                        f'"{column.name}" {col_type} {nullability} {default}'
                    )
                )

    async with engine.begin() as conn:
        await conn.run_sync(_add_missing_columns)
