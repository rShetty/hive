"""Scoped API keys for agents.

Agents can hold multiple API keys, each with a restricted scope set, in
addition to their single master key (Agent.api_key_hash). Scoped keys are
useful for handing out least-privilege credentials — e.g. a key that can only
send heartbeats, or one that can only complete delegations.

The master key (stored on the Agent row) always grants full access; scoped keys
grant only the scopes listed in ``scopes``.
"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.orm import relationship
from database import Base


# Recognised scopes. The master key implicitly has all of these.
SCOPE_HEARTBEAT = "heartbeat"      # POST /api/agent/heartbeat
SCOPE_DELEGATE = "delegate"        # POST /api/delegate/request (as caller)
SCOPE_COMPLETE = "complete"        # POST /api/delegate/{id}/complete, /fail
SCOPE_PROFILE_READ = "profile:read"   # GET /api/agent/me, /skills
SCOPE_PROFILE_WRITE = "profile:write" # PUT /api/agent/me, /visibility
SCOPE_ALL = "*"                    # master key equivalent

ALL_SCOPES = {
    SCOPE_HEARTBEAT, SCOPE_DELEGATE, SCOPE_COMPLETE,
    SCOPE_PROFILE_READ, SCOPE_PROFILE_WRITE, SCOPE_ALL,
}


class AgentApiKey(Base):
    __tablename__ = "agent_api_keys"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id = Column(String(36), ForeignKey("agents.id"), nullable=False, index=True)
    name = Column(String(100), nullable=True)  # human label, e.g. "ci-runner"

    # Lookup: first 16 chars of the raw key, indexed. Same scheme as the
    # master key — prefix narrows to ~1 row, then bcrypt-verify.
    key_prefix = Column(String(16), nullable=False, index=True)
    key_hash = Column(String(255), nullable=False)

    scopes = Column(JSON, default=list)  # e.g. ["heartbeat", "complete"]
    revoked = Column(Boolean, default=False)
    last_used = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    agent = relationship("Agent", back_populates="api_keys")
