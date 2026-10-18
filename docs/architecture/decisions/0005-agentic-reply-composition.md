# Agentic reply composition over verified tool facts

Date: 18 October 2026 (late evening). Status: implemented and running in the local
runtime; live logs show composed replies accepted (`compose message=ai prompt=ai`).

## Context

[ADR 3](0003-bounded-conversation-and-presentation.md) let the planner write
greetings and help, but kept every tool receipt as fixed template text. Live use
produced robotic, multi-bubble replies (one bubble per tool message). The owner
decided the experience should be agentic, with AI-written replies over verified
facts rather than templates. Spoken replies are out of scope. Prices, quantities and
order numbers come from JioMart and must never be changed or invented by a model.

## Decision

Keep tools as the only source of facts. After a turn's tools run, `Composer.apply`
sends the customer's sanitized latest message and the tool drafts to
`ShoppingAgent.compose_reply`. The drafts are the text replies plus the body of at
most one quick-reply, `cta_url` or cart card. The model returns one `message` and
an optional `button_prompt`, using structured output with `store=False`. Customer
text and drafts are treated as untrusted data.

Accept the model's text only if a deterministic check (`faithful`) passes:

- the set of ₹ amounts is exactly the draft's: none dropped, none added;
- every order-ID pattern in the draft is present;
- every "N × Product" line keeps its product name with the same N directly before it;
- the message is at most 3,500 characters and the lead-in at most 1,024.

On any failure, exception or missing model, send the verified drafts unchanged.
Never let the model write buttons, IDs, URLs, list menus or product cards. Never
recompose login, OTP, address, logout, pincode, FORGET or NEW SESSION replies,
quick-action or list-row taps, or replies the planner already wrote (`Written`).

## Consequences

Replies read as one natural message in the customer's language while amounts,
order IDs and line items stay exactly as JioMart reported them. A paraphrase that
changes a non-numeric fact, such as an availability or delivery claim, is not caught
by the check. The model instructions forbid such claims, but this remains a
residual risk. Each composable reply adds one model call and some latency. This
narrows ADR 3's rule that tool receipts are never reworded. Receipts may now be
reworded, but only under the fact check and with the draft as the fallback.

Verification: `tests/test_composer.py` covers accepted rewrites, rejection of
changed, dropped or invented facts and order IDs, fallback to drafts, and the
never-recomposed cases. The full suite has 261 passing tests.

See [agent architecture](../../agent-architecture.md),
[reply formats](../../whatsapp-reply-formats.md) and
[status](../../project-status.md).
