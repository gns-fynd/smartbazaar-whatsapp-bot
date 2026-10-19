"""AI-written replies over tool facts, with a deterministic faithfulness check.

Tools produce draft messages from JioMart's own data. The composer asks the model to
turn those drafts into one natural WhatsApp reply in the customer's language, then
checks that every price, order ID and cart/list line survived exactly and that no new
amount was invented. Anything that fails the check falls back to the verified draft.
Interactive buttons and payment links are never written by the model.
"""

import logging
import re
from typing import Any

from .memory import safe_turn

log = logging.getLogger("smartbazaar")

_MONEY = re.compile(r"₹\s?(-?[\d,]+(?:\.\d+)?)")
_ORDER_ID = re.compile(r"\b(?:JM|FY|TR)[A-Z0-9]{8,}\b")
_LINE = re.compile(r"(\d+)\s*×\s*(.+?)(?=\s+—|\s+\(|\s+⚠|$)", re.M)
_COMPOSABLE = {"quick_reply", "cta_url", "cart_card"}


def _amounts(text: str) -> set[float]:
    values = set()
    for raw in _MONEY.findall(text):
        try:
            values.add(round(float(raw.replace(",", "")), 2))
        except ValueError:
            continue
    return values


def _lines(text: str) -> list[tuple[str, str]]:
    return [(qty, name.strip()) for qty, name in _LINE.findall(text) if name.strip()]


def faithful(draft: str, composed: str) -> bool:
    """Every draft amount, order ID and list line is kept; no new amount appears."""
    if not composed.strip():
        return False
    draft_amounts, new_amounts = _amounts(draft), _amounts(composed)
    if not draft_amounts <= new_amounts or not new_amounts <= draft_amounts:
        return False
    if not set(_ORDER_ID.findall(draft)) <= set(_ORDER_ID.findall(composed)):
        return False
    folded = composed.casefold()
    lines = folded.splitlines()
    for qty, name in _lines(draft):
        key = name.casefold()[:30]
        if key not in folded:
            return False
        # The quantity must sit right before the product name ("2 × Tomato…"), not anywhere in the line.
        pairing = re.compile(rf"(?<!\d){re.escape(qty)}\s*[×x]\s*{re.escape(key[:12])}")
        if not any(pairing.search(line) for line in lines):
            return False
    return True


class Composer:
    def __init__(self, agent):
        self.agent = agent

    async def apply(self, customer_text: str, replies: list, language: str | None = None) -> list:
        compose = getattr(self.agent, "compose_reply", None)
        texts = [r for r in replies if isinstance(r, str)]
        interactive = next((r for r in replies if isinstance(r, dict) and r.get("type") in _COMPOSABLE), None)
        if compose is None or (not texts and interactive is None):
            return replies
        draft = "\n\n".join(texts)
        prompt_draft = interactive.get("body", "") if interactive else ""
        try:
            # ``language`` is only known (and passed) when the customer tapped a button.
            extra = {"language": language} if language else {}
            result = await compose(safe_turn(customer_text)[:480], draft, prompt_draft, **extra)
        except Exception as exc:  # The verified draft is always a safe answer.
            log.warning("compose_failed type=%s", type(exc).__name__)
            return replies
        message = (result.get("message") or "").strip() if isinstance(result, dict) else ""
        button_prompt = (result.get("button_prompt") or "").strip() if isinstance(result, dict) else ""
        use_message = bool(texts) and faithful(draft, message) and len(message) <= 3500
        use_prompt = bool(interactive) and faithful(prompt_draft, button_prompt) and len(button_prompt) <= 1024
        log.info("compose message=%s prompt=%s", "ai" if use_message else "draft", "ai" if use_prompt else "draft")
        composed: list[Any] = []
        placed = False
        for reply in replies:
            if isinstance(reply, str):
                if not use_message:
                    composed.append(reply)
                elif not placed:
                    composed.append(message)
                    placed = True
            elif reply is interactive and use_prompt:
                composed.append({**reply, "body": button_prompt,
                                 "fallback": button_prompt + reply.get("fallback", "")[len(prompt_draft):]})
            else:
                composed.append(reply)
        return composed
