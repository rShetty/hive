"""Pydantic schemas for API requests/responses."""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, EmailStr, ConfigDict, field_validator, model_validator
from datetime import datetime


class HiveBaseModel(BaseModel):
    """Base model with protected namespace config to avoid model_* warnings."""
    model_config = ConfigDict(protected_namespaces=())


# ============== User Schemas ==============

class UserBase(HiveBaseModel):
    email: EmailStr
    name: str


class UserCreate(UserBase):
    password: str

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class UserResponse(UserBase):
    id: str
    is_active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


class UserUpdate(HiveBaseModel):
    name: Optional[str] = None
    model_api_keys: Optional[Dict[str, str]] = None


# ============== Auth Schemas ==============

class Token(HiveBaseModel):
    access_token: str
    token_type: str


class TokenData(HiveBaseModel):
    sub: Optional[str] = None


class LoginRequest(HiveBaseModel):
    email: EmailStr
    password: str


# ============== Skill Schemas ==============

class SkillBase(HiveBaseModel):
    name: str
    display_name: str
    description: str
    tier: str = "core"
    category: str = "general"
    required_env_vars: List[str] = []
    # definition: {"kind": "prompt"|"tool"|"both", ...}
    definition: Optional[Dict[str, Any]] = None


class SkillCreate(SkillBase):
    visibility: Optional[str] = "private"  # private (default) | platform (admin)


class SkillResponse(SkillBase):
    id: str
    is_active: bool
    source: str = "core"
    visibility: str = "platform"
    owner_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("is_active", mode="before")
    @classmethod
    def coerce_is_active(cls, v):
        if isinstance(v, str):
            return v.lower() == "true"
        return bool(v)


# ============== AgentSkill Schemas (defined before AgentDetailResponse) ==============

class AgentSkillCreate(HiveBaseModel):
    skill_id: str
    config: Optional[Dict[str, str]] = None


class AgentSkillResponse(HiveBaseModel):
    id: str
    skill_id: str
    config: Optional[Dict[str, Any]] = None
    added_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class AgentSkillWithDetailsResponse(HiveBaseModel):
    id: str
    skill_id: str
    config: Optional[Dict[str, Any]] = None
    added_at: datetime
    skill: Optional[SkillResponse] = None
    
    model_config = ConfigDict(from_attributes=True)


# ============== Agent Schemas ==============

class AgentBase(HiveBaseModel):
    name: str
    description: Optional[str] = None


class AgentCreate(AgentBase):
    skill_ids: List[str] = []
    skill_names: List[str] = []  # alternative: resolve skills by name
    skill_configs: Optional[Dict[str, Dict[str, str]]] = {}
    # Agentic identity (optional at creation time)
    slug: Optional[str] = None
    avatar_url: Optional[str] = None
    capabilities: List[str] = []
    tags: List[str] = []
    # BYOA — external agents provide their own endpoint URL
    endpoint_url: Optional[str] = None
    agent_type: str = "managed"  # managed | external | openclaw


class AgentResponse(AgentBase):
    id: str
    slug: Optional[str] = None
    avatar_url: Optional[str] = None
    capabilities: List[str] = []
    tags: List[str] = []
    agent_type: str = "managed"
    status: str
    is_public: bool = False
    ready: Optional[bool] = True
    endpoint_url: Optional[str]
    version: str
    last_seen: Optional[datetime]
    created_at: datetime
    owner_id: Optional[str]
    owner_name: Optional[str] = None
    owner_email: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)


class AgentDetailResponse(AgentResponse):
    skills: List[AgentSkillWithDetailsResponse] = []


class AgentRegistrationResponse(HiveBaseModel):
    agent_id: str
    api_key: str
    health_check_endpoint: str
    health_check_token: str
    status: str


class MCPServerSpec(HiveBaseModel):
    """An MCP server the agent can use as a tool source."""
    name: str
    url: str = ""                  # base URL of the MCP HTTP/SSE server
    description: Optional[str] = None
    transport: str = "http"        # http | sse | stdio
    headers: Optional[Dict[str, str]] = None  # optional auth headers
    command: Optional[str] = None  # for stdio: shell command to launch
    env: Optional[Dict[str, str]] = None      # for stdio: extra env vars


class HostedAgentRequest(HiveBaseModel):
    """Bring-Your-Own-Key hosted agent.

    The platform hosts the runtime (no endpoint_url required). The user
    supplies an LLM key + picks tools (skills) and optional MCP servers,
    and Hive spins up a running agent that accepts requests at the
    platform-assigned endpoint and exposes a chat + dashboard.
    """
    name: str
    description: Optional[str] = None
    framework: str = "openclaw"   # openclaw | langchain | crewai | custom
    # LLM key for this agent (provider -> key). e.g. {"openrouter": "sk-or-..."}
    model_key: Dict[str, str] = {}
    skill_ids: List[str] = []
    skill_names: List[str] = []
    # MCP servers: ad-hoc specs and/or references to the user's MCP registry
    mcp_servers: List[MCPServerSpec] = []
    mcp_server_ids: List[str] = []
    tags: List[str] = []
    capabilities: List[str] = []


class HostedAgentResponse(HiveBaseModel):
    agent_id: str
    slug: str
    api_key: str
    url: str
    dashboard_url: str
    endpoint_url: str
    status: str


# ============== MCP Server Registry Schemas ==============

class MCPServerCreate(HiveBaseModel):
    name: str
    url: str = ""
    description: Optional[str] = None
    transport: str = "http"          # http | sse | stdio
    auth_type: str = "headers"       # headers | oauth
    # Optional auth headers (sent to the MCP server). Stored encrypted.
    headers: Optional[Dict[str, str]] = None
    # For stdio transport: command + optional env used to launch the server.
    command: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    # Pre-registered OAuth client credentials (for providers without DCR, e.g.
    # GitHub). When supplied the connect flow skips dynamic registration.
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None
    oauth_scopes: Optional[str] = None

    @model_validator(mode="after")
    def _check_transport(self):
        if self.transport not in ("http", "sse", "stdio"):
            raise ValueError("transport must be 'http', 'sse', or 'stdio'")
        if self.auth_type not in ("headers", "oauth"):
            raise ValueError("auth_type must be 'headers' or 'oauth'")
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio transport requires a 'command'")
        else:
            if not self.url:
                raise ValueError("http/sse transport requires a 'url'")
        return self


class MCPServerUpdate(HiveBaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    transport: Optional[str] = None
    auth_type: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    command: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None
    oauth_scopes: Optional[str] = None


class MCPServerResponse(HiveBaseModel):
    id: str
    owner_id: str
    name: str
    url: str
    description: Optional[str] = None
    transport: str = "http"
    auth_type: str = "headers"
    oauth_connected: bool = False
    command: Optional[str] = None
    oauth_client_id: Optional[str] = None
    visibility: str = "private"
    is_active: bool = True
    created_at: Optional[datetime] = None
    # Number of agents currently granted access (populated by the API)
    agent_count: Optional[int] = None


class AgentMCPGrantRequest(HiveBaseModel):
    # Agent ids to grant/revoke access for (the MCP server is in the URL path).
    agent_ids: List[str] = []
    # Optional per-agent auth-header overrides, keyed by agent id.
    headers: Optional[Dict[str, Dict[str, str]]] = None


class AgentMCPAccessResponse(HiveBaseModel):
    id: str
    agent_id: str
    mcp_server_id: str
    mcp_server_name: Optional[str] = None
    mcp_server_url: Optional[str] = None
    enabled: bool = True
    created_at: Optional[datetime] = None


class AgentProfileUpdate(HiveBaseModel):
    """Allowed fields for agent self-update. Prevents setting privileged fields."""
    name: Optional[str] = None
    description: Optional[str] = None
    avatar_url: Optional[str] = None
    capabilities: Optional[List[str]] = None
    tags: Optional[List[str]] = None


class AgentHeartbeatRequest(HiveBaseModel):
    """Optional body for heartbeat — agent can signal its readiness."""
    ready: bool = True  # False when busy processing a long task


class AgentHeartbeatResponse(HiveBaseModel):
    status: str
    message: str
    ready: bool = True


# ============== Health Check ==============

class HealthCheckResponse(HiveBaseModel):
    status: str
    token: str
    agent_id: str
    skills: List[str]


# ============== Filters ==============

class AgentFilter(HiveBaseModel):
    status: Optional[str] = None
    skill_id: Optional[str] = None
    owner_id: Optional[str] = None
    search: Optional[str] = None


# ============== Agent Invite Schemas ==============

class AgentInviteCreate(HiveBaseModel):
    agent_name: Optional[str] = None
    agent_type: str = "BYOA_CUSTOM"


class AgentInviteResponse(HiveBaseModel):
    invite_id: str
    invite_token: str
    expires_at: datetime
    instructions_url: str
    

class AgentAcceptInvite(HiveBaseModel):
    invite_token: str
    name: str
    description: Optional[str] = None
    endpoint_url: str
    capabilities: List[str] = []
    tags: List[str] = []
    skill_names: List[str] = []


# ============== Wallet & Transaction Schemas ==============

class WalletResponse(HiveBaseModel):
    id: str
    user_id: str
    balance: float
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class TransactionCreate(HiveBaseModel):
    target_agent_id: str
    amount: float
    task_description: str
    max_tokens: Optional[float] = None
    callback_url: Optional[str] = None
    timeout_seconds: int = 300


class TransactionResponse(HiveBaseModel):
    id: str
    from_wallet_id: str
    to_wallet_id: str
    amount: float
    platform_fee: Optional[float] = None
    session_id: Optional[str] = None
    delegation_depth: int = 0
    task_result: Optional[Dict[str, Any]] = None
    transaction_type: str
    delegating_agent_id: Optional[str]
    executing_agent_id: Optional[str]
    task_description: Optional[str]
    status: str
    created_at: datetime
    completed_at: Optional[datetime]
    
    model_config = ConfigDict(from_attributes=True)


# ============== Delegation Schemas ==============

class DelegationRequest(HiveBaseModel):
    target_agent_id: str
    task_description: str
    max_tokens: float
    callback_url: Optional[str] = None
    timeout_seconds: int = 300
    context: Optional[Dict[str, Any]] = None
    # Optional session ID to group related delegations (multi-turn workflows)
    session_id: Optional[str] = None
    
    @field_validator("callback_url")
    @classmethod
    def validate_callback_url(cls, v: Optional[str]) -> Optional[str]:
        """Validate callback URL to prevent SSRF attacks."""
        if v is None:
            return v
        
        from urllib.parse import urlparse
        parsed = urlparse(v)
        
        # Must be HTTP/HTTPS
        if parsed.scheme not in ["http", "https"]:
            raise ValueError("Callback URL must use HTTP or HTTPS")
        
        # Block private IP ranges (basic SSRF protection)
        hostname = parsed.hostname
        if hostname:
            import ipaddress
            try:
                ip = ipaddress.ip_address(hostname)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    raise ValueError("Callback URL cannot point to private IP addresses")
            except ValueError:
                # Not an IP address, probably a domain - allow it
                pass
        
        # Block localhost variations
        if hostname and hostname.lower() in ["localhost", "127.0.0.1", "::1", "0.0.0.0"]:
            raise ValueError("Callback URL cannot be localhost")
        
        return v
    
    @field_validator("max_tokens")
    @classmethod
    def validate_max_tokens(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("max_tokens must be positive")
        if v > 1000:
            raise ValueError("max_tokens cannot exceed 1000")
        return v


class DelegationResponse(HiveBaseModel):
    delegation_id: str
    status: str
    message: str


class DelegationComplete(HiveBaseModel):
    result: Dict[str, Any]
    tokens_used: float


class TokenEstimateRequest(HiveBaseModel):
    task_description: str
    target_agent_id: Optional[str] = None


class TokenEstimateResponse(HiveBaseModel):
    estimated_tokens: int
    breakdown: Dict[str, Any]


# ============== Review Schemas ==============

class AgentReviewCreate(HiveBaseModel):
    agent_id: str
    delegation_id: str
    rating: int
    comment: Optional[str] = None
    
    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v: int) -> int:
        if not 1 <= v <= 5:
            raise ValueError("Rating must be between 1 and 5")
        return v


class AgentReviewResponse(HiveBaseModel):
    id: str
    agent_id: str
    reviewer_user_id: str
    rating: int
    comment: Optional[str]
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


# ============== Marketplace Schemas ==============

class MarketplaceAgentCard(HiveBaseModel):
    id: str
    name: str
    slug: Optional[str]
    avatar_url: Optional[str]
    marketplace_description: Optional[str]
    pricing_model: Optional[Dict[str, Any]]
    tags: List[str]
    status: str
    owner_id: Optional[str]
    last_seen: Optional[datetime]
    average_rating: Optional[float] = None
    total_reviews: int = 0
    
    model_config = ConfigDict(from_attributes=True)


class PricingModel(HiveBaseModel):
    """Validated pricing model structure."""
    type: str  # "free" or "token"
    rate: Optional[float] = 0.0
    
    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ["free", "token"]:
            raise ValueError("Pricing type must be 'free' or 'token'")
        return v
    
    @field_validator("rate")
    @classmethod
    def validate_rate(cls, v: Optional[float]) -> float:
        if v is not None and v < 0:
            raise ValueError("Rate cannot be negative")
        return v or 0.0


class VisibilityUpdate(HiveBaseModel):
    is_public: bool
    marketplace_description: Optional[str] = None
    pricing_model: Optional[PricingModel] = None
