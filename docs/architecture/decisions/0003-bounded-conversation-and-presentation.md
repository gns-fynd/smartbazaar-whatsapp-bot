# Conversational entry and bounded dialogue

Date: 18 October 2026. Status: implemented for the local POC.

## Context

Structured shopping memory already retained lists/preferences and rotated short task
context, but greetings/help and every unconnected message returned fixed text before
the planner. There was no recent exchange window. The owner requested natural
grocery-focused conversation before login and useful response formats.

## Decision

Use the existing typed planner for eligible conversational messages before and after
login. Extend its output with bounded natural reply text, a presentation preference,
and allowlisted navigation actions. Guest outputs never execute shopping tools.
Keep login, OTP, sensitive input, owner identity, and tool execution in deterministic
server code. Keep tool receipts authoritative rather than rewriting facts from
unconstrained model prose.

Store at most eight sanitized user/assistant messages, each capped at 480 characters,
inside encrypted session state. Rotate daily in Asia/Kolkata, after four idle hours,
or across account boundaries. Exclude login/OTP/address controls and clear the window
on new login, LOGOUT, FORGET and NEW SESSION. Preserve the independent lifecycle of
account-scoped shopping lists/preferences. No vector store or growing transcript is
introduced. The privacy notice explicitly describes the changed retention.

Provide image-free quick replies with opaque, owner/account/location-bound, single-use
IDs expiring after 15 minutes. The model may choose among supported actions; the server
constructs IDs and executes the selected action. Catalog searches retain verified
images and deterministic product facts, while an explicit text request suppresses
cards. Interactive failures use text fallback. No new cart/order capability is granted.

## Verification and limits

148 local tests and Ruff passed. Five synthetic live OpenAI checks exercised a guest
greeting, capabilities, unrelated-topic redirection, contextual recall and Hinglish.
The staged service was updated and local/public readiness returned HTTP 200. Human
acceptance of the new greeting buttons is recorded separately in project status.
Generated language is probabilistic; scope instructions and tests reduce drift,
while server authorization gates remain independent of model output. Voice/photo
processing still requires a connected account and remains shopping-only.

See [agent architecture](../../agent-architecture.md),
[reply formats](../../whatsapp-reply-formats.md) and
[status](../../project-status.md).

Late-evening note, 18 October 2026: the rule that tool receipts are never reworded is
narrowed by [ADR 5](0005-agentic-reply-composition.md). Receipts may now be rewritten by
the model, but only under a deterministic fact check, with the verified draft as fallback.
