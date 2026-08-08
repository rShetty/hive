"""Optional mTLS (mutual TLS) support for agent↔platform transport auth.

mTLS provides transport-level cryptographic identity: the agent presents a
client certificate to Hive, and Hive presents its server certificate to the
agent. This is the strongest form of transport auth and is **optional** —
agents that don't have a client cert fall back to API-key + Ed25519 signing.

When enabled (``MTLS_ENABLED=1``):
  - Hive's nginx/uvicorn is configured to request client certificates.
  - Agents registered with mTLS get their cert fingerprint stored on the
    Agent row. Hive matches the presented cert's fingerprint to identify
    the agent without requiring an API key.
  - The mTLS identity is **additive** — agents can still use API keys for
    non-transport auth (e.g. when behind a reverse proxy that terminates TLS).

Configuration:
  - ``MTLS_ENABLED`` — set to ``1`` to enable.
  - ``MTLS_CA_CERT_PATH`` — path to the CA cert that signs agent client certs.
  - Agent cert fingerprints are stored in ``Agent.mtls_cert_fingerprint``.
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.agent import Agent


MTLS_ENABLED = os.getenv("MTLS_ENABLED", "").lower() in ("1", "true", "yes")
MTLS_CA_CERT_PATH = os.getenv("MTLS_CA_CERT_PATH", "")


def compute_cert_fingerprint(cert_pem: str) -> str:
    """Compute the SHA-256 fingerprint of a PEM-encoded certificate.

    This is the same format used by nginx ``$ssl_client_fingerprint`` —
    the hex digest of the DER-encoded certificate.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    der = cert.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


async def identify_agent_by_cert(
    fingerprint: str, db: AsyncSession
) -> Optional[Agent]:
    """Look up an agent by its mTLS client certificate fingerprint.

    Returns the Agent if found, None otherwise. The fingerprint must have
    been registered on the Agent row (via ``set_agent_mtls_cert``).
    """
    result = await db.execute(
        select(Agent).where(Agent.mtls_cert_fingerprint == fingerprint)
    )
    return result.scalar_one_or_none()


def is_mtls_enabled() -> bool:
    """Whether mTLS client cert verification is enabled."""
    return MTLS_ENABLED


def get_mtls_nginx_config() -> str:
    """Return nginx config snippet for mTLS client cert verification.

    Used by deploy scripts to configure the fronting nginx/Traefik.
    """
    if not MTLS_ENABLED:
        return "# mTLS not enabled"

    ca_path = MTLS_CA_CERT_PATH or "/etc/hive/mtls-ca.crt"
    return f"""# mTLS — request (but don't require) client certificates.
# Agents with certs are identified by fingerprint; agents without fall
# back to API-key auth.
ssl_client_certificate {ca_path};
ssl_verify_client optional;
ssl_verify_depth 2;

# Expose the client cert fingerprint to the upstream app.
proxy_set_header X-Client-Cert-Verify $ssl_client_verify;
proxy_set_header X-Client-Cert-Fingerprint $ssl_client_fingerprint;
proxy_set_header X-Client-Cert-DN $ssl_client_s_dn;
"""
