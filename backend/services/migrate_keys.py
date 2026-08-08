"""Migration tool: issue Ed25519 keypairs to legacy agents that don't have one.

Usage (from the backend directory):

    python -m services.migrate_keys               # dry run — show agents without keys
    python -m services.migrate_keys --apply       # issue keys + write output JSON
    python -m services.migrate_keys --agent-id ID # migrate a single agent

Agents registered before the Ed25519 identity system was introduced have
``signing_key_id = NULL``. This script issues a keypair for each, stores the
public key on the Agent row, and writes the private keys to a JSON file
(``key_migration_output.json``) so the operator can deliver them to agent
owners out-of-band.

Once all agents have keys and have been updated to sign callbacks with
Ed25519, the legacy ``HIVE_SIGNING_SECRET`` HMAC path can be disabled.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure backend/ is on the path when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from database import async_session_maker
from models.agent import Agent
from services.agent_keys import new_signing_fields


async def find_legacy_agents(db) -> list[Agent]:
    """Find all agents without a signing key."""
    result = await db.execute(
        select(Agent).where(Agent.signing_key_id.is_(None))
    )
    return list(result.scalars().all())


async def migrate_agent(agent: Agent, db) -> dict:
    """Issue an Ed25519 keypair for a single legacy agent.

    Returns ``{"agent_id", "agent_name", "signing_key_id", "private_key_pem"}``.
    The private key is returned so the caller can write it to the output file.
    """
    signing_fields, private_pem = new_signing_fields()
    agent.signing_key_id = signing_fields["signing_key_id"]
    agent.signing_public_key = signing_fields["signing_public_key"]
    agent.signing_key_created_at = signing_fields["signing_key_created_at"]
    await db.flush()
    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "slug": agent.slug,
        "signing_key_id": signing_fields["signing_key_id"],
        "private_key_pem": private_pem,
    }


async def run(apply: bool = False, agent_id: str | None = None) -> None:
    async with async_session_maker() as db:
        if agent_id:
            result = await db.execute(
                select(Agent).where(Agent.id == agent_id)
            )
            agents = [result.scalar_one_or_none()]
            if agents[0] is None:
                print(f"Agent {agent_id} not found")
                return
            if agents[0].signing_key_id:
                print(f"Agent {agent_id} already has a signing key: {agents[0].signing_key_id}")
                return
        else:
            agents = await find_legacy_agents(db)

        if not agents:
            print("✅ All agents already have Ed25519 signing keys. Nothing to migrate.")
            return

        print(f"Found {len(agents)} agent(s) without Ed25519 signing keys:\n")
        for a in agents:
            print(f"  {a.id[:8]}  {a.name}  (type={a.agent_type}, status={a.status})")

        if not apply:
            print("\nDry run — no changes made. Re-run with --apply to issue keys.")
            return

        print("\nIssuing Ed25519 keypairs...\n")
        migrated = []
        for agent in agents:
            if agent is None:
                continue
            result = await migrate_agent(agent, db)
            migrated.append(result)
            print(f"  ✅ {agent.name} ({agent.id[:8]}) → key_id={result['signing_key_id']}")

        await db.commit()

        # Write private keys to a file for out-of-band delivery.
        output_path = "key_migration_output.json"
        with open(output_path, "w") as f:
            json.dump({
                "migrated_at": datetime.now(timezone.utc).isoformat(),
                "count": len(migrated),
                "agents": migrated,
            }, f, indent=2)

        print(f"\n✅ Migrated {len(migrated)} agent(s).")
        print(f"📋 Private keys written to {output_path}")
        print("   Deliver each private key to the respective agent owner.")
        print("   The file contains sensitive private keys — handle accordingly.")


def main():
    ap = argparse.ArgumentParser(description="Issue Ed25519 keys to legacy agents")
    ap.add_argument("--apply", action="store_true", help="Apply the migration (default: dry run)")
    ap.add_argument("--agent-id", type=str, default=None, help="Migrate a single agent by ID")
    args = ap.parse_args()
    asyncio.run(run(apply=args.apply, agent_id=args.agent_id))


if __name__ == "__main__":
    main()
