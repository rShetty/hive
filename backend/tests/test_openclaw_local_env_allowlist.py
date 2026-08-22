"""Unit tests for local-subprocess agent environment isolation.

Issue #3 — ``spawn_openclaw_agent`` must NOT pass Hive's full ``os.environ``
to spawned agent processes. Only an explicit allowlist of benign OS variables
(PATH/HOME/LANG/...) plus approved agent-runtime variables may be forwarded,
and secret-suffixed names are delivered as ``*_FILE`` paths instead of
plaintext env. Optionally, agents drop to a dedicated unprivileged user
(``OPENCLAW_AGENT_USER``).
"""
import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."
))
from services import openclaw_local  # noqa: E402


_AGENT_ID = "deadbeefdeadbeef"

# Fixed keys injected by spawn_openclaw_agent itself.
_INJECTED_KEYS = {
    "AGENT_ID", "AGENT_NAME", "AGENT_API_KEY",
    "HIVE_URL", "HIVE_API_KEY", "MARKETPLACE_URL",
    "INSTANCE_ID", "SKILLS", "PORT",
}

# Env vars touched by these tests — restored in tearDown.
_TOUCHED_VARS = (
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ",
    "HIVE_SECRET_MARKER", "HIVE_UNRELATED_SETTING",
    "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    openclaw_local._AGENT_USER_ENV,
    openclaw_local._AGENT_GROUP_ENV,
)


class _FakeSubprocess:
    """Stand-in for the subprocess module capturing the Popen call."""

    STDOUT = object()
    Popen = mock.MagicMock()


class TestSpawnEnvAllowlist(unittest.TestCase):
    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in _TOUCHED_VARS}
        # Deterministic baseline: only PATH/HOME present from the OS set.
        for name in _TOUCHED_VARS:
            os.environ.pop(name, None)
        os.environ["PATH"] = "/usr/bin:/bin"
        os.environ["HOME"] = "/home/hive-test"
        # Markers that must NEVER reach the agent environment.
        os.environ["HIVE_SECRET_MARKER"] = "leak-me-not"
        os.environ["HIVE_UNRELATED_SETTING"] = "nope"

        self._popen = _FakeSubprocess.Popen = mock.MagicMock(
            return_value=mock.MagicMock()
        )

    def tearDown(self):
        for name, value in self._saved.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
        shutil.rmtree(
            os.path.join("/tmp", "hive-secrets", f"proc-{_AGENT_ID[:8]}"),
            ignore_errors=True,
        )
        log_fh = getattr(self, "_log_fh", None)
        if log_fh is not None:
            log_path = log_fh.name
            log_fh.close()
            if os.path.exists(log_path):
                os.unlink(log_path)

    def _spawn(self, **kwargs):
        patcher = mock.patch.object(openclaw_local, "subprocess", _FakeSubprocess)
        patcher.start()
        self.addCleanup(patcher.stop)
        container_id = openclaw_local.spawn_openclaw_agent(
            agent_id=_AGENT_ID,
            agent_name="Test Agent",
            port=9911,
            api_key="am-test-key",
            skills=["skill-a"],
            **kwargs,
        )
        self.assertTrue(container_id.startswith("proc-openclaw-"))
        self._log_fh = self._popen.call_args.kwargs["stdout"]
        return self._popen.call_args.kwargs["env"]

    def _expected_env_keys(self, env_vars=None):
        """Every env key the spawn may legitimately produce for these inputs."""
        allowed = {name for name in openclaw_local._BASE_ENV_ALLOWLIST
                   if os.environ.get(name)} | _INJECTED_KEYS
        merged = {name for name in openclaw_local._AGENT_ENV_ALLOWLIST
                  if os.environ.get(name)}
        merged.update(env_vars or {})
        from services.secrets import is_secret_key
        for name in merged:
            allowed.add(name if not is_secret_key(name) else f"{name}_FILE")
        return allowed

    def test_no_nonallowlisted_os_environ_vars_leak(self):
        env_vars = {"MY_CUSTOM_VAR": "custom-value",
                    "OPENAI_API_KEY": "user-key"}
        env = self._spawn(env_vars=env_vars)
        self.assertNotIn("HIVE_SECRET_MARKER", env)
        self.assertNotIn("HIVE_UNRELATED_SETTING", env)
        # Every key must be an allowlisted OS var, an injected context key,
        # or something derived from the explicit spawn inputs (plain user env
        # or a secret routed through *_FILE). Nothing else may appear.
        unexpected = set(env) - self._expected_env_keys(env_vars)
        self.assertEqual(unexpected, set(),
                         f"unexpected env var(s) leaked to agent: {unexpected}")

    def test_only_base_allowlist_values_are_forwarded(self):
        os.environ["TMPDIR"] = "/tmp/hive-tmp"
        env = self._spawn()
        self.assertEqual(env.get("PATH"), "/usr/bin:/bin")
        self.assertEqual(env.get("HOME"), "/home/hive-test")
        self.assertEqual(env.get("TMPDIR"), "/tmp/hive-tmp")
        # Unset allowlisted vars are simply absent — never fabricated.
        self.assertNotIn("LANG", env)
        self.assertNotIn("LC_ALL", env)

    def test_server_llm_key_forwarded_as_secret_file_not_plaintext(self):
        os.environ["OPENROUTER_API_KEY"] = "sk-or-server-secret"
        env = self._spawn()
        self.assertNotIn("OPENROUTER_API_KEY", env)
        key_file = env["OPENROUTER_API_KEY_FILE"]
        self.assertTrue(key_file.endswith("/openrouter_api_key"))
        with open(key_file) as fh:
            self.assertEqual(fh.read(), "sk-or-server-secret")

    def test_openrouter_model_safety_net_and_forwarding(self):
        # With a key but no model configured, the safety-net model is applied.
        os.environ["OPENROUTER_API_KEY"] = "sk-or-server-secret"
        env = self._spawn()
        self.assertEqual(env["OPENROUTER_MODEL"], "openai/gpt-4o-mini")
        # A server-configured model wins over the default...
        os.environ["OPENROUTER_MODEL"] = "vendor/custom-model"
        env = self._spawn()
        self.assertEqual(env["OPENROUTER_MODEL"], "vendor/custom-model")

    def test_user_env_vars_take_precedence_over_server_env(self):
        os.environ["OPENAI_API_KEY"] = "server-openai-key"
        env = self._spawn(
            env_vars={"OPENAI_API_KEY": "user-openai-key",
                      "MY_CUSTOM_VAR": "custom-value"},
        )
        # User key replaces the server-level one entirely (single *_FILE).
        key_file = env["OPENAI_API_KEY_FILE"]
        with open(key_file) as fh:
            self.assertEqual(fh.read(), "user-openai-key")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        # Plain non-secret user vars are forwarded verbatim.
        self.assertEqual(env["MY_CUSTOM_VAR"], "custom-value")

    def test_fixed_agent_context_keys_present(self):
        env = self._spawn()
        for key in _INJECTED_KEYS:
            self.assertIn(key, env)
        self.assertEqual(env["PORT"], "9911")
        self.assertEqual(env["SKILLS"], "skill-a")


@unittest.skipUnless(os.name == "posix", "privilege dropping requires POSIX")
class TestPrivilegeDrop(unittest.TestCase):
    def setUp(self):
        self._saved_user = os.environ.get(openclaw_local._AGENT_USER_ENV)
        self._saved_group = os.environ.get(openclaw_local._AGENT_GROUP_ENV)
        os.environ.pop(openclaw_local._AGENT_USER_ENV, None)
        os.environ.pop(openclaw_local._AGENT_GROUP_ENV, None)

    def tearDown(self):
        for name, value in (
            (openclaw_local._AGENT_USER_ENV, self._saved_user),
            (openclaw_local._AGENT_GROUP_ENV, self._saved_group),
        ):
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)

    def test_unconfigured_returns_none(self):
        self.assertIsNone(openclaw_local._agent_privilege_drop())

    def test_configured_user_returns_callable(self):
        import pwd
        current_user = pwd.getpwuid(os.getuid()).pw_name
        os.environ[openclaw_local._AGENT_USER_ENV] = current_user
        drop = openclaw_local._agent_privilege_drop()
        self.assertIsNotNone(drop)
        self.assertTrue(callable(drop))

    def test_unknown_user_fails_closed(self):
        os.environ[openclaw_local._AGENT_USER_ENV] = "hive-no-such-user-xyz"
        with self.assertRaises(RuntimeError):
            openclaw_local._agent_privilege_drop()

    def test_spawn_receives_preexec_fn_when_configured(self):
        import pwd
        current_user = pwd.getpwuid(os.getuid()).pw_name
        os.environ[openclaw_local._AGENT_USER_ENV] = current_user

        popen = mock.MagicMock(return_value=mock.MagicMock())
        fake = type("_FS", (), {"STDOUT": object(),
                                "Popen": staticmethod(popen)})
        with mock.patch.object(openclaw_local, "subprocess", fake):
            openclaw_local.spawn_openclaw_agent(
                agent_id=_AGENT_ID,
                agent_name="Test Agent",
                port=9912,
                api_key="am-test-key",
                skills=[],
            )
            log_fh = popen.call_args.kwargs["stdout"]
            log_path = log_fh.name
            log_fh.close()
            if os.path.exists(log_path):
                os.unlink(log_path)
            self.assertTrue(callable(popen.call_args.kwargs["preexec_fn"]))
        shutil.rmtree(
            os.path.join("/tmp", "hive-secrets", f"proc-{_AGENT_ID[:8]}"),
            ignore_errors=True,
        )


if __name__ == "__main__":
    unittest.main()
