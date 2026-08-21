"""Alembic migration baseline for Hive (issue #14).

This revision is the **stamp point** for databases created by the legacy
``create_all``/auto-migration path in ``database.py``: their schema already
matches the current models, so they must NOT run the baseline DDL again.

Fresh (empty) databases run the full baseline below.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, name: str) -> bool:
    if bind.dialect.name == "postgresql":
        from sqlalchemy import text
        row = bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :name"
            ),
            {"name": name},
        ).first()
    else:  # sqlite
        from sqlalchemy import text
        row = bind.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"
            ),
            {"name": name},
        ).first()
    return row is not None


def upgrade() -> None:
    """Create the full baseline schema on fresh databases.

    Idempotent: if the legacy auto-migration path already created the schema
    (detected via the ``users`` table), this is a no-op — the deployment just
    stamps ``0001_baseline`` as applied (handled by the startup wrapper).
    """
    bind = op.get_bind()
    if _table_exists(bind, "users"):
        # Schema already exists (legacy create_all path) — nothing to do.
        # The startup wrapper stamps this revision in that case.
        return

    from database import Base
    import models  # noqa: F401 — ensure every table is registered

    # Create every table in dependency order as declared by the models'
    # metadata. Base.metadata.create_all emits CREATE TABLE for each missing
    # table plus all indexes/constraints — exactly the baseline we want.
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    """Drop the baseline schema (destructive — dev use only)."""
    bind = op.get_bind()
    if not _table_exists(bind, "users"):
        return
    from database import Base
    import models  # noqa: F401

    Base.metadata.drop_all(bind=bind)
