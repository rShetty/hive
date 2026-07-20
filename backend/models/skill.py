"""Skill model for agent capabilities."""
import uuid
from sqlalchemy import Column, String, Text, JSON, ForeignKey
from sqlalchemy.orm import relationship
from database import Base


class Skill(Base):
    __tablename__ = "skills"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(100), nullable=False)
    description = Column(Text, nullable=False)
    
    # Tier: core (no auth), connected (needs user auth), premium (future)
    tier = Column(String(20), default="core")
    
    # Category for grouping
    category = Column(String(50), default="general")
    
    # Required environment variables for connected skills
    # e.g., ["GITHUB_TOKEN", "LINEAR_API_KEY"]
    required_env_vars = Column(JSON, default=list)
    
    is_active = Column(String(10), default="true")  # Stored as string for SQLite compat
    
    # ---- User-created skill registry ----
    # source: "core" (platform-seeded) or "user" (created by a user)
    source = Column(String(20), default="core")
    # visibility: "platform" (everyone can use) or "private" (owner only)
    visibility = Column(String(20), default="platform")
    # Owner of a user-created skill (null for platform/core skills)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    # Skill definition: either a prompt/instruction spec, a tool spec, or both.
    #   {"kind": "prompt", "instructions": "..."}
    #   {"kind": "tool", "endpoint": "...", "method": "POST",
    #    "params_schema": {...}, "headers_env": ["X_API_KEY"]}
    #   {"kind": "both", "instructions": "...", "tool": {...}}
    definition = Column(JSON, default=dict)
    
    # Relationships
    agent_skills = relationship("AgentSkill", back_populates="skill")
    owner = relationship("User")
    
    def __repr__(self):
        return f"<Skill {self.name}>"
