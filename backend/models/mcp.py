"""MCP server registry + per-agent access grants.

Users register MCP servers they own and explicitly grant individual agents
access to them. Auth headers are stored encrypted.
"""
import uuid
from sqlalchemy import Column, String, Text, JSON, ForeignKey, Boolean, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class MCPServer(Base):
    """A user-registered MCP (Model Context Protocol) server."""

    __tablename__ = "mcp_servers"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)

    name = Column(String(100), nullable=False)
    url = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)

    # Transport: "http" (streamable HTTP), "sse", or "stdio"
    transport = Column(String(20), default="http")

    # For stdio transport: the command (and optional env) used to launch the
    # MCP server as a local subprocess. JSON-RPC is spoken over stdin/stdout.
    command = Column(Text, nullable=True)
    env_encrypted = Column(Text, nullable=True)

    # Encrypted JSON of optional auth headers, e.g. {"Authorization": "Bearer ..."}
    headers_encrypted = Column(Text, nullable=True)

    # Auth mode: "headers" (static) or "oauth" (OAuth 2.0 connect flow).
    auth_type = Column(String(20), default="headers")

    # For OAuth: encrypted JSON of {access_token, refresh_token, expires_at,
    # token_type, scope, client_id, client_secret, issuer}. Populated by the
    # connect flow; used to build the Authorization header at runtime.
    oauth_encrypted = Column(Text, nullable=True)

    # Visibility: "private" (owner only) — MCP servers are not shared publicly
    # by default; access is granted per-agent via AgentMCPAccess.
    visibility = Column(String(20), default="private")

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User")
    access_grants = relationship(
        "AgentMCPAccess", back_populates="mcp_server",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<MCPServer {self.name}>"


class AgentMCPAccess(Base):
    """Grant of a specific MCP server's access to a specific agent."""

    __tablename__ = "agent_mcp_access"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id = Column(String(36), ForeignKey("agents.id"), nullable=False, index=True)
    mcp_server_id = Column(String(36), ForeignKey("mcp_servers.id"), nullable=False, index=True)

    # Optional per-agent override of auth headers (encrypted JSON)
    headers_encrypted = Column(Text, nullable=True)

    # Whether the grant is currently active
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    agent = relationship("Agent", back_populates="mcp_access")
    mcp_server = relationship("MCPServer", back_populates="access_grants")

    def __repr__(self):
        return f"<AgentMCPAccess {self.agent_id}:{self.mcp_server_id}>"
