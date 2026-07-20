"""Agent model for AI agents registered in the marketplace."""
import re
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Integer, Enum, JSON, Boolean
from sqlalchemy.orm import relationship
from database import Base
import enum


class AgentType(str, enum.Enum):
    """How the agent is hosted."""
    MANAGED = "managed"      # Hive-managed container
    EXTERNAL = "external"    # BYOA — agent runs elsewhere, registers via SDK
    OPENCLAW = "openclaw"    # One-click OpenClaw on VPS


class AgentStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFYING = "verifying"
    ACTIVE = "active"
    IDLE = "idle"
    OFFLINE = "offline"
    ERROR = "error"


class Agent(Base):
    __tablename__ = "agents"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    
    # ---- Agentic Identity ----
    # A unique, human-readable slug (e.g. "my-coding-agent")
    slug = Column(String(120), unique=True, nullable=True, index=True)
    # Optional avatar / icon URL
    avatar_url = Column(String(500), nullable=True)
    # Machine-readable capabilities manifest (list of capability strings)
    capabilities = Column(JSON, default=list)
    # Free-form tags for discovery (e.g. ["devops", "python", "openclaw"])
    tags = Column(JSON, default=list)
    
    # Marketplace visibility and pricing
    is_public = Column(Boolean, default=False)  # Whether agent appears in marketplace
    marketplace_description = Column(Text, nullable=True)  # Public-facing description
    pricing_model = Column(JSON, nullable=True)  # {"type": "free"|"token", "rate": 10}
    
    # Agent type: managed | external | openclaw
    agent_type = Column(String(20), default=AgentType.MANAGED.value)

    # Per-agent encrypted configuration (LLM keys, integration tokens, etc.)
    # Encrypted with the same Fernet key as user-level model_api_keys_encrypted.
    config_encrypted = Column(Text, nullable=True)

    # OpenClaw instance tracking — the UUID used when deploying to VPS.
    # Allows us to locate /opt/hive/openclaw-{instance_id[:8]} for reconfig.
    openclaw_instance_id = Column(String(36), nullable=True)
    
    # Owner (required - every agent must have a human owner)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    owner = relationship("User", back_populates="agents")
    
    # Authentication
    # First 12 chars of the raw API key stored in clear for O(1) prefix filtering.
    # We still bcrypt-verify the full key — the prefix is not a secret (it has no
    # entropy advantage over a UUID), it just narrows the scan from N → ~1 row.
    api_key_prefix = Column(String(16), nullable=True, index=True)
    api_key_hash = Column(String(255), nullable=False)
    
    # Status
    status = Column(String(20), default=AgentStatus.PENDING.value)
    
    # Endpoint configuration
    endpoint_url = Column(String(500), nullable=True)  # Public URL path
    internal_port = Column(Integer, nullable=True)  # Container port
    container_id = Column(String(100), nullable=True)  # Docker container ID
    
    # Health tracking
    last_seen = Column(DateTime, nullable=True)
    last_health_check = Column(DateTime, nullable=True)
    health_check_token = Column(String(100), nullable=True)
    # Whether the agent is ready to accept new delegations (self-reported via heartbeat)
    ready = Column(Boolean, default=True)
    
    # Version
    version = Column(String(50), default="1.0.0")
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    skills = relationship("AgentSkill", back_populates="agent", cascade="all, delete-orphan")
    mcp_access = relationship("AgentMCPAccess", back_populates="agent", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Agent {self.name} ({self.status})>"
    
    @staticmethod
    def generate_slug(name: str) -> str:
        """Generate a URL-safe slug from agent name."""
        slug = re.sub(r"[^\w\s-]", "", name.lower())
        slug = re.sub(r"[\s_]+", "-", slug).strip("-")
        # Append short uuid to guarantee uniqueness
        return f"{slug}-{uuid.uuid4().hex[:6]}"
    
    def calculate_status(self):
        """Calculate current status based on last_seen."""
        if self.status == AgentStatus.ERROR.value:
            return AgentStatus.ERROR
        
        if not self.last_seen:
            if self.status == AgentStatus.PENDING.value:
                return AgentStatus.PENDING
            return AgentStatus.OFFLINE
        
        minutes_since = (datetime.utcnow() - self.last_seen).total_seconds() / 60
        
        if minutes_since < 5:
            return AgentStatus.ACTIVE
        elif minutes_since < 30:
            return AgentStatus.IDLE
        else:
            return AgentStatus.OFFLINE
