"""W3C Verifiable Credentials (VC) support for Hive agents.

Hive, as the platform authority, issues Verifiable Credentials attesting to
agent properties. These VCs are signed with Hive's platform Ed25519 private
key and can be verified by any third party using the public key published at
``/.well-known/hive-identity``.

Supported credential types:
  - ``HiveOwnershipCredential`` — attests that a user owns an agent.
  - ``HiveAgentCredential`` — attests to agent metadata (type, capabilities,
    status, registration date).
  - ``HiveMarketplaceCredential`` — attests that an agent is listed on the
    Hive marketplace with specific pricing.

All credentials follow the W3C Verifiable Credentials Data Model v2
(https://www.w3.org/TR/vc-data-model-2.0/).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models.agent import Agent
from models.user import User
from services.did import agent_to_did


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_credential(
    *,
    credential_type: str,
    subject_did: str,
    claims: dict,
    issuer_did: str = "did:hive:platform",
) -> dict:
    """Build an unsigned W3C Verifiable Credential."""
    return {
        "@context": [
            "https://www.w3.org/ns/credentials/v2",
            "https://w3id.org/security/suites/ed25519-2020/v1",
        ],
        "id": f"urn:uuid:{uuid.uuid4()}",
        "type": ["VerifiableCredential", credential_type],
        "issuer": issuer_did,
        "issuanceDate": _now_iso(),
        "credentialSubject": {
            "id": subject_did,
            **claims,
        },
    }


async def issue_ownership_credential(
    agent: Agent, owner: User, db: AsyncSession
) -> dict:
    """Issue a credential attesting that ``owner`` owns ``agent``.

    The credential is signed with Hive's platform Ed25519 key. Any third
    party can verify it using the public key at /.well-known/hive-identity.
    """
    from services.platform_keys import sign_outbound

    vc = _create_credential(
        credential_type="HiveOwnershipCredential",
        subject_did=agent_to_did(agent.id),
        claims={
            "agent_name": agent.name,
            "agent_slug": agent.slug,
            "owner_email": owner.email,
            "owner_name": owner.name,
            "owner_did": f"did:hive:user:{owner.id}",
            "registered_at": agent.created_at.isoformat() if agent.created_at else None,
        },
    )
    return _sign_vc(vc)


async def issue_agent_credential(agent: Agent, db: AsyncSession) -> dict:
    """Issue a credential attesting to agent metadata (type, capabilities, status)."""
    vc = _create_credential(
        credential_type="HiveAgentCredential",
        subject_did=agent_to_did(agent.id),
        claims={
            "agent_name": agent.name,
            "agent_type": agent.agent_type,
            "capabilities": agent.capabilities or [],
            "tags": agent.tags or [],
            "status": agent.status,
            "version": agent.version,
            "signing_key_id": agent.signing_key_id,
            "has_cryptographic_identity": agent.signing_public_key is not None,
        },
    )
    return _sign_vc(vc)


async def issue_marketplace_credential(agent: Agent, db: AsyncSession) -> dict:
    """Issue a credential attesting to marketplace listing (public + pricing)."""
    vc = _create_credential(
        credential_type="HiveMarketplaceCredential",
        subject_did=agent_to_did(agent.id),
        claims={
            "agent_name": agent.name,
            "is_public": agent.is_public,
            "pricing_model": agent.pricing_model,
            "marketplace_description": agent.marketplace_description,
        },
    )
    return _sign_vc(vc)


async def issue_all_credentials(
    agent: Agent, db: AsyncSession
) -> list[dict]:
    """Issue all applicable VCs for an agent. Used by the credentials endpoint."""
    creds = []

    # Ownership credential
    result = await db.execute(select(User).where(User.id == agent.owner_id))
    owner = result.scalar_one_or_none()
    if owner:
        creds.append(await issue_ownership_credential(agent, owner, db))

    # Agent metadata credential
    creds.append(await issue_agent_credential(agent, db))

    # Marketplace credential (only for public agents)
    if agent.is_public:
        creds.append(await issue_marketplace_credential(agent, db))

    return creds


def _sign_vc(vc: dict) -> dict:
    """Sign a Verifiable Credential with Hive's platform Ed25519 key.

    Adds a ``proof`` block following the Ed25519Signature2020 suite.
    """
    from services.platform_keys import sign_outbound

    # Canonically serialise the credential (without the proof block).
    vc_without_proof = {k: v for k, v in vc.items() if k != "proof"}
    body = json.dumps(vc_without_proof, sort_keys=True, separators=(",", ":")).encode()
    ts = str(int(datetime.now(timezone.utc).timestamp()))

    result = sign_outbound(ts, body)
    if not result:
        # No platform key loaded (dev mode) — return unsigned with a note.
        vc["proof"] = {
            "type": "Ed25519Signature2020",
            "created": _now_iso(),
            "verificationMethod": "did:hive:platform#platform-key",
            "status": "unsigned — platform key not loaded",
        }
        return vc

    sig_b64, key_id = result
    vc["proof"] = {
        "type": "Ed25519Signature2020",
        "created": _now_iso(),
        "verificationMethod": f"did:hive:platform#{key_id}",
        "proofPurpose": "assertionMethod",
        "proofValue": sig_b64,
        "proofTimestamp": ts,
    }
    return vc


def verify_vc(vc: dict, platform_public_pem: Optional[str] = None) -> bool:
    """Verify a Hive-issued Verifiable Credential.

    Returns True if the Ed25519 signature is valid. In dev mode (no
    platform key), returns True for unsigned VCs.
    """
    proof = vc.get("proof")
    if not proof:
        return False

    if proof.get("status", "").startswith("unsigned"):
        return True  # dev mode

    sig_b64 = proof.get("proofValue")
    ts = proof.get("proofTimestamp")
    if not sig_b64 or not ts:
        return False

    if not platform_public_pem:
        return False

    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives import serialization
    from cryptography.exceptions import InvalidSignature

    try:
        pub = serialization.load_pem_public_key(platform_public_pem.encode("ascii"))
        vc_without_proof = {k: v for k, v in vc.items() if k != "proof"}
        body = json.dumps(vc_without_proof, sort_keys=True, separators=(",", ":")).encode()
        message = f"{ts}.".encode() + body
        pub.verify(base64.b64decode(sig_b64), message)
        return True
    except (InvalidSignature, ValueError, Exception):
        return False
