"""Platform-level Ed25519 signing keys for Hive → agent payloads.

Hive generates its own Ed25519 keypair at startup (persisted to the database
or env vars) and uses it to sign outbound delegation payloads sent to agents.
Agents verify these signatures using Hive's published public key (via the
``/.well-known/hive-identity`` endpoint).

This replaces the shared ``HIVE_SIGNING_SECRET`` for the outbound direction.
The legacy HMAC is still sent alongside the Ed25519 signature during the
dual-signing transition window so agents that haven't been updated to verify
Ed25519 keep working.
"""
from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature


# In-memory cache of the platform keypair (loaded once at startup).
_platform_private_key: Optional[Ed25519PrivateKey] = None
_platform_public_pem: Optional[str] = None
_platform_key_id: Optional[str] = None


def _generate_platform_keypair() -> tuple[str, str, str]:
    """Generate a fresh platform Ed25519 keypair.

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

    key_id = f"hive-{uuid.uuid4().hex[:12]}"
    return private_pem, public_pem, key_id


async def load_or_create_platform_key(db: AsyncSession) -> None:
    """Load the platform keypair from the DB, or generate + persist one.

    The key is stored in a ``platform_keys`` table (auto-created). If the
    env var ``HIVE_PLATFORM_PRIVATE_KEY`` is set (PEM), it takes precedence
    — useful for deployments that inject keys via secret files.
    """
    global _platform_private_key, _platform_public_pem, _platform_key_id

    # 1. Check env-injected key (takes precedence).
    env_priv = os.getenv("HIVE_PLATFORM_PRIVATE_KEY", "")
    if env_priv and "BEGIN PRIVATE KEY" in env_priv:
        priv = serialization.load_pem_private_key(
            env_priv.encode("ascii"), password=None
        )
        _platform_private_key = priv
        _platform_public_pem = priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        _platform_key_id = os.getenv("HIVE_PLATFORM_KEY_ID", "hive-env")
        return

    # 2. Check DB.
    await _ensure_platform_keys_table(db)
    result = await db.execute(
        text("SELECT private_pem, public_pem, key_id FROM platform_keys "
             "ORDER BY created_at DESC LIMIT 1")
    )
    row = result.fetchone()
    if row:
        _platform_private_key = serialization.load_pem_private_key(
            row[0].encode("ascii"), password=None
        )
        _platform_public_pem = row[1]
        _platform_key_id = row[2]
        return

    # 3. Generate + persist.
    private_pem, public_pem, key_id = _generate_platform_keypair()
    await db.execute(
        text("INSERT INTO platform_keys (key_id, private_pem, public_pem, created_at) "
             "VALUES (:kid, :priv, :pub, :ts)"),
        {
            "kid": key_id,
            "priv": private_pem,
            "pub": public_pem,
            "ts": datetime.now(timezone.utc),
        },
    )
    await db.commit()
    _platform_private_key = serialization.load_pem_private_key(
        private_pem.encode("ascii"), password=None
    )
    _platform_public_pem = public_pem
    _platform_key_id = key_id


async def _ensure_platform_keys_table(db: AsyncSession) -> None:
    """Create the platform_keys table if it doesn't exist (cross-DB)."""
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS platform_keys (
            id SERIAL PRIMARY KEY,
            key_id VARCHAR(60) UNIQUE NOT NULL,
            private_pem TEXT NOT NULL,
            public_pem TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    await db.commit()


def sign_outbound(timestamp: str, body: bytes) -> Optional[tuple[str, str]]:
    """Sign an outbound payload with Hive's platform private key.

    Returns ``(signature_base64, key_id)`` or ``None`` if no platform key
    is loaded (dev mode without the key).
    """
    if _platform_private_key is None:
        return None
    message = f"{timestamp}.".encode() + body
    sig = _platform_private_key.sign(message)
    return base64.b64encode(sig).decode("ascii"), _platform_key_id or "hive"


def get_platform_public_pem() -> Optional[str]:
    """Return the platform's public key PEM (for the well-known endpoint)."""
    return _platform_public_pem


def get_platform_key_id() -> Optional[str]:
    return _platform_key_id
