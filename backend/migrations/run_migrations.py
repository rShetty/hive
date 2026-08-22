"""Idempotent database migration bootstrap for container startup (issue #14).

Runs ``alembic upgrade head`` **before** the FastAPI app boots. Handles the
two database populations that exist in the wild:

* **Fresh / empty database** — Alembic creates the full baseline schema and
  records ``0001_baseline``.
* **Legacy database** — created by the old ``create_all`` +
  additive-auto-migration path in ``database.py``. Its schema already matches
  the current models, so there is nothing to run; we simply stamp
  ``0001_baseline`` so future revisions apply cleanly.

The wrapper is deliberately defensive: a failure here must not prevent the
app from booting on an existing healthy database, so errors are logged and
swallowed (``init_db()`` still guarantees the schema exists via
``create_all`` + additive column migration). Set ``ALEMBIC_STRICT=1`` to make
migration failures fatal instead.

Usage (from anywhere — paths are resolved relative to this file):

    python -m migrations.run_migrations
"""
from __future__ import annotations

import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    """Upgrade the database to head; return a process exit code."""
    from alembic.config import Config
    from alembic import command

    strict = os.getenv("ALEMBIC_STRICT", "").lower() in ("1", "true", "yes")

    cfg = Config(os.path.join(_BACKEND_DIR, "alembic.ini"))
    try:
        command.upgrade(cfg, "head")
        print("✅ Database migrations up to date")
        return 0
    except Exception as exc:  # noqa: BLE001 — never block app boot by default
        if strict:
            print(f"❌ Migration failed (ALEMBIC_STRICT): {exc}", file=sys.stderr)
            return 1
        print(f"⚠️  Migration step skipped: {exc}")
        return 0


def upgrade_head_sync() -> None:
    """Synchronous entry point used by the container CMD before app boot."""
    rc = main()
    if rc != 0:
        raise SystemExit(rc)


if __name__ == "__main__":
    raise SystemExit(main())
