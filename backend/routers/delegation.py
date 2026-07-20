"""Agent-to-agent delegation routes.

Design notes
------------
* ``POST /user-request`` and ``POST /request`` escrow tokens, persist a
  ``Transaction`` row, and return a ``delegation_id`` immediately. The
  actual HTTP call to the executing agent — which can take many minutes —
  runs in a background coroutine so the frontend can open its SSE stream
  before any work has happened.
* Log messages are persisted to ``DelegationLog`` and published to a
  per-delegation ``asyncio.Queue`` via ``services.delegation_hub``. SSE
  subscribers drain the queue (no polling) and, on reconnect, replay
  history from the DB so nothing is ever missed.
* The in-memory ``delegation_status`` dict remains as a fast live-status
  cache used by legacy endpoints and for SSE termination checks.
"""
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status, Header, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import asyncio
import json
import logging
import os
import hmac as _hmac
import hashlib

from database import get_db, async_session_maker
from models.agent import Agent, AgentStatus
from models.user import User
from models.wallet import Wallet
from models.transaction import Transaction, TransactionType, TransactionStatus
from models.delegation_log import DelegationLog
from schemas import (
    DelegationRequest,
    DelegationResponse,
    DelegationComplete,
    TokenEstimateRequest,
    TokenEstimateResponse,
)
from routers.agent_api import get_agent_from_api_key
from routers.wallet import get_or_create_wallet
from services.agent_client import (
    get_agent_client,
    AgentTimeoutError,
    AgentConnectionError,
    AgentClientError,
)
from services import delegation_hub
from middleware.rate_limit import limiter, RATE_LIMITS
from auth import get_current_active_user, get_user_from_query_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/delegate", tags=["delegation"])

# Live status cache — DB is still the source of truth on terminal writes,
# but SSE generators check this dict for fast loop termination and legacy
# endpoints read it for real-time status without hitting the DB.
delegation_status: dict[str, str] = {}   # delegation_id -> status string

# Legacy in-memory log store. Kept for the non-streaming /logs fallback
# endpoints; new code should read from DelegationLog.
delegation_logs: dict[str, list] = {}

# Platform economics
PLATFORM_FEE_PCT = Decimal("0.10")

# Delegation safety
MAX_DELEGATION_DEPTH = 5

# HMAC key for signing outbound delegation payloads sent to agents
HIVE_SIGNING_SECRET = os.getenv("HIVE_SIGNING_SECRET", "change-me-in-production")


def _sign_payload(body: bytes, timestamp: str = "") -> str:
    """HMAC-SHA256 hex digest for a payload (same scheme as agent_client)."""
    message = f"{timestamp}.".encode() + body
    return _hmac.new(
        HIVE_SIGNING_SECRET.encode(),
        message,
        hashlib.sha256,
    ).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
#  Log + status helpers — persist to DB, publish to SSE hub
# ══════════════════════════════════════════════════════════════════════════

async def add_delegation_log(
    delegation_id: str,
    level: str,
    message: str,
    data: dict | None = None,
    source: str = "system",
) -> dict:
    """Persist a log entry and fan it out to SSE subscribers.

    Returns the event dict that was published, so callers can reuse it.
    """
    now = datetime.utcnow()
    entry = {
        "timestamp": now.isoformat(),
        "level": level,
        "message": message,
        "data": data or {},
        "source": source,
    }

    # Fan out to live SSE subscribers first (non-blocking put_nowait).
    delegation_hub.publish(delegation_id, {"type": "log", "data": entry})

    # Legacy in-memory mirror (kept for backwards-compat /logs endpoints).
    delegation_logs.setdefault(delegation_id, []).append(entry)

    # Persist to DB so reconnecting clients can replay history.
    try:
        async with async_session_maker() as session:
            session.add(
                DelegationLog(
                    delegation_id=delegation_id,
                    timestamp=now,
                    level=level,
                    message=message,
                    data=data or {},
                    source=source,
                )
            )
            await session.commit()
    except Exception as e:
        log.warning("failed to persist delegation log: %s", e)

    print(f"📝 [{delegation_id[:8]}] [{source.upper()}] {level.upper()}: {message}")
    return entry


async def set_delegation_status(delegation_id: str, status_value: str) -> None:
    """Update the live status cache and notify SSE subscribers."""
    delegation_status[delegation_id] = status_value
    delegation_hub.publish(
        delegation_id,
        {"type": "status", "data": {"status": status_value}},
    )
    await add_delegation_log(
        delegation_id,
        "info",
        f"Status changed to: {status_value}",
        source="system",
    )


async def _settle_delegation(
    db,
    transaction: "Transaction",
    tokens_used: Decimal,
    to_wallet: "Wallet",
    from_wallet: "Wallet",
    task_result: dict | None = None,
) -> None:
    """Apply platform fee, pay the executing agent, refund the remainder.

    Mutates wallet balances and transaction in place; caller must commit.
    """
    tokens_used = min(tokens_used, transaction.amount)

    platform_fee = (tokens_used * PLATFORM_FEE_PCT).quantize(Decimal("0.0001"))
    agent_receives = tokens_used - platform_fee

    to_wallet.balance += agent_receives
    transaction.platform_fee = platform_fee

    refund = transaction.amount - tokens_used
    if refund > Decimal("0"):
        from_wallet.balance += refund

    transaction.amount = tokens_used
    transaction.task_result = task_result
    transaction.status = TransactionStatus.COMPLETED.value
    transaction.completed_at = datetime.utcnow()


# ══════════════════════════════════════════════════════════════════════════
#  Background executor — runs the agent HTTP call off the request thread
# ══════════════════════════════════════════════════════════════════════════

def _internal_endpoint_for(openclaw_instance_id: str | None, endpoint_url: str | None) -> str | None:
    """Prefer internal container-name URL over public host:port.

    When both Hive and the agent run on the shared ``hive-net`` Docker
    network, Hive can reach the agent directly by container name — this
    bypasses host firewalls (UFW) that drop bridge→host-port traffic and
    avoids bouncing requests through the public IP unnecessarily.

    Falls back to the stored public ``endpoint_url`` for external BYOA
    agents and for any OpenClaw deployments that predate ``hive-net``.
    """
    if openclaw_instance_id:
        # generate_compose uses container_name = openclaw-{instance_id[:8]},
        # so the DNS name matches on the shared network.
        return f"http://openclaw-{openclaw_instance_id[:8]}:9000"
    return endpoint_url

async def _execute_delegation_task(
    delegation_id: str,
    target_endpoint: str,
    target_agent_name: str,
    task_description: str,
    max_tokens: float,
    callback_url: str | None,
    context: dict | None,
    timeout_seconds: int,
) -> None:
    """Dispatch the delegation to the executing agent.

    Runs entirely in the background after the HTTP response has been sent,
    with its own DB session (the request-scoped session is already closed).
    Streams progress into the SSE channel as it goes.
    """
    await add_delegation_log(
        delegation_id,
        "info",
        f"Dispatching to {target_agent_name}",
    )
    await add_delegation_log(
        delegation_id,
        "info",
        f"Contacting agent at {target_endpoint}",
    )

    agent_client = get_agent_client(timeout=timeout_seconds)

    try:
        agent_response = await agent_client.send_delegation_task(
            target_endpoint=target_endpoint,
            delegation_id=delegation_id,
            task_description=task_description,
            max_tokens=max_tokens,
            callback_url=callback_url,
            context=context,
            timeout=timeout_seconds,
        )
    except AgentTimeoutError:
        await _mark_failed(delegation_id, "agent_timeout",
                           f"Agent did not respond within {timeout_seconds}s")
        return
    except (AgentConnectionError, AgentClientError) as e:
        await _mark_failed(delegation_id, "agent_error",
                           f"Failed to reach agent: {e}")
        return
    except Exception as e:
        log.exception("Unexpected delegation failure")
        await _mark_failed(delegation_id, "agent_error", f"Unexpected error: {e}")
        return

    reported_status = agent_response.get("status", "unknown")
    await add_delegation_log(
        delegation_id,
        "info",
        f"Agent responded synchronously with status={reported_status}",
        data={"raw_status": reported_status},
    )

    if reported_status == "completed":
        tokens_used = Decimal(str(agent_response.get("tokens_used", max_tokens)))
        await _settle_from_background(
            delegation_id,
            tokens_used=tokens_used,
            task_result=agent_response.get("result"),
        )
        return

    # Agent accepted and will call back asynchronously (pending state). Nothing
    # to do here — the callback/complete endpoints will settle later.
    await add_delegation_log(
        delegation_id,
        "info",
        "Agent accepted task; awaiting callback",
    )


async def _settle_from_background(
    delegation_id: str,
    tokens_used: Decimal,
    task_result: dict | None,
) -> None:
    """Settle a delegation from the background executor using a fresh session."""
    async with async_session_maker() as session:
        result = await session.execute(
            select(Transaction).where(Transaction.id == delegation_id)
        )
        transaction = result.scalar_one_or_none()
        if not transaction:
            log.error("background settle: transaction %s not found", delegation_id)
            return
        if transaction.status != TransactionStatus.PENDING.value:
            # Already settled via callback or /complete — idempotent no-op.
            return

        to_wallet = (await session.execute(
            select(Wallet).where(Wallet.id == transaction.to_wallet_id)
        )).scalar_one()
        from_wallet = (await session.execute(
            select(Wallet).where(Wallet.id == transaction.from_wallet_id)
        )).scalar_one()

        await _settle_delegation(
            session, transaction, tokens_used,
            to_wallet=to_wallet, from_wallet=from_wallet,
            task_result=task_result,
        )
        await session.commit()

    await set_delegation_status(delegation_id, "completed")


async def _mark_failed(delegation_id: str, reason: str, message: str) -> None:
    """Fail a pending delegation and refund the delegator. Idempotent."""
    await add_delegation_log(delegation_id, "error", message, data={"reason": reason})

    async with async_session_maker() as session:
        result = await session.execute(
            select(Transaction).where(Transaction.id == delegation_id)
        )
        transaction = result.scalar_one_or_none()
        if not transaction:
            log.error("_mark_failed: transaction %s not found", delegation_id)
            return
        if transaction.status != TransactionStatus.PENDING.value:
            return

        from_wallet = (await session.execute(
            select(Wallet).where(Wallet.id == transaction.from_wallet_id)
        )).scalar_one()

        from_wallet.balance += transaction.amount
        transaction.status = TransactionStatus.FAILED.value
        transaction.completed_at = datetime.utcnow()
        transaction.refund_reason = reason
        transaction.task_description = (transaction.task_description or "") + \
            f"\n\nFailed: {message}"
        await session.commit()

    await set_delegation_status(delegation_id, "failed")


# ══════════════════════════════════════════════════════════════════════════
#  Token estimation — suggests a budget based on task + target agent
# ══════════════════════════════════════════════════════════════════════════

# Verbs that signal heavier reasoning or output volume. Matching is done
# against lower-cased description using plain substring containment so
# variants like "analyzing" or "researched" still count.
_COMPLEXITY_KEYWORDS = (
    "research", "analyz", "analys", "write", "compare", "summari",
    "draft", "plan", "review", "design", "build", "create", "implement",
    "translate", "debug", "optimi", "generate", "explain", "investigat",
    "evaluat", "refactor", "benchmark",
)


def _estimate_task_tokens(description: str, agent_min_rate: float) -> dict:
    """Heuristic token budget: base + length bonus × complexity multiplier.

    Transparent rather than smart — the breakdown is surfaced in the UI so
    the user can see exactly why a number was suggested and override it.
    """
    desc = (description or "").strip()
    base = 20
    length_bonus = min(80, len(desc) // 10)  # 1 token per 10 chars, capped

    desc_lower = desc.lower()
    matched = sorted({kw for kw in _COMPLEXITY_KEYWORDS if kw in desc_lower})
    multiplier = 1.0 + 0.2 * min(4, len(matched))  # up to 1.8× at 4+ hits

    raw = (base + length_bonus) * multiplier
    estimated = max(agent_min_rate, raw)
    estimated = max(10, min(1000, int(round(estimated))))

    return {
        "estimated_tokens": estimated,
        "breakdown": {
            "base": base,
            "length_bonus": length_bonus,
            "complexity_multiplier": round(multiplier, 2),
            "matched_keywords": matched[:6],
            "agent_min_rate": float(agent_min_rate),
            "description_chars": len(desc),
        },
    }


@router.post("/estimate", response_model=TokenEstimateResponse)
async def estimate_delegation_tokens(
    payload: TokenEstimateRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Estimate how many tokens a task will cost on a given agent."""
    agent_min = Decimal("0")
    if payload.target_agent_id:
        result = await db.execute(
            select(Agent).where(Agent.id == payload.target_agent_id)
        )
        agent = result.scalar_one_or_none()
        if agent and agent.pricing_model:
            if agent.pricing_model.get("type") == "token":
                agent_min = Decimal(str(agent.pricing_model.get("rate", 0)))

    return _estimate_task_tokens(payload.task_description, float(agent_min))


# ══════════════════════════════════════════════════════════════════════════
#  Delegation submission — returns immediately, executes in background
# ══════════════════════════════════════════════════════════════════════════

@router.post("/user-request", response_model=DelegationResponse)
@limiter.limit(RATE_LIMITS["delegate_request"])
async def user_request_delegation(
    request: Request,
    delegation: DelegationRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """User-to-agent delegation.

    Escrows tokens, creates the Transaction, and schedules the outbound
    agent call as a background task so the HTTP response returns in tens
    of milliseconds — the frontend can then open its SSE stream before
    any real work has started.
    """
    result = await db.execute(
        select(Agent).where(Agent.id == delegation.target_agent_id)
    )
    target_agent = result.scalar_one_or_none()
    if not target_agent:
        raise HTTPException(status_code=404, detail="Target agent not found")

    if target_agent.status not in [AgentStatus.ACTIVE.value, AgentStatus.IDLE.value]:
        raise HTTPException(
            status_code=503,
            detail=f"Target agent is {target_agent.status}",
        )
    if target_agent.ready is False:
        raise HTTPException(
            status_code=503,
            detail="Target agent is busy and not accepting new tasks",
        )
    if not target_agent.is_public:
        raise HTTPException(
            status_code=403,
            detail="Target agent is not available for public delegation",
        )

    user_wallet = await get_or_create_wallet(current_user.id, db)
    target_wallet = await get_or_create_wallet(target_agent.owner_id, db)

    # Atomic escrow: deduct then flush to detect overdraft.
    user_wallet.balance -= Decimal(str(delegation.max_tokens))
    await db.flush()
    if user_wallet.balance < 0:
        await db.rollback()
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient tokens. Required: {delegation.max_tokens}",
        )

    if target_agent.pricing_model:
        if target_agent.pricing_model.get("type") == "token":
            required_rate = Decimal(str(target_agent.pricing_model.get("rate", 0)))
            if delegation.max_tokens < float(required_rate):
                raise HTTPException(
                    status_code=400,
                    detail=f"Agent requires minimum {required_rate} tokens",
                )

    transaction = Transaction(
        from_wallet_id=user_wallet.id,
        to_wallet_id=target_wallet.id,
        amount=Decimal(str(delegation.max_tokens)),
        transaction_type=TransactionType.DELEGATION.value,
        delegating_agent_id=None,
        executing_agent_id=target_agent.id,
        originating_user_id=current_user.id,
        session_id=delegation.session_id,
        delegation_depth=0,
        task_description=delegation.task_description,
        status=TransactionStatus.PENDING.value,
    )
    db.add(transaction)
    await db.commit()
    await db.refresh(transaction)

    # Seed live status + initial log BEFORE scheduling the background task
    # so any SSE client that connects on the next tick sees the greeting.
    delegation_status[transaction.id] = "pending"
    delegation_logs[transaction.id] = []
    await add_delegation_log(
        transaction.id,
        "info",
        f"Delegation queued for {target_agent.name} ({delegation.max_tokens} tokens)",
    )

    # Kick off the agent call after the response is sent.
    background_tasks.add_task(
        _execute_delegation_task,
        delegation_id=transaction.id,
        target_endpoint=target_agent.endpoint_url,
        target_agent_name=target_agent.name,
        task_description=delegation.task_description,
        max_tokens=delegation.max_tokens,
        callback_url=delegation.callback_url,
        context=delegation.context,
        timeout_seconds=delegation.timeout_seconds,
    )

    return DelegationResponse(
        delegation_id=transaction.id,
        status="pending",
        message=f"Delegation queued — streaming progress for {target_agent.name}",
    )


@router.post("/request", response_model=DelegationResponse)
@limiter.limit(RATE_LIMITS["delegate_request"])
async def request_delegation(
    request: Request,
    delegation: DelegationRequest,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_agent_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Agent-to-agent delegation (same async flow as /user-request)."""
    result = await db.execute(
        select(Agent).where(Agent.id == delegation.target_agent_id)
    )
    target_agent = result.scalar_one_or_none()
    if not target_agent:
        raise HTTPException(status_code=404, detail="Target agent not found")

    if target_agent.status not in [AgentStatus.ACTIVE.value, AgentStatus.IDLE.value]:
        raise HTTPException(
            status_code=503,
            detail=f"Target agent is {target_agent.status}",
        )
    if target_agent.ready is False:
        raise HTTPException(
            status_code=503,
            detail="Target agent is busy and not accepting new tasks",
        )
    if not target_agent.is_public and target_agent.owner_id != agent.owner_id:
        raise HTTPException(
            status_code=403,
            detail="Target agent is not available for delegation",
        )

    # Enforce delegation depth via the session chain.
    current_depth = 0
    if delegation.session_id:
        depth_result = await db.execute(
            select(Transaction.delegation_depth)
            .where(Transaction.session_id == delegation.session_id)
            .order_by(Transaction.delegation_depth.desc())
            .limit(1)
        )
        max_depth_row = depth_result.scalar_one_or_none()
        if max_depth_row is not None:
            current_depth = max_depth_row + 1

    if current_depth >= MAX_DELEGATION_DEPTH:
        raise HTTPException(
            status_code=400,
            detail=f"Delegation chain too deep (max {MAX_DELEGATION_DEPTH} hops).",
        )

    delegating_wallet = await get_or_create_wallet(agent.owner_id, db)
    target_wallet = await get_or_create_wallet(target_agent.owner_id, db)

    if target_agent.pricing_model:
        if target_agent.pricing_model.get("type") == "token":
            required_rate = Decimal(str(target_agent.pricing_model.get("rate", 0)))
            if delegation.max_tokens < float(required_rate):
                raise HTTPException(
                    status_code=400,
                    detail=f"Agent requires minimum {required_rate} tokens",
                )

    originating_user_id = None
    if delegation.session_id:
        origin_result = await db.execute(
            select(Transaction.originating_user_id)
            .where(Transaction.session_id == delegation.session_id)
            .limit(1)
        )
        originating_user_id = origin_result.scalar_one_or_none()

    transaction = Transaction(
        from_wallet_id=delegating_wallet.id,
        to_wallet_id=target_wallet.id,
        amount=Decimal(str(delegation.max_tokens)),
        transaction_type=TransactionType.DELEGATION.value,
        delegating_agent_id=agent.id,
        executing_agent_id=target_agent.id,
        originating_user_id=originating_user_id,
        session_id=delegation.session_id,
        delegation_depth=current_depth,
        task_description=delegation.task_description,
        status=TransactionStatus.PENDING.value,
    )

    delegating_wallet.balance -= Decimal(str(delegation.max_tokens))
    await db.flush()
    if delegating_wallet.balance < 0:
        await db.rollback()
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient tokens. Required: {delegation.max_tokens}",
        )

    db.add(transaction)
    await db.commit()
    await db.refresh(transaction)

    delegation_status[transaction.id] = "pending"
    delegation_logs[transaction.id] = []
    await add_delegation_log(
        transaction.id,
        "info",
        f"Delegation queued: {agent.name} → {target_agent.name} "
        f"({delegation.max_tokens} tokens)",
    )

    background_tasks.add_task(
        _execute_delegation_task,
        delegation_id=transaction.id,
        target_endpoint=target_agent.endpoint_url,
        target_agent_name=target_agent.name,
        task_description=delegation.task_description,
        max_tokens=delegation.max_tokens,
        callback_url=delegation.callback_url,
        context=delegation.context,
        timeout_seconds=delegation.timeout_seconds,
    )

    return DelegationResponse(
        delegation_id=transaction.id,
        status="pending",
        message=f"Delegation queued — streaming progress for {target_agent.name}",
    )


# ══════════════════════════════════════════════════════════════════════════
#  Status + history endpoints
# ══════════════════════════════════════════════════════════════════════════

@router.get("/{delegation_id}/status")
async def get_delegation_status(
    delegation_id: str,
    agent: Agent = Depends(get_agent_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Check status of a delegation (agent view)."""
    result = await db.execute(
        select(Transaction).where(Transaction.id == delegation_id)
    )
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="Delegation not found")

    if transaction.delegating_agent_id != agent.id and transaction.executing_agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Not authorized to view this delegation")

    live = delegation_status.get(transaction.id, transaction.status)
    return {
        "delegation_id": transaction.id,
        "status": live,
        "amount": float(transaction.amount),
        "task_description": transaction.task_description,
        "created_at": transaction.created_at,
        "completed_at": transaction.completed_at,
    }


@router.get("/{delegation_id}/user-status")
async def get_user_delegation_status(
    delegation_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Check status of a user's delegation (user view)."""
    result = await db.execute(
        select(Transaction).where(Transaction.id == delegation_id)
    )
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="Delegation not found")

    user_wallet = await get_or_create_wallet(current_user.id, db)
    if transaction.from_wallet_id != user_wallet.id:
        raise HTTPException(status_code=403, detail="Not authorized to view this delegation")

    live = delegation_status.get(transaction.id, transaction.status)
    return {
        "delegation_id": transaction.id,
        "status": live,
        "amount": float(transaction.amount),
        "task_description": transaction.task_description,
        "created_at": transaction.created_at,
        "completed_at": transaction.completed_at,
    }


async def _fetch_history(delegation_id: str, db: AsyncSession) -> list[dict]:
    """Return all persisted log events for a delegation, oldest-first."""
    result = await db.execute(
        select(DelegationLog)
        .where(DelegationLog.delegation_id == delegation_id)
        .order_by(DelegationLog.timestamp.asc(), DelegationLog.id.asc())
    )
    return [entry.to_event() for entry in result.scalars().all()]


@router.get("/{delegation_id}/user-logs")
async def get_user_delegation_logs(
    delegation_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """All logs for a user's delegation (non-streaming, from DB)."""
    result = await db.execute(
        select(Transaction).where(Transaction.id == delegation_id)
    )
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="Delegation not found")

    user_wallet = await get_or_create_wallet(current_user.id, db)
    if transaction.from_wallet_id != user_wallet.id:
        raise HTTPException(status_code=403, detail="Not authorized to view this delegation")

    logs = await _fetch_history(delegation_id, db)
    return {
        "delegation_id": delegation_id,
        "logs": logs,
        "status": delegation_status.get(delegation_id, transaction.status),
        "total_logs": len(logs),
    }


# ══════════════════════════════════════════════════════════════════════════
#  SSE streaming (push-based, with DB history replay on connect)
# ══════════════════════════════════════════════════════════════════════════

async def _sse_event_generator(delegation_id: str, db: AsyncSession):
    """Yield SSE frames: subscribe → replay history → tail live events.

    Subscribing BEFORE reading DB history is deliberate: any event published
    during the DB read lands in our queue, so nothing is lost. The frontend
    de-dupes on timestamp so a brief overlap between history and live events
    is harmless.
    """
    queue = delegation_hub.subscribe(delegation_id)
    HEARTBEAT_INTERVAL = 15.0  # seconds between keep-alive comments

    try:
        # Replay persisted history (includes events written after we
        # subscribed — those will also arrive via the queue and be de-duped
        # by the client on timestamp).
        history = await _fetch_history(delegation_id, db)

        initial_status = delegation_status.get(delegation_id)
        if initial_status is None:
            # Recover from DB if the live cache has been evicted (e.g.
            # process restart since the delegation completed).
            result = await db.execute(
                select(Transaction.status).where(Transaction.id == delegation_id)
            )
            initial_status = result.scalar_one_or_none() or "pending"

        yield f"data: {json.dumps({'type': 'status', 'data': {'status': initial_status}})}\n\n"
        for entry in history:
            yield f"data: {json.dumps({'type': 'log', 'data': entry})}\n\n"

        if initial_status in delegation_hub.TERMINAL_STATUSES:
            yield f"data: {json.dumps({'type': 'done', 'data': {'status': initial_status}})}\n\n"
            return

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                if delegation_status.get(delegation_id) in delegation_hub.TERMINAL_STATUSES:
                    yield (
                        f"data: {json.dumps({'type': 'done', 'data': {'status': delegation_status[delegation_id]}})}\n\n"
                    )
                    return
                continue

            yield f"data: {json.dumps(event)}\n\n"

            if (
                event.get("type") == "status"
                and event.get("data", {}).get("status") in delegation_hub.TERMINAL_STATUSES
            ):
                yield f"data: {json.dumps({'type': 'done', 'data': event['data']})}\n\n"
                return
    finally:
        delegation_hub.unsubscribe(delegation_id, queue)


@router.get("/{delegation_id}/user-stream")
async def stream_user_delegation(
    delegation_id: str,
    current_user: User = Depends(get_user_from_query_token),
    db: AsyncSession = Depends(get_db),
):
    """Stream delegation progress using Server-Sent Events (user view)."""
    result = await db.execute(
        select(Transaction).where(Transaction.id == delegation_id)
    )
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="Delegation not found")

    user_wallet = await get_or_create_wallet(current_user.id, db)
    if transaction.from_wallet_id != user_wallet.id:
        raise HTTPException(status_code=403, detail="Not authorized to view this delegation")

    return StreamingResponse(
        _sse_event_generator(delegation_id, db),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{delegation_id}/stream")
async def stream_delegation(
    delegation_id: str,
    agent: Agent = Depends(get_agent_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Stream delegation progress using SSE (agent view)."""
    result = await db.execute(
        select(Transaction).where(Transaction.id == delegation_id)
    )
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="Delegation not found")

    if transaction.delegating_agent_id != agent.id and transaction.executing_agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Not authorized to view this delegation")

    return StreamingResponse(
        _sse_event_generator(delegation_id, db),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{delegation_id}/logs")
async def get_delegation_logs(
    delegation_id: str,
    agent: Agent = Depends(get_agent_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """All logs for a delegation (non-streaming, from DB)."""
    result = await db.execute(
        select(Transaction).where(Transaction.id == delegation_id)
    )
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="Delegation not found")

    if transaction.delegating_agent_id != agent.id and transaction.executing_agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Not authorized to view this delegation")

    logs = await _fetch_history(delegation_id, db)
    return {
        "delegation_id": delegation_id,
        "logs": logs,
        "status": delegation_status.get(delegation_id, transaction.status),
        "total_logs": len(logs),
    }


# ══════════════════════════════════════════════════════════════════════════
#  Progress + completion callbacks (from executing agent)
# ══════════════════════════════════════════════════════════════════════════

@router.post("/{delegation_id}/progress")
async def agent_progress_update(
    delegation_id: str,
    progress: dict,
    agent: Agent = Depends(get_agent_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Executing agent posts streaming progress back to Hive.

    Body: {"level": str, "message": str, "data": dict}
    """
    result = await db.execute(
        select(Transaction).where(Transaction.id == delegation_id)
    )
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="Delegation not found")

    if transaction.executing_agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Not authorized to update this delegation")

    level = progress.get("level", "info")
    message = progress.get("message", "No message")
    data = progress.get("data", {})

    await add_delegation_log(delegation_id, level, message, data, source="agent")

    return {
        "success": True,
        "delegation_id": delegation_id,
        "message": "Progress update received",
    }


@router.post("/{delegation_id}/complete")
@limiter.limit(RATE_LIMITS["delegate_complete"])
async def complete_delegation(
    request: Request,
    delegation_id: str,
    completion: DelegationComplete,
    agent: Agent = Depends(get_agent_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Executing agent marks delegation complete (authenticated by API key)."""
    result = await db.execute(
        select(Transaction).where(Transaction.id == delegation_id)
    )
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="Delegation not found")

    if transaction.executing_agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Only executing agent can complete delegation")

    if transaction.status != TransactionStatus.PENDING.value:
        raise HTTPException(status_code=400, detail=f"Delegation is already {transaction.status}")

    tokens_used = Decimal(str(completion.tokens_used))

    target_wallet = (await db.execute(
        select(Wallet).where(Wallet.id == transaction.to_wallet_id)
    )).scalar_one()
    from_wallet = (await db.execute(
        select(Wallet).where(Wallet.id == transaction.from_wallet_id)
    )).scalar_one()

    await _settle_delegation(
        db, transaction, tokens_used,
        to_wallet=target_wallet, from_wallet=from_wallet,
        task_result=completion.result,
    )
    await db.commit()

    await set_delegation_status(delegation_id, "completed")

    return {
        "success": True,
        "delegation_id": delegation_id,
        "tokens_used": float(tokens_used),
        "status": "completed",
    }


@router.post("/{delegation_id}/fail")
async def fail_delegation(
    delegation_id: str,
    reason: str,
    agent: Agent = Depends(get_agent_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Executing agent marks delegation failed. Refunds escrow."""
    result = await db.execute(
        select(Transaction).where(Transaction.id == delegation_id)
    )
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="Delegation not found")

    if transaction.executing_agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Only executing agent can fail delegation")

    if transaction.status != TransactionStatus.PENDING.value:
        raise HTTPException(status_code=400, detail=f"Delegation is already {transaction.status}")

    delegating_wallet = (await db.execute(
        select(Wallet).where(Wallet.id == transaction.from_wallet_id)
    )).scalar_one()
    delegating_wallet.balance += transaction.amount

    transaction.status = TransactionStatus.FAILED.value
    transaction.completed_at = datetime.utcnow()
    transaction.refund_reason = "agent_error"
    transaction.task_description = (transaction.task_description or "") + f"\n\nFailed: {reason}"
    await db.commit()

    await set_delegation_status(delegation_id, "failed")

    return {
        "success": True,
        "delegation_id": delegation_id,
        "status": "failed",
        "refunded": float(transaction.amount),
    }


def _verify_callback_signature(request: Request, body: bytes) -> None:
    """Validate HMAC-SHA256 signature on inbound agent callbacks."""
    sig_header = request.headers.get("X-Hive-Signature", "")
    ts_header = request.headers.get("X-Hive-Timestamp", "")

    if not sig_header or not ts_header:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Hive-Signature or X-Hive-Timestamp header",
        )

    expected = f"sha256={_sign_payload(body, ts_header)}"
    if not _hmac.compare_digest(sig_header, expected):
        raise HTTPException(status_code=401, detail="Invalid callback signature")


@router.post("/{delegation_id}/callback")
@limiter.limit(RATE_LIMITS["delegate_callback"])
async def delegation_callback(
    request: Request,
    delegation_id: str,
    callback_data: DelegationComplete,
    db: AsyncSession = Depends(get_db),
):
    """Async callback: executing agent reports completion, signed with HMAC."""
    raw_body = await request.body()
    _verify_callback_signature(request, raw_body)

    result = await db.execute(
        select(Transaction).where(Transaction.id == delegation_id)
    )
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="Delegation not found")

    if transaction.status != TransactionStatus.PENDING.value:
        raise HTTPException(status_code=400, detail=f"Delegation is already {transaction.status}")

    tokens_used = Decimal(str(callback_data.tokens_used))

    target_wallet = (await db.execute(
        select(Wallet).where(Wallet.id == transaction.to_wallet_id)
    )).scalar_one()
    from_wallet = (await db.execute(
        select(Wallet).where(Wallet.id == transaction.from_wallet_id)
    )).scalar_one()

    await _settle_delegation(
        db, transaction, tokens_used,
        to_wallet=target_wallet, from_wallet=from_wallet,
        task_result=callback_data.result,
    )
    await db.commit()

    await set_delegation_status(delegation_id, "completed")

    return {
        "success": True,
        "delegation_id": delegation_id,
        "tokens_used": float(tokens_used),
        "status": "completed",
        "message": "Delegation marked as completed",
    }


# ══════════════════════════════════════════════════════════════════════════
#  Discovery + listing
# ══════════════════════════════════════════════════════════════════════════

@router.get("/discover")
async def discover_agents_for_delegation(
    skill: str = None,
    max_cost: float = None,
    min_rating: float = None,
    agent: Agent = Depends(get_agent_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Discover public agents available for delegation."""
    query = select(Agent).where(
        Agent.is_public == True,
        Agent.status.in_([AgentStatus.ACTIVE.value, AgentStatus.IDLE.value]),
        Agent.ready != False,
    )

    if skill:
        from models.agent_skill import AgentSkill
        query = query.join(AgentSkill).join(AgentSkill.skill).where(
            AgentSkill.skill.has(name=skill)
        )

    result = await db.execute(query.limit(20))
    agents = result.scalars().all()

    discovered = []
    for a in agents:
        if a.id == agent.id:
            continue
        if a.ready is False:
            continue
        if max_cost and a.pricing_model:
            if a.pricing_model.get("type") == "token":
                rate = a.pricing_model.get("rate", 0)
                if rate > max_cost:
                    continue
        discovered.append({
            "id": a.id,
            "name": a.name,
            "slug": a.slug,
            "description": a.marketplace_description or a.description,
            "pricing_model": a.pricing_model,
            "tags": a.tags or [],
            "status": a.status,
            "last_seen": a.last_seen,
        })

    return {"agents": discovered, "count": len(discovered)}


@router.get("/user-delegations")
async def get_user_delegations(
    status: str = None,
    limit: int = 20,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List delegations initiated by the current user."""
    user_wallet = await get_or_create_wallet(current_user.id, db)

    query = select(Transaction).where(Transaction.from_wallet_id == user_wallet.id)
    if status:
        query = query.where(Transaction.status == status)
    query = query.order_by(Transaction.created_at.desc()).limit(limit)
    result = await db.execute(query)
    transactions = result.scalars().all()

    return {
        "delegations": [
            {
                "id": t.id,
                "task_description": t.task_description,
                "amount": float(t.amount),
                "status": t.status,
                "created_at": t.created_at.isoformat(),
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "task_result": t.task_result,
            }
            for t in transactions
        ],
        "total": len(transactions),
    }


@router.get("/my-delegations")
async def get_my_delegations(
    status: str = None,
    limit: int = 20,
    agent: Agent = Depends(get_agent_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """List delegations involving this agent (as delegator or executor)."""
    query = select(Transaction).where(
        (Transaction.delegating_agent_id == agent.id)
        | (Transaction.executing_agent_id == agent.id)
    )
    if status:
        query = query.where(Transaction.status == status)
    query = query.order_by(Transaction.created_at.desc()).limit(limit)
    result = await db.execute(query)
    transactions = result.scalars().all()

    return {
        "delegations": [
            {
                "id": t.id,
                "task_description": t.task_description,
                "amount": float(t.amount),
                "status": t.status,
                "created_at": t.created_at.isoformat(),
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "task_result": t.task_result,
                "role": "delegator" if t.delegating_agent_id == agent.id else "executor",
            }
            for t in transactions
        ],
        "total": len(transactions),
    }
