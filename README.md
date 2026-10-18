# SmartBazaar WhatsApp Shopping Agent

A Python/FastAPI WhatsApp shopping agent for an existing JioMart account. It supports login, catalog search, account-scoped memory, and Meta voice/image input. Behind server flags it adds products to and edits the account's real JioMart cart (the only basket; the local shopping list was retired on 19 October 2026), places customer-confirmed orders (online via JioPay or cash on delivery) and follows them, with invoices and a flag-gated order cancel. One online order was placed from chat and paid live on 18 October 2026; see [status](docs/project-status.md) for what remains unverified.

## Start here

- [Agent architecture and build](docs/agent-architecture.md) — implemented tools, memory/session policy, media handling and remaining purchase gates.
- [API map](docs/agent-api-map.md) — official history, catalog, cart and payment contracts, separated from live proof.

- [Technical PRD](docs/technical-prd.md) — consolidated product scope, technical design, requirements, acceptance criteria, and dependencies.
- [WhatsApp agent experience/build plan](docs/whatsapp-agent-experience-plan.md) — 22 use cases, matching decisions, chat examples and staged implementation gates.
- [Real-account API validation](docs/commerce-api-validation-2026-10-18.md) — verified login/address/search milestone and remaining limits.
- [Current status](docs/project-status.md) — what is actually verified and what remains open.
- [JioMart account integration plan](docs/jiomart-account-integration-plan.md) — consumer login, candidate APIs, access dependencies and proof sequence.
- [Original source files](docs/reference/README.md) — the original PDF and Shyam's pilot guidance.
- [Agent context](.ai/agents.md) — task-oriented navigation for Codex, Claude, and other agents.
- [Context maintenance guide](.ai/README.md) — how to keep repository knowledge current.

## Current state

The app uses structured OpenAI planning, scoped tools, deterministic product checks, encrypted SQLite memory and an encrypted persistent outbox. Short context rotates daily in India time or after four idle hours; explicit preferences survive reconnection to the same account. With `JIOMART_CART_WRITES` on, a resolved product goes straight into the real JioMart cart, which is the only basket, and a product needing a choice becomes a short-lived question; CHECKOUT reviews the cart as it is. With cart writes off, the bot searches but cannot add anything. Cart writes and checkout are disabled by default. When enabled, only server-owned cart tools and single-use confirmation buttons can change the cart or place an order, and customer-facing text is AI-written from tool facts under a deterministic check.

Meta two-way login/search and product cards are confirmed. New media input and memory behavior have local regression coverage; read [project status](docs/project-status.md) for current live validation. The incoming queue remains memory-only, so interrupted requests must be resent.

This folder was created on 16 October 2026. The PRD was copied intact from the earlier task; the original PDF and stakeholder screenshot were copied with SHA-256 verification. See [copy provenance](docs/reference/copy-manifest.json).

## Agent setup

This project includes a portable copy of [RepoMind](https://github.com/gnsepili/RepoMind), pinned to commit `a340a14482622066743352c037574651c8652745`. Its `repo-context` skill is in `.agents/skills/repo-context/`; root `AGENTS.md` and `CLAUDE.md` route agents to the generated `.ai/` knowledge layer.

Open this directory as your project in Codex or Claude Code. The context system is local guidance and validation tooling; it does not run a background AI service or upload project data. Do not assume adding a folder automatically registers it in every agent application's project list.

## Available commands

Run from this project root with Python 3.10 or newer:

```sh
python3 .agents/skills/repo-context/scripts/context.py inventory --repo .
python3 .agents/skills/repo-context/scripts/context.py validate --repo .
python3 .agents/skills/repo-context/scripts/context.py impact --repo . --changed docs/technical-prd.md
```

## Local POC run and checks

Python 3.12+ and `uv` are used by the local runtime manifest. Create a private `.env` file; do not commit it or paste its values into logs:

```dotenv
OPENAI_API_KEY=<private-key>
OPENAI_MODEL=gpt-6.1-sol
META_ACCESS_TOKEN=<private-test-token>
META_APP_SECRET=<private-app-secret>
META_PHONE_NUMBER_ID=<test-phone-number-id>
META_WABA_ID=<test-waba-id>
WEBHOOK_VERIFY_TOKEN=<random-private-token>
SESSION_ENCRYPTION_KEY=<fernet-key>
ALLOWED_WHATSAPP_NUMBERS=<digits-only-allowlisted-test-number>
JIOMART_APPLICATION_TOKEN=<optional-private-token-if-needed>
```

Generate `SESSION_ENCRYPTION_KEY` locally with `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Start the current local run with `uv run uvicorn smartbazaar.app:create_app --factory --host 127.0.0.1 --port 8080 --no-access-log`. Suppressing access logs prevents callback query parameters from being written to terminal output. It serves the factual test-user notice at `/privacy`; a stable public HTTPS URL is still required for ongoing Meta use. Run the available local checks with `uv run python -m pytest -q` and `uv run ruff check smartbazaar tests`.

The process uses an in-memory inbound queue: inbound work interrupted by a process restart must be resent. Outbound messages are encrypted in SQLite and retried by the process. The allowlist and Meta HMAC signature validation are enforced before queueing messages.

## Try the agent

After LOGIN and OTP in WhatsApp, try “buy dahi”, “choose 1”, “skip”, “make 1 two in cart”, “remember cart line 1 for curd”, CART, CHECKOUT, ORDERS, INVOICE 1, MEMORY, or FORGET. Every reply ends with tap-able next steps. A voice note works like typed text; a clear product/list photo can provide shopping items after authentication. LOGOUT removes local access and shopping memory.

## Remaining purchase gates

Original gates (payment handoff and same-account visibility have since been shown for one online order): validate real sellable quantity and seller/store identifiers, existing-cart sharing and changes, authoritative quote/fees, supported payment handoff, version-bound customer confirmation, checkout idempotency/reconciliation and same-account order visibility. The present build can edit the real JioMart cart and submit a customer-confirmed order when `JIOMART_CART_WRITES` and `JIOMART_CHECKOUT` are on (both default off; online payment also needs `JIOMART_ONLINE_PAYMENT`). One online order placed by it was paid and confirmed live on 18 October. Cash on delivery, shipment-stage pushes, the corrected session refresh and upstream idempotency are not yet verified; see [project status](docs/project-status.md). Manager-voice output and production hardening remain later work.

This is a local Git repository on `main`, with no remote configured and no initial commit created during setup.

The 17 October development company, synthetic product and guest-cart tests are retained as [historical evidence](docs/commerce-readiness.md). The user excluded that route on 18 October: the POC must use the actual JioMart account and create a real order. Selected consumer account/address/search calls now work. On 18 October one real online order was placed from chat, paid and seen in the JioMart app.

## WhatsApp transport

Meta is the active transport. OpenWA was retired after verification succeeded on 18 October; its [runbook](docs/openwa-poc.md) remains historical evidence. The owner confirmed receiving search, product images and button interactions. WABA billing flag 141006 remains specific to business-initiated messaging.

## Availability checks

See [operations and recovery](docs/operations-reliability.md). `/healthz` proves the HTTP process is reachable; `/readyz` returns HTTP 503 when credential, worker or observed delivery evidence is degraded. Meta credentials are checked at startup and every five minutes, including an expiry warning. A system-user token without scheduled expiry replaces the temporary testing token; revocation, host/network failures and account restrictions remain possible. Current supervisor and callback acceptance are recorded in [project status](docs/project-status.md).
