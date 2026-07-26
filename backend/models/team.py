"""Team models for hierarchical multi-agent orchestration."""
import uuid
import enum
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Integer, JSON, Numeric
from sqlalchemy.orm import relationship
from database import Base


class TeamRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TeamDelegationStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Team(Base):
    """A team of agents with hierarchical delegation."""
    __tablename__ = "teams"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    root_agent_id = Column(String(36), ForeignKey("agents.id"), nullable=False)
    max_depth = Column(Integer, default=3)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", back_populates="teams")
    root_agent = relationship("Agent", foreign_keys=[root_agent_id])
    members = relationship("TeamMember", back_populates="team", cascade="all, delete-orphan")
    runs = relationship("TeamRun", back_populates="team", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Team {self.name}>"


class TeamMember(Base):
    """A member of a team with role and reporting hierarchy."""
    __tablename__ = "team_members"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    team_id = Column(String(36), ForeignKey("teams.id"), nullable=False, index=True)
    agent_id = Column(String(36), ForeignKey("agents.id"), nullable=False)
    role = Column(String(50), nullable=False, default="member")
    reports_to_member_id = Column(String(36), ForeignKey("team_members.id"), nullable=True)
    max_tokens = Column(Integer, default=200)

    created_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team", back_populates="members")
    agent = relationship("Agent")
    reports_to = relationship("TeamMember", remote_side=[id], backref="direct_reports")

    def __repr__(self):
        return f"<TeamMember {self.role} agent={self.agent_id[:8]}>"


class TeamRun(Base):
    """A single execution of a team task."""
    __tablename__ = "team_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    team_id = Column(String(36), ForeignKey("teams.id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    task = Column(Text, nullable=False)
    status = Column(String(20), default=TeamRunStatus.PENDING.value)

    delegation_tree = Column(JSON, nullable=True)
    total_tokens_used = Column(Integer, default=0)
    output_data = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)

    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team", back_populates="runs")
    user = relationship("User")
    delegations = relationship("TeamDelegation", back_populates="run", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<TeamRun {self.id[:8]} ({self.status})>"


class TeamDelegation(Base):
    """Tracks a single delegation within a team run (tree node)."""
    __tablename__ = "team_delegations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    team_run_id = Column(String(36), ForeignKey("team_runs.id"), nullable=False, index=True)
    parent_delegation_id = Column(String(36), ForeignKey("team_delegations.id"), nullable=True)
    delegation_id = Column(String(36), ForeignKey("transactions.id"), nullable=True)
    agent_id = Column(String(36), ForeignKey("agents.id"), nullable=False)

    task_description = Column(Text, nullable=True)
    status = Column(String(20), default=TeamDelegationStatus.PENDING.value)
    tokens_used = Column(Integer, default=0)
    result_data = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    depth = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    run = relationship("TeamRun", back_populates="delegations")
    agent = relationship("Agent")
    parent = relationship("TeamDelegation", remote_side=[id], backref="children")

    def __repr__(self):
        return f"<TeamDelegation {self.id[:8]} depth={self.depth}>"
