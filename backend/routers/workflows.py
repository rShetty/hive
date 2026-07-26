"""Workflow management and execution routes."""
import json
import asyncio
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from sqlalchemy.orm import selectinload

from database import get_db, async_session_maker
from models.user import User
from models.agent import Agent, AgentStatus
from models.workflow import (
    Workflow, WorkflowStep, WorkflowRun, WorkflowStepRun,
    WorkflowStatus, WorkflowRunStatus, StepRunStatus
)
from models.wallet import Wallet
from models.transaction import Transaction, TransactionType, TransactionStatus
from schemas import (
    WorkflowCreate, WorkflowUpdate, WorkflowResponse, WorkflowDetailResponse,
    WorkflowStepCreate, WorkflowStepUpdate, WorkflowStepResponse,
    WorkflowRunCreate, WorkflowRunResponse, WorkflowStepRunResponse,
)
from auth import get_current_active_user
from services.agent_client import get_agent_client, AgentTimeoutError, AgentConnectionError, AgentClientError
from services import delegation_hub
from routers.delegation import delegation_status

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_owned_workflow(workflow_id: str, user: User, db: AsyncSession) -> Workflow:
    from models.agent import Agent
    from models.agent_skill import AgentSkill
    result = await db.execute(
        select(Workflow)
        .options(
            selectinload(Workflow.steps)
            .selectinload(WorkflowStep.agent)
            .selectinload(Agent.skills)
            .selectinload(AgentSkill.skill)
        )
        .where(Workflow.id == workflow_id)
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if workflow.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Not your workflow")
    return workflow


async def _get_workflow_with_runs(workflow_id: str, db: AsyncSession) -> Workflow:
    result = await db.execute(
        select(Workflow)
        .options(
            selectinload(Workflow.steps),
            selectinload(Workflow.runs).selectinload(WorkflowRun.step_runs)
        )
        .where(Workflow.id == workflow_id)
    )
    return result.scalar_one_or_none()


def _step_to_response(step: WorkflowStep, agent_name: str = None) -> WorkflowStepResponse:
    agent = getattr(step, 'agent', None)
    agent_skills = None
    if agent:
        try:
            skills = getattr(agent, 'skills', None)
            if skills:
                agent_skills = [s.skill.display_name for s in skills if s.skill]
        except Exception:
            pass
    return WorkflowStepResponse(
        id=step.id,
        workflow_id=step.workflow_id,
        agent_id=step.agent_id,
        agent_name=agent_name or (agent.name if agent else None),
        agent_description=agent.description if agent else None,
        agent_status=agent.status if agent else None,
        agent_skills=agent_skills,
        agent_endpoint=agent.endpoint_url if agent else None,
        name=step.name,
        description=step.description,
        step_order=step.step_order,
        task_template=step.task_template,
        max_tokens=step.max_tokens,
        timeout_seconds=step.timeout_seconds,
        input_mapping=step.input_mapping,
        condition=step.condition,
        created_at=step.created_at,
    )


def _run_to_response(run: WorkflowRun, workflow_name: str = None, agent_map: dict = None) -> WorkflowRunResponse:
    step_runs = []
    for sr in (run.step_runs or []):
        # Get agent name from agent_map if provided, else try relationship
        agent_name = None
        if agent_map and sr.agent_id in agent_map:
            agent_name = agent_map[sr.agent_id]
        elif hasattr(sr, 'agent') and sr.agent is not None:
            agent_name = getattr(sr.agent, "name", None)
        step_runs.append(WorkflowStepRunResponse(
            id=sr.id,
            workflow_run_id=sr.workflow_run_id,
            workflow_step_id=sr.workflow_step_id,
            agent_id=sr.agent_id,
            agent_name=agent_name,
            delegation_id=sr.delegation_id,
            status=sr.status,
            step_order=sr.step_order,
            input_data=sr.input_data,
            output_data=sr.output_data,
            error_message=sr.error_message,
            tokens_used=sr.tokens_used,
            started_at=sr.started_at,
            completed_at=sr.completed_at,
            created_at=sr.created_at,
        ))
    task_text = None
    if run.input_data:
        if isinstance(run.input_data, dict):
            task_text = run.input_data.get("task") or run.input_data.get("query")

    return WorkflowRunResponse(
        id=run.id,
        workflow_id=run.workflow_id,
        workflow_name=workflow_name,
        user_id=run.user_id,
        status=run.status,
        task=task_text,
        input_data=run.input_data,
        output_data=run.output_data,
        error_message=run.error_message,
        total_tokens_used=run.total_tokens_used,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
        step_runs=step_runs,
    )


def _resolve_template(template: str, context: dict) -> str:
    """Replace {{key}} placeholders in a template string with context values."""
    import re
    def _replace(match):
        key = match.group(1).strip()
        parts = key.split(".")
        val = context
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p)
            else:
                return match.group(0)
        if val is None:
            return ""
        return str(val) if not isinstance(val, str) else val

    return re.sub(r"\{\{(.+?)\}\}", _replace, template)


# ── Workflow CRUD ─────────────────────────────────────────────────────────────

@router.get("", response_model=list[WorkflowResponse])
async def list_workflows(
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List the current user's workflows."""
    query = select(Workflow).where(Workflow.owner_id == current_user.id)
    if status:
        query = query.where(Workflow.status == status)
    query = query.order_by(desc(Workflow.updated_at)).limit(limit).offset(offset)
    result = await db.execute(query)
    workflows = result.scalars().all()

    out = []
    for wf in workflows:
        # Count steps
        step_count = (await db.execute(
            select(func.count()).select_from(WorkflowStep).where(WorkflowStep.workflow_id == wf.id)
        )).scalar() or 0

        # Get last run status
        last_run = (await db.execute(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == wf.id)
            .order_by(desc(WorkflowRun.created_at))
            .limit(1)
        )).scalar_one_or_none()

        resp = WorkflowResponse(
            id=wf.id,
            name=wf.name,
            description=wf.description,
            owner_id=wf.owner_id,
            status=wf.status,
            max_tokens_per_run=wf.max_tokens_per_run,
            timeout_seconds=wf.timeout_seconds,
            auto_retry=wf.auto_retry,
            max_retries=wf.max_retries,
            step_count=step_count,
            last_run_status=last_run.status if last_run else None,
            last_run_at=last_run.created_at if last_run else None,
            created_at=wf.created_at,
            updated_at=wf.updated_at,
        )
        out.append(resp)

    return out


@router.post("", response_model=WorkflowDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    data: WorkflowCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Create a new workflow with optional initial steps."""
    workflow = Workflow(
        name=data.name,
        description=data.description,
        owner_id=current_user.id,
        status=data.status or WorkflowStatus.DRAFT.value,
        max_tokens_per_run=data.max_tokens_per_run,
        timeout_seconds=data.timeout_seconds,
        auto_retry=data.auto_retry,
        max_retries=data.max_retries,
    )
    db.add(workflow)
    await db.flush()

    # Add steps if provided
    steps_out = []
    if data.steps:
        for i, step_data in enumerate(data.steps):
            # Verify agent exists and user has access
            agent = (await db.execute(
                select(Agent).where(Agent.id == step_data.agent_id)
            )).scalar_one_or_none()
            if not agent:
                raise HTTPException(status_code=404, detail=f"Agent {step_data.agent_id} not found")

            step = WorkflowStep(
                workflow_id=workflow.id,
                agent_id=step_data.agent_id,
                name=step_data.name,
                description=step_data.description,
                step_order=step_data.step_order if step_data.step_order > 0 else i,
                task_template=step_data.task_template,
                max_tokens=step_data.max_tokens,
                timeout_seconds=step_data.timeout_seconds,
                input_mapping=step_data.input_mapping,
                condition=step_data.condition,
            )
            db.add(step)
            await db.flush()
            steps_out.append(_step_to_response(step, agent.name))

    await db.commit()
    await db.refresh(workflow)

    return WorkflowDetailResponse(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        owner_id=workflow.owner_id,
        status=workflow.status,
        max_tokens_per_run=workflow.max_tokens_per_run,
        timeout_seconds=workflow.timeout_seconds,
        auto_retry=workflow.auto_retry,
        max_retries=workflow.max_retries,
        step_count=len(steps_out),
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        steps=steps_out,
    )


@router.get("/{workflow_id}", response_model=WorkflowDetailResponse)
async def get_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get workflow details including steps."""
    workflow = await _get_owned_workflow(workflow_id, current_user, db)

    steps_out = []
    for step in sorted(workflow.steps, key=lambda s: s.step_order):
        agent_name = getattr(step.agent, "name", None) if step.agent else None
        steps_out.append(_step_to_response(step, agent_name))

    last_run = (await db.execute(
        select(WorkflowRun)
        .where(WorkflowRun.workflow_id == workflow.id)
        .order_by(desc(WorkflowRun.created_at))
        .limit(1)
    )).scalar_one_or_none()

    return WorkflowDetailResponse(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        owner_id=workflow.owner_id,
        status=workflow.status,
        max_tokens_per_run=workflow.max_tokens_per_run,
        timeout_seconds=workflow.timeout_seconds,
        auto_retry=workflow.auto_retry,
        max_retries=workflow.max_retries,
        step_count=len(steps_out),
        last_run_status=last_run.status if last_run else None,
        last_run_at=last_run.created_at if last_run else None,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        steps=steps_out,
    )


@router.put("/{workflow_id}", response_model=WorkflowResponse)
async def update_workflow(
    workflow_id: str,
    data: WorkflowUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update workflow metadata."""
    workflow = await _get_owned_workflow(workflow_id, current_user, db)

    if data.name is not None:
        workflow.name = data.name
    if data.description is not None:
        workflow.description = data.description
    if data.status is not None:
        if data.status not in [s.value for s in WorkflowStatus]:
            raise HTTPException(status_code=400, detail="Invalid status")
        workflow.status = data.status
    if data.max_tokens_per_run is not None:
        workflow.max_tokens_per_run = data.max_tokens_per_run
    if data.timeout_seconds is not None:
        workflow.timeout_seconds = data.timeout_seconds
    if data.auto_retry is not None:
        workflow.auto_retry = data.auto_retry
    if data.max_retries is not None:
        workflow.max_retries = data.max_retries

    workflow.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(workflow)

    step_count = (await db.execute(
        select(func.count()).select_from(WorkflowStep).where(WorkflowStep.workflow_id == workflow.id)
    )).scalar() or 0

    return WorkflowResponse(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        owner_id=workflow.owner_id,
        status=workflow.status,
        max_tokens_per_run=workflow.max_tokens_per_run,
        timeout_seconds=workflow.timeout_seconds,
        auto_retry=workflow.auto_retry,
        max_retries=workflow.max_retries,
        step_count=step_count,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
    )


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Delete a workflow and all its steps and runs."""
    workflow = await _get_owned_workflow(workflow_id, current_user, db)
    await db.delete(workflow)
    await db.commit()


# ── Step management ───────────────────────────────────────────────────────────

@router.post("/{workflow_id}/steps", response_model=WorkflowStepResponse, status_code=status.HTTP_201_CREATED)
async def add_step(
    workflow_id: str,
    data: WorkflowStepCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Add a step to a workflow."""
    workflow = await _get_owned_workflow(workflow_id, current_user, db)

    # Verify agent exists
    agent = (await db.execute(
        select(Agent).where(Agent.id == data.agent_id)
    )).scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get current max step_order
    max_order = (await db.execute(
        select(func.max(WorkflowStep.step_order))
        .where(WorkflowStep.workflow_id == workflow_id)
    )).scalar() or 0

    step = WorkflowStep(
        workflow_id=workflow_id,
        agent_id=data.agent_id,
        name=data.name,
        description=data.description,
        step_order=data.step_order if data.step_order > 0 else max_order + 1,
        task_template=data.task_template,
        max_tokens=data.max_tokens,
        timeout_seconds=data.timeout_seconds,
        input_mapping=data.input_mapping,
        condition=data.condition,
    )
    db.add(step)
    workflow.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(step)

    return _step_to_response(step, agent.name)


@router.put("/{workflow_id}/steps/{step_id}", response_model=WorkflowStepResponse)
async def update_step(
    workflow_id: str,
    step_id: str,
    data: WorkflowStepUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update a workflow step."""
    workflow = await _get_owned_workflow(workflow_id, current_user, db)

    result = await db.execute(
        select(WorkflowStep).where(
            WorkflowStep.id == step_id,
            WorkflowStep.workflow_id == workflow_id,
        )
    )
    step = result.scalar_one_or_none()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")

    if data.agent_id is not None:
        agent = (await db.execute(
            select(Agent).where(Agent.id == data.agent_id)
        )).scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        step.agent_id = data.agent_id
    if data.name is not None:
        step.name = data.name
    if data.description is not None:
        step.description = data.description
    if data.step_order is not None:
        step.step_order = data.step_order
    if data.task_template is not None:
        step.task_template = data.task_template
    if data.max_tokens is not None:
        step.max_tokens = data.max_tokens
    if data.timeout_seconds is not None:
        step.timeout_seconds = data.timeout_seconds
    if data.input_mapping is not None:
        step.input_mapping = data.input_mapping
    if data.condition is not None:
        step.condition = data.condition

    workflow.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(step)

    agent_name = getattr(step.agent, "name", None) if step.agent else None
    return _step_to_response(step, agent_name)


@router.delete("/{workflow_id}/steps/{step_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_step(
    workflow_id: str,
    step_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Delete a workflow step."""
    workflow = await _get_owned_workflow(workflow_id, current_user, db)

    result = await db.execute(
        select(WorkflowStep).where(
            WorkflowStep.id == step_id,
            WorkflowStep.workflow_id == workflow_id,
        )
    )
    step = result.scalar_one_or_none()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")

    await db.delete(step)
    workflow.updated_at = datetime.utcnow()
    await db.commit()


# ── Workflow execution ────────────────────────────────────────────────────────

async def _execute_workflow_run(run_id: str, workflow_id: str) -> None:
    """Background task: execute all steps in a workflow run sequentially."""
    async with async_session_maker() as db:
        # Load the run
        result = await db.execute(
            select(WorkflowRun)
            .options(selectinload(WorkflowRun.step_runs))
            .where(WorkflowRun.id == run_id)
        )
        run = result.scalar_one_or_none()
        if not run:
            return

        # Load workflow with steps
        wf_result = await db.execute(
            select(Workflow)
            .options(selectinload(Workflow.steps).selectinload(WorkflowStep.agent))
            .where(Workflow.id == workflow_id)
        )
        workflow = wf_result.scalar_one_or_none()
        if not workflow:
            return

        # Update run status
        run.status = WorkflowRunStatus.RUNNING.value
        run.started_at = datetime.utcnow()
        await db.commit()

        # Build context from task string — all agents collaborate on the same task
        task_text = ""
        if run.input_data:
            if isinstance(run.input_data, dict) and "task" in run.input_data:
                task_text = run.input_data["task"]
            elif isinstance(run.input_data, dict) and "query" in run.input_data:
                task_text = run.input_data["query"]
            else:
                task_text = str(run.input_data)
        context = {"task": task_text, "workflow_input": task_text}
        accumulated_tokens = 0
        failed = False

        # Publish initial status
        delegation_hub.publish(f"workflow_{run_id}", {
            "type": "status",
            "data": {"status": "running"}
        })
        steps = sorted(workflow.steps, key=lambda s: s.step_order)
        delegation_hub.publish(f"workflow_{run_id}", {
            "type": "log",
            "data": {
                "timestamp": datetime.utcnow().isoformat(),
                "level": "info",
                "message": f"Workflow run started — {len(steps)} step(s)",
                "data": {"task": task_text},
                "source": "workflow",
            }
        })
        agent_client = get_agent_client(timeout=workflow.timeout_seconds)

        for step in steps:
            # Check step condition
            if step.condition and step.condition.get("skip_if"):
                skip_expr = _resolve_template(step.condition["skip_if"], context)
                if skip_expr.lower() in ("true", "1", "yes"):
                    # Create skipped step run
                    step_run = WorkflowStepRun(
                        workflow_run_id=run.id,
                        workflow_step_id=step.id,
                        agent_id=step.agent_id,
                        status=StepRunStatus.SKIPPED.value,
                        step_order=step.step_order,
                        input_data={},
                        output_data={},
                    )
                    db.add(step_run)
                    await db.commit()
                    delegation_hub.publish(f"workflow_{run_id}", {
                        "type": "log",
                        "data": {
                            "timestamp": datetime.utcnow().isoformat(),
                            "level": "info",
                            "message": f"Step {step.step_order + 1} skipped",
                            "data": {"step": step.step_order},
                            "source": "workflow",
                        }
                    })
                    continue

            # Resolve task template
            task_description = _resolve_template(step.task_template, context)

            # Resolve input mapping
            input_data = {}
            if step.input_mapping:
                for k, v in step.input_mapping.items():
                    input_data[k] = _resolve_template(str(v), context) if isinstance(v, str) else v

            # Create step run
            step_run = WorkflowStepRun(
                workflow_run_id=run.id,
                workflow_step_id=step.id,
                agent_id=step.agent_id,
                status=StepRunStatus.RUNNING.value,
                step_order=step.step_order,
                input_data=input_data,
                started_at=datetime.utcnow(),
            )
            db.add(step_run)
            await db.commit()
            await db.refresh(step_run)

            # Publish step start
            delegation_hub.publish(f"workflow_{run_id}", {
                "type": "step_update",
                "data": {
                    "id": step_run.id,
                    "status": "running",
                    "step_order": step.step_order,
                    "agent_name": step.agent.name if step.agent else None,
                    "tokens_used": 0,
                }
            })
            delegation_hub.publish(f"workflow_{run_id}", {
                "type": "log",
                "data": {
                    "timestamp": datetime.utcnow().isoformat(),
                    "level": "info",
                    "message": f"Step {step.step_order + 1}: {step.name} — delegating to {step.agent.name if step.agent else 'agent'}",
                    "data": {"step": step.step_order, "agent": step.agent.name if step.agent else None},
                    "source": "workflow",
                }
            })

            # Create delegation transaction
            from routers.wallet import get_or_create_wallet
            user_wallet = await get_or_create_wallet(run.user_id, db)
            agent_wallet = await get_or_create_wallet(step.agent.owner_id, db) if step.agent.owner_id else None

            if not agent_wallet:
                step_run.status = StepRunStatus.FAILED.value
                step_run.error_message = "Agent has no owner wallet"
                step_run.completed_at = datetime.utcnow()
                await db.commit()
                delegation_hub.publish(f"workflow_{run_id}", {
                    "type": "step_update",
                    "data": {
                        "id": step_run.id,
                        "status": "failed",
                        "error_message": "Agent has no owner wallet",
                        "step_order": step.step_order,
                        "agent_name": step.agent.name if step.agent else None,
                        "tokens_used": 0,
                    }
                })
                delegation_hub.publish(f"workflow_{run_id}", {
                    "type": "log",
                    "data": {
                        "timestamp": datetime.utcnow().isoformat(),
                        "level": "error",
                        "message": f"Step {step.step_order + 1} failed — agent has no owner wallet",
                        "data": {"step": step.step_order},
                        "source": "workflow",
                    }
                })
                failed = True
                break

            # Escrow tokens
            user_wallet.balance -= step.max_tokens
            await db.flush()
            if user_wallet.balance < 0:
                await db.rollback()
                step_run.status = StepRunStatus.FAILED.value
                step_run.error_message = "Insufficient tokens"
                step_run.completed_at = datetime.utcnow()
                await db.commit()
                delegation_hub.publish(f"workflow_{run_id}", {
                    "type": "step_update",
                    "data": {
                        "id": step_run.id,
                        "status": "failed",
                        "error_message": "Insufficient tokens",
                        "step_order": step.step_order,
                        "agent_name": step.agent.name if step.agent else None,
                        "tokens_used": 0,
                    }
                })
                delegation_hub.publish(f"workflow_{run_id}", {
                    "type": "log",
                    "data": {
                        "timestamp": datetime.utcnow().isoformat(),
                        "level": "error",
                        "message": f"Step {step.step_order + 1} failed — insufficient tokens",
                        "data": {"step": step.step_order},
                        "source": "workflow",
                    }
                })
                failed = True
                break

            transaction = Transaction(
                from_wallet_id=user_wallet.id,
                to_wallet_id=agent_wallet.id,
                amount=step.max_tokens,
                transaction_type=TransactionType.DELEGATION.value,
                delegating_agent_id=None,
                executing_agent_id=step.agent_id,
                originating_user_id=run.user_id,
                session_id=run.id,
                delegation_depth=0,
                task_description=task_description,
                status=TransactionStatus.PENDING.value,
            )
            db.add(transaction)
            await db.commit()
            await db.refresh(transaction)

            step_run.delegation_id = transaction.id
            await db.commit()

            # Build endpoint URL
            agent = step.agent
            endpoint = agent.endpoint_url
            if agent.agent_type == "openclaw" and agent.openclaw_instance_id:
                endpoint = f"http://openclaw-{agent.openclaw_instance_id[:8]}:9000"
            if not endpoint:
                step_run.status = StepRunStatus.FAILED.value
                step_run.error_message = "Agent has no endpoint"
                step_run.completed_at = datetime.utcnow()
                await db.commit()
                delegation_hub.publish(f"workflow_{run_id}", {
                    "type": "step_update",
                    "data": {
                        "id": step_run.id,
                        "status": "failed",
                        "error_message": "Agent has no endpoint",
                        "step_order": step.step_order,
                        "agent_name": step.agent.name if step.agent else None,
                        "tokens_used": 0,
                    }
                })
                delegation_hub.publish(f"workflow_{run_id}", {
                    "type": "log",
                    "data": {
                        "timestamp": datetime.utcnow().isoformat(),
                        "level": "error",
                        "message": f"Step {step.step_order + 1} failed — agent has no endpoint",
                        "data": {"step": step.step_order},
                        "source": "workflow",
                    }
                })
                failed = True
                break

            # Call the agent
            try:
                agent_response = await agent_client.send_delegation_task(
                    target_endpoint=endpoint,
                    delegation_id=transaction.id,
                    task_description=task_description,
                    max_tokens=step.max_tokens,
                    context=input_data,
                    timeout=step.timeout_seconds,
                )
            except (AgentTimeoutError, AgentConnectionError, AgentClientError) as e:
                # Fail and refund
                from decimal import Decimal
                user_wallet.balance += Decimal(str(step.max_tokens))
                transaction.status = TransactionStatus.FAILED.value
                transaction.completed_at = datetime.utcnow()
                transaction.refund_reason = f"agent_error: {e}"
                step_run.status = StepRunStatus.FAILED.value
                step_run.error_message = str(e)
                step_run.completed_at = datetime.utcnow()
                await db.commit()
                delegation_hub.publish(f"workflow_{run_id}", {
                    "type": "step_update",
                    "data": {
                        "id": step_run.id,
                        "status": "failed",
                        "error_message": str(e),
                        "step_order": step.step_order,
                        "agent_name": step.agent.name if step.agent else None,
                        "tokens_used": 0,
                    }
                })
                delegation_hub.publish(f"workflow_{run_id}", {
                    "type": "log",
                    "data": {
                        "timestamp": datetime.utcnow().isoformat(),
                        "level": "error",
                        "message": f"Step {step.step_order + 1} failed — {e}",
                        "data": {"step": step.step_order, "error": str(e)},
                        "source": "workflow",
                    }
                })
                failed = True
                break

            # Process response
            reported_status = agent_response.get("status", "unknown")
            tokens_used = agent_response.get("tokens_used", step.max_tokens)
            result_data = agent_response.get("result", {})

            if reported_status == "completed":
                from decimal import Decimal
                # Settle the transaction
                tokens_used_dec = Decimal(str(min(tokens_used, step.max_tokens)))
                agent_receives = tokens_used_dec * Decimal("0.9")  # 10% platform fee
                agent_wallet.balance += agent_receives
                refund = Decimal(str(step.max_tokens)) - tokens_used_dec
                if refund > 0:
                    user_wallet.balance += refund

                transaction.amount = tokens_used_dec
                transaction.platform_fee = tokens_used_dec * Decimal("0.1")
                transaction.task_result = result_data
                transaction.status = TransactionStatus.COMPLETED.value
                transaction.completed_at = datetime.utcnow()

                step_run.status = StepRunStatus.COMPLETED.value
                step_run.output_data = result_data
                step_run.tokens_used = int(float(tokens_used))
                step_run.completed_at = datetime.utcnow()
                accumulated_tokens += int(float(tokens_used))

                delegation_hub.publish(f"workflow_{run_id}", {
                    "type": "step_update",
                    "data": {
                        "id": step_run.id,
                        "status": "completed",
                        "step_order": step.step_order,
                        "agent_name": step.agent.name if step.agent else None,
                        "tokens_used": int(float(tokens_used)),
                        "output_data": result_data,
                    }
                })
                delegation_hub.publish(f"workflow_{run_id}", {
                    "type": "log",
                    "data": {
                        "timestamp": datetime.utcnow().isoformat(),
                        "level": "info",
                        "message": f"Step {step.step_order + 1} completed — {int(float(tokens_used))} tokens used",
                        "data": {"step": step.step_order, "tokens_used": int(float(tokens_used))},
                        "source": "workflow",
                    }
                })

                # Update context for next step
                context["prev_output"] = result_data
                context[f"step_{step.step_order}"] = {"output": result_data}
            else:
                # Agent accepted but is processing async — wait for callback
                callback_timeout = step.timeout_seconds or workflow.timeout_seconds or 300
                poll_interval = 2
                elapsed = 0
                while elapsed < callback_timeout:
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                    current_status = delegation_status.get(transaction.id)
                    if current_status in ("completed", "failed"):
                        break

                # Re-read transaction to get callback result
                await db.refresh(transaction)
                result_data = transaction.task_result or {}
                tokens_used = float(transaction.amount or step.max_tokens)

                if current_status == "failed":
                    step_run.status = StepRunStatus.FAILED.value
                    step_run.error_message = "Agent callback reported failure"
                    step_run.completed_at = datetime.utcnow()
                    failed = True
                    await db.commit()
                    delegation_hub.publish(f"workflow_{run_id}", {
                        "type": "step_update",
                        "data": {
                            "id": step_run.id,
                            "status": "failed",
                            "error_message": "Agent callback reported failure",
                            "step_order": step.step_order,
                            "agent_name": step.agent.name if step.agent else None,
                            "tokens_used": 0,
                        }
                    })
                    delegation_hub.publish(f"workflow_{run_id}", {
                        "type": "log",
                        "data": {
                            "timestamp": datetime.utcnow().isoformat(),
                            "level": "error",
                            "message": f"Step {step.step_order + 1} failed — agent callback reported failure",
                            "data": {"step": step.step_order},
                            "source": "workflow",
                        }
                    })
                    break

                from decimal import Decimal
                tokens_used_dec = Decimal(str(min(tokens_used, step.max_tokens)))
                agent_receives = tokens_used_dec * Decimal("0.9")
                agent_wallet.balance += agent_receives
                refund = Decimal(str(step.max_tokens)) - tokens_used_dec
                if refund > 0:
                    user_wallet.balance += refund

                transaction.amount = tokens_used_dec
                transaction.platform_fee = tokens_used_dec * Decimal("0.1")
                transaction.status = TransactionStatus.COMPLETED.value
                transaction.completed_at = datetime.utcnow()

                step_run.status = StepRunStatus.COMPLETED.value
                step_run.output_data = result_data
                step_run.tokens_used = int(float(tokens_used))
                step_run.completed_at = datetime.utcnow()
                accumulated_tokens += int(float(tokens_used))

                delegation_hub.publish(f"workflow_{run_id}", {
                    "type": "step_update",
                    "data": {
                        "id": step_run.id,
                        "status": "completed",
                        "step_order": step.step_order,
                        "agent_name": step.agent.name if step.agent else None,
                        "tokens_used": int(float(tokens_used)),
                        "output_data": result_data,
                    }
                })
                delegation_hub.publish(f"workflow_{run_id}", {
                    "type": "log",
                    "data": {
                        "timestamp": datetime.utcnow().isoformat(),
                        "level": "info",
                        "message": f"Step {step.step_order + 1} completed — {int(float(tokens_used))} tokens used",
                        "data": {"step": step.step_order, "tokens_used": int(float(tokens_used))},
                        "source": "workflow",
                    }
                })

                context["prev_output"] = result_data
                context[f"step_{step.step_order}"] = {"output": result_data}

            await db.commit()

            if failed:
                break

        # Finalize run
        if failed:
            run.status = WorkflowRunStatus.FAILED.value
            run.error_message = "One or more steps failed"
        else:
            run.status = WorkflowRunStatus.COMPLETED.value
            run.output_data = context.get("prev_output")

        run.total_tokens_used = accumulated_tokens
        run.completed_at = datetime.utcnow()
        await db.commit()

        # Publish completion event
        delegation_hub.publish(f"workflow_{run_id}", {
            "type": "status",
            "data": {"status": run.status}
        })
        delegation_hub.publish(f"workflow_{run_id}", {
            "type": "log",
            "data": {
                "timestamp": datetime.utcnow().isoformat(),
                "level": "info" if not failed else "error",
                "message": f"Workflow {run.status} — {accumulated_tokens} total tokens",
                "data": {"tokens_used": accumulated_tokens},
                "source": "workflow",
            }
        })
        delegation_hub.publish(f"workflow_{run_id}", {
            "type": "done",
            "data": {"status": run.status, "tokens_used": accumulated_tokens}
        })


@router.post("/{workflow_id}/run", response_model=WorkflowRunResponse)
async def start_workflow_run(
    workflow_id: str,
    data: WorkflowRunCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Start a new workflow run."""
    workflow = await _get_owned_workflow(workflow_id, current_user, db)

    if workflow.status != WorkflowStatus.ACTIVE.value:
        raise HTTPException(
            status_code=400,
            detail="Workflow must be active to run. Set status to 'active' first.",
        )

    if not workflow.steps:
        raise HTTPException(status_code=400, detail="Workflow has no steps")

    # Create run — store task as input_data for backward compat
    run = WorkflowRun(
        workflow_id=workflow_id,
        user_id=current_user.id,
        status=WorkflowRunStatus.PENDING.value,
        input_data={"task": data.task} if data.task else {},
        config_overrides=data.config_overrides,
    )
    db.add(run)
    await db.commit()

    # Create step run placeholders
    for step in sorted(workflow.steps, key=lambda s: s.step_order):
        step_run = WorkflowStepRun(
            workflow_run_id=run.id,
            workflow_step_id=step.id,
            agent_id=step.agent_id,
            status=StepRunStatus.PENDING.value,
            step_order=step.step_order,
        )
        db.add(step_run)
    await db.commit()

    # Re-load run with step_runs eagerly loaded
    result = await db.execute(
        select(WorkflowRun)
        .options(selectinload(WorkflowRun.step_runs))
        .where(WorkflowRun.id == run.id)
    )
    run = result.scalar_one()

    # Start background execution
    import asyncio
    asyncio.create_task(_execute_workflow_run(run.id, workflow_id))

    # Build agent name map from loaded steps (which have agent eagerly loaded)
    agent_map = {}
    for step in workflow.steps:
        if step.agent:
            agent_map[step.agent_id] = step.agent.name

    return _run_to_response(run, workflow.name, agent_map=agent_map)


@router.get("/{workflow_id}/runs", response_model=list[WorkflowRunResponse])
async def list_workflow_runs(
    workflow_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List runs for a workflow."""
    workflow = await _get_owned_workflow(workflow_id, current_user, db)

    result = await db.execute(
        select(WorkflowRun)
        .options(selectinload(WorkflowRun.step_runs).selectinload(WorkflowStepRun.agent))
        .where(WorkflowRun.workflow_id == workflow_id)
        .order_by(desc(WorkflowRun.created_at))
        .limit(limit)
        .offset(offset)
    )
    runs = result.scalars().all()

    return [_run_to_response(r, workflow.name) for r in runs]


@router.get("/{workflow_id}/runs/{run_id}", response_model=WorkflowRunResponse)
async def get_workflow_run(
    workflow_id: str,
    run_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get details of a specific workflow run."""
    workflow = await _get_owned_workflow(workflow_id, current_user, db)

    result = await db.execute(
        select(WorkflowRun)
        .options(selectinload(WorkflowRun.step_runs).selectinload(WorkflowStepRun.agent))
        .where(
            WorkflowRun.id == run_id,
            WorkflowRun.workflow_id == workflow_id,
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    return _run_to_response(run, workflow.name)


@router.get("/{workflow_id}/runs/{run_id}/stream")
async def stream_workflow_run(
    workflow_id: str,
    run_id: str,
    token: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Stream workflow run progress via SSE."""
    # Validate token (same pattern as delegation stream)
    from auth import SECRET_KEY, ALGORITHM, JWT_ISSUER, JWT_AUDIENCE
    from jose import jwt, JWTError

    user_id = None
    if token:
        try:
            payload = jwt.decode(
                token, SECRET_KEY, algorithms=[ALGORITHM],
                options={"require": ["exp", "iss", "aud", "sub"]},
                issuer=JWT_ISSUER, audience=JWT_AUDIENCE,
            )
            user_id = payload.get("sub")
        except JWTError:
            pass

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    # Verify ownership
    result = await db.execute(
        select(WorkflowRun).where(
            WorkflowRun.id == run_id,
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.user_id == user_id,
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    async def _event_generator():
        queue = delegation_hub.subscribe(f"workflow_{run_id}")
        TERMINAL = {"completed", "failed", "cancelled"}

        try:
            # Replay current status
            yield f"data: {json.dumps({'type': 'status', 'data': {'status': run.status}})}\n\n"

            if run.status in TERMINAL:
                yield f"data: {json.dumps({'type': 'done', 'data': {'status': run.status}})}\n\n"
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    # Re-check status from DB
                    async with async_session_maker() as check_db:
                        fresh = (await check_db.execute(
                            select(WorkflowRun.status).where(WorkflowRun.id == run_id)
                        )).scalar_one_or_none()
                        if fresh in TERMINAL:
                            yield f"data: {json.dumps({'type': 'done', 'data': {'status': fresh}})}\n\n"
                            return
                    continue

                yield f"data: {json.dumps(event)}\n\n"

                if event.get("type") == "done":
                    return
        finally:
            delegation_hub.unsubscribe(f"workflow_{run_id}", queue)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
