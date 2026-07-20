"""Database configuration and session management."""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool
import os

# Use SQLite for POC - easy to set up, single file
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agent_marketplace.db")

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    poolclass=NullPool,
)

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


async def init_db():
    """Initialize database tables and migrate any missing columns.

    `create_all` only creates tables that do not yet exist — it does NOT add
    columns to existing tables. To keep the app working e2e without manual
    migrations, we also ALTER existing tables to add any columns the models
    declare but the database is missing.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from sqlalchemy import text

    def _add_missing_columns(dbapi_conn):
        for table_name, table in Base.metadata.tables.items():
            rows = dbapi_conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
            existing = {row[1] for row in rows}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = str(column.type)
                nullability = "" if column.nullable else "NOT NULL"
                default = ""
                # Only emit a literal DEFAULT for simple scalar defaults; skip
                # callable defaults (e.g. dict/list factories) which can't be
                # expressed as a SQL literal.
                if column.default is not None and not callable(column.default.arg):
                    default = f"DEFAULT {column.default.arg}"
                dbapi_conn.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type} {nullability} {default}")
                )

    async with engine.begin() as conn:
        await conn.run_sync(_add_missing_columns)
