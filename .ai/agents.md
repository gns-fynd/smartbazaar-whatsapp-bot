# SmartBazaar POC context

## System overview

SmartBazaar is a local, single-process WhatsApp shopping-agent POC for an existing JioMart customer account. It accepts an allowlisted, HMAC-verified Meta webhook, handles JioMart OTP outside the model, searches catalog candidates and renders cautious search replies. It implements encrypted account-scoped preferences, bounded history/cart reads and Meta voice/image input. Behind the `JIOMART_CART_WRITES`/`JIOMART_CHECKOUT` flags (default off; on in the local runtime), with online payment also behind `JIOMART_ONLINE_PAYMENT`, it adds to and edits the account's real JioMart cart, places customer-confirmed orders (JioPay through a server-opened payment session, or cash on delivery), lists/tracks/reorders orders and pushes order updates. Customer-facing replies are AI-written from tool drafts under a deterministic fact check, product search can return catalog-image cards, and the cart is a picture card. Late on 18 October, live cart writes and one online order (created from chat, paid by UPI and confirmed by a watcher push) were verified; cash on delivery and shipment-stage pushes were not, and production deployment remains unimplemented.

**Current build, 19 October 2026 (implemented and locally tested; not yet deployed or verified on WhatsApp).** The local shopping list is retired: the JioMart cart is the only basket, and a product needing a choice is a short-lived question (6 hours, replaced by any newer add request), shown as numbered pictures or a tap menu ([ADR 6](../docs/architecture/decisions/0006-jiomart-cart-is-the-only-basket.md)). Every reply ends with two or three context-aware buttons. Voice notes go through the normal text path. JioMart sessions are refreshed before the access token expires and renewed by a background keep-alive (`session_keepalive.py`); local sessions last 30 days. Orders gain INVOICE (JioMart's PDF), a flag-gated CANCEL ORDER (`JIOMART_ORDER_CANCEL`, default off) and an in-memory order-summary PDF (`receipt.py`). See the section at the end.

Manual evidence establishes that selected JioMart consumer OTP/session/refresh, saved-address and location-dependent search calls worked on 18 October 2026. The user has confirmed live Meta replies and product search, including a multi-item search screenshot. Read [status](../docs/project-status.md) for dated facts and [the PRD](../docs/technical-prd.md) for requirements and proposed architecture.

## Technology stack

| Area | Observed implementation |
| --- | --- |
| Language/runtime | Python 3.12+ declared in `pyproject.toml` |
| HTTP service | FastAPI application factory in `smartbazaar/app.py` |
| State | Local SQLite with Fernet-encrypted customer state, outbox payloads (including base64 PDF documents), order attempts and payment links. Sessions expire 30 days after the customer's last message (`SESSION_TTL_SECONDS`; 24 hours before 19 October) |
| Queue | Bounded in-memory `asyncio.Queue`; it is lost on restart. An in-process order-watch worker runs when checkout is enabled, and a session keep-alive loop always runs |
| AI | OpenAI Python SDK Responses structured parsing for intent, ranking and reply composition (`compose_reply`, checked by `composer.faithful`); configured model defaults to `gpt-6.1-sol` |
| Imaging | Pillow builds the in-memory JPEG cart picture and numbered choice picture (`cart_image.py`) and the one-page order-summary PDF (`receipt.py`) |
| Commerce | Async `httpx` JioMart OTP/address/search, history/order/tracking reads (with a `www.jiomart.com/api` fallback), and flag-gated cart writes, address selection, payment options, checkout and a server-side payment session. Account requests follow one JioMart-host redirect, and a 401/403 triggers one token refresh and retry. Since 19 October it also refreshes proactively when the access JWT has 5 minutes or less left (documented body, then cookie-only on HTTP 400), exposes `keep_alive`, flag-gated shipment cancellation and invoice download. Production contract still unverified |
| Channel | Meta Cloud API client and HMAC verifier; app published; inbound OTP login, saved address and AI/JioMart search succeeded, with business verification complete and live replies confirmed; business-initiated billing remains a separate flag. Outbound adds quick replies, list menus, `cta_url` pay buttons, uploaded cart and choice pictures with an image header, uploaded PDF documents, and a best-effort read receipt with typing indicator |
| Tests | `pytest` and `pytest-asyncio`; Ruff linting |

## Directory structure

```text
smartbazaar/
  app.py               FastAPI webhook, queue/order-watch/keep-alive workers, pay pages, lifecycle
  config.py            private environment-backed settings
  conversation.py      OTP-first login/search orchestration, command routing, next-step buttons
  tools.py             add to the real cart, short-lived product questions, skip, preferences
  memory.py            session rotation, preference/history policy, bounded chat window
  checkout.py          real cart (the only basket): add, edit, review, confirmed checkout, pay links
  composer.py          AI-written replies over tool drafts with a faithfulness check
  cart_image.py        in-memory JPEG grids: cart thumbnails, numbered product choices (Pillow)
  receipt.py           in-memory one-page order-summary PDF (Pillow)
  orders.py            ORDERS/ORDER/TRACK/REORDER/CANCEL ORDER/INVOICE for the linked account
  order_watch.py       background order-status pushes for chat-placed orders
  session_keepalive.py background renewal of stored JioMart sessions
  jiomart.py           bounded JioMart HTTP adapter
  agent.py             Responses intent/ranking/composition adapter
  catalog.py           normalization, deterministic matching, rendering, product page links
  media.py             authenticated Meta voice/image download, Whisper and vision extraction
  storage.py           encrypted SQLite state/event/outbox/attempt store
  whatsapp.py          Meta signature, outbound text/interactive/document client, picture uploads
  channel_health.py    Meta credential/phone/account readiness probe
  privacy.py           public privacy notice
  openwa.py            retired temporary OpenWA transport
scripts/               local supervisor and tunnel tooling
tests/                 isolated adapter/matching/flow checks
docs/                  requirements, observation status and evidence
work/                  local captures and runtime state; not production evidence
```

## Code conventions

Python files use `snake_case`; classes use `PascalCase`; imports are standard library, third party, then local. Public boundaries use small typed/Pydantic contracts and safe exception messages. The POC avoids logging request bodies, secrets, OTPs, phone numbers, session cookies and raw upstream exceptions. Tests use injected fake OpenAI/HTTP transports instead of live services.

## Configuration and local operation

`Settings` loads a private `.env`; values are secrets or identifiers and must not be committed. Required names are `OPENAI_API_KEY`, `OPENAI_MODEL`, `META_ACCESS_TOKEN`, `META_APP_SECRET`, `META_PHONE_NUMBER_ID`, `META_WABA_ID`, `WEBHOOK_VERIFY_TOKEN`, `SESSION_ENCRYPTION_KEY`, and `ALLOWED_WHATSAPP_NUMBERS`. `JIOMART_APPLICATION_TOKEN` is optional because the adapter can discover a public runtime application token in memory. Commerce settings are `JIOMART_CART_WRITES` and `JIOMART_CHECKOUT` (both default false; checkout needs cart writes), `CHECKOUT_MAX_TOTAL` (optional; default 0 = no limit since 19 October, was 1500 rupees), `JIOMART_ONLINE_PAYMENT` (default false; true in the runtime), `PUBLIC_BASE_URL` (empty means the local ngrok agent API is used), `WHATSAPP_DISPLAY_NUMBER` and `ORDER_WATCH_INTERVAL_SECONDS` (default 20). Since 19 October: `JIOMART_ORDER_CANCEL` (default false), `SESSION_KEEPALIVE_INTERVAL_SECONDS` (default 600) and `SESSION_TTL_SECONDS` (default 2592000, 30 days). See [operations](../docs/operations-reliability.md).

Run `uv run uvicorn smartbazaar.app:create_app --factory --host 127.0.0.1 --port 8080 --no-access-log` after creating private configuration. A public HTTPS endpoint and Meta callback setup are separately required to receive real webhooks. Run `uv run python -m pytest -q` and `uv run ruff check smartbazaar tests` for local checks. The context validator is structural only: `python3 .agents/skills/repo-context/scripts/context.py validate --repo .`.

## Cross-cutting controls

- [Webhook and persistence pattern](patterns/webhook-state-safety.md) describes signature/allowlist checks, event deduplication, encrypted state and restart limits.
- [External adapter pattern](patterns/bounded-external-adapters.md) describes safe HTTP/Responses calls, bounded input, redirects, structured output and error handling.
- The POC serializes a customer's in-process messages with an owner lock. It does not provide cross-process locking, durable inbound jobs, cart versions, or purchase idempotency for upstream commerce. Local shopping-list versions existed until 18 October; the list is retired. The flag-gated checkout path substitutes a reviewed-cart snapshot for a cart version and a persisted, button-keyed `order_attempts` row for upstream idempotency. Unknown outcomes block new orders until reconciled; checkout is never retried automatically.

## Features map

- [WhatsApp login and catalog search](features/whatsapp-login-search.md) — an allowlisted test sender can link an existing JioMart account by OTP and request product listings. With the commerce flags on, chosen products go straight into the real cart (the only basket), which the sender can edit or empty, review and order with a confirmed button. Payment is on JioPay through a server-opened payment session, or cash on delivery, and the bot follows the order, with invoices and a flag-gated cancel. Every reply ends with next-step buttons. Recipient search delivery, live cart edits and one paid online order are confirmed; the 19 October changes, cash on delivery and production deployment remain unverified.

## Module map

### Core module

- [Login and search POC](modules/login-search-poc.md) — owns webhook ingress, conversation control, encrypted local state, catalog matching, the external adapters, the checkout, orders and order-watch flows, the session keep-alive and the order-summary PDF.

### Documentation module

- [Project documents](modules/project-documents.md) — owns requirements, evidence and dated status; it must distinguish observations from code and proposals.

## Architecture

- [Implemented POC interactions](architecture/module-interactions.md)
- [Local POC data model](architecture/data-model.md)

## External systems

| System | Implementation evidence and limit |
| --- | --- |
| Meta Cloud API | `whatsapp.py` and `app.py` implement outbound text, challenge and HMAC verification. Callback/subscription setup and app publication succeeded; OTP login and inbound search succeeded, and the user confirmed delivered replies after business verification; billing remains separate. |
| OpenAI Responses | `agent.py` uses `AsyncOpenAI.responses.parse`; several bounded calls with the configured private key/model were observed locally. It cannot mutate accounts or carts. |
| JioMart consumer endpoints | `jiomart.py` implements OTP/address/search plus bounded history/cart reads and validates an allowlisted redirect. Live search, size lookup, and an authenticated invalid-cart snapshot were observed; stable pagination, seller routing, cart mutations and orders remain pending. Since the evening of 18 October it also implements flag-gated cart add/update/remove, select-address, payment options, cart meta and v2 checkout, plus order list/detail/shipment tracking. Late on 18 October, live cart writes, an online checkout and reads of that new order worked. Account requests follow one JioMart-host 307/308 redirect (`/ext/…` paths go to `www.jiomart.com`), order reads fall back to the `www.jiomart.com/api` gateway, and a 401/403 triggers one token refresh with `{access_token}`. Live logs on 18–19 October showed that refresh failing (HTTP 400) about hourly; the 19 October build refreshes before expiry, falls back to a cookie-only refresh and runs a background keep-alive, all unverified live. Flag-gated shipment cancellation and invoice download use documented Storefront routes that have not been called live. Older orders are not returned because JioMart's website reads them from a legacy cookie-authenticated "UL OMS" extension the bot cannot use. |
| JioPay hosted payment | Earlier: `/pay/{token}` posted JioMart's `base64_html` form from the browser. Now, for JIOPP/JioOnePay, `/pay/{token}` makes the bot open JioMart's `payment_session` with the account's cookie. The browser receives the provider form or redirect (live chain `www.jiomart.com` → `store1.jiomartjcp.com` → `payments.jio.com`). One UPI payment succeeded live on 18 October; callback behavior is unverified. |
| ngrok agent API | `app.py` reads `http://127.0.0.1:4040/api/tunnels` for the HTTPS tunnel serving port 8080 when `PUBLIC_BASE_URL` is empty. The free plan shows a browser warning page before `/pay`. |
| SQLite | `storage.py` owns local POC state. It is not the PRD's proposed production data store. |

## Task navigation

| Task | Read first |
| --- | --- |
| Change webhook, queue, state, OTP or search flow | [login/search module](modules/login-search-poc.md) and [architecture](architecture/module-interactions.md) |
| Change AI matching | `smartbazaar/agent.py`, `smartbazaar/catalog.py`, [feature context](features/whatsapp-login-search.md), and the experience plan's matching section |
| Continue JioMart validation | [status](../docs/project-status.md), [integration plan](../docs/jiomart-account-integration-plan.md), and `smartbazaar/jiomart.py` |
| Continue Meta setup | [PRD section 11](../docs/technical-prd.md#11-whatsapp-setup-actual-progress-and-remaining-work), [status](../docs/project-status.md), and `smartbazaar/app.py` |
| Add cart/order work | PRD sections 7–8, 13–14 and 23 first, then `smartbazaar/checkout.py`, [architecture](../docs/agent-architecture.md) and [ADR 4](../docs/architecture/decisions/0004-real-cart-checkout-and-order-updates.md). Mutations are flag-gated; preserve the single-use button, snapshot, persisted attempt and unknown-outcome rules |
| Change order history, tracking, reorder or order pushes | `smartbazaar/orders.py`, `smartbazaar/order_watch.py`, [data model](architecture/data-model.md) and [operations](../docs/operations-reliability.md) |
| Change payment links or pay pages | `smartbazaar/app.py` (`/pay`, `/paid`, public origin), `checkout.py` (`_submit`, `_pay_message`), `jiomart.py` (`_payment_link`, `open_payment_session`, `checkout` payment fields), `storage.py` (`supersede_unpaid`) and [webhook safety](patterns/webhook-state-safety.md) |
| Change reply wording or composition | `smartbazaar/composer.py`, `agent.py` (`compose_reply`), `conversation.py` (`_composable`, `Written`), [ADR 5](../docs/architecture/decisions/0005-agentic-reply-composition.md) and [reply formats](../docs/whatsapp-reply-formats.md) |
| Change the cart card, choice pictures, list menus, documents or interactive payloads | `smartbazaar/checkout.py` (`_cart_card`, `_breakup_lines`), `cart_image.py` (`build_collage`), `whatsapp.py` (`_cart_card`, `_choice_card`, `_document`), `tools.py` (`next_choice`), `receipt.py` and [reply formats](../docs/whatsapp-reply-formats.md) |
| Change how products are added, product questions or “add it” | `smartbazaar/tools.py` (`_add`, `_choose`, `_skip`, `focus`), `checkout.py` (`add_products`, `view_context`), `memory.py` (`PENDING_TTL`, `roll_session`), `conversation.py` (`VIEWED_ADDS`, command map) and [ADR 6](../docs/architecture/decisions/0006-jiomart-cart-is-the-only-basket.md) |
| Change substitutes for unavailable products | `smartbazaar/tools.py` (`_unavailable`, `_substitutes`, `cart_substitutes`, `next_choice`, `_choose`), `catalog.py` (`product_is_substitute`, `same_category`), `checkout.py` (`substitutes`, `unavailable_lines`, `remove_line`, the `find_substitutes` button), `conversation.py` (`SUBSTITUTE_COMMANDS`) and [reply formats](../docs/whatsapp-reply-formats.md) |
| Change next-step buttons or button lifetimes | `smartbazaar/conversation.py` (`_with_buttons`, `_next_steps`, `LABELS`, `BUTTON_TTL`), `checkout.py` (`CONFIRM_TTL`), `orders.py` (`ACTION_TTL`) and [webhook safety](patterns/webhook-state-safety.md) |
| Change JioMart session refresh or keep-alive | `smartbazaar/jiomart.py` (`_request`, `_refresh_session`, `keep_alive`), `session_keepalive.py`, `storage.py` (`active_sessions`, `save(touch=False)`), `config.py` and [operations](../docs/operations-reliability.md) |
| Change cancellation, invoices or the order-summary PDF | `smartbazaar/orders.py` (`cancel`, `_confirm_cancel`, `invoice`, `invoice_document`), `jiomart.py` (`cancellation_reasons`, `cancel_shipment`, `invoice_pdf`), `receipt.py`, `order_watch.py` and the [API map](../docs/agent-api-map.md) |

## Known limits

The local POC accepts text, product-card taps (Add to cart, View details, Next option; browse-only before 19 October), and authenticated Meta audio/image input. Media support is locally tested; real voice/photo quality acceptance remains pending. Its inbound queue is memory-only, so a restart requires the sender to resend; encrypted outbox messages persist and are retried. Listing `sellable` status and prices are not quantity-stock or cart guarantees. Search failure or a partial page must not be described as unavailable. The app is published and business details are complete, and verification is now complete. Actual user-initiated login/search and recipient replies work; earlier 131031 failures occurred before verification. WABA business-initiated billing remains separately flagged.

Commerce limits: earlier this said no live cart write, order or payment had been verified. Late on 18 October cart writes and one paid online order were verified; cash on delivery, shipment-stage pushes and the corrected token refresh are not. Reconciliation of an unknown checkout is a same-total/time heuristic. Order pushes go out only within 23.5 hours of the customer's last message; there are no approved templates. Payment links depend on the dynamic free ngrok URL and its browser warning page, and a JioMart payment session opens only once. Refunds are not implemented; cancellation is flag-gated and unverified (19 October). ORDER STATUS now re-sends the payment link for an order awaiting payment. HISTORY lists orders and also lifts FORGET's purchase-history import suppression. The earlier reply copy saying CHECKOUT adds the list to the cart is gone with the list.

## Temporary OpenWA channel

The user selected OpenWA for the internal weekend demo. `smartbazaar/openwa.py` implements outgoing text; `app.py` accepts signed `/webhooks/openwa` events only when `CHANNEL_PROVIDER=openwa`. The Meta provider remains the default. OpenWA requires its API base URL/key, generated session UUID and HMAC secret in private configuration; account linking is done by scanning a QR using a dedicated bot account. Tester identity and events are provider/session-scoped; raw unresolved LIDs, groups and outgoing messages do not enter the conversation. The live deployment uses a separate database and requires fresh JioMart authentication. See [runbook](../docs/openwa-poc.md) for current acceptance status.

## Current transport update

The user retired OpenWA after Meta verification succeeded on 18 October. OpenWA is logged out and stopped; the runtime uses Meta with its original database. Graph confirms business verification, with BUSINESS/APP AVAILABLE and only WABA payment flag 141006 remaining. A post-verification text send was accepted; recipient search delivery is now confirmed by the user. Earlier OpenWA runtime/verification-blocker descriptions are historical.

## Product image/button replies

Meta search replies now support real catalog images and browse-only buttons through the existing conversation, catalog, encrypted outbox and WhatsApp adapter. Button IDs resolve deterministically against expiring owner-bound state. See [reply formats](../docs/whatsapp-reply-formats.md). Pre-approved notification templates and cart actions are not included.

Live card proof: the authenticated milk card was delivered by Meta on 18 October, with no delivery error. The owner later confirmed product image/button interactions. See current project status for the latest suite and agent-build evidence.

## Shopping agent and memory build

Read [agent architecture](../docs/agent-architecture.md) and [API map](../docs/agent-api-map.md) for the latest implementation and remaining commerce gates. `agent.py` plans typed actions; `tools.py` dispatches scoped shopping-list and memory tools; `memory.py` rotates bounded conversational context daily in Asia/Kolkata or after four idle hours. The encrypted `shopping_memory` table is separate from credential TTL and keyed by channel owner plus application/account digest. No full transcript or vector database is stored.

Preferences resolve unspecified products; current explicit brand/pack requests win. Purchase evidence must be recent and delivered; empty history is unresolved. `media.py` handles authenticated, bounded Meta attachments using Whisper and vision, and `shopping_only` prevents administrative execution through media. Real cart reads verify the authoritative session, while writes remain disabled and are not exposed to the planner. These additions expand the existing Python application module; they are not deployed microservices or a standalone MCP server.

The 18 October build passed 90 tests, Ruff, and context validation, and was activated on the existing local Meta service. Post-start health reports ready with `shopping_draft_memory_media` and disabled cart-write/checkout flags. A live cart read returned two lines marked invalid; the tested available curd candidate's documented seller request returned HTTP 400. History remains empty/unresolved. Real voice/photo acceptance and order placement are not proven. Long replies are split without dropping list entries; recovery directs authenticated users to inspect LIST before retrying an interrupted request.

Later same-day replies failed despite healthy ingress: the temporary Meta token expired at 15:30 IST (HTTP 401, OAuth 190/463). Renewal passed a Graph read, the service restarted, and the owner confirmed receiving a fresh reply. Health now exposes passive process-local outbound observations and degrades on observed HTTP 401. It starts `not_observed` and becomes `available` after provider acceptance, which alone does not establish delivery. This repair passed 91 tests and Ruff. See project status before asserting live channel readiness.

## Credential reliability update

The current Meta credential is a dedicated Employee system-user token, generated with owner approval and verified through Graph as valid SYSTEM_USER with both expiry fields zero. It is privately configured and the service has returned green readiness. It can still be revoked; laptop/network availability is independent. `channel_health.py` performs startup/five-minute credential, phone and messaging-account reads. `/readyz` returns 503 for expiry warnings, stale/failed checks, dead workers or observed delivery failures; `/healthz` remains a liveness endpoint. Recent auth-rejected replies enter bounded encrypted `auth_wait`. Read [operations](../docs/operations-reliability.md) and latest project status before claiming supervision or end-to-end delivery.

The first system-token send exposed a missing permission on Meta's separate messaging account. Owner-approved access resolved it; the probe now checks that account as well. App and dynamic tunnel LaunchAgents are active from `~/Library/Application Support/SmartBazaar`, using one migrated encrypted database. Both process restart tests passed, and the owner confirmed a fresh LIST reply after them. Source edits require staging into that runtime. See the operations runbook for safe updates and host limitations. Final verification: 126 tests and Ruff passed; structural context validation passed.

## Natural conversation and reply selection

Guests and connected users can receive AI-authored greetings, grocery guidance and capability help. The planner emits bounded natural copy plus a presentation choice; scoped tools still own all state changes and factual receipts. Guests never execute commerce tools. `memory.py` stores at most eight sanitized recent messages (480 characters each) in encrypted session state, rotating daily or after four idle hours and clearing at account linking, LOGOUT, FORGET or NEW SESSION. This is separate from durable account-scoped lists/preferences. Login/OTP/address fields never enter this dialogue.

`quick_reply` messages provide image-free buttons. The model requests only named supported actions; the server issues opaque single-use owner/account/location-bound IDs with a 15-minute expiry. A Connect JioMart tap starts the deterministic login path; other buttons navigate help/list/history/search guidance. Search retains verified catalog image cards, and an explicit text presentation suppresses images. See [reply formats](../docs/whatsapp-reply-formats.md).

## Real cart, checkout and order updates — 18 October 2026

This supersedes the earlier statements above that cart writes are not exposed to the planner and that checkout is unavailable. `checkout.py` (`CheckoutFlow`) copies resolved list lines into the account's real JioMart cart on CHECKOUT, selects the saved address and reviews JioMart's own totals. It offers single-use, 10-minute payment buttons (`sbco_`) bound to a SHA-256 snapshot of the reviewed cart, persists an `order_attempts` row before the shielded checkout POST, and classifies the outcome. Online orders get a 45-minute `payment_links` token behind `/pay/{token}`. CART and the `cart_remove`/`cart_quantity` actions edit the real cart. `orders.py` (`OrdersFlow`) serves ORDERS/HISTORY, ORDER n, TRACK n and REORDER n (list only) with `sbor_` buttons. `order_watch.py` (`OrderWatcher`) pushes each order stage once within WhatsApp's window, without extending the sign-in.

Implemented and tested: 231 tests and Ruff pass. Live: a real CART read, several redeployments with green readiness, flags on in the runtime since 21:47 IST, and the pay route reachable through ngrok. Unverified: cart writes in the JioMart app, cart edits, payment option names, any order, JioPay acceptance, callback behavior and watcher pushes. Read the [module](modules/login-search-poc.md), [data model](architecture/data-model.md), [architecture](../docs/agent-architecture.md) and [status](../docs/project-status.md) before changing this path.

## Late evening 18 October — cart as the basket, server-side payment, agentic replies

This supersedes the section above where they differ: CHECKOUT no longer copies the list, “Remove other items” is gone, and most “unverified” statements are now resolved. Live on the real account: CART reads, adds, removals, quantity changes and EMPTY CART. One online order (JM6AD4FE…) was created from chat, paid by UPI through a server-opened payment session, confirmed by the watcher's "✅ Payment received" push, listed by ORDERS and visible in the JioMart app. Composed replies passed the fact check, and the picture cart card was delivered. Implemented and tested (261 tests):

- **Basket semantics.** CHECKOUT reviews the cart without adding list items. With cart writes on, add/replace/choose move only that request's resolved lines (`CheckoutFlow.sync_list(only=…)`). Products already in the cart are left as they are, and duplicates use the larger quantity. ADD MY LIST (`add_list`), CLEAR LIST (`clear_list`) and EMPTY CART (`empty_cart`) are commands and planner actions. "Remove other items" is retired.
- **Payment.** `JIOMART_ONLINE_PAYMENT` gates JIOPP. Checkout's merchant IDs are stored behind `/pay/{token}`, where `JioMartClient.open_payment_session` posts them with the account cookie and returns the provider form or redirect. A newer chat order marks older unpaid attempts `superseded`. Unpaid orders JioMart will not return stop being watched after 3 hours.
- **Composer.** `composer.py` and `ShoppingAgent.compose_reply` write one reply from tool drafts. `faithful` keeps amounts, order IDs and "N × Product" lines exact, or the draft is sent. See [ADR 5](../docs/architecture/decisions/0005-agentic-reply-composition.md).
- **Presentation.** `sbch_` list menus for product choices; the `cart_card` picture (Pillow, JioMart CDN only) with Checkout/Empty cart or payment buttons; an always-balanced charge breakup; and a read receipt plus typing indicator.
- **Operations.** Deploy only when no event is processing and no outbox row is pending or sending. Install new dependencies into the runtime venv with `uv export` + `uv pip install --python <runtime venv>`. See [operations](../docs/operations-reliability.md).

Read [status](../docs/project-status.md), [ADR 4 amendment](../docs/architecture/decisions/0004-real-cart-checkout-and-order-updates.md) and the [polish backlog](../docs/product-polish-backlog.md) before changing these paths.

## 19 October 2026 — one basket, next-step buttons, session keep-alive, cancel and invoices

Implemented and locally tested: 307 tests and Ruff pass. **Not deployed to the supervised runtime; none of these paths is verified on WhatsApp or against JioMart.** This supersedes the sections above where they differ, in particular the shopping list, ADD MY LIST/CLEAR LIST, list versions on checkout buttons, the 15-minute quick-action and 10-minute checkout-button lifetimes, the 24-hour session lifetime and the "Pending choices" quick action. Read [ADR 6](../docs/architecture/decisions/0006-jiomart-cart-is-the-only-basket.md), the [module](modules/login-search-poc.md) and [status](../docs/project-status.md) before changing these paths.

- **Single basket.** `ShoppingTools(commerce, agent, cart)` adds confident matches straight into the JioMart cart through `CheckoutFlow.add_products` (already-present products left as they are, duplicates at the larger quantity, quantity caps, stale references re-found by exact catalog ID). Ambiguous products become `memory["pending"]` questions: at most three options, asked one at a time, pincode-bound, 6-hour lifetime, replaced by any newer add request and dropped on location change, CANCEL, "clear list"/"skip all", NEW SESSION or expiry. `roll_session` deletes `draft`/`choices` left by older builds. LIST shows the cart. Retired planner actions: `add_list`, `clear_list`, `replace`, `review`, `remove`, `set_quantity`. New: `skip`, `cancel_order`, `invoice`, and the intent fields `viewed` (product just shown, `state["focus"]`) and `checkout_after`. Planner context adds `pending`, `choices`, `viewed` and `cart` (last shown cart lines, `state["cart_view"]`, 6 hours). `memory["cart_products"]` (≤40) backs "remember cart line 2 for curd"; REORDER adds into the cart; `clear_ordered_lines` only records the order in `memory["orders"]`.
- **Buttons.** `Conversation._with_buttons`/`_next_steps` end every reply with two or three context-aware shortcuts. `sbqa_`/`sbpc_` buttons last 24 hours (≤40 kept), `sbco_` 30 minutes, `sbor_` 24 hours; all single-use and owner/account-bound, and payment buttons still compare the reviewed-cart snapshot. Product cards: Add to cart / View details (with the `https://www.jiomart.com/product/<slug>` link) / Next option or Search again. Choices with images are a numbered `choice_card` picture with tap buttons and Skip; otherwise a tap menu with a "Skip this item" row.
- **Voice.** Transcripts take the normal text path (`handle(..., voice=True)`), so voice reaches every command, including for guests; replies start with the transcript. Photos stay shopping-only; no media is read during an OTP challenge.
- **Sessions.** `JioMartClient._request` refreshes proactively when the access JWT has ≤5 minutes left; `_refresh_session` tries the documented body, then cookie-only on HTTP 400; `keep_alive` and `session_keepalive.py` renew stored sessions every `SESSION_KEEPALIVE_INTERVAL_SECONDS` under the owner lock with `touch=False`; `SESSION_TTL_SECONDS` defaults to 30 days. Live logs on 18–19 October motivated this (hourly `session_refresh_failed status=400`, then 401). The owner must LOGIN once after deploy.
- **Orders.** ORDER n is a card with Track order / Invoice / Cancel order (or Reorder). CANCEL ORDER n needs `JIOMART_ORDER_CANCEL` and a confirmation tap; unknown outcomes are never retried. INVOICE n sends JioMart's invoice PDF (download allowlist dot-boundary matched against JioMart/Fynd platform domains, HTTPS, no redirects, ≤5 MB, PDF only). `receipt.py` builds an order-summary PDF for the cash-on-delivery placement reply and the online payment push; the Delivered push attaches the invoice when available. PDFs travel in the encrypted outbox as a `document` message type.
- **Known gap found during this documentation pass.** `JioMartClient.open_payment_session` checks redirect hops with a plain suffix match on `jiomart.com`/`jiomartjcp.com`, not a dot-boundary match, so the "cookie only goes to JioMart hosts" statements above were stronger than the code. Fixed later on 19 October: hops now use the same dot-boundary match (`host == h or host.endswith("." + h)`), and `tests/test_commerce_tools.py` checks that a lookalike host is never requested. See the [API map](../docs/agent-api-map.md).

## Render hosting — 19 October 2026

Later live observation during tester onboarding: Render is now serving the bot and its public health endpoint reports ready with delivered replies; the Mac LaunchAgent plists are absent. A new tester is saved in Render's allowlist with **Save only**, pending deployment and Meta recipient OTP verification. The free service still has ephemeral SQLite storage, so deployment was deferred to avoid silently losing customer sessions and order-attempt records. See the dated tester/hosting observation in [status](../docs/project-status.md). Earlier claims below that Render deployment is pending are historical.

`Dockerfile`, `scripts/docker-entrypoint.sh`, `.dockerignore` and `render.yaml` package the bot for Render as one web service on the free plan (no disk: the SQLite database at `/var/data` resets on restart or deploy; a paid disk is commented in `render.yaml`). `.github/workflows/keep-awake.yml` pings `/healthz` every 5 minutes so the free service doesn't sleep, once the `RENDER_HEALTH_URL` repository variable is set. It uses health check `/healthz`; `/readyz` would fail deploys while channel checks are degraded. When `PUBLIC_BASE_URL` is empty, `Settings.render_external_url` (`RENDER_EXTERNAL_URL`) is the public origin for pay links. Keep one instance. The image was verified locally; Render deployment, JioMart reachability from Singapore and the Meta webhook switch are pending. See [operations](../docs/operations-reliability.md#render-hosting--19-october-2026).

## Purchase-history personalisation — 19 October 2026

`ShoppingTools.refresh_history` builds purchase evidence from `list_orders`/`get_order_detail` (the reads behind ORDERS) instead of the empty history endpoint, which is now only a fallback. Each product counts once per non-cancelled order, most bought first. `tools._add` adds the usual product directly ("(your usual)") when it is still listed and passes `product_matches_exact`. `_usual_first` marks products bought before at the top of a choice. `checkout.clear_ordered_lines` records a chat order's products as evidence immediately, via `memory["cart_products"]`. FORGET's history suppression still applies.

## Store-manager voice replies — 19 October 2026

The problem statement asks for replies in the store manager's voice; the owner re-scoped it in on 19 October. `smartbazaar/tts.py` (`FishAudioTTS`) calls Fish Audio's free `s2.1-pro-free` model (`POST https://api.fish.audio/v1/tts`) with the cloned voice `FISH_AUDIO_VOICE_ID` (`reference_id`). It requests `opus` and, when the bytes are an Ogg container, uploads them as `audio/ogg`; otherwise it falls back to `mp3` sent as `audio/mpeg`. `spoken_text` turns a reply into speech: one sentence per line, separators become pauses, ₹ amounts become "rupees", links and emoji are dropped, at most about 350 characters. When `VOICE_REPLIES=voice` (default; `always` and `off` also exist) and a key is set, `Conversation.handle(voice=True)` appends `{"type": "voice_note", "text": …}` after the text reply, so buttons never wait for synthesis. `WhatsApp.send` synthesises at send time and sends `{"type": "audio", "audio": {"id": …, "voice": true}}`. A failure is a non-retryable `tts_failed` with no text fallback and does not mark WhatsApp unavailable. Live check on 19 October with the owner's key and cloned voice: Ogg/Opus mono 48 kHz, about 4 seconds per reply, correct upload and payload against a mocked Meta API. Not yet seen on a real phone.

## JioMart session refresh fixed — 19 October 2026

This supersedes the earlier refresh descriptions above. JioMart keeps the refresh token in `R.session`, with `Path=/api/service/application/user/authentication` on `.jiomartjcp.com`; the access JWT is `f.session`. The old refresh URL (`/service/…/token/refresh` on `api.jiomartjcp.com`) was outside that path, so the refresh cookie was never sent and every refresh got HTTP 400. `JioMartClient._refresh_session` now makes one POST with no body to `/api/service/application/user/authentication/v1.0/token/refresh` with the cookie jar. Proven live: HTTP 200 with a new 1-hour `f.session`, even 17 hours after the access token expired. After a failed refresh the same token is not retried for 15 minutes (4xx), or 60 seconds (transport error, 429 or 5xx); the log line is `session_refresh_skipped reason=backoff`. `R.session`'s own lifetime and rotation are unknown. The contract is in the [API map](../docs/agent-api-map.md).

## Personalised substitutions — 19 October 2026

Implemented and locally tested (353 tests, Ruff); not pushed, deployed or verified on WhatsApp. This supersedes the 19 October section above where they differ (a blocked review's buttons, and the “no sellable exact match” question). When `_add` finds no sellable exact match, `ShoppingTools._unavailable` names what can't be had. That is a listing JioMart marks `sellable: false`, the customer's usual, or nothing, and a search miss is “couldn’t find”, never “unavailable”. `_substitutes` then picks up to three options that pass `catalog.product_is_substitute`. That gate is the relevance check plus any requested formulation, a different product, and the same kind: JioMart's category when both listings have one, otherwise the product noun. Bought-before products come first (one name look-up when the search didn't return one), then the same category, then model alternatives. The options become an ordinary `memory["pending"]` question carrying `substitute: {note}`, which `next_choice` renders as “X is unavailable. Would one of these do?”. Nothing is added until a tap. At checkout, `CheckoutFlow.substitutes` (set by `Conversation` to `ShoppingTools.cart_substitutes`) adds an `sbco_` **Find substitutes** button next to **Remove unavailable**. It asks about each unavailable line (at most three) with `substitute: {note, replaces: <cart line key>}`. `_choose` adds the tapped product and only then calls `CheckoutFlow.remove_line` for that one line; Skip leaves the cart unchanged. Typed “find substitutes” does the same. Guards are unchanged: single-use owner-bound buttons, newer requests replace open questions, and the JioMart cart is the only basket.
