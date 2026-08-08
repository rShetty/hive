"""W3C Decentralized Identifier (DID) support for Hive agents.

Implements the ``did:hive`` DID method. Every agent registered in Hive
automatically gets a DID: ``did:hive:{agent_id}``.

DID documents are resolvable via ``GET /.well-known/did/{did}`` and contain:
  - The agent's Ed25519 public key (verification method)
  - Service endpoints (delegation, marketplace, dashboard)
  - The agent's human-readable metadata

This makes agent identity portable and resolvable by external systems
without requiring direct API access to Hive.
"""
from __future__ import annotations

import os
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.agent import Agent


DID_PREFIX = "did:hive:"


def agent_to_did(agent_id: str) -> str:
    """Convert an agent UUID to its DID."""
    return f"{DID_PREFIX}{agent_id}"


def did_to_agent_id(did: str) -> Optional[str]:
    """Extract the agent UUID from a did:hive DID. Returns None if invalid."""
    if not did.startswith(DID_PREFIX):
        return None
    agent_id = did[len(DID_PREFIX):]
    # Basic UUID format check (36 chars, hyphens at expected positions).
    if len(agent_id) != 36:
        return None
    return agent_id


async def build_did_document(agent: Agent, marketplace_url: str) -> dict:
    """Build a W3C DID Document for an agent.

    Follows the W3C DID Core specification (https://w3.org/TR/did-core/).
    """
    did = agent_to_did(agent.id)
    doc: dict = {
        "@context": [
            "https://www.w3.org/ns/did/v1",
            "https://w3id.org/security/suites/ed25519-2020/v1",
        ],
        "id": did,
        "alsoKnownAs": [],
        "verificationMethod": [],
        "service": [],
    }

    # Human-readable name via alsoKnownAs (not standard, but useful for
    # display purposes). We put the marketplace URL here.
    if agent.slug:
        doc["alsoKnownAs"].append(f"{marketplace_url}/a/{agent.slug}/")

    # Ed25519 verification method (for signing callbacks / delegation requests)
    if agent.signing_public_key:
        vm_id = f"{did}#signing-key"
        doc["verificationMethod"].append({
            "id": vm_id,
            "type": "Ed25519VerificationKey2020",
            "controller": did,
            "publicKeyMultibase": _pem_to_multibase(agent.signing_public_key),
        })
        # Assert that this key can be used for authentication and assertion.
        doc["authentication"] = [vm_id]
        doc["assertionMethod"] = [vm_id]

    # Service endpoints
    services = []
    if agent.endpoint_url:
        endpoint = agent.endpoint_url
        if endpoint.startswith("/"):
            endpoint = f"{marketplace_url}{endpoint}"
        services.append({
            "id": f"{did}#delegation",
            "type": "HiveDelegationEndpoint",
            "serviceEndpoint": endpoint,
        })
    services.append({
        "id": f"{did}#marketplace",
        "type": "HiveMarketplaceEntry",
        "serviceEndpoint": f"{marketplace_url}/api/marketplace/agents/{agent.id}",
    })
    if agent.slug:
        services.append({
            "id": f"{did}#dashboard",
            "type": "HiveDashboard",
            "serviceEndpoint": f"{marketplace_url}/a/{agent.slug}/",
        })
    doc["service"] = services

    # Hive-specific extensions
    doc["x-hive"] = {
        "agent_id": agent.id,
        "name": agent.name,
        "slug": agent.slug,
        "agent_type": agent.agent_type,
        "status": agent.status,
        "capabilities": agent.capabilities or [],
        "tags": agent.tags or [],
        "owner_did": f"did:hive:user:{agent.owner_id}" if agent.owner_id else None,
        "signing_key_id": agent.signing_key_id,
        "pricing_model": agent.pricing_model,
        "is_public": agent.is_public,
        "version": agent.version,
    }

    return doc


def _pem_to_multibase(pem: str) -> str:
    """Convert a PEM-encoded Ed25519 public key to multibase format.

    The multibase encoding uses the base64url prefix 'u' followed by the
    raw 32-byte Ed25519 public key (multicodec prefix 0xed01).
    """
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub = serialization.load_pem_public_key(pem.encode("ascii"))
    if not isinstance(pub, Ed25519PublicKey):
        return ""
    raw_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    # Multicodec: 0xed (Ed25519) + 0x01 (1-byte length indicator) + raw key
    multicodec = b"\xed\x01" + raw_bytes
    return "u" + base64.urlsafe_b64encode(multicodec).decode("ascii").rstrip("=")


async def resolve_did(did: str, db: AsyncSession) -> Optional[dict]:
    """Resolve a did:hive DID to its DID Document.

    Returns None if the DID is malformed or the agent doesn't exist.
    """
    agent_id = did_to_agent_id(did)
    if not agent_id:
        return None

    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        return None

    marketplace_url = os.getenv("MARKETPLACE_URL", "http://localhost:8000")
    return await build_did_document(agent, marketplace_url)
