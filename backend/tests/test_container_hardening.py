"""Unit tests for the hardened agent-container payload builder.

Issue #6 — agent containers must be created with resource limits
(nano_cpus/mem_limit/pids_limit), ``cap_drop ALL``, minimal added caps and
``no-new-privileges``; values configurable via env with sane defaults.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."
))
from services.container_manager import build_agent_container_limits  # noqa: E402

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Every env var that can influence the payload — restored after each test.
_PAYLOAD_ENV_VARS = (
    "AGENT_CPU_LIMIT",
    "AGENT_MEM_LIMIT",
    "AGENT_PIDS_LIMIT",
    "AGENT_ADDED_CAPS",
    "AGENT_READ_ONLY_ROOTFS",
)


class TestBuildAgentContainerLimits(unittest.TestCase):
    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in _PAYLOAD_ENV_VARS}
        for name in _PAYLOAD_ENV_VARS:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)

    def test_defaults_are_least_privilege_and_capped(self):
        limits = build_agent_container_limits()

        # Resource limits prevent CPU/memory starvation and fork bombs.
        self.assertEqual(limits["nano_cpus"], int(0.5 * 1e9))
        self.assertEqual(limits["mem_limit"], "256m")
        self.assertEqual(limits["pids_limit"], 128)

        # Capability + privilege hardening.
        self.assertEqual(limits["cap_drop"], ["ALL"])
        self.assertNotIn("cap_add", limits)
        self.assertIn("no-new-privileges:true", limits["security_opt"])

        # Read-only rootfs with a small writable tmpfs at /tmp by default.
        self.assertTrue(limits["read_only"])
        self.assertIn("/tmp", limits["tmpfs"])

    def test_env_overrides_are_respected(self):
        os.environ["AGENT_CPU_LIMIT"] = "2"
        os.environ["AGENT_MEM_LIMIT"] = "512m"
        os.environ["AGENT_PIDS_LIMIT"] = "64"

        limits = build_agent_container_limits()
        self.assertEqual(limits["nano_cpus"], int(2 * 1e9))
        self.assertEqual(limits["mem_limit"], "512m")
        self.assertEqual(limits["pids_limit"], 64)

    def test_invalid_env_values_fall_back_to_defaults(self):
        os.environ["AGENT_CPU_LIMIT"] = "not-a-number"
        os.environ["AGENT_PIDS_LIMIT"] = "also-bad"

        limits = build_agent_container_limits()
        self.assertEqual(limits["nano_cpus"], int(0.5 * 1e9))
        self.assertEqual(limits["pids_limit"], 128)

    def test_added_caps_are_explicit_and_narrow(self):
        os.environ["AGENT_ADDED_CAPS"] = " NET_BIND_SERVICE , NET_ADMIN "

        limits = build_agent_container_limits()
        self.assertEqual(limits["cap_add"], ["NET_BIND_SERVICE", "NET_ADMIN"])
        # Caps are *added* on top of dropping ALL — never instead of it.
        self.assertEqual(limits["cap_drop"], ["ALL"])

    def test_blank_added_caps_do_not_emit_cap_add(self):
        os.environ["AGENT_ADDED_CAPS"] = " , "

        limits = build_agent_container_limits()
        self.assertNotIn("cap_add", limits)

    def test_read_only_rootfs_can_be_disabled(self):
        os.environ["AGENT_READ_ONLY_ROOTFS"] = "0"

        limits = build_agent_container_limits()
        self.assertNotIn("read_only", limits)
        self.assertNotIn("tmpfs", limits)
        # Disabling the rootfs toggle must not weaken the rest.
        self.assertEqual(limits["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", limits["security_opt"])


class TestContainerCreationWiring(unittest.TestCase):
    def test_both_creation_paths_apply_the_hardened_payload(self):
        """Regression guard: every client.containers.run call site for agents
        must spread build_agent_container_limits() into its payload."""
        path = os.path.join(_REPO_ROOT, "backend", "services", "container_manager.py")
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertEqual(
            source.count("**build_agent_container_limits()"),
            2,
            "create_container and create_openclaw_container must both apply "
            "the hardened payload from build_agent_container_limits()",
        )


if __name__ == "__main__":
    unittest.main()
