"""Bounded shopping memory and a small, sanitized conversational window."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .catalog import product_matches_exact

SESSION_IDLE_SECONDS = 4 * 3600
PREFERENCE_TTL = 180 * 86400
HISTORY_TTL = 30 * 86400
# A product question waits this long for an answer; any newer add request replaces it.
PENDING_TTL = 6 * 3600
MAX_RECENT_TURNS = 8
MAX_TURN_CHARS = 480


def safe_turn(text: str) -> str:
    """Keep conversational context free of common credentials/contact details."""
    text = re.sub(r"https?://\S+|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[redacted]", text)
    text = re.sub(r"(?<!\d)\+?\d[\d ()-]{8,}\d(?!\d)|\b\d{4,8}\b", "[redacted]", text)
    text = re.sub(r"\b(?:otp|password|api[_ -]?key|access[_ -]?token)\s*[:=]?\s*\S+|sk-(?:proj-)?[A-Za-z0-9_-]{12,}", "[redacted]", text, flags=re.I)
    return text.strip()[:MAX_TURN_CHARS]


def recent_turns(state: dict, now: float, scope: str) -> list[dict]:
    """Rotate daily, on idle, or across account boundaries; never read credentials."""
    day = datetime.fromtimestamp(now, ZoneInfo("Asia/Kolkata")).date().isoformat()
    dialogue = state.get("dialogue", {})
    if (dialogue.get("day") != day or dialogue.get("scope") != scope
            or now - dialogue.get("last_active", 0) > SESSION_IDLE_SECONDS):
        dialogue = {"day": day, "scope": scope, "turns": []}
    dialogue["last_active"] = now
    state["dialogue"] = dialogue
    return dialogue["turns"][-MAX_RECENT_TURNS:]


def remember_exchange(state: dict, now: float, scope: str, user: str, assistant: str) -> None:
    turns = recent_turns(state, now, scope)
    turns += [{"role": "user", "content": safe_turn(user)},
              {"role": "assistant", "content": safe_turn(assistant)}]
    state["dialogue"]["turns"] = turns[-MAX_RECENT_TURNS:]


def canonical_query(value: str) -> str:
    import re

    return re.sub(r"\b(?:dahi|dahe)\b|दही", "curd", value.strip().casefold())[:200]


def roll_session(memory: dict, now: float) -> bool:
    """Rotate conversational context; preferences survive and stale product questions go."""
    day = datetime.fromtimestamp(now, ZoneInfo("Asia/Kolkata")).date().isoformat()
    active = memory.get("active", {})
    fresh = active.get("day") != day or now - active.get("last_active", 0) > SESSION_IDLE_SECONDS
    if fresh:
        active = {"day": day, "queries": []}
    active["last_active"] = now
    memory["active"] = active
    # The JioMart cart is the only basket; an unanswered question never outlives its request.
    memory.pop("draft", None)
    memory.pop("choices", None)
    if memory.get("pending", {}).get("expires", 0) < now:
        memory.pop("pending", None)
    memory["preferences"] = {
        key: value for key, value in memory.get("preferences", {}).items()
        if value.get("expires", 0) > now
    }
    return fresh


def remembered_choice(memory: dict, query: str, now: float) -> dict | None:
    """Only an exact category preference or compatible purchased item may help."""
    query = canonical_query(query)
    explicit = memory.get("preferences", {}).get(query)
    if explicit and explicit.get("expires", 0) > now:
        return explicit
    history = memory.get("history", {})
    if now - history.get("checked_at", 0) > HISTORY_TTL:
        return None
    candidates = [
        item for item in history.get("items", [])
        if product_matches_exact(query, item)
        and isinstance(item.get("purchased_at"), (int, float))
        and 0 <= now - item["purchased_at"] <= PREFERENCE_TTL
    ]
    if not candidates:
        return None
    # Evidence comes from non-cancelled orders, most often and then most recently bought first.
    first = candidates[0]
    return {"product": first, "source": "purchase_history", "expires": now + 3600}
