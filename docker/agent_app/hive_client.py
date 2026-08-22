"""Hardened outbound-call helpers for agent → Hive callbacks.

Shared by main.py / main_langchain.py / main_crewai.py (CodeQL
py/partial-ssrf #14-#21 and py/log-injection follow-ups):

  * ``hive_callback_url`` builds callback URLs against the configured
    ``HIVE_URL`` with the scheme+host pinned at parse time, the path
    percent-encoded and the delegation/resource segments allowlisted, so a
    poisoned ``HIVE_URL`` or injected segment can never retarget the request
    to an arbitrary host.
  * ``sanitize_log_text`` strips newlines/control characters before
    untrusted values reach logs, blocking log-forgery.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = ("http", "https")
# Delegation IDs are uuid4 strings; keep headroom for other resource ids.
_SAFE_SEGMENT = r"[A-Za-z0-9_-]{1,64}"

import re as _re
_SEGMENT_RE = _re.compile(_SAFE_SEGMENT)


def sanitize_log_text(value: object, limit: int = 300) -> str:
    """Make ``value`` safe for a single log line (no CRLF/control chars)."""
    text = str(value)
    cleaned = "".join(
        ch if ch.isprintable() and ch not in "\r\n" else " " for ch in text
    )
    return cleaned[:limit]


def hive_base_url() -> str:
    """Validated ``HIVE_URL`` base (scheme+host only); empty when unset/bad."""
    base = (os.getenv("HIVE_URL") or "").strip().rstrip("/")
    if not base:
        return ""
    parsed = urlparse(base)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.hostname:
        logger.warning("HIVE_URL is not a valid http(s) URL; callbacks disabled")
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        logger.warning("HIVE_URL must be a bare scheme://host[:port]; callbacks disabled")
        return ""
    # Rebuild from validated parts so path/query/fragment can never smuggle
    # through — the callback path is appended by hive_callback_url only.
    host = parsed.hostname
    port = parsed.port
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc = f"{netloc}:{port}"
    return f"{parsed.scheme.lower()}://{netloc}"


def hive_callback_url(path: str, *segments: object) -> str | None:
    """Build ``<HIVE_URL>/<path>/<segments...>`` for a server-side callback.

    Returns ``None`` when callbacks are disabled or a segment fails the
    allowlist — callers must treat that as "skip the callback", never fall
    back to an unvalidated URL.
    """
    base = hive_base_url()
    if not base:
        return None
    clean_path = quote(str(path).strip("/"), safe="/")
    clean_segments = []
    for seg in segments:
        text = str(seg)
        if not _SEGMENT_RE.fullmatch(text):
            # CodeQL py/log-injection (#32): the rejected segment is
            # attacker-controlled — repr() escapes CR/LF so a forged log
            # entry is impossible; truncated for line hygiene.
            logger.warning("Rejecting callback with invalid segment: %r", text[:64])
            return None
        clean_segments.append(text)
    url = f"{base}/{clean_path}"
    for seg in clean_segments:
        url += f"/{seg}"
    return url
