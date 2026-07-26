"""Workflow models for multi-agent orchestration."""
import uuid
import enum
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Integer, JSON, Boolean, Numeric
from sqlalchemy.orm import relationship
from database import Base


class WorkflowStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class WorkflowRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Workflow(Base):
    """A workflow is a named sequence of agent tasks."""
    __tablename__ = "workflows"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    status = Column(String(20), default=WorkflowStatus.DRAFT.value)

    # Workflow-level configuration
    max_tokens_per_run = Column(Integer, default=500)
    timeout_seconds = Column(Integer, default=600)
    auto_retry = Column(Boolean, default=False)
    max_retries = Column(Integer, default=2)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    owner = relationship("User", back_populates="workflows")
    steps = relationship("WorkflowStep", back_populates="workflow", cascade="all, delete-orphan",
                         order_by="WorkflowStep.step_order")
    runs = relationship("WorkflowRun", back_populates="workflow", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Workflow {self.name} ({self.status})>"


class WorkflowStep(Base):
    """A single step in a workflow, assigned to a specific agent."""
    __tablename__ = "workflow_steps"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_id = Column(String(36), ForeignKey("workflows.id"), nullable=False, index=True)
    agent_id = Column(String(36), ForeignKey("agents.id"), nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    step_order = Column(Integer, nullable=False, default=0)

    # Task configuration
    task_template = Column(Text, nullable=False)  # Template with {{prev_output}} placeholders
    max_tokens = Column(Integer, default=100)
    timeout_seconds = Column(Integer, default=300)

    # Input mapping: how to use outputs from previous steps
    # Example: {"query": "{{step_1.output.results}", "context": "{{step_0.output.summary}}"}
    input_mapping = Column(JSON, nullable=True)

    # Step-level conditions (optional)
    # Example: {"skip_if": "{{prev_output.status}} == 'skip'"}
    condition = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    workflow = relationship("Workflow", back_populates="steps")
    agent = relationship("Agent")
    runs = relationship("WorkflowStepRun", back_populates="step", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<WorkflowStep {self.name} (order={self.step_order})>"


class WorkflowRun(Base):
    """A single execution instance of a workflow."""
    __tablename__ = "workflow_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_id = Column(String(36), ForeignKey("workflows.id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    status = Column(String(20), default=WorkflowRunStatus.PENDING.value)

    # Run configuration
    input_data = Column(JSON, nullable=True)  # Initial input for the workflow
    config_overrides = Column(JSON, nullable=True)  # Override step configs for this run

    # Results
    output_data = Column(JSON, nullable=True)  # Final output from the last step
    error_message = Column(Text, nullable=True)

    # Token tracking
    total_tokens_used = Column(Integer, default=0)

    # Timing
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    workflow = relationship("Workflow", back_populates="runs")
    user = relationship("User")
    step_runs = relationship("WorkflowStepRun", back_populates="run", cascade="all, delete-orphan",
                            order_by="WorkflowStepRun.created_at")

    def __repr__(self):
        return f"<WorkflowRun {self.id[:8]} ({self.status})>"


class WorkflowStepRun(Base):
    """Execution record for a single step within a workflow run."""
    __tablename__ = "workflow_step_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_run_id = Column(String(36), ForeignKey("workflow_runs.id"), nullable=False, index=True)
    workflow_step_id = Column(String(36), ForeignKey("workflow_steps.id"), nullable=False)
    agent_id = Column(String(36), ForeignKey("agents.id"), nullable=False)
    delegation_id = Column(String(36), ForeignKey("transactions.id"), nullable=True)

    status = Column(String(20), default=StepRunStatus.PENDING.value)
    step_order = Column(Integer, nullable=False)

    # Input/output for this step
    input_data = Column(JSON, nullable=True)
    output_data = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)

    # Token tracking
    tokens_used = Column(Integer, default=0)

    # Timing
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    run = relationship("WorkflowRun", back_populates="step_runs")
    step = relationship("WorkflowStep", back_populates="runs")
    agent = relationship("Agent")
    delegation = relationship("Transaction")

    def __repr__(self):
        return f"<WorkflowStepRun step={self.workflow_step_id[:8]} ({self.status})>"
