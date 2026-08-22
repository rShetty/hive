"""Regression guard: the host Docker socket must never be mounted into Hive.

Issue #5 — mounting /var/run/docker.sock into the platform container grants
host-level access to any container escape / RCE. Docker access must go
through a config-driven DOCKER_HOST (socket proxy or remote daemon) instead.
"""
import os
import unittest

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))

_COMPOSE_FILES = [
    "docker-compose.yml",
    "docker-compose.prod.yml",
]

# Files that may legitimately reference the socket (the *proxy* consumes it).
_SOCKET_PROXY_FILES = {
    "docker-compose.yml",       # docker-socket-proxy service (with-docker profile)
    "docker-compose.prod.yml",  # docker-socket-proxy service (with-docker profile)
}


class TestComposeNoDockerSocket(unittest.TestCase):
    def _read(self, name):
        with open(os.path.join(_REPO_ROOT, name), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_marketplace_service_has_no_socket_mount(self):
        """The marketplace service must not mount /var/run/docker.sock."""
        for name in _COMPOSE_FILES:
            with self.subTest(compose=name):
                text = self._read(name)
                # Extract the marketplace service block (up to the next
                # top-level service key).
                start = text.find("marketplace:")
                self.assertNotEqual(start, -1, f"{name}: no marketplace service")
                next_svc = text.find("\n  ", start + len("marketplace:"))
                block = text[start:next_svc if next_svc != -1 else len(text)]
                self.assertNotIn(
                    "/var/run/docker.sock", block,
                    f"{name}: marketplace service must not mount the Docker socket",
                )

    def test_deploy_sh_generated_compose_has_no_socket_mount(self):
        """deploy.sh generates a prod compose file inline — its marketplace
        service must be clean and wired to the socket proxy."""
        path = os.path.join(_REPO_ROOT, "deploy.sh")
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        start = text.find("<<DOCKEREOF")
        self.assertNotEqual(start, -1, "deploy.sh: DOCKEREOF heredoc not found")
        end = text.find("DOCKEREOF", start + len("<<DOCKEREOF"))
        block = text[start:end]
        mkt_start = block.find("marketplace:")
        self.assertNotEqual(mkt_start, -1)
        next_svc = block.find("\n  ", mkt_start + len("marketplace:"))
        mkt_block = block[mkt_start:next_svc if next_svc != -1 else len(block)]
        self.assertNotIn(
            "/var/run/docker.sock", mkt_block,
            "deploy.sh generated marketplace service must not mount the Docker socket",
        )
        self.assertIn(
            "docker-socket-proxy", block,
            "deploy.sh generated compose should wire DOCKER_HOST to the socket proxy",
        )

    def test_socket_only_mounted_into_proxy_services(self):
        """If the socket appears at all, it is only inside the socket-proxy service."""
        for name in _COMPOSE_FILES:
            with self.subTest(compose=name):
                text = self._read(name)
                if "/var/run/docker.sock" in text:
                    self.assertIn("docker-socket-proxy", text)

    def test_container_manager_fails_loud_when_docker_required(self):
        """With HIVE_REQUIRE_DOCKER=1 an unreachable endpoint raises instead of degrading."""
        import sys
        sys.path.insert(0, os.path.join(_REPO_ROOT, "backend"))
        from services.container_manager import (
            DockerUnavailableError,
            _docker_required,
            get_docker_client,
        )

        old_host = os.environ.pop("DOCKER_HOST", None)
        old_req = os.environ.get("HIVE_REQUIRE_DOCKER")
        try:
            os.environ["HIVE_REQUIRE_DOCKER"] = "1"
            os.environ["DOCKER_HOST"] = "tcp://127.0.0.1:1"  # nothing listens here
            self.assertTrue(_docker_required())
            with self.assertRaises(DockerUnavailableError) as ctx:
                get_docker_client()
            self.assertIn("docker-socket-proxy", str(ctx.exception))
        finally:
            if old_host is not None:
                os.environ["DOCKER_HOST"] = old_host
            else:
                os.environ.pop("DOCKER_HOST", None)
            if old_req is not None:
                os.environ["HIVE_REQUIRE_DOCKER"] = old_req
            else:
                os.environ.pop("HIVE_REQUIRE_DOCKER", None)


if __name__ == "__main__":
    unittest.main()
