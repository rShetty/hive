"""Regression tests for docker/agent_app/hive_client.py.

Covers the CodeQL py/partial-ssrf fixes (#14-#21): callback URLs must be
built only from a validated HIVE_URL base with allowlisted segments, and
log text must never carry control characters.
"""
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker" / "agent_app"))
hive_client = importlib.import_module("hive_client")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HIVE_URL", "https://hive.example.com")


# ── hive_base_url ────────────────────────────────────────────────────────────

def test_base_url_strips_path_query_fragment(monkeypatch):
    monkeypatch.setenv("HIVE_URL", "https://user@evil.test/path?q=1#x")
    assert hive_client.hive_base_url() == ""


def test_base_url_rejects_non_http_schemes(monkeypatch):
    for scheme in ("file:///etc/passwd", "gopher://evil.test", "ftp://x.test"):
        monkeypatch.setenv("HIVE_URL", scheme)
        assert hive_client.hive_base_url() == "", scheme


def test_base_url_keeps_host_and_port(monkeypatch):
    monkeypatch.setenv("HIVE_URL", "https://hive.example.com:8443/sub/path")
    assert hive_client.hive_base_url() == "https://hive.example.com:8443"


def test_base_url_empty_when_unset(monkeypatch):
    monkeypatch.delenv("HIVE_URL", raising=False)
    assert hive_client.hive_base_url() == ""


# ── hive_callback_url ────────────────────────────────────────────────────────

def test_callback_url_happy_path():
    url = hive_client.hive_callback_url("api/delegate", "3f2b9c1e-1111-2222-3333-444455556666", "progress")
    assert url == "https://hive.example.com/api/delegate/3f2b9c1e-1111-2222-3333-444455556666/progress"


def test_callback_url_rejects_traversal_segment():
    assert hive_client.hive_callback_url("api/delegate", "../../admin", "progress") is None


def test_callback_url_rejects_url_injection():
    assert hive_client.hive_callback_url("api/delegate", "x/../../evil.test", "complete") is None
    assert hive_client.hive_callback_url("api/delegate", "a/b@c", "fail") is None


def test_callback_url_none_when_disabled(monkeypatch):
    monkeypatch.delenv("HIVE_URL", raising=False)
    assert hive_client.hive_callback_url("api/delegate", "abc", "progress") is None


def test_callback_url_never_smuggles_via_path():
    url = hive_client.hive_callback_url("/api/delegate//evil/", "abc")
    # Path is normalized; host part can never change.
    assert url.startswith("https://hive.example.com/")


# ── sanitize_log_text ────────────────────────────────────────────────────────

def test_sanitize_strips_crlf_and_control_chars():
    dirty = "line1\nline2\r\nFAKE ERROR\x00 injected"
    clean = hive_client.sanitize_log_text(dirty)
    assert "\n" not in clean and "\r" not in clean and "\x00" not in clean


def test_sanitize_truncates():
    assert len(hive_client.sanitize_log_text("a" * 10_000)) == 300
