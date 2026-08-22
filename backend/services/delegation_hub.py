"""Push-based event hub for delegation SSE streaming.

Maintains per-delegation ``asyncio.Queue`` fan-out. Writers (background
execution coroutines, agent progress callbacks) publish events; SSE
subscribers tail their own queue until the delegation reaches a terminal
state or the client disconnects.

This replaces the old 1-second polling loop that read a shared in-memory
dict, and is the low-latency path for real-time updates. Persistence (for
reconnect / history) is handled separately by ``DelegationLog``.
"""
import asyncio
import logging
from typing import Any, Dict, List

log = logging.getLogger(__name__)

# delegation_id -> list of subscriber queues
_subscribers: Dict[str, List[asyncio.Queue]] = {}

# Terminal statuses — used by the SSE generator to stop tailing
TERMINAL_STATUSES = frozenset({"completed", "failed"})


def publish(delegation_id: str, event: Dict[str, Any]) -> None:
    """Fan an event out to every SSE subscriber watching this delegation.

    Non-blocking: uses ``put_nowait`` so slow or abandoned consumers never
    stall writers. Full queues drop the event and log a warning — the
    subscriber can still catch up via DB history on reconnect.
    """
    subs = _subscribers.get(delegation_id)
    if not subs:
        return
    for q in list(subs):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # CodeQL py/log-injection (#22): event type can originate from
            # agent-supplied payloads — strip control chars before logging.
            import re as _re
            evt_type = _re.sub(r"[\r\n\x00-\x1f]", " ", str(event.get("type")))[:64]
            log.warning(
                "SSE queue full for delegation %s — dropping event of type %s",
                delegation_id,
                evt_type,
            )


def subscribe(delegation_id: str) -> asyncio.Queue:
    """Register a new subscriber queue for this delegation and return it."""
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.setdefault(delegation_id, []).append(q)
    return q


def unsubscribe(delegation_id: str, q: asyncio.Queue) -> None:
    """Remove a subscriber queue when its SSE client disconnects."""
    subs = _subscribers.get(delegation_id)
    if not subs:
        return
    try:
        subs.remove(q)
    except ValueError:
        pass
    if not subs:
        _subscribers.pop(delegation_id, None)


def subscriber_count(delegation_id: str) -> int:
    """Number of SSE clients currently tailing this delegation (for debug)."""
    return len(_subscribers.get(delegation_id, []))
