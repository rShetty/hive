"""Team endpoints — hierarchical multi-agent orchestration."""
import json
import asyncio
import httpx
from typing import AsyncGenerator
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from models.team import Team, TeamMember, TeamRun, TeamDelegation
from models.agent import Agent, AgentStatus
from models.transaction import Transaction, TransactionType, TransactionStatus
from models.wallet import Wallet
from routers.wallet import get_or_create_wallet
from schemas import (
    TeamCreate, TeamResponse, TeamDetailResponse, TeamUpdate,
    TeamMemberResponse, TeamRunCreate, TeamRunResponse, TeamDelegationResponse,
)
from database import get_db
from auth import get_current_active_user
from models.user import User
from middleware.rate_limit import limiter, RATE_LIMITS
from services import delegation_hub


async def _check_agent_alive(agent: Agent) -> bool:
    """Quick health check: ping the agent's /health endpoint directly."""
    # Issue #17 (SSRF): only contact endpoints that pass the public-URL guard.
    from services.url_guard import validate_public_http_url

    # For managed agents with an internal_port, check the port directly
    if agent.internal_port:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"http://localhost:{agent.internal_port}/health?token=x")
                return resp.status_code == 200
        except Exception:
            return False
    # Fallback: check via marketplace proxy
    if not agent.endpoint_url:
        return False
    try:
        endpoint = agent.endpoint_url
        if endpoint.startswith("/"):
            base_url = MARKETPLACE_URL
            endpoint = f"{base_url}{endpoint}"
        else:
            validate_public_http_url(endpoint)
        health_url = endpoint.replace("/invoke", "/health")
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{health_url}?token=x")
            return resp.status_code == 200
    except Exception:
        return False
from services.agent_client import get_agent_client
from routers.delegation import delegation_status, delegation_logs, add_delegation_log

import os
MARKETPLACE_URL = os.getenv("MARKETPLACE_URL", "http://localhost:8000")
AGENT_DELEGATION_TIMEOUT = int(os.getenv("AGENT_DELEGATION_TIMEOUT", "300"))
HIVE_URL = os.getenv("HIVE_URL", "http://localhost:8000")
HIVE_API_KEY = os.getenv("HIVE_API_KEY", "")


router = APIRouter(prefix="/api/teams", tags=["teams"])

TEAM_RATE_LIMITS = {
    "team_list": "600/hour",
    "team_create": "600/hour",
    "team_detail": "600/hour",
    "team_update": "600/hour",
    "team_delete": "600/hour",
    "team_run": "30/hour",
    "team_stream": "120/hour",
    "team_run_detail": "240/hour",
}


def _member_to_response(m: TeamMember, agents: dict) -> TeamMemberResponse:
    agent = agents.get(m.agent_id)
    return TeamMemberResponse(
        id=m.id,
        agent_id=m.agent_id,
        agent_name=agent.name if agent else None,
        agent_status=agent.status if agent else None,
        role=m.role,
        reports_to_member_id=m.reports_to_member_id,
        max_tokens=m.max_tokens,
        created_at=m.created_at,
    )


def _team_to_response(t: Team, agents: dict, member_count: int = 0, last_run_status: str = None) -> TeamResponse:
    root_agent = agents.get(t.root_agent_id)
    return TeamResponse(
        id=t.id,
        name=t.name,
        description=t.description,
        owner_id=t.owner_id,
        root_agent_id=t.root_agent_id,
        root_agent_name=root_agent.name if root_agent else None,
        max_depth=t.max_depth,
        member_count=member_count,
        last_run_status=last_run_status,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def _run_to_response(r: TeamRun, team_name: str = None, delegations: list = None) -> TeamRunResponse:
    return TeamRunResponse(
        id=r.id,
        team_id=r.team_id,
        team_name=team_name,
        user_id=r.user_id,
        task=r.task,
        status=r.status,
        delegation_tree=r.delegation_tree,
        total_tokens_used=r.total_tokens_used,
        output_data=r.output_data,
        error_message=r.error_message,
        started_at=r.started_at,
        completed_at=r.completed_at,
        created_at=r.created_at,
        delegations=delegations or [],
    )


def _delegation_to_response(d: TeamDelegation, agents: dict) -> TeamDelegationResponse:
    agent = agents.get(d.agent_id)
    return TeamDelegationResponse(
        id=d.id,
        parent_delegation_id=d.parent_delegation_id,
        delegation_id=d.delegation_id,
        agent_id=d.agent_id,
        agent_name=agent.name if agent else None,
        task_description=d.task_description,
        status=d.status,
        tokens_used=d.tokens_used,
        result_data=d.result_data,
        error_message=d.error_message,
        depth=d.depth,
        created_at=d.created_at,
        completed_at=d.completed_at,
    )


async def _load_agents_map(db: AsyncSession, agent_ids: list) -> dict:
    if not agent_ids:
        return {}
    result = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
    return {a.id: a for a in result.scalars().all()}


# ────────────── CRUD ──────────────

@router.get("/", response_model=list[TeamResponse])
@limiter.limit(TEAM_RATE_LIMITS["team_list"])
async def list_teams(
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Team).where(Team.owner_id == current_user.id).order_by(Team.created_at.desc())
    )
    teams = result.scalars().all()
    if not teams:
        return []

    team_ids = [t.id for t in teams]
    agent_ids = set()
    for t in teams:
        agent_ids.add(t.root_agent_id)
    for tid in team_ids:
        result = await db.execute(select(TeamMember.agent_id).where(TeamMember.team_id == tid))
        agent_ids.update(r[0] for r in result.all())
    agents = await _load_agents_map(db, list(agent_ids))

    out = []
    for t in teams:
        result = await db.execute(select(func.count(TeamMember.id)).where(TeamMember.team_id == t.id))
        member_count = result.scalar() or 0
        last_status = None
        result = await db.execute(
            select(TeamRun.status)
            .where(TeamRun.team_id == t.id)
            .order_by(TeamRun.created_at.desc())
            .limit(1)
        )
        row = result.first()
        if row:
            last_status = row[0]
        out.append(_team_to_response(t, agents, member_count, last_status))
    return out


@router.post("/", response_model=TeamDetailResponse, status_code=201)
@limiter.limit(TEAM_RATE_LIMITS["team_create"])
async def create_team(
    request: Request,
    data: TeamCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Agent).where(Agent.id == data.root_agent_id, Agent.owner_id == current_user.id)
    )
    root_agent = result.scalar_one_or_none()
    if not root_agent:
        raise HTTPException(status_code=404, detail="Root agent not found or not owned by you")
    if root_agent.status != AgentStatus.ACTIVE.value:
        raise HTTPException(status_code=400, detail=f"Root agent '{root_agent.name}' is {root_agent.status}. Deploy or restart the agent before creating a team.")

    team = Team(
        name=data.name,
        description=data.description,
        owner_id=current_user.id,
        root_agent_id=data.root_agent_id,
        max_depth=data.max_depth,
    )
    db.add(team)
    await db.flush()

    agent_ids_needed = set()
    for m in data.members:
        agent_ids_needed.add(m.agent_id)

    result = await db.execute(
        select(Agent).where(Agent.id.in_(agent_ids_needed), Agent.owner_id == current_user.id)
    )
    owned_agents = {a.id: a for a in result.scalars().all()}
    missing = [aid for aid in agent_ids_needed if aid not in owned_agents]
    if missing:
        raise HTTPException(status_code=400, detail=f"Agent(s) not found or not owned by you: {missing}")

    all_agent_ids = {data.root_agent_id} | agent_ids_needed
    all_agent_ids.discard(None)

    member_ids = {}
    for m in data.members:
        if m.agent_id not in owned_agents:
            raise HTTPException(status_code=400, detail=f"Agent {m.agent_id} not owned by you")
        member = TeamMember(
            team_id=team.id,
            agent_id=m.agent_id,
            role=m.role,
            max_tokens=m.max_tokens,
        )
        db.add(member)
        await db.flush()
        member_ids[m.agent_id] = member.id

    for m in data.members:
        if m.reports_to_agent_id:
            member_obj = await db.get(TeamMember, member_ids[m.agent_id])
            report_to_mid = member_ids.get(m.reports_to_agent_id)
            if not report_to_mid:
                raise HTTPException(status_code=400, detail=f"reports_to_agent_id {m.reports_to_agent_id} not in members")
            member_obj.reports_to_member_id = report_to_mid

    await db.commit()
    await db.refresh(team)

    result = await db.execute(
        select(TeamMember).where(TeamMember.team_id == team.id).options(selectinload(TeamMember.agent))
    )
    members = result.scalars().all()
    all_agent_ids = {team.root_agent_id} | {m.agent_id for m in members}
    agents = await _load_agents_map(db, list(all_agent_ids))

    resp = _team_to_response(team, agents, len(members))
    return TeamDetailResponse(
        **resp.model_dump(),
        members=[_member_to_response(m, agents) for m in members],
    )


@router.get("/{team_id}", response_model=TeamDetailResponse)
@limiter.limit(TEAM_RATE_LIMITS["team_detail"])
async def get_team(
    request: Request,
    team_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Team).where(Team.id == team_id, Team.owner_id == current_user.id))
    team = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    result = await db.execute(
        select(TeamMember).where(TeamMember.team_id == team.id).options(selectinload(TeamMember.agent))
    )
    members = result.scalars().all()
    all_agent_ids = {team.root_agent_id} | {m.agent_id for m in members}
    agents = await _load_agents_map(db, list(all_agent_ids))

    resp = _team_to_response(team, agents, len(members))
    return TeamDetailResponse(
        **resp.model_dump(),
        members=[_member_to_response(m, agents) for m in members],
    )


@router.patch("/{team_id}", response_model=TeamDetailResponse)
@limiter.limit(TEAM_RATE_LIMITS["team_update"])
async def update_team(
    request: Request,
    team_id: str,
    data: TeamUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Team).where(Team.id == team_id, Team.owner_id == current_user.id))
    team = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if data.name is not None:
        team.name = data.name
    if data.description is not None:
        team.description = data.description
    if data.root_agent_id is not None:
        result = await db.execute(
            select(Agent).where(Agent.id == data.root_agent_id, Agent.owner_id == current_user.id)
        )
        root = result.scalar_one_or_none()
        if not root:
            raise HTTPException(status_code=404, detail="Root agent not found")
        team.root_agent_id = data.root_agent_id
    if data.max_depth is not None:
        team.max_depth = data.max_depth

    await db.commit()
    await db.refresh(team)

    result = await db.execute(
        select(TeamMember).where(TeamMember.team_id == team.id).options(selectinload(TeamMember.agent))
    )
    members = result.scalars().all()
    all_agent_ids = {team.root_agent_id} | {m.agent_id for m in members}
    agents = await _load_agents_map(db, list(all_agent_ids))

    resp = _team_to_response(team, agents, len(members))
    return TeamDetailResponse(
        **resp.model_dump(),
        members=[_member_to_response(m, agents) for m in members],
    )


@router.delete("/{team_id}", status_code=204)
@limiter.limit(TEAM_RATE_LIMITS["team_delete"])
async def delete_team(
    request: Request,
    team_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Team).where(Team.id == team_id, Team.owner_id == current_user.id))
    team = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    await db.delete(team)
    await db.commit()


# ────────────── RUN ──────────────

def _build_team_context(team: Team, members: list, db_agents: dict) -> dict:
    """Build the context payload sent to the root agent."""
    root_agent = db_agents.get(team.root_agent_id)
    team_members = []
    for m in members:
        agent = db_agents.get(m.agent_id)
        if not agent:
            continue
        team_members.append({
            "member_id": m.id,
            "agent_id": m.agent_id,
            "name": agent.name,
            "role": m.role,
            "reports_to": m.reports_to_member_id,
            "endpoint_url": agent.endpoint_url,
            "description": agent.description or "",
        })
    return {
        "team_context": {
            "team_id": team.id,
            "team_name": team.name,
            "root_agent_id": team.root_agent_id,
            "root_agent_endpoint": root_agent.endpoint_url if root_agent else None,
            "description": team.description or "",
            "max_depth": team.max_depth,
            "role": "root",
            "members": team_members,
            "delegation_instructions": (
                "You are the root agent of a team. To delegate work to team members, "
                "call the Hive delegation API: POST /api/delegate/request with "
                '{"target_agent_id":"<member_agent_id>","task_description":"...","max_tokens":N}. '
                "You have access to member agent IDs via the team_context provided. "
                "Always report final results back."
            ),
        }
    }


async def _fail_team_run(team_run_id: str, delegation_id: str, error_msg: str):
    """Mark a team run and its root delegation as failed with a clear message."""
    from datetime import datetime as _dt
    from database import async_session_maker
    async with async_session_maker() as db:
        td_result = await db.execute(
            select(TeamDelegation).where(TeamDelegation.delegation_id == delegation_id)
        )
        td = td_result.scalar_one_or_none()
        if td:
            td.status = "failed"
            td.error_message = error_msg[:500]
        run_result = await db.execute(select(TeamRun).where(TeamRun.id == team_run_id))
        run = run_result.scalar_one_or_none()
        if run:
            run.status = "failed"
            run.error_message = error_msg
            run.completed_at = _dt.utcnow()
        await db.commit()
    await add_delegation_log(delegation_id, "error", error_msg)
    delegation_hub.publish(delegation_id, {
        "type": "status",
        "data": {"status": "failed", "error": error_msg},
    })


async def _run_team_delegation(
    team_run_id: str,
    root_delegation_id: str,
    target_agent_name: str,
    task_description: str,
    max_tokens: float,
    callback_url: str,
    context: dict,
):
    """Background task: orchestrate team delegation server-side.

    1. Call root agent /invoke to plan delegations (LLM produces JSON list).
    2. For each sub-agent delegation, create a Transaction + call /delegate.
    3. Wait for sub-agent callbacks to complete.
    4. Compile results and call root agent /invoke for final synthesis.
    5. Complete the root delegation with the synthesized answer.
    """
    from datetime import datetime as _dt
    from database import async_session_maker
    from models.team import TeamRun, TeamDelegation
    from models.transaction import Transaction, TransactionType, TransactionStatus
    from models.wallet import Wallet
    from routers.wallet import get_or_create_wallet
    from services import delegation_hub

    team_ctx = context.get("team_context", {})
    members = team_ctx.get("members", [])
    team_id = team_ctx.get("team_id")
    root_agent_id = team_ctx.get("root_agent_id")
    root_agent_endpoint = team_ctx.get("root_agent_endpoint")

    import logging as _logging
    _log = _logging.getLogger("hive.teams")
    _log.info("Team delegation started: team_run_id=%s root_delegation_id=%s root_agent_id=%s endpoint=%s",
              team_run_id, root_delegation_id, root_agent_id, root_agent_endpoint)

    # Load root agent's wallet for escrow
    async with async_session_maker() as db:
        from models.agent import Agent as AgentModel
        root_agent_result = await db.execute(select(AgentModel).where(AgentModel.id == root_agent_id))
        root_agent = root_agent_result.scalar_one_or_none()
        root_wallet_result = await db.execute(select(Wallet).where(Wallet.user_id == root_agent.owner_id))
        root_wallet = root_wallet_result.scalar_one_or_none()

    if not root_agent:
        await _fail_team_run(team_run_id, root_delegation_id, "Root agent not found in database")
        return

    # Verify root agent is alive before proceeding
    is_alive = await _check_agent_alive(root_agent)
    _log.info("Agent alive check: %s (port=%s)", is_alive, root_agent.internal_port)
    if not is_alive:
        await _fail_team_run(
            team_run_id, root_delegation_id,
            f"Agent '{root_agent.name}' is not responding. The agent container may have crashed. "
            f"Please redeploy or restart the agent from the agent detail page."
        )
        return

    try:
        client = get_agent_client(timeout=AGENT_DELEGATION_TIMEOUT)

        # ── Step 1: Ask root agent to plan delegations ──
        plan_prompt = (
            f"Team task: {task_description}\n\n"
            f"Available team members:\n"
        )
        for m in members:
            plan_prompt += (
                f"- {m['name']} (agent_id={m['agent_id']}, role={m['role']}): {m.get('description', '')}\n"
            )
        plan_prompt += (
            f"\nReturn a JSON list of delegations. Each item: "
            f'{{"agent_id":"<member_agent_id>","task":"<specific sub-task>"}}.\n'
            f"If the task doesn't need delegation, return an empty list: []\n"
            f"Return ONLY the JSON list, nothing else."
        )

        await add_delegation_log(root_delegation_id, "info", "Asking root agent to plan delegations")

        # Call root agent's /invoke endpoint directly for planning
        # Issue #17 (SSRF): relative endpoints resolve to this Hive instance
        # (trusted); absolute endpoints must pass the public-URL guard.
        from services.url_guard import validate_public_http_url
        if root_agent_endpoint.startswith("/"):
            invoke_url = f"{MARKETPLACE_URL}{root_agent_endpoint}"
        else:
            try:
                invoke_url = validate_public_http_url(root_agent_endpoint)
            except ValueError as e:
                await _fail_team_run(team_run_id, root_delegation_id, f"Root agent endpoint rejected: {e}")
                return
        plan_payload = {
            "task": plan_prompt,
            "max_tokens": max_tokens,
            "context": context,
        }
        plan_output = "[]"
        try:
            async with httpx.AsyncClient(timeout=min(AGENT_DELEGATION_TIMEOUT, 120)) as http_client:
                plan_resp = await http_client.post(invoke_url, json=plan_payload)
                plan_resp.raise_for_status()
                plan_body = plan_resp.json()
                plan_output = plan_body.get("result", {}).get("output", "[]")
        except Exception as e:
            await add_delegation_log(root_delegation_id, "warning", f"Plan call failed: {e}")

        # Parse the delegation plan from LLM output
        delegations_to_make = []
        try:
            import re
            # Try to extract JSON array from the output
            json_match = re.search(r'\[.*\]', plan_output, re.DOTALL)
            if json_match:
                delegations_to_make = json.loads(json_match.group())
        except (json.JSONDecodeError, AttributeError):
            pass

        # If no JSON array found, try to parse pseudo-tool-call format:
        # <|tool_call>call:POST /api/delegate/request { ... }<tool_call|>
        if not delegations_to_make:
            try:
                import re
                tc_matches = re.findall(
                    r'(?:tool_call>|tool_call\|>)(.*?)<(?:tool_call|tool_call\|>)',
                    plan_output, re.DOTALL
                )
                for tc in tc_matches:
                    # Extract JSON body from "call:POST /api/delegate/request { ... }"
                    body_match = re.search(r'\{.*\}', tc, re.DOTALL)
                    if body_match:
                        body = json.loads(body_match.group())
                        agent_id = body.get("target_agent_id", "")
                        task = body.get("task_description", body.get("task", ""))
                        if agent_id:
                            delegations_to_make.append({"agent_id": agent_id, "task": task})
            except Exception:
                pass

        # Also try to extract individual agent_id + task from freeform text
        if not delegations_to_make and members:
            try:
                import re
                agent_id_match = re.search(r'"target_agent_id"\s*:\s*"([^"]+)"', plan_output)
                task_match = re.search(r'"task_description"\s*:\s*"([^"]+)"', plan_output)
                if agent_id_match and task_match:
                    delegations_to_make.append({
                        "agent_id": agent_id_match.group(1),
                        "task": task_match.group(1),
                    })
            except Exception:
                pass

        if not delegations_to_make:
            # No delegations needed — just run the task on root agent (sync)
            await add_delegation_log(root_delegation_id, "info", "No sub-delegations needed — running task on root agent")

            invoke_result = await client.send_delegation_task(
                target_endpoint=root_agent_endpoint,
                delegation_id=root_delegation_id,
                task_description=task_description,
                max_tokens=max_tokens,
                callback_url=callback_url,
                context=context,
                timeout=AGENT_DELEGATION_TIMEOUT,
                sync=True,
            )

            # Extract output from sync result and update TeamRun
            output = invoke_result.get("result", {}).get("output", "")
            result_payload = {"output": output, "agent_id": root_agent_id}

            async with async_session_maker() as db:
                td_result = await db.execute(
                    select(TeamDelegation).where(TeamDelegation.delegation_id == root_delegation_id)
                )
                root_td = td_result.scalar_one_or_none()
                tree = {}
                if root_td:
                    root_td.status = "completed"
                    root_td.completed_at = _dt.utcnow()
                    root_td.result_data = result_payload
                    tree[root_td.id] = {
                        "agent_id": root_td.agent_id,
                        "parent_id": None,
                        "status": "completed",
                        "result": result_payload,
                    }

                run_result = await db.execute(select(TeamRun).where(TeamRun.id == team_run_id))
                run = run_result.scalar_one_or_none()
                if run:
                    run.status = "completed"
                    run.completed_at = _dt.utcnow()
                    run.output_data = result_payload
                    run.delegation_tree = tree
                await db.commit()

            delegation_hub.publish(root_delegation_id, {
                "type": "status",
                "data": {"status": "completed", "result": result_payload},
            })
            await add_delegation_log(root_delegation_id, "success", "Team delegation completed")
            return

        await add_delegation_log(
            root_delegation_id, "info",
            f"Root agent planned {len(delegations_to_make)} delegations"
        )

        # ── Step 2: Delegate to each sub-agent ──
        sub_results = {}
        sub_tasks = []

        for i, d in enumerate(delegations_to_make):
            agent_id = d.get("agent_id", "")
            sub_task = d.get("task", task_description)

            if not agent_id:
                continue

            # Find the agent's endpoint
            member_info = next((m for m in members if m["agent_id"] == agent_id), None)
            if not member_info:
                await add_delegation_log(root_delegation_id, "warning", f"Agent {agent_id} not in team — skipping")
                continue

            sub_delegation_id = f"{root_delegation_id}-sub-{i}"

            # Create sub-delegation record + Transaction
            async with async_session_maker() as db:
                from models.agent import Agent as AgentModel
                from models.wallet import Wallet as WalletModel

                # Find sub-agent's owner wallet
                sub_agent_result = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
                sub_agent = sub_agent_result.scalar_one_or_none()
                if not sub_agent:
                    continue

                sub_wallet_result = await db.execute(select(WalletModel).where(WalletModel.user_id == sub_agent.owner_id))
                sub_wallet = sub_wallet_result.scalar_one_or_none()
                if not sub_wallet:
                    # Create wallet if needed
                    from routers.wallet import get_or_create_wallet
                    sub_wallet = await get_or_create_wallet(sub_agent.owner_id, db)

                # Create a real Transaction for this sub-delegation
                from models.team import TeamRun as TeamRunModel
                tr_result = await db.execute(select(TeamRunModel).where(TeamRunModel.id == team_run_id))
                team_run_record = tr_result.scalar_one_or_none()

                sub_amount = Decimal(str(max_tokens / max(len(delegations_to_make), 1)))
                sub_tx = Transaction(
                    from_wallet_id=root_wallet.id,
                    to_wallet_id=sub_wallet.id,
                    amount=sub_amount,
                    transaction_type=TransactionType.DELEGATION.value,
                    delegating_agent_id=team_ctx.get("root_agent_id"),
                    executing_agent_id=agent_id,
                    originating_user_id=team_run_record.user_id if team_run_record else None,
                    delegation_depth=1,
                    task_description=sub_task,
                    status=TransactionStatus.PENDING.value,
                )
                db.add(sub_tx)
                await db.flush()

                # Link TeamDelegation to the real transaction
                td = TeamDelegation(
                    team_run_id=team_run_id,
                    delegation_id=sub_tx.id,
                    agent_id=agent_id,
                    task_description=sub_task,
                    status="running",
                    depth=1,
                    parent_delegation_id=root_delegation_id,
                )
                db.add(td)
                await db.commit()
                # Use the real transaction ID for the sub-agent call
                sub_delegation_id = sub_tx.id

            await add_delegation_log(root_delegation_id, "info", f"Delegating to {member_info['name']}: {sub_task[:100]}")

            # Call sub-agent
            sub_tasks.append((
                sub_delegation_id,
                agent_id,
                member_info["name"],
                member_info["endpoint_url"],
                sub_task,
            ))

        # Execute all sub-delegations concurrently (sync mode — waits for result)
        async def _run_sub(delegation_id, agent_id, name, endpoint, task):
            try:
                result = await client.send_delegation_task(
                    target_endpoint=endpoint,
                    delegation_id=delegation_id,
                    task_description=task,
                    max_tokens=max_tokens / max(len(sub_tasks), 1),
                    sync=True,
                    timeout=min(AGENT_DELEGATION_TIMEOUT, 120),
                )
                output = result.get("result", {}).get("output", "")
                sub_results[agent_id] = {"name": name, "output": output, "status": "completed"}
                async with async_session_maker() as db:
                    td_result = await db.execute(select(TeamDelegation).where(TeamDelegation.delegation_id == delegation_id))
                    td = td_result.scalar_one_or_none()
                    if td:
                        td.status = "completed"
                        td.completed_at = _dt.utcnow()
                        td.result_data = {"output": output}
                    await db.commit()
                await add_delegation_log(root_delegation_id, "success", f"{name} completed: {output[:100]}")
            except Exception as e:
                sub_results[agent_id] = {"name": name, "output": "", "status": "failed", "error": str(e)}
                async with async_session_maker() as db:
                    td_result = await db.execute(select(TeamDelegation).where(TeamDelegation.delegation_id == delegation_id))
                    td = td_result.scalar_one_or_none()
                    if td:
                        td.status = "failed"
                        td.error_message = str(e)[:500]
                    await db.commit()
                await add_delegation_log(root_delegation_id, "error", f"{name} failed: {e}")

        await asyncio.gather(*[_run_sub(*args) for args in sub_tasks])

        # ── Step 3: Compile results and ask root agent to synthesize ──
        results_text = "\n\n".join(
            f"## {r['name']} ({aid})\nStatus: {r['status']}\nOutput: {r['output']}"
            for aid, r in sub_results.items()
        )
        synthesis_prompt = (
            f"Original task: {task_description}\n\n"
            f"Delegation results:\n{results_text}\n\n"
            f"Synthesize a final answer from these results."
        )

        await add_delegation_log(root_delegation_id, "info", "Synthesizing final answer from sub-results")

        # Call root agent's /invoke directly for synthesis
        # Issue #17 (SSRF): same endpoint policy as the planning call above.
        from services.url_guard import validate_public_http_url as _vpu
        if root_agent_endpoint.startswith("/"):
            synthesis_url = f"{MARKETPLACE_URL}{root_agent_endpoint}"
        else:
            try:
                synthesis_url = _vpu(root_agent_endpoint)
            except ValueError:
                synthesis_url = None
        synthesis_payload = {
            "task": synthesis_prompt,
            "max_tokens": max_tokens,
            "context": context,
        }
        final_output = results_text
        if not synthesis_url:
            await add_delegation_log(root_delegation_id, "warning", "Synthesis skipped: endpoint rejected")
        try:
            if synthesis_url:
                async with httpx.AsyncClient(timeout=min(AGENT_DELEGATION_TIMEOUT, 120)) as http_client:
                    synth_resp = await http_client.post(synthesis_url, json=synthesis_payload)
                    synth_resp.raise_for_status()
                    synth_body = synth_resp.json()
                    final_output = synth_body.get("result", {}).get("output", results_text)
        except Exception as e:
            await add_delegation_log(root_delegation_id, "warning", f"Synthesis call failed: {e}")

        # ── Step 4: Complete the root delegation directly in DB ──
        result_payload = {
            "output": final_output,
            "agent_id": root_agent_id,
            "sub_results": sub_results,
        }

        # Build the delegation tree
        async with async_session_maker() as db:
            td_result = await db.execute(
                select(TeamDelegation).where(TeamDelegation.team_run_id == team_run_id)
            )
            all_tds = td_result.scalars().all()
            tree = {}
            for td in all_tds:
                tree[td.id] = {
                    "agent_id": td.agent_id,
                    "parent_id": td.parent_delegation_id,
                    "status": td.status if td.status != "running" else "completed",
                    "result": td.result_data,
                }
            # Ensure root is completed
            root_td_result = await db.execute(
                select(TeamDelegation).where(TeamDelegation.delegation_id == root_delegation_id)
            )
            root_td = root_td_result.scalar_one_or_none()
            if root_td:
                root_td.status = "completed"
                root_td.completed_at = _dt.utcnow()
                root_td.result_data = result_payload
                tree[root_td.id] = {
                    "agent_id": root_td.agent_id,
                    "parent_id": None,
                    "status": "completed",
                    "result": result_payload,
                }

            run_result = await db.execute(select(TeamRun).where(TeamRun.id == team_run_id))
            run = run_result.scalar_one_or_none()
            if run:
                run.status = "completed"
                run.completed_at = _dt.utcnow()
                run.output_data = result_payload
                run.delegation_tree = tree

            # Set root transaction to completed
            tx_result = await db.execute(
                select(Transaction).where(Transaction.id == root_delegation_id)
            )
            root_tx = tx_result.scalar_one_or_none()
            if root_tx:
                root_tx.status = TransactionStatus.COMPLETED.value
                root_tx.completed_at = _dt.utcnow()
                root_tx.task_result = result_payload

            await db.commit()

        # Publish completion event to hub
        delegation_hub.publish(root_delegation_id, {
            "type": "status",
            "data": {"status": "completed", "result": result_payload},
        })

        await add_delegation_log(root_delegation_id, "success", "Team delegation completed")

    except Exception as exc:
        import traceback
        _err = traceback.format_exc()
        error_msg = str(exc)
        if "502" in error_msg or "Bad Gateway" in error_msg:
            error_msg = f"Agent '{target_agent_name}' is not responding (502 Bad Gateway). The agent container may have crashed. Please redeploy the agent."
        elif "Connection refused" in error_msg or "Failed to connect" in error_msg:
            error_msg = f"Cannot connect to agent '{target_agent_name}'. The agent is offline. Please redeploy or restart it."
        async with async_session_maker() as db:
            td_result = await db.execute(select(TeamDelegation).where(TeamDelegation.delegation_id == root_delegation_id))
            td = td_result.scalar_one_or_none()
            if td:
                td.status = "failed"
                td.error_message = str(exc)[:500]
            run_result = await db.execute(select(TeamRun).where(TeamRun.id == team_run_id))
            run = run_result.scalar_one_or_none()
            if run:
                run.status = "failed"
                run.error_message = f"Team delegation failed: {error_msg}"
                run.completed_at = _dt.utcnow()
            await db.commit()
            await add_delegation_log(root_delegation_id, "error", f"Team delegation failed: {error_msg}")


@router.post("/{team_id}/run", response_model=TeamRunResponse)
@limiter.limit(TEAM_RATE_LIMITS["team_run"])
async def start_team_run(
    request: Request,
    team_id: str,
    data: TeamRunCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Team).where(Team.id == team_id, Team.owner_id == current_user.id))
    team = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    result = await db.execute(select(Agent).where(Agent.id == team.root_agent_id))
    root_agent = result.scalar_one_or_none()
    if not root_agent:
        raise HTTPException(status_code=500, detail="Root agent not found")
    if root_agent.status not in [AgentStatus.ACTIVE.value, AgentStatus.IDLE.value]:
        raise HTTPException(status_code=503, detail=f"Root agent '{root_agent.name}' is {root_agent.status}. Please redeploy the agent or restart it from the agent detail page.")

    result = await db.execute(
        select(TeamMember).where(TeamMember.team_id == team.id).options(selectinload(TeamMember.agent))
    )
    members = list(result.scalars().all())
    all_agent_ids = {team.root_agent_id} | {m.agent_id for m in members}
    agents = await _load_agents_map(db, list(all_agent_ids))

    dead_agents = []
    for m in members:
        if m.agent_id not in agents:
            continue
        agent = agents[m.agent_id]
        if agent.status not in [AgentStatus.ACTIVE.value, AgentStatus.IDLE.value]:
            dead_agents.append(f"{agent.name} ({agent.status})")

    if dead_agents:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot start team run — agents are offline: {', '.join(dead_agents)}. Please redeploy or restart them first."
        )

    user_wallet = await get_or_create_wallet(current_user.id, db)
    root_wallet = await get_or_create_wallet(root_agent.owner_id, db)

    delegation_tokens = float(team.max_depth * 200)
    user_wallet.balance -= Decimal(str(delegation_tokens))
    await db.flush()
    if user_wallet.balance < 0:
        await db.rollback()
        raise HTTPException(status_code=402, detail="Insufficient tokens")

    transaction = Transaction(
        from_wallet_id=user_wallet.id,
        to_wallet_id=root_wallet.id,
        amount=Decimal(str(delegation_tokens)),
        transaction_type=TransactionType.DELEGATION.value,
        delegating_agent_id=None,
        executing_agent_id=root_agent.id,
        originating_user_id=current_user.id,
        delegation_depth=0,
        task_description=data.task,
        status=TransactionStatus.PENDING.value,
    )
    db.add(transaction)
    await db.flush()

    team_run = TeamRun(
        team_id=team.id,
        user_id=current_user.id,
        task=data.task,
        status="running",
        started_at=__import__('datetime').datetime.utcnow(),
    )
    db.add(team_run)
    await db.flush()

    root_delegation = TeamDelegation(
        team_run_id=team_run.id,
        delegation_id=transaction.id,
        agent_id=team.root_agent_id,
        task_description=data.task,
        status="running",
        depth=0,
    )
    db.add(root_delegation)
    await db.commit()
    await db.refresh(team_run)
    await db.refresh(transaction)

    delegation_status[transaction.id] = "pending"
    delegation_logs[transaction.id] = []
    await add_delegation_log(transaction.id, "info", f"Team run started — root agent: {root_agent.name}")

    delegation_hub.publish(
        transaction.id,
        {"type": "team_run_started", "team_run_id": team_run.id, "task": data.task},
    )

    team_context = _build_team_context(team, members, agents)
    callback_url = f"{MARKETPLACE_URL}/api/delegate/{transaction.id}/callback"

    background_tasks.add_task(
        _run_team_delegation,
        team_run_id=team_run.id,
        root_delegation_id=transaction.id,
        target_agent_name=root_agent.name,
        task_description=data.task,
        max_tokens=delegation_tokens,
        callback_url=callback_url,
        context=team_context,
    )

    resp = _run_to_response(team_run, team.name)
    return resp


# ────────────── STREAM ──────────────

# delegation_status, delegation_logs, and add_delegation_log are imported
# from routers.delegation at the top of this file. Do NOT redefine them here.


async def stream_team_run_events(team_run_id: str, team: Team, db: AsyncSession) -> AsyncGenerator[str, None]:
    """SSE generator: replay state → tail live events → emit terminal."""
    from database import async_session_maker

    # Load initial run state
    result = await db.execute(select(TeamRun).where(TeamRun.id == team_run_id))
    run = result.scalar_one_or_none()
    if not run:
        yield f"data: {json.dumps({'type': 'error', 'message': 'Team run not found'})}\n\n"
        return

    root_td = None
    tx_id = None
    td_result = await db.execute(
        select(TeamDelegation)
        .where(TeamDelegation.team_run_id == team_run_id, TeamDelegation.depth == 0)
        .options(selectinload(TeamDelegation.agent))
    )
    root_delegations = td_result.scalars().all()
    if root_delegations:
        root_td = root_delegations[0]
        tx_id = root_td.delegation_id

    def _emit(event_dict):
        return f"data: {json.dumps(event_dict, default=str)}\n\n"

    # 1. Emit initial state
    yield _emit({"type": "team_run_started", "team_run_id": team_run_id, "status": run.status, "task": run.task, "team_name": team.name})

    # Emit existing delegations
    all_td_result = await db.execute(
        select(TeamDelegation)
        .where(TeamDelegation.team_run_id == team_run_id)
        .options(selectinload(TeamDelegation.agent))
    )
    all_delegations = all_td_result.scalars().all()
    all_agent_ids = {d.agent_id for d in all_delegations}
    agents_map = await _load_agents_map(db, list(all_agent_ids))
    yield _emit({"type": "delegations", "delegations": [_delegation_to_response(d, agents_map).model_dump() for d in all_delegations]})

    # Emit existing delegation tree if present
    if run.delegation_tree:
        yield _emit({"type": "delegation_tree", "tree": run.delegation_tree})
    if run.output_data:
        yield _emit({"type": "output", "output": run.output_data})

    # Replay logs
    if tx_id:
        for line in delegation_logs.get(tx_id, []):
            agent_name = root_td.agent.name if root_td and root_td.agent else "Agent"
            level = line.get("level", "info") if isinstance(line, dict) else "info"
            message = line.get("message", str(line)) if isinstance(line, dict) else str(line)
            yield _emit({"type": "log", "agent": agent_name, "level": level, "message": message})

    # If already terminal, emit finished and return
    if run.status in ["completed", "failed"]:
        yield _emit({"type": "team_run_finished", "status": run.status, "team_run_id": team_run_id})
        return

    # 2. Subscribe to hub for live events
    queue = delegation_hub.subscribe(tx_id) if tx_id else None
    HEARTBEAT = 15.0
    seen_log_count = len(delegation_logs.get(tx_id, [])) if tx_id else 0
    last_delegation_count = len(all_delegations)

    try:
        poll_count = 0
        max_polls = 480  # 4 minutes at 0.5s
        while poll_count < max_polls:
            poll_count += 1

            # Drain hub queue (non-blocking)
            if queue:
                try:
                    while True:
                        event = queue.get_nowait()
                        # Normalize hub events to the format the frontend expects
                        if event.get("type") == "log" and "data" in event:
                            d = event["data"]
                            yield _emit({"type": "log", "agent": root_td.agent.name if root_td and root_td.agent else "Agent", "level": d.get("level", "info"), "message": d.get("message", "")})
                        elif event.get("type") == "status":
                            status = event.get("data", {}).get("status")
                            yield _emit({"type": "status_update", "status": status, "team_run_id": team_run_id})
                            if status in ["completed", "failed"]:
                                yield _emit({"type": "team_run_finished", "status": status, "team_run_id": team_run_id})
                                return
                        else:
                            yield _emit(event)
                except asyncio.QueueEmpty:
                    pass

            # Poll DB every ~2 seconds for delegation list / status changes
            if poll_count % 4 == 0:
                async with async_session_maker() as poll_db:
                    run_result = await poll_db.execute(select(TeamRun).where(TeamRun.id == team_run_id))
                    fresh_run = run_result.scalar_one_or_none()
                    if not fresh_run:
                        continue

                    td_fresh = await poll_db.execute(
                        select(TeamDelegation)
                        .where(TeamDelegation.team_run_id == team_run_id)
                        .options(selectinload(TeamDelegation.agent))
                    )
                    fresh_delegations = td_fresh.scalars().all()

                    # Emit new delegations
                    if len(fresh_delegations) != last_delegation_count:
                        last_delegation_count = len(fresh_delegations)
                        fresh_agent_ids = {d.agent_id for d in fresh_delegations}
                        fresh_agents = await _load_agents_map(poll_db, list(fresh_agent_ids))
                        yield _emit({"type": "delegations", "delegations": [_delegation_to_response(d, fresh_agents).model_dump() for d in fresh_delegations]})

                    # Emit new logs
                    if tx_id:
                        current_logs = delegation_logs.get(tx_id, [])
                        if len(current_logs) > seen_log_count:
                            for line in current_logs[seen_log_count:]:
                                agent_name = root_td.agent.name if root_td and root_td.agent else "Agent"
                                level = line.get("level", "info") if isinstance(line, dict) else "info"
                                message = line.get("message", str(line)) if isinstance(line, dict) else str(line)
                                yield _emit({"type": "log", "agent": agent_name, "level": level, "message": message})
                            seen_log_count = len(current_logs)

                    # Emit tree / output if appeared
                    if fresh_run.delegation_tree and not run.delegation_tree:
                        yield _emit({"type": "delegation_tree", "tree": fresh_run.delegation_tree})
                    if fresh_run.output_data and not run.output_data:
                        yield _emit({"type": "output", "output": fresh_run.output_data})

                    # Check terminal
                    if fresh_run.status in ["completed", "failed"]:
                        yield _emit({"type": "status_update", "status": fresh_run.status, "team_run_id": team_run_id})
                        yield _emit({"type": "team_run_finished", "status": fresh_run.status, "team_run_id": team_run_id})
                        return

                    # Emit status update
                    if fresh_run.status != run.status:
                        run = fresh_run
                        yield _emit({"type": "status_update", "status": run.status, "team_run_id": team_run_id})

            await asyncio.sleep(0.5)

        # Timeout
        yield _emit({"type": "error", "message": "Stream timeout"})
    finally:
        if tx_id and queue:
            delegation_hub.unsubscribe(tx_id, queue)


@router.get("/{team_id}/runs/{run_id}/stream")
@limiter.limit(TEAM_RATE_LIMITS["team_stream"])
async def stream_team_run(
    request: Request,
    team_id: str,
    run_id: str,
    token: str = "",
    db: AsyncSession = Depends(get_db),
):
    # EventSource can't send Authorization headers, so accept ?token= query param
    from auth import get_current_user
    from fastapi.security import HTTPAuthorizationCredentials

    if not token:
        raise HTTPException(status_code=401, detail="Missing token query parameter")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    current_user = await get_current_user(credentials=creds, db=db)
    result = await db.execute(
        select(Team).where(Team.id == team_id, Team.owner_id == current_user.id)
    )
    team = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    result = await db.execute(select(TeamRun).where(TeamRun.id == run_id, TeamRun.team_id == team_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Team run not found")

    return StreamingResponse(
        stream_team_run_events(run_id, team, db),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{team_id}/runs/{run_id}", response_model=TeamRunResponse)
@limiter.limit(TEAM_RATE_LIMITS["team_run_detail"])
async def get_team_run(
    request: Request,
    team_id: str,
    run_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Team).where(Team.id == team_id, Team.owner_id == current_user.id))
    team = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    result = await db.execute(select(TeamRun).where(TeamRun.id == run_id, TeamRun.team_id == team_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Team run not found")

    result = await db.execute(
        select(TeamDelegation)
        .where(TeamDelegation.team_run_id == run_id)
        .options(selectinload(TeamDelegation.agent))
    )
    delegations = result.scalars().all()

    all_agent_ids = {d.agent_id for d in delegations}
    agents = await _load_agents_map(db, list(all_agent_ids))

    return _run_to_response(run, team.name, [_delegation_to_response(d, agents) for d in delegations])


@router.get("/{team_id}/runs", response_model=list[TeamRunResponse])
@limiter.limit(TEAM_RATE_LIMITS["team_run_detail"])
async def list_team_runs(
    request: Request,
    team_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Team).where(Team.id == team_id, Team.owner_id == current_user.id))
    team = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    result = await db.execute(
        select(TeamRun).where(TeamRun.team_id == team_id).order_by(TeamRun.created_at.desc())
    )
    runs = result.scalars().all()
    return [_run_to_response(r, team.name) for r in runs]
