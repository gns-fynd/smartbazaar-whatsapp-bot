# Project documents module

## Purpose and scope

This module owns the repository's living requirements, dated external-status record, original source references, and human entry point. It does not implement the runtime POC; source-backed runtime behavior belongs to [the login/search module](login-search-poc.md). Its job is to keep implementation facts, manual observations, and future requirements distinct so a reader does not mistake a document, dashboard action, or API acceptance for a completed customer journey.

The maintained product baseline is [technical PRD](../../docs/technical-prd.md). [Project status](../../docs/project-status.md) records the most recent non-secret evidence. The [experience plan](../../docs/whatsapp-agent-experience-plan.md) supplies matching and future cart behavior. JioMart evidence and unresolved dependencies live in the [validation record](../../docs/commerce-api-validation-2026-10-18.md) and [integration plan](../../docs/jiomart-account-integration-plan.md). Original PDF and screenshot evidence remain unchanged in `docs/reference/`.

## Current facts to preserve

The local runtime now includes typed shopping-list tools, encrypted account-scoped memory and Meta voice/image input, alongside login/search. The current test count and live probes are recorded in project status. Restart-durable incoming jobs remain unimplemented. Since the 18 October evening build, real-cart sync, customer-confirmed checkout (JioPay hosted page or cash on delivery), order history/tracking/reorder and order-update pushes are implemented behind `JIOMART_CART_WRITES`/`JIOMART_CHECKOUT` (default off, on in the runtime). They are locally tested. Late on 18 October, live cart writes and one paid online order were verified; cash on delivery, shipment-stage pushes and the corrected session refresh are not. Live evidence and owner decisions for that round are in the [product polish backlog](../../docs/product-polish-backlog.md), written by the coordinator; treat it as evidence and do not rewrite it during documentation passes. Documents must keep "implemented", "live-verified" and "expected/unverified" separate for these steps.

19 October 2026: the local shopping list is retired in code ([ADR 6](../../docs/architecture/decisions/0006-jiomart-cart-is-the-only-basket.md)); next-step buttons, picture choices, voice for every command, proactive session refresh with a background keep-alive (30-day local sessions), a flag-gated order cancel, invoices and an order-summary PDF were added. All of it is implemented and locally tested (307 tests) but not deployed or verified live; [project status](../../docs/project-status.md) has the dated record. On the owner's request, the [product polish backlog](../../docs/product-polish-backlog.md) received an appended status table for that round; its 18 October rows were left as written.

Meta two-way login/search and product image/button replies are confirmed after verification; older restriction records are historical. JioMart history remains unresolved, and the account's older orders sit behind a legacy website-cookie order history the bot cannot read. The real checkout path is established live for one online order; other paths are not. Never reactivate the Company 153 development storefront as a substitute.

The [agent architecture](../../docs/agent-architecture.md) describes actual tools/session-memory behavior and the implemented checkout/order design with its remaining gaps; [ADR 4](../../docs/architecture/decisions/0004-real-cart-checkout-and-order-updates.md) records the decision (with a late-evening amendment), and [ADR 5](../../docs/architecture/decisions/0005-agentic-reply-composition.md) records AI-written replies under a fact check. [ADR 6](../../docs/architecture/decisions/0006-jiomart-cart-is-the-only-basket.md) records retiring the local list. The [API map](../../docs/agent-api-map.md) records official documented endpoints, not a claim of live compatibility. Original source evidence is unchanged.

## Documentation rules

Record a fact with date, bounded evidence, and remaining limitation. An HTTP acceptance, a dashboard subscription, a signed callback, delivery receipt, inbound customer message, successful OTP link, and completed order are different events. Keep tokens, app secret, session cookies, OTPs, full customer identifiers, addresses, raw webhook content and raw provider failures out of documents and logs.

When source behavior changes, update [agents.md](../agents.md), the relevant module/feature/pattern/architecture context, this documentation module when the facts affect planning, and [context-map.json](../context-map.json). The context checker validates links and map coverage only; it cannot prove external integrations. Preserve unrelated source evidence instead of rewriting it to fit current implementation.

## Related context

- [Runtime module](login-search-poc.md)
- [Feature: WhatsApp login and catalog search](../features/whatsapp-login-search.md)
- [Implemented interactions](../architecture/module-interactions.md)
- [Root context](../agents.md)
- [README](../../README.md)
- [Project status](../../docs/project-status.md)
- [Technical PRD](../../docs/technical-prd.md)
