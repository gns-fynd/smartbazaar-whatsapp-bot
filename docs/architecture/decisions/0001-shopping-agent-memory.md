# ADR 1: Scoped tools and structured shopping memory

Status: accepted for the local POC, 18 October 2026.

A single WhatsApp thread must support repeated shopping over months without sending an ever-growing transcript to the model. Account identity, product selection and purchase approval have different trust/lifecycle requirements.

Use one structured intent planner with in-process typed tools, deterministic product/identity checks and server-bound account context. Keep encrypted shopping memory separate from authentication state. Retain a bounded task list, explicit product preferences and normalized purchase evidence. Rotate short conversational context daily in Asia/Kolkata or after four idle hours, preserving unfinished lists.

MCP can expose the same tools later if another client needs them. A network MCP service, vector database, autonomous agent swarm and freeform running-summary memory are unnecessary for the current structured grocery task. This choice reduces independent services and keeps transaction authority in ordinary application code.

Consequences: local SQLite and process locks remain a POC constraint; durable jobs and process-safe persistence are required before scale. Daily resets do not replace cart/quote/order versioning. Checkout remains gated until real consumer contracts, explicit confirmation and uncertain-outcome reconciliation are implemented and verified. See [agent architecture](../../agent-architecture.md) for the implemented and future boundaries.
