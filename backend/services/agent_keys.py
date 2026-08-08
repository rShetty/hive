"""Per-agent Ed25519 signing keys for cryptographically verifiable identity.

Each agent gets an Ed25519 keypair at registration:
  * the **private key** is returned to the owner exactly once (like the API key)
    and used by the agent to sign its async completion callbacks to Hive;
  * the **public key** is stored on the Agent row and used by Hive to verify
    those callbacks.

This replaces the single shared ``HIVE_SIGNING_SECRET`` for inbound callbacks:
an agent can only sign its own callbacks, never another agent's. The legacy
HMAC path is retained during the dual-signing transition window so existing
agents without keys keep working until rotated.

Outbound (Hive → agent) payloads continue to be HMAC-signed by the platform
with ``HIVE_SIGNING_SECRET``; agents verify that signature on receipt.
"""
from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature


def generate_keypair() -> tuple[str, str, str]:
    """Generate a new Ed25519 keypair.

    Returns ``(private_pem, public_pem, key_id)``.
    """
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    private_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    public_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")

    key_id = f"ak-{uuid.uuid4().hex[:12]}"
    return private_pem, public_pem, key_id


def new_signing_fields() -> tuple[dict, str]:
    """Return ``(agent_fields, private_pem)`` for a freshly generated keypair.

    ``agent_fields`` is a dict of columns to set on the Agent row
    (signing_key_id, signing_public_key, signing_key_created_at). The private
    PEM is returned separately so the caller can hand it to the owner once.
    """
    private_pem, public_pem, key_id = generate_keypair()
    fields = {
        "signing_key_id": key_id,
        "signing_public_key": public_pem,
        "signing_key_created_at": datetime.now(timezone.utc),
    }
    return fields, private_pem


def _load_public(public_pem: str) -> Ed25519PublicKey:
    return serialization.load_pem_public_key(public_pem.encode("ascii"))


def _load_private(private_pem: str) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(
        private_pem.encode("ascii"), password=None
    )


def _message(timestamp: str, body: bytes) -> bytes:
    """Canonical message covered by both HMAC and Ed25519 signatures."""
    return f"{timestamp}.".encode() + body


def sign_callback(
    *, timestamp: str, body: bytes, private_pem: str
) -> str:
    """Sign a callback payload with the agent's private key. Returns base64."""
    sig = _load_private(private_pem).sign(_message(timestamp, body))
    return base64.b64encode(sig).decode("ascii")


async def verify_callback_signature(
    *,
    delegation_id: str,
    key_id: str,
    timestamp: str,
    body: bytes,
    signature: str,
    db: Optional[AsyncSession] = None,
) -> bool:
    """Verify an Ed25519 callback signature against the agent's stored public key.

    Looks up the agent by ``signing_key_id``. The signature covers
    ``timestamp + "." + body`` — the body itself contains the delegation_id, so
    a signature cannot be replayed against a different delegation.
    """
    from models.agent import Agent

    # Use the provided session or open one (the verifier may be called before
    # the request-scoped session is available).
    close_after = False
    if db is None:
        from database import async_session_maker
        db = async_session_maker()
        close_after = True
    try:
        result = await db.execute(
            select(Agent).where(Agent.signing_key_id == key_id)
        )
        agent = result.scalar_one_or_none()
        if agent is None or not agent.signing_public_key:
            return False
        try:
            pub = _load_public(agent.signing_public_key)
            sig_bytes = base64.b64decode(signature)
            pub.verify(sig_bytes, _message(timestamp, body))
            return True
        except (InvalidSignature, ValueError, Exception):
            return False
    finally:
        if close_after:
            await db.close()


async def get_agent_public_key(agent_id: str, db: AsyncSession) -> Optional[str]:
    from models.agent import Agent
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        return None
    return agent.signing_public_key
