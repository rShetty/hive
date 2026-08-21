"""Issue #14: Alembic migration baseline must be wired into startup.

Guards the three acceptance criteria:
* a baseline migration exists and is discoverable by Alembic;
* the container CMD runs migrations before app boot (idempotent wrapper);
* the migration workflow is documented under docs/LLD/.

The functional behaviour of ``upgrade head`` (fresh create / legacy stamp) is
exercised end-to-end here against a temporary SQLite database.
"""
import os
import subprocess
import sys
import tempfile
import unittest

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_REPO_ROOT = os.path.normpath(os.path.join(_BACKEND, ".."))
sys.path.insert(0, _BACKEND)


class TestAlembicWiring(unittest.TestCase):
    def test_baseline_revision_exists(self):
        path = os.path.join(_BACKEND, "alembic", "versions", "0001_baseline.py")
        self.assertTrue(os.path.isfile(path), "0001_baseline.py missing")
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('revision: str = "0001_baseline"', source)
        self.assertIn("down_revision", source)

    def test_dockerfile_runs_migrations_before_app_boot(self):
        with open(os.path.join(_REPO_ROOT, "Dockerfile"), "r", encoding="utf-8") as fh:
            dockerfile = fh.read()
        self.assertIn(
            "python -m migrations.run_migrations", dockerfile,
            "container CMD must apply migrations before uvicorn starts",
        )
        # Migrations must run *before* the app process in the same command.
        self.assertLess(
            dockerfile.find("migrations.run_migrations"),
            dockerfile.rfind("uvicorn"),
            "migration step must precede the uvicorn boot",
        )

    def test_migration_docs_exist(self):
        path = os.path.join(_REPO_ROOT, "docs", "LLD", "14-migrations.md")
        self.assertTrue(os.path.isfile(path), "docs/LLD/14-migrations.md missing")
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        for needle in ("alembic upgrade head", "alembic revision --autogenerate"):
            self.assertIn(needle, text, f"docs must explain '{needle}'")


class TestUpgradeHeadEndToEnd(unittest.TestCase):
    """Run the real ``alembic upgrade head`` against a temp SQLite DB."""

    @classmethod
    def _run_upgrade(cls, url_env):
        env = dict(os.environ)
        env.pop("ALEMBIC_DATABASE_URL", None)
        env.update(url_env)
        return subprocess.run(
            [sys.executable, "-m", "migrations.run_migrations"],
            cwd=_BACKEND, env=env, capture_output=True, text=True,
        )

    def test_fresh_database_gets_baseline_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "fresh.db")
            result = self._run_upgrade({"DATABASE_URL": f"sqlite+aiosqlite:///{db}"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("✅", result.stdout)

            import sqlite3
            conn = sqlite3.connect(db)
            try:
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                version = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchall()
            finally:
                conn.close()
            for expected in ("users", "agents", "wallets", "alembic_version"):
                self.assertIn(expected, tables)
            self.assertEqual(version, [("0001_baseline",)])

    def test_legacy_database_is_stamped_not_duplicated(self):
        # Build a "legacy" DB exactly the way the old create_all path did.
        # NOTE: deliberately synchronous — asyncio.run() would unset the
        # global event loop and break later tests that rely on it.
        import database  # noqa: E402
        import sqlalchemy  # noqa: E402

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "legacy.db")
            url = f"sqlite+aiosqlite:///{db}"

            engine = sqlalchemy.create_engine(url.replace("+aiosqlite", ""))
            database.Base.metadata.create_all(engine)
            engine.dispose()

            result = self._run_upgrade({"DATABASE_URL": url})
            self.assertEqual(result.returncode, 0, result.stderr)

            import sqlite3
            conn = sqlite3.connect(db)
            try:
                version = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchall()
                users_cols = [
                    row[1] for row in conn.execute("PRAGMA table_info(users)")
                ]
            finally:
                conn.close()
            self.assertEqual(version, [("0001_baseline",)])
            self.assertIn("id", users_cols)  # schema intact, not duplicated

    def test_second_upgrade_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "idem.db")
            url_env = {"DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
            self.assertEqual(self._run_upgrade(url_env).returncode, 0)
            again = self._run_upgrade(url_env)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("✅", again.stdout)


if __name__ == "__main__":
    unittest.main()
