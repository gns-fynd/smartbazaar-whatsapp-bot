# Bounded external adapters

## Overview

The POC keeps OpenAI, JioMart and Meta calls behind small adapters that accept limited data, validate outputs, and expose safe errors. `ShoppingAgent` cannot execute tools or mutate commerce state. `JioMartClient` owns isolated cookies and exposes OTP/address/search, bounded history/cart reads, and disabled-by-default cart-write adapters. Since 18 October it also exposes flag-gated select-address, payment-option and checkout calls, plus order list/detail/tracking reads. `WhatsApp` owns text delivery (plus quick replies, catalog cards and `cta_url` link buttons). This division limits a model or catalog title from becoming authority over accounts, URLs, cart actions or secrets.

The pattern matters because upstream input and output are untrusted. Product titles can contain instruction-like text; redirect locations can point elsewhere; model structured output can invent identifiers; HTTP errors can embed sensitive request data. The adapter boundary validates each of these rather than passing them through to the conversation controller.

## Where used

- [Login and search POC](../modules/login-search-poc.md): `agent.py` sends bounded safe context to `responses.parse`, requests Pydantic output, uses `store=False`, and turns failure into `AgentError` without logging a payload.
- [Login and search POC](../modules/login-search-poc.md): `jiomart.py` manages the OTP/session/search requests and restricts catalog redirects to the observed allowed host/path.
- [Login and search POC](../modules/login-search-poc.md): `catalog.py` normalizes only display fields and removes model-selected IDs that are absent, irrelevant, or fail explicit identity constraints.

## Rules

Planner inputs include customer shopping text, recent product queries, validated pincode, and bounded product/list/preference fields. Media extraction separately sends ephemeral media bytes to OpenAI after authentication. They must exclude OTPs, access tokens, cookies, full addresses and customer identifiers. Candidate lists are capped before ranking. Model output is advisory: IDs must belong to the supplied candidate set and exact matches must pass deterministic category, brand/formulation, and explicit-pack checks.

JioMart redirects must be resolved then checked for HTTPS, allowed host and expected path before a follow-up request. A broad or partial result never proves that an item is unavailable. Model or transport failure must be communicated as uncertainty, not as successful retrieval or a false stock claim.

Validate each authentication endpoint against its own response schema. A common `success` flag is not present on every valid OTP or session response. Require the endpoint’s positive evidence, reject explicit errors, and check identity against the final session user before persisting authentication. Carry the cookie issued by OTP verification directly into session validation. Token refresh is an expired-token operation with its own body and trusted-user-context contract; do not insert a bodyless refresh into a new-login sequence. The corrected path has authenticated a real account and read a saved default address.

Diagnostic logs may contain only bounded adapter-owned stage names, numeric HTTP status, and bounded internal codes. They must exclude URLs, provider bodies, exception text, OTPs, phone numbers, cookies, access tokens, and refresh tokens. Response-contract mismatches at HTTP 200 need a distinct internal code so they can be separated from transport and HTTP failures without retaining the response.

## Gotchas

Diet Coke, Coke Zero, regular Coke and Pepsi are distinct formulations/brands for exact matching. A keychain, syrup, or energy drink is not a beverage alternative merely because the catalog title contains product words. Conversely, a shopper who explicitly requests a keychain must not be blocked by a blanket merchandise ban. These rules are enforced after model ranking because product data is untrusted.

Use the configured model rather than hardcoding one. The present default is `gpt-6.1-sol`; several bounded local calls and live search/ranking checks were observed, while channel behavior, cart and order capabilities remain independently pending.

## Related patterns

- [Webhook and local-state safety](webhook-state-safety.md) describes safe entry and reply delivery around these adapters.

The temporary OpenWA adapter uses a configured loopback API base, an API key scoped to the test session/chat allowlist, bounded text and HTTP timeout. It accepts only a successful response with a message identifier and emits sanitized DeliveryError codes. A send accepted by the gateway is not proof that WhatsApp delivered it. OpenWA credentials and session data remain local; its transport is replaceable without changing commerce or AI behavior.

Catalog product cards accept HTTPS image URLs only from the observed JioMart catalog host, use actual catalog pictures, and cap Meta body/footer/button lengths. The transport constructs the interactive envelope itself; model output cannot supply arbitrary buttons, URLs or actions. Missing media falls back to ordinary search text. A synchronous card send failure can downgrade its outbox payload to text; a later asynchronous delivery failure is still only recorded.

## Media and history boundaries

`media.py` reconciles signed-webhook MIME, provider metadata and actual download content type; follows no redirects; permits only specific Meta media hosts; and limits streams before passing bytes to OpenAI. Ogg/Opus WhatsApp audio uses an OGG filename; unsupported raw AAC/Opus is rejected. Whisper audio is ephemeral; image Responses requests use `store=False`. A second deterministic `shopping_only` action allowlist protects against extracted administrative commands.

`get_purchase_history` caps orders, detail hydration and normalized products, validates pagination/status/date, and carries minimal provenance. A failed optional history request does not invalidate a working search session. Empty/unusable history is unresolved. `get_cart` verifies authoritative account identity before reading a possibly guest-accessible endpoint. Cart writes require the constructor gate and are not reachable from planner tools. Schema evidence and live evidence remain distinct in [API map](../../docs/agent-api-map.md).

## Channel readiness probe

`channel_health.py` performs only bounded Graph reads: token metadata using app authorization, then the configured phone and messaging account with the channel token. It rejects malformed schemas, app mismatch, absent messaging scope and expired credentials. Both expiry fields are considered; zero means no scheduled expiry. Safe results contain only status/reason enums and timestamps. The app runs this at startup and every five minutes, with no HTTP-triggered probes or customer-message sends. Token/URL-bearing raw exceptions and responses must never enter logs. Health-check success is narrower than recipient delivery or commerce readiness.

The planner also emits natural `reply`, `presentation` and allowlisted `quick_actions` fields. Context allows a boolean connection state and eight user/assistant turns of at most 480 characters, alongside curated task fields. The server fixes cart-write/checkout capabilities to false. Prior exchanges and current catalog/user text remain untrusted data. Guest planner outputs can only produce guidance, not tool execution. Structured tool results supply factual receipts; generated prose cannot replace them. (Since 18 October the capability flags come from server configuration, `JIOMART_CART_WRITES`/`JIOMART_CHECKOUT`, rather than a fixed false. They still cannot be changed by model input.)

## Commerce mutation adapters — 18 October 2026

Where used: `jiomart.py` (adapter), `checkout.py`, `orders.py` and `order_watch.py` (callers), `whatsapp.py` (`cta_url`) and `app.py` (pay page, ngrok origin).

- **Two independent gates.** `JioMartClient` raises `CommerceError("unavailable", stage=…, diagnostic_code="writes_disabled"/"checkout_disabled")` unless its constructor flags are set; checkout also requires cart writes. `CheckoutFlow` checks its own flags before calling. Planner capability flags only describe what is available.
- **Typed bodies only.** `_typed_cart_items` builds 1–20 entries from server-held identifiers: `item_id` (digits become integers), a plain-text `item_size`, `quantity` 1–99 (0 for removal), and `meta.vertical_code` (letters/underscore, else `default`) with `compute_delivery_fee: true`. Adds need the listing's `seller_id`; updates/removals need JioMart's line `identifier` and `item_index` from a fresh cart read. Anything else raises `ValueError` before any request. No model-supplied body, URL or ID reaches JioMart.
- **Identity before every cart/checkout call.** `_verify_cart_session` re-reads `/session` and requires the stored account reference (and any explicit application) to match before reading or changing the cart, selecting an address or checking out.
- **Definite vs unknown.** `success:false` on a cart write is `rejected`. On checkout it is `rejected` only without an order number; with one it is `unknown`. Everything before the checkout POST is wrapped as `rejected`/`changed`, so callers can treat it as “nothing placed”. HTTP 401/403 is `auth_expired`, 429 `rate_limited`, 5xx/transport `unavailable`, and the checkout POST's HTTP 400 `rejected`.
- **Bounded upstream hints.** `CommerceError.upstream` keeps at most 120 characters of a provider error code/message, with runs of four or more digits replaced and symbols stripped. It is populated only for `cart_write`, `checkout`, `payment`, `select_address` and `product_sellers` stages, never `*_session` or login stages. Other diagnostics remain stage/status/code only. `payment_modes` logs option labels and the payment host; `orders_shape` logs field names once; neither logs values.
- **Payment endpoint allowlist.** `_payment_link` accepts only an HTTPS URL of at most 500 characters, with no port or userinfo, on `jio.com`, `jiopay.com`, `jiopay.in`, `jiomart.com`, `jiomartjcp.com` or `jiopaymentsbank.com` (or their subdomains). The checkout `base64_html` must match a base64 pattern and be at most 300,000 characters. `WhatsApp._cta_url_payload` independently rejects non-HTTPS or credential-bearing URLs and over-length labels/bodies.
- **Public origin is local evidence only.** `public_origin` accepts a configured `https://` origin or a bare `*.ngrok(-free).app/dev/io` host. `ngrok_public_url` reads only `http://127.0.0.1:4040/api/tunnels` (2-second timeout, no redirects) and picks an HTTPS tunnel whose local address ends in `:8080`. Request headers are never consulted.
- **Order reads are display-safe summaries.** `_order_summary`/`_shipment_summary`/`_order_item` keep IDs only if they match the safe-ID pattern and labels only if they are short plain text; epochs are bounded to 2000–2100. Totals come from `breakup_values` or fully paid shipment sums. An empty order list is reported to the customer as “couldn’t find any orders”, not as proof of none; the legacy website-cookie history is out of reach.

Gotchas: request shapes that follow JioMart's public web code (reorder-style add, `status=1`, payment flows, `base64_html` post, cart meta) are static evidence, not live verification. Keep them labelled that way in the [API map](../../docs/agent-api-map.md). The `jiomart.py` docstrings still describe checkout as absent; trust the code and this section.

## Late-evening additions — 18 October 2026

Where used: `jiomart.py`, `composer.py`, `agent.py`, `cart_image.py`, `whatsapp.py` and `app.py` in the [module](../modules/login-search-poc.md).

- **One redirect, JioMart hosts only.** `_account_request` follows a single 307/308 with the same method and body. It rewrites `api.jiomartjcp.com/ext/…` to `www.jiomart.com`, accepts only `api.jiomartjcp.com`/`www.jiomart.com` under `/service/application/`, `/api/service/application/` or `/ext/`, and attaches the session cookie explicitly when the host changes. It refuses anything else (`unsafe_redirect`) and any second redirect (`redirect_loop`). Logs carry host and a sanitized path only.
- **Payment session stays on JioMart.** `open_payment_session` requires a vetted `api_link` and sends the account cookie only while every hop is on a `jiomart.com`/`jiomartjcp.com` host (at most six hops). The first off-JioMart location is returned to the browser instead of being fetched. HTML is parsed only for its first form (HTTPS action), and the cookie never reaches the customer's browser. Logs keep hop/status/host/content type and field names, never values.
- **Refresh is bounded and safe to retry.** Only HTTP 401/403 on non-login stages triggers one documented refresh (`{"access_token": …}`) and one retry. The token value is never logged; a failed refresh logs a status or reason with cookie names only. The corrected body is unverified live.
- **Images only from the catalog CDN.** `cart_image.fetch_images` accepts HTTPS `cdn1.jiomartjcp.com` URLs without userinfo (at most 2,048 characters). It fetches at most nine with a 6-second timeout, no redirects and 2 MB per image; failures become blank tiles. The JPEG is built in memory and uploaded once.
- **Model prose is checked, not trusted.** `Composer` treats model output like any other untrusted adapter output. `faithful` requires the exact ₹ amount set, every order ID and every “N × Product” pairing from the draft, and length limits apply. Any failure or exception sends the verified draft. The model never supplies buttons, IDs or URLs. `compose_reply` uses structured output, `store=False`, low reasoning effort and bounded inputs (480-character sanitized customer text, 6,000-character draft, 1,024-character button draft).

Gotcha: the faithfulness check is numeric and lexical. It cannot catch a changed non-numeric claim (for example availability wording), which the composer instructions forbid. See [ADR 5](../../docs/architecture/decisions/0005-agentic-reply-composition.md).

## Changes on 19 October 2026

Implemented and locally tested; not deployed and not exercised against JioMart. Where used: `jiomart.py`, `session_keepalive.py`, `orders.py`, `receipt.py`, `catalog.py`, `cart_image.py` and `whatsapp.py` in the [module](../modules/login-search-poc.md). See the [API map](../../docs/agent-api-map.md).

- **Refresh before expiry, at most once per request.** `_request` reads the unverified `exp` of the JWT-shaped access cookie and refreshes when 300 seconds or less remain; the after-401/403 refresh runs only if no refresh happened for that request. `_refresh_session` tries the documented `{access_token}` body, then once with no body on HTTP 400. Logs carry `variant`, status or a reason and cookie names, never values. The token's signature is not verified; `exp` is used only to decide timing.
- **Keep-alive is bounded.** `keep_alive(session, within=900)` makes no request when more than 15 minutes remain, otherwise one refresh (plus the application-token bootstrap when none is configured). It returns a boolean and writes cookies back to the stored session.
- **Cancellation is a gated mutation with an explicit unknown outcome.** `cancellation_reasons` and `cancel_shipment` raise `order_cancel_disabled` unless `enable_order_cancel` is set. IDs pass the safe-ID check; reason text is bounded (160 characters from JioMart, 300 sent). A failure envelope is `rejected`; transport errors and 5xx become `unknown`, which callers never retry. Only full-shipment cancellation exists.
- **Invoice downloads have their own allowlist.** `invoice_pdf` fetches the presigned link only over HTTPS on an exact or dot-separated subdomain match of `jiomart.com`, `jiomartjcp.com`, `fynd.com`, `pixelbin.io` or `jiocommerce.io`, with no redirects, a 5 MB cap on both `content-length` and streamed bytes, and a PDF content type or `%PDF` signature. This replaced an earlier draft's plain suffix match. Which host JioMart actually uses is unverified.
- **Locally built PDFs.** `receipt.py` renders only fields already in the confirmed attempt record, in memory, and the filename is reduced to safe characters. `WhatsApp._document` re-validates filename, signature and size before upload.
- **Product links from slugs only.** `catalog.product_link` builds `https://www.jiomart.com/product/<slug>` only when the catalog slug matches `[a-z0-9][a-z0-9-]{0,199}`; titles and model output never become URLs.
- **Choice pictures reuse the CDN rule.** `build_collage(..., labels=[…])` draws numbered tiles from the same `cdn1.jiomartjcp.com` fetches; if any image is missing the transport sends the text-based tap menu instead.
- **Known gap.** `open_payment_session` still decides whether a redirect hop stays on JioMart with `hostname.endswith(("jiomart.com", "jiomartjcp.com"))`, without a dot boundary, and sends the account cookie header on hops it follows. The earlier statement that the cookie goes only to JioMart hosts was therefore stronger than the code. Fixed later on 19 October: hops now use the same dot-boundary match (`host == h or host.endswith("." + h)`), and `tests/test_commerce_tools.py` checks that a lookalike host is never requested.
