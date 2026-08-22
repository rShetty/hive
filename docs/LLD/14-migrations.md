# 14 — Database Migrations (Alembic)

Status: implemented on `enterprise-hardening` (issue #14)

## Why

`backend/database.py` ships a hand-rolled, additive-only auto-migrator
(`init_db()` → `create_all` + `ALTER TABLE … ADD COLUMN`). It keeps the app
booting across small model changes, but it cannot:

- rename or drop columns,
- backfill data,
- change constraints,
- be reviewed or rolled back as a unit.

Alembic (already pinned in `backend/requirements.txt`) is now the source of
truth for schema changes. `alembic==1.13.1`.

## Layout

```
backend/
├── alembic.ini                  # Alembic config (paths are %(here)s-relative)
├── alembic/
│   ├── env.py                   # URL resolution + target_metadata = Base.metadata
│   ├── script.py.mako           # revision template
│   └── versions/
│       └── 0001_baseline.py     # baseline: full current schema
└── migrations/
    └── run_migrations.py        # idempotent `alembic upgrade head` wrapper
```

## URL handling

Alembic runs with **sync** drivers; the app runs async. `backend/alembic/env.py`
resolves the target database as:

1. `ALEMBIC_DATABASE_URL` (preferred; lets you point migrations at a DB the app
   itself never touches), then
2. `DATABASE_URL` (the same variable the app uses), then
3. `sqlite+aiosqlite:///./agent_marketplace.db`.

The URL is translated between sync and async driver schemes in both directions
(`aiosqlite` ↔ `pysqlite`, `asyncpg` ↔ `psycopg2`-style `postgresql://`), so
operators can pass either form to either component. The app's `database.py`
builds its engine at import time and must always see an async-driver URL;
`env.py` normalises that before importing it.

## Baseline semantics

`0001_baseline` is the **stamp point** for pre-Alembic databases:

- **Fresh (empty) DB** — `upgrade head` creates every table, index and
  constraint from the models' metadata and records `0001_baseline`.
- **Legacy DB** (schema created by the old `create_all` path) — the revision
  detects an existing schema (via the `users` table) and is a **no-op**; the
  wrapper stamps `0001_baseline` as applied. Nothing is duplicated or dropped.

## Container startup wiring

The Dockerfile CMD applies migrations before the app boots:

```dockerfile
CMD ["sh", "-c", "python -m migrations.run_migrations && exec python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
```

The wrapper is **idempotent** and safe on every restart:

- already at head → no-op success;
- fresh DB → baseline created;
- legacy DB → stamped at baseline;
- failure → logged and **non-fatal by default**, so a transient DB hiccup never
  prevents boot on an existing healthy database (`init_db()` in the app still
  guarantees the schema via `create_all`). Set `ALEMBIC_STRICT=1` to make
  migration failures exit non-zero and block the deploy instead.

Run it by hand (works from any CWD):

```bash
cd backend
../.venv-enterprise/bin/python -m migrations.run_migrations
```

## Migration workflow (day-to-day)

```bash
cd backend

# 1. Change models under backend/models/ as needed.

# 2. Autogenerate a revision (diffs models against the DB at
#    ALEMBIC_DATABASE_URL / DATABASE_URL — point it at a scratch DB):
ALEMBIC_DATABASE_URL="sqlite:///./scratch_mig.db" \
  ../.venv-enterprise/bin/python -m alembic revision --autogenerate -m "add X"

# 3. REVIEW the generated file. Autogenerate is a draft: check server defaults,
#    renames (it emits drop+add), and SQLite table-rebuild limitations.

# 4. Test the upgrade on a scratch DB, then downgrade + upgrade again:
ALEMBIC_DATABASE_URL="sqlite:///./scratch_mig.db" \
  ../.venv-enterprise/bin/python -m alembic upgrade head
ALEMBIC_DATABASE_URL="sqlite:///./scratch_mig.db" \
  ../.venv-enterprise/bin/python -m alembic downgrade -1
ALEMBIC_DATABASE_URL="sqlite:///./scratch_mig.db" \
  ../.venv-enterprise/bin/python -m alembic upgrade head

# 5. Commit the file under backend/alembic/versions/. Never edit an already
#    deployed revision — always add a new one.
```

### Applying in production

Migrations run automatically on container start. To run them manually (e.g.
before a rolling deploy):

```bash
docker compose -f docker-compose.prod.yml exec marketplace \
  python -m migrations.run_migrations
```

Or against the production DB from an operator machine:

```bash
cd backend
ALEMBIC_DATABASE_URL="postgresql://user:pass@host:5432/hive" \
  ../.venv-enterprise/bin/python -m alembic upgrade head
```

### Rollback

```bash
cd backend
ALEMBIC_DATABASE_URL="postgresql://user:pass@host:5432/hive" \
  ../.venv-enterprise/bin/python -m alembic downgrade -1   # one revision back
```

Rollbacks are only as good as the revision's `downgrade()`. The baseline's
downgrade is destructive (drops all tables) and must never be run in prod.

## Relationship with the legacy auto-migrator

`init_db()` in `backend/database.py` is kept as a **safety net**: it still
guarantees tables/columns exist at boot. All *reviewed* schema change now goes
through Alembic revisions; do not rely on the auto-migrator for new columns —
add a revision so the change is captured, reviewable and reversible.

## Regression tests

`backend/tests/test_alembic_baseline.py` guards the wiring: baseline revision
presence, Dockerfile ordering, docs presence, fresh-DB upgrade, legacy stamping
and idempotency.
