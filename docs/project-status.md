# Project status

Updated **19 October 2026**. Context setup date: **16 October 2026**. Meta and local-POC observations below include the latest same-day setup checks. Development commerce observations below are from **17 October 2026**.

## 19 October 2026 — one basket, buttons on every reply, session keep-alive, cancel and invoices

This section supersedes the late-evening 18 October section below where they differ; that section remains as dated history. Everything listed under "implemented" is local source and tests only. **None of it is deployed to the supervised runtime yet, and none of the new paths has been exercised on WhatsApp.** The decision for the basket change is [ADR 6](architecture/decisions/0006-jiomart-cart-is-the-only-basket.md).

### Observed problems that drove this build

- **Lingering list lines (owner report).** Unresolved shopping-list lines such as "1 × bread — choose a product" stayed on the local list and resurfaced after unrelated requests ("Pending choices", "Which bread would you like?").
- **Hourly sign-out (live logs, 18–19 October).** The runtime log showed `session_refresh_failed status=400`, then HTTP 401, about every hour. The bot refreshed only after a 401, when the access token had already expired. JioMart's own website refreshes proactively, at most about 120 seconds before the token's `exp`. The live session has since expired, so **the owner must send LOGIN once after the next deploy.**

### Implemented and locally tested

- **The JioMart cart is the only basket.** The local shopping list (`memory["draft"]`) is retired. Confident matches go straight into the real cart through `CheckoutFlow.add_products`. Products already in the cart are left as they are, the same product asked for twice is one line at the larger quantity, quantity limits apply, and stale product references are re-found by exact catalog ID only. A product that needs a choice becomes a short-lived question (`memory["pending"]`, 6 hours, asked one at a time). Any newer add request replaces open questions; a location change, CANCEL, "clear list"/"skip all" and expiry also drop them. LIST and "my list" show the cart. ADD MY LIST and the `add_list`, `clear_list`, `replace`, `review`, `remove` and `set_quantity` planner actions are retired. New planner actions are `skip`, `cancel_order` and `invoice`, with new intent fields `viewed` ("add it" means the product just shown) and `checkout_after` ("add it and check out"). REORDER now adds straight into the cart. Checkout buttons no longer carry a list version; the reviewed-cart snapshot is the version.
- **Buttons on every reply.** Each reply ends with two or three context-aware buttons (for example View cart / Checkout / Keep shopping after an add, or My orders / Track my order / Keep shopping after an order reply). Guests get Connect JioMart / What can you do?; during OTP entry, Resend OTP / Cancel login. Product and quick-action buttons now last 24 hours (at most 40 kept), checkout buttons 30 minutes (was 10) and order buttons 24 hours. Every button is still single-use and owner/account-bound, and a payment button still requires the cart to match the reviewed snapshot.
- **Product cards and picture choices.** Product cards offer Add to cart / View details / Next option (or Search again). View details adds the product page link `https://www.jiomart.com/product/<slug>`. When up to three options all have catalog images, a choice is one numbered picture with a button per option (plus Skip); otherwise, or if the picture cannot be built, it is a tap menu with a "Skip this item" row.
- **Voice notes behave like typed text.** A transcript goes through the normal conversation path, so voice can do anything text can (orders, cart edits, help), including for guests. Replies start with "🎤 “<what was heard>”". Photos remain shopping-only, and no media is read during an OTP challenge.
- **Session keep-alive (fix for the hourly sign-out).** Every JioMart request refreshes first when the access token expires within 5 minutes. Refresh tries the documented `{access_token}` body and, on HTTP 400, once more with the cookie only, as the website does. A background loop (`session_keepalive.py`, every `SESSION_KEEPALIVE_INTERVAL_SECONDS`, default 600) renews each stored session used within the session lifetime, under the owner lock, without extending that lifetime. The local session lifetime (`SESSION_TTL_SECONDS`) is now 30 days after the customer's last message (was 24 hours).
- **Order cancellation (flag-gated).** `JIOMART_ORDER_CANCEL` (default false) enables CANCEL ORDER n and a Cancel order button on the order card. A confirmation card ("Yes, cancel it" / "Keep my order") precedes the call. The bot uses the documented Storefront shipment-reasons and `cancelled_customer` status routes with a neutral reason, and never retries an unknown outcome. JioMart's own website appears to cancel through different extensions, so the documented route may not work for JioMart (see the [API map](agent-api-map.md)).
- **Invoices and order summaries.** INVOICE n and the order card's Invoice button fetch JioMart's tax invoice through the documented shipment-invoice route and send it as a WhatsApp PDF document. The download must be HTTPS on an allowlisted JioMart/Fynd platform domain (dot-boundary match), unredirected, at most 5 MB and a PDF. The order watcher attaches the invoice to the "Delivered" push when available. Separately, `receipt.py` builds an in-memory one-page order-summary PDF ("not a tax invoice") that accompanies the cash-on-delivery "Order placed" reply and the online "✅ Payment received" push. The order card is now a button card: Track order / Invoice / Cancel order (or Reorder when it cannot be cancelled). The watcher's cancelled push is neutral ("Order X is cancelled").
- **Privacy notice** updated for the cart, orders, invoices, 30-day sessions with background renewal and the absence of a shopping list.

**Verification:** `uv run python -m pytest -q` → 307 passed (one existing Starlette/httpx deprecation warning); Ruff passed. Seven synthetic requests to the configured live OpenAI planner on 19 October were routed as intended: "Ok add to cart and checkout" with a product just shown (add the viewed product, then review); "cancel my last order" (cancel order 1); "send me the bill for my order" (invoice); "one more milk please" with milk on cart line 1 at quantity 1 (set line 1 to 2); "I don't want bread" with a bread question open (skip); a Hindi request for two Amul milk and one bread (add both); and "show my list" (cart). These were planner-only checks; no WhatsApp message or JioMart call was made for them.

### Not verified

- Deployment to the supervised runtime, and any of the new paths on WhatsApp.
- Proactive refresh, the cookie-only refresh fallback and the background keep-alive against JioMart. Whether a refreshed session lasts 30 days is unknown.
- Whether JioMart accepts the documented cancellation route; whether the invoice route returns a document and which host serves it (the allowlist is a guess based on the platform's usual CDNs).
- Rendering of the numbered choice picture, documents and the new button cards in WhatsApp.
- Everything still listed as unverified in the late-evening section below (cash on delivery, shipment-stage pushes, re-sent payment links, `callback_url` handling).

### Known gap found during the documentation pass (since fixed)

- `open_payment_session` follows payment redirects with the account cookie while the host ends in `jiomart.com` or `jiomartjcp.com`, using a plain suffix match rather than the dot-boundary match now used for invoice downloads and payment links. A lookalike host such as `evil-jiomart.com` would have passed. No such redirect was observed. Fixed later on 19 October: hops now use the same dot-boundary match (`host == h or host.endswith("." + h)`), and `tests/test_commerce_tools.py` checks that a lookalike host is never requested.

### Render hosting prepared (19 October, about 04:00 IST)

The owner chose Render to move the bot off the laptop. The repository now has a `Dockerfile`, entrypoint and `render.yaml` Blueprint (one starter service in Singapore with a persistent disk, health check `/healthz`, secrets entered in the dashboard). Pay links use `RENDER_EXTERNAL_URL` when `PUBLIC_BASE_URL` is empty. The image was built and booted locally with dummy settings and passed the checks listed in [operations](operations-reliability.md#render-hosting--19-october-2026). Not yet deployed to Render. Open risk: JioMart may refuse non-Indian IP addresses; check with a LOGIN right after the first deploy.

### Deployed and first live feedback (19 October, about 02:35 IST)

The owner ran the deploy from the source checkout (the supervised app restarted, all 18 source files matched, local and public `/readyz` returned 200; backup in the runtime's `work/rollback-smartbazaar-20261019`). In the first live checkout the owner reported that "make payment" seemed to show the cart again. Cause: the ₹2,849 total was above `CHECKOUT_MAX_TOTAL` (₹1,500), and the blocked review reused the cart card's layout with the reason at the bottom. The same card showed "Discount: ₹-650" plus an invented "Delivery & other charges: ₹650", because JioMart's subtotal already had the discount in it. Fixed in source (not yet redeployed): a blocked review now starts with "⚠️ Can’t place this order yet" and the reason. The breakup shows "Items: ₹2,849 (you save ₹650)" when the discount is already in the subtotal (an `mrp_total` match, or the totals only balancing without it). 309 tests pass. The owner then removed the limit: `CHECKOUT_MAX_TOTAL` now defaults to 0 (no limit) and remains available as an optional ceiling; every order still needs a payment tap on the exact reviewed total. 310 tests pass.

## Late evening 18 October 2026 — first live order, cart as the basket, agentic replies

*Partly superseded by the 19 October section above: the shopping list, ADD MY LIST, CLEAR LIST and the "Session refresh" item below describe the 18 October build.*

This section supersedes the "Not yet verified live" list and parts of the "Known gaps" list in the evening section below; those remain as dated history. Live evidence and product decisions for this round are recorded in the [product polish backlog](product-polish-backlog.md).

### Live-verified on 18 October (real account, real money)

- **First end-to-end order.** An online order (JM6AD4FE…) was created from chat and paid by UPI. The bot opened JioMart's payment session server-side, the customer's browser reached the JioPay page, and payment completed there.
- **Payment confirmation push.** The order watcher saw JioMart report the order paid/confirmed and pushed "✅ Payment received" within about 30 seconds.
- **Same-account visibility.** ORDERS lists the new order, and it is visible in the JioMart app.
- **Real cart.** CART reads, adds, removals, quantity changes and EMPTY CART all worked on the account's own JioMart cart.
- **Checkout review.** JioMart's own lines, fees and total were shown. The saved-address step was answered by JioMart's shipping-fee extension, reached by following a redirect to `www.jiomart.com/ext/jmshipmentfee/cart/v1.0/select_address`.
- **Voice input.** A Hindi/English voice note became three list lines (rabri, tomato, paneer).
- **AI-written replies.** Live logs show `compose message=ai prompt=ai`: composed replies passed the faithfulness check.
- **Picture cart card.** A product-image header with item bullets, totals and payment buttons was delivered.
- **Observed JioMart behavior.** Payment options for this account were JIOPP (aggregator JioOnePay, payment link on `www.jiomart.com`), JIOPP_COD and COD. A JioMart payment session opens only once. Posting the payment form directly from the customer's browser failed ("Invalid session" or HTTP 405, method not allowed) because that browser holds no JioMart sign-in. The server-side hop chain was `www.jiomart.com` (302) → `store1.jiomartjcp.com` (HTML form) → `payments.jio.com`. JioMart access tokens appear to last about one hour.
- **Deploy incident.** A redeploy while a reply was mid-send caused one duplicate cart card. The deploy rule now waits for no message processing, pending **or sending** (see [operations](operations-reliability.md)).

### Implemented and locally tested (not all exercised live)

- **The JioMart cart is the only basket.** CHECKOUT never adds list items: it reviews the real cart as it is, and older list items are not mentioned there. With cart writes on, "add", "replace" and "choose" move only the lines that request created or resolved into the JioMart cart, then remove them from the list. A product already in the cart is left at its cart quantity. The same product asked for twice becomes one line at the larger quantity, never the sum. New commands and planner actions: ADD MY LIST (`add_list`, moves every chosen list line, then reviews), CLEAR LIST (`clear_list`, cart unchanged) and EMPTY CART (`empty_cart`). The "Remove other items" button is retired; an old one only answers that it is no longer used.
- **JioMart extension redirects.** Account requests (cart writes, address selection, payment options, cart meta, checkout and storefront order reads) follow one 307/308 redirect to a JioMart host with the same method and body. A redirect from `api.jiomartjcp.com` to an `/ext/…` path is sent to `www.jiomart.com` with the session cookie. Any second redirect is refused. Order list and detail reads fall back to the `www.jiomart.com/api` gateway when the API host returns an empty list (list) or HTTP 404 (detail).
- **Server-side payment session.** Online checkout returns `merchant_order_id` and `merchant_transaction_id` (JioOnePay); the bot stores them with the vetted payment link behind a `/pay/{token}` row. Opening `/pay` makes the bot POST them to JioMart's `payment_session` with the account's own session cookie. It follows up to six JioMart-hosted hops, then hands the browser the provider form (auto-submitted) or a 303 redirect. The older direct-post path remains only for a `base64_html` form. Online payment is offered only when `JIOMART_ONLINE_PAYMENT` is true (default false; true in the runtime), JioMart offers a JIOPP option and a public origin exists.
- **Superseded and abandoned payments.** Each tap on Pay online creates a new JioMart order. Once any new chat order is created, older `awaiting_payment` attempts of the same owner and account become `superseded`, and their links are deleted. An unpaid online order that JioMart will not return stops being watched after 3 hours (`unpaid`, links deleted, no message). ORDER STATUS now re-sends the stored payment link while an order awaits payment; because a session opens once, a second visit may not reach JioPay (unverified).
- **Session refresh.** On HTTP 401/403 outside login stages, the adapter POSTs `/service/application/user/authentication/v1.0/token/refresh` with body `{"access_token": <current JWT cookie>}` (documented `RefreshTokenRequestSchema`) and retries once. An earlier body-less version returned HTTP 400 live. **The new body is not yet verified live.**
- **Agentic replies (`composer.py`).** After tools run, the model (`ShoppingAgent.compose_reply`) rewrites the text drafts into one reply and may reword the lead-in of a quick-reply, link or cart card. A deterministic check requires every ₹ amount, order ID and "N × Product" line to survive and forbids new amounts; otherwise the verified draft is sent. Login/OTP/address replies and model-written greetings/help (`Written`) are never recomposed. Buttons, links and IDs are never model-written. See [decision 5](architecture/decisions/0005-agentic-reply-composition.md).
- **Tap menus.** Product choices are WhatsApp interactive list messages (rows: name; price · pack · brand). A tapped row (`sbch_…`) maps to "choose N"; typed "choose N", "option N" and "pick N" are deterministic commands.
- **Picture cart card (`cart_image.py`).** CART and the CHECKOUT review render a white JPEG grid of up to nine JioMart CDN thumbnails with ×N badges. The bot uploads it to WhatsApp media and sends a button message with an image header, or an image with caption when there are no buttons. On failure the saved text fallback is sent. CART card buttons are Checkout and Empty cart; the review card carries the payment buttons. Pillow is a new dependency.
- **Charges.** The breakup shows Items, Discount/Coupon, Delivery, Cash on delivery fee and Convenience fee when present. When unmapped charges remain, it adds "Delivery & other charges" for the remainder, then Total, so the lines always add up.
- **Read receipt and typing indicator** are sent best effort for every inbound Meta message.

**Verification:** 261 tests passed (one existing Starlette/httpx deprecation warning). The repository context validator passed after this documentation update.

### Still unverified or open

- Session refresh with the new body (backlog item 1). If JioMart rejects it, hourly re-login remains.
- A cash-on-delivery order; shipped, out-for-delivery, delivered and cancelled pushes; ORDER/TRACK/REORDER against the new order; and the order-total mismatch warning.
- Tap-to-choose list menus, read receipts and typing indicators are deployed; no separate human acceptance is recorded here.
- Whether a re-sent `/pay` link can reopen a payment session, and JioMart's handling of our `callback_url`.
- Older orders behind JioMart's legacy "UL OMS" history remain invisible to the bot.

### Earlier known gaps, re-checked against code

- Fixed: ORDER STATUS re-sends the payment link for an order awaiting payment; the buy/order search prefix follows the checkout flag; `.env.example` lists every commerce setting; the `config.py` origin comment matches the code.
- Resolved later the same night: HISTORY lifts FORGET's purchase-history import suppression; module docstrings describe the cart and checkout gates.
- Resolved later the same night: CART/empty-cart text, REORDER (now moves items straight into the cart) and the planner instruction no longer say CHECKOUT adds the list; typed “add to cart” means ADD MY LIST. In the code, only ADD MY LIST does that. The "add to cart" command maps to the cart review, not to adding list lines.

## Real cart, checkout, payment and order updates — 18 October 2026 (evening)

This section supersedes earlier statements in this file that cart writes and checkout are disabled, unimplemented or not exposed to the planner. Those statements describe earlier deployments and are kept as history.

### Implemented and locally tested

- **Real JioMart cart (`checkout.py`).** CHECKOUT copies resolved shopping-list lines into the linked account's own JioMart cart. Adds use the shape observed in JioMart's web "reorder" request: `item_id`, `item_size`, `seller_id` and `meta.vertical_code`/`meta.compute_delivery_fee`, with JioMart assigning the store. List lines saved before cart support are re-found by exact catalog ID only, never a lookalike, to obtain their cart identifiers. Changed quantities are updated, JioMart quantity limits are applied and disclosed, unrelated cart lines are preserved and labelled, and the saved address is selected so JioMart prices delivery. The review shows JioMart's own line prices, fee breakup and total. CART shows the real cart with its fee breakup. Cart lines can be removed by number, by name or as "remove unavailable items", or re-quantified, through new `cart_remove`/`cart_quantity` planner actions and typed commands.
- **Ordering.** The review offers "Pay online" (JioPay hosted page) and/or "Cash on delivery" buttons, depending on the payment options JioMart returns for that cart. Buttons are single-use, expire after 10 minutes and are bound to the account, saved address and list version. They are also bound to a SHA-256 snapshot of cart lines, quantities, line prices, availability, total and address. The buttons are withheld when any line is unavailable, the cart is invalid, the total is missing or above `CHECKOUT_MAX_TOTAL` (default ₹1,500), the cart read was incomplete or no saved address is selected. An `order_attempts` row is persisted before the checkout call, and submission is shielded from the 150-second handler timeout. Failures before the checkout POST are definite rejections. A transport/5xx failure, a reply without an order number, or a `success:false` reply that still carries an order ID is recorded as **unknown**. An unknown attempt blocks new orders until ORDER STATUS reconciles it against JioMart's order list (exactly one order with the same total, created after the attempt began) or the customer sends CLEAR ORDER CHECK. After creation the bot re-reads the order total and warns the customer if it differs from the confirmed total.
- **Online payment.** Checkout returns JioPay's form as `data.base64_html`. The bot stores it behind a random token in `payment_links` with a 45-minute expiry and sends a WhatsApp "Pay ₹X" link button with a text fallback. `GET /pay/{token}` serves an auto-submitting form that posts `base64_html` to the payment option's `api_link`. That link must be HTTPS on a Jio-owned domain, and the page works only while the attempt is still awaiting payment. `/paid/{token}` is navigation only: it triggers an immediate status check and never marks an order paid. The public origin comes from `PUBLIC_BASE_URL` or the local ngrok agent API (`127.0.0.1:4040`), never from request headers.
- **Order updates (`order_watch.py`).** A background worker polls orders placed from chat. It pushes payment confirmed (online orders only), shipped, out for delivery, delivered (items, total, payment mode) and cancelled once each, plus one unpaid-payment nudge after 20 minutes and a give-up message after 3 hours. Payment links are revoked when an order settles. Ordered list lines are cleared at placement for cash on delivery, and when JioMart shows payment for online orders. Background reads never extend the sign-in expiry (`Store.save(..., touch=False)`). Messages are sent only within WhatsApp's customer-service window; the code uses 23.5 hours from the customer's last inbound message. Outside that window the update is stored and shown before the reply to the customer's next message.
- **Orders (`orders.py`).** ORDERS and HISTORY list recent orders using My Orders parameters `page_size=12&status=1`, with Track/Reorder/Details buttons. ORDER n shows details, TRACK n shows the shipment timeline plus courier/AWB, and REORDER n puts the exact same catalog IDs back onto the shopping list, never straight into the cart or an order. History parsing was fixed to read `shipment_status.value/title` and `item.id`.
- **Logging.** The app logger now writes INFO to stderr, which is the supervised runtime's app log. Safe diagnostics are `commerce_failed` (with a bounded upstream hint only for commerce mutation stages, never login/session stages), `payment_modes` (offered labels and payment host), `orders_listed` (count), `orders_shape` (field names only), `order_watch` (stage/status words) and `order_total_mismatch`.

New settings and defaults: `JIOMART_CART_WRITES=false`, `JIOMART_CHECKOUT=false` (effective only with cart writes), `CHECKOUT_MAX_TOTAL=1500`, `PUBLIC_BASE_URL` empty, `WHATSAPP_DISPLAY_NUMBER` empty, `ORDER_WATCH_INTERVAL_SECONDS=20`. See [operations](operations-reliability.md), [architecture](agent-architecture.md) and [decision 4](architecture/decisions/0004-real-cart-checkout-and-order-updates.md).

**Verification:** 231 tests passed (one existing Starlette/httpx deprecation warning) and Ruff passed. The repository context validator passed after the matching context update.

### Live-verified on 18 October

- A CART read on the real account showed the real JioMart cart: one line marked unavailable and a Sprite line.
- The bot was redeployed several times to the supervised runtime in `~/Library/Application Support/SmartBazaar`, each time with green readiness.
- As of 21:47 IST, the runtime configuration has cart writes and checkout **on**.
- The public `/pay` route was reachable through the ngrok tunnel.

### Not yet verified live

- That a CHECKOUT cart write appears in the JioMart app, and that cart edits work.
- The payment option names JioMart offers this account.
- Any order placement, cash on delivery or online. No order has been confirmed as placed through the bot.
- That JioPay accepts the form posted from our page, and whether JioMart honours our `callback_url`.
- Order-watcher pushes.

The first live order is a real purchase with real payment and fulfilment consequences.

### Orders finding

The account's two older orders still do not appear: the consumer order list returns an empty page. JioMart's website first queries a legacy "UL OMS" extension (`/ext/ul-backend/v2/order-history`) that authenticates with jiomart.com website cookies the bot does not hold. The bot does not call that route. Orders created on the current platform, including any placed from chat, are expected to appear in ORDERS and in the watcher. That is not yet verified.

### Known gaps found during documentation review (not fixed here)

- ORDER STATUS reaches the stored payment link only for submitting/unknown attempts. For an order awaiting payment it shows JioMart order details instead, although the payment nudge and the expired-link page tell the customer to send ORDER STATUS for the link.
- A search request containing "buy", "order", "purchase" or "cart" still prefixes "This version doesn’t add to cart or place orders", even when the flags are on.
- `jiomart.py` docstrings still say checkout is absent. The `config.py` and `app.public_origin` comments describe learning the origin from signed webhooks, but the code uses `PUBLIC_BASE_URL` or the ngrok agent API. `.env.example` does not yet list `PUBLIC_BASE_URL`, `WHATSAPP_DISPLAY_NUMBER` or `ORDER_WATCH_INTERVAL_SECONDS`.
- HISTORY now lists orders. No chat command reaches the older purchase-history refresh, so after FORGET automatic history import stays suppressed.
- Free ngrok shows a browser warning page before `/pay`; the customer must continue past it. A payment link sent before a tunnel URL change stops working.

## Shopping agent, memory and media build — 18 October 2026

**Later outbound incident:** the owner reported no replies. Recent inbound events had completed and replies were persisted, but the Meta messages endpoint returned HTTP 401. A read-only Graph check confirmed OAuth error 190/subcode 463: the temporary token expired at 15:30 IST on 18 October. Local and public health endpoints were reachable; that did not establish outbound readiness. The existing Meta app's token was renewed with previous settings, verified by a Graph HTTP 200 read, and saved privately. The service restarted with that credential. Fresh recipient delivery remains pending; failed replies were not replayed. Production credential lifecycle work remains open.

The owner then sent a fresh test message and confirmed receiving the reply. This establishes post-renewal recipient delivery and supersedes the pending-delivery note above.

The repaired health endpoint reports passive `outbound_transport` observations: `not_observed` after startup, `auth_rejected` with `status=degraded` after outbound HTTP 401, and `available` after provider acceptance. It performs no health-triggered sends/probes and does not equate acceptance with delivery. This repair passed 91 tests and Ruff; one existing Starlette/httpx deprecation warning remains.

The owner confirmed this build started from working login/search and assigned the remaining agent/commerce work to this session. The new code implements typed shopping actions, a versioned local list with add/replace/remove/quantity/choice flows, explicit product preferences, bounded history evidence, daily/idle context rotation, and Meta voice/image input. [Architecture](agent-architecture.md) and [API map](agent-api-map.md) explain implemented behavior and remaining purchase gates.

Shopping memory is encrypted independently of credentials and scoped to channel owner plus verified application/account. It survives reconnection and daily session rotation. Current requests override saved products; a missing preferred SKU requires a choice. History failures do not block basic shopping. FORGET and LOGOUT provide distinct memory/access controls. No unbounded transcript is sent to the model.

A live model smoke check correctly interpreted five synthetic requests: dahi, explicit brand/two packs, quantity editing, saving a preference and ambiguous yes. This is a smoke check rather than a measured production evaluation. Media uses Whisper and structured vision with bounded authenticated downloads; local tests do not establish real voice/photo quality.

Fresh-session API probes: the owner reconnected in WhatsApp, and authoritative session identity matched. History returned an empty page and remains **unresolved**, not proof of no purchases. The final cart read parsed a complete two-line snapshot marked `is_valid: false`; this does not establish a checkout-ready cart. One Amul curd candidate returned an available size with positive quantity, but its exact documented seller request returned HTTP 400. This is evidence for that candidate and request only. No cart write, checkout, payment or order was performed. Cart-write adapters remain disabled by default and are not exposed through conversational tools.

**Validation and activation:** 90 tests passed; Ruff and the repository context validator passed with no errors. The test suite emitted one Starlette/httpx deprecation warning. The existing local Meta service was restarted only after inbound processing and pending outbound counts were zero. A separate post-start health check returned HTTP 200, `status=ready`, `scope=shopping_draft_memory_media`, and both cart-write/checkout flags false. The temporary encrypted pre-upgrade backup was removed after successful startup. The existing callback tunnel was left running. New shopping/memory/media behavior is implemented and locally tested; real WhatsApp voice/photo acceptance and an end-to-end purchase remain unverified.

Long list replies are split at message boundaries without dropping items. Interrupted requests can leave saved list edits, so recovery asks authenticated users to check LIST before retrying; the inbound queue itself is still not durable.

Older snapshots below are retained as dated history. The latest confirmed Meta state is working two-way login/search/cards after verification, with OpenWA retired; older verification/delivery blockers are superseded.

## Product image and button milestone — 18 October 2026

Meta interactive product-image replies are implemented and running. An authenticated live milk search generated a Mother Dairy 500 ml card with image, observed ₹30 price and View details / Next option buttons. The encrypted outbox records Meta delivery as `delivered`, without error. A real button tap remains pending; local action/transport tests pass (45 tests total, Ruff clean). No cart or purchase action is enabled. See [reply formats](whatsapp-reply-formats.md).

## Confirmed two-way Meta milestone

The owner confirmed receiving Meta replies and successful product search, and supplied a WhatsApp screenshot showing results for a three-item request (Diet Coke, milk and bread). This supersedes earlier pending-delivery notes: real Meta inbound, authenticated search and outbound replies are working. It proves search, not cart mutation or ordering. OpenWA remains retired.

## Meta restored after verification — 18 October 2026

The owner supplied a screenshot showing Fynd verified. A fresh Graph API read confirmed `business_verification_status=verified`; BUSINESS and APP health are AVAILABLE. WABA health still reports BLOCKED with 141006, specifically describing payment-method blocking of business-initiated conversations. The earlier 141010 verification error is absent. The user asked to drop OpenWA: its linked session was logged out successfully, gateway stopped, and the shopping service restarted with `CHANNEL_PROVIDER=meta` and the original encrypted Meta database. The existing public callback tunnel remains running. A text reply in the existing Meta conversation was accepted by the messages API; recipient delivery after verification remains to be confirmed. Earlier restriction observations below are historical, not the current verification status.

## Temporary OpenWA setup — 18 October 2026

The user selected a temporary self-hosted OpenWA channel for the weekend demo. The local gateway and shopping service are running; the provider is configured with a signed loopback webhook and scoped outbound key. The gateway's signed webhook test succeeded (HTTP 200), unsigned webhook calls are rejected, and the inactive Meta POST route returns 404. The owner linked the dedicated WhatsApp account after a QR refresh; the gateway reports `ready`. An introductory message to the configured tester was accepted by OpenWA. Recipient delivery and real inbound LOGIN/search through this provider remain pending; do not call this a verified two-way channel yet. The Python suite has 41 passing tests and Ruff is clean. See [setup, privacy patch and acceptance](openwa-poc.md).

## Current scope and next proof — 18 October 2026

The user requires **the customer's existing JioMart account and a real JioMart purchase through WhatsApp**, visible in that same account. A dummy store, separate development account or guest checkout is outside the POC path. Do not resume the historical Company 153 checkout.

**Live API milestone:** direct OTP initiation/verification, existing-account session validation, refresh, saved-address retrieval and location-dependent catalog search succeeded on 18 October. These tests used the real JioMart consumer environment, without browser automation. Read the [sanitized validation record](commerce-api-validation-2026-10-18.md) for exact tested routes, response scope and local product captures. No consumer cart write, payment or order was performed.

**History is unresolved:** order reads returned zero even after broader date filters and refresh; this is not evidence that the customer has no purchases. Search also needs correct pagination: its reported 64 matches produced a first page of 12, and a cursor attempt repeated the page. The requested top 10 were exported as complete product objects; the raw first-page response was retained separately. Broad keyword hits do not establish exact product availability.

**Local implementation milestone:** a single-process Python/FastAPI login-and-search POC now exists. It accepts only HMAC-verified, allowlisted Meta webhook events; uses a bounded in-memory inbound queue; encrypts persisted customer state and outbound payloads in local SQLite; routes OTPs outside the model; and exposes JioMart OTP/address/search plus an OpenAI Responses intent/ranking adapter. The queue is not restart-durable, so interrupted inbound work must be resent. The encrypted outbox is persistent. Bounded live JioMart search/ranking checks now exercise the adapter, but they do not establish cart, stock, payment or order integration.

**Channel observation:** the private app secret is configured. The local health and privacy endpoints returned 200 through a temporary tunnel; Meta accepted callback/subscription setup and signed callbacks. The real WhatsApp path authenticated the JioMart account, read one saved default address, and completed an inbound Diet Coke search through AI and the adapter. The generated reply was accepted at the API boundary but failed delivery with Meta code `131031`.

**Current channel state:** after the owner entered the required business details, the profile-missing `131000` condition cleared. Meta Business Support now shows the account as permanently restricted: it cannot start conversations, respond to customer messages, or add phone numbers, and the UI says business verification is required before messaging. The only offered remedy is Start Verification; the last observed status was pending submission. Business health remains `LIMITED` with `141010` (not verified), and the WABA remains `BLOCKED` with `141006` for a separate billing/payment issue affecting business-initiated messaging. The temporary tunnel is not a stable deployment. Authentication and inbound search are proven; two-way WhatsApp delivery is not.

The owner sent LOGIN and OTP attempts from WhatsApp. The latest attempt authenticated the account; subsequent inbound search reached AI and JioMart and generated a reply. After the owner reported submitting verification, both Meta UI and Graph API still showed `pending_submission`, not review or approval. Evidence: `work/verification/meta-whatsapp-restriction.png` and `work/verification/meta-verification-business-type.png`.

**Model check:** the OpenAI adapter was exercised several times with the configured private key and `gpt-6.1-sol` during bounded live search/ranking checks. Diet Coke returned no exact result and Coke Zero 300 ml at ₹36 as an alternative; milk returned two Amul listings from the current response. This proves bounded model/adapter behavior only, not quantity stock, universal catalog availability or a commerce action.

**Local code status:** the login/search POC's current test suite reports 41 passing tests and Ruff clean. The OpenAI adapter has a 25-second timeout and one retry; the JioMart adapter rejects a session whose authoritative identity is explicitly unverified. These local controls do not remove the external Meta business/legal/payment verification blocker above.

**First auth parser repair:** the first bot code attempt exposed a response-contract issue worth correcting: verification, refresh and session parsing all required a common `success` flag, but their documented success schemas differ. That first revision validated `user_exists` plus a user object, a refresh access token, and an authoritative session user separately; explicit failure envelopes and identity/application mismatches remain rejected. Regression fixtures reproduce the old rejection for valid flagless responses. At that revision, 36 tests passed and Ruff was clean. The service was restarted for the later live attempt recorded below. The exact upstream response from the failed attempt was not retained, so this repair alone does not establish that the supplied OTP was valid.

**Historical live retry, 18 October afternoon:** an earlier LOGIN/code attempt returned a generic commerce failure and saved no session; its exact upstream stage was unproven. The response-parser and pre-session refresh defects were then corrected. Subsequent live testing authenticated the account and completed the inbound search path described above.

**Meta token renewal:** the afternoon replies failed at HTTP 401; a separate Graph check confirmed OAuth error 190/subcode 463 (expired token). The existing test token was renewed with the same permissions and saved privately. A fresh read returned HTTP 200, but business verification remains `pending_submission` and sending remains `BLOCKED` with health codes `141006` and `141010`. The account restriction is separate from token expiry and JioMart login handling. Earlier provider-accepted replies had failed asynchronously with `131031`; no recipient delivery has succeeded.

The user accepts OTP in chat or browser login with an actual session handoff, and hosted payment/UPI with return to WhatsApp. The API OTP route is now demonstrated; a browser delegation/callback route and production authentication handler are not implemented. Keep OTPs out of the future model/log path and verify payment server-side independently of a return click.

Next: formalize the observed API contracts and prove two-way WhatsApp; build a text shopping draft with exact matching; validate real SKU/quantity stock, eligible sellers, customer cart and authoritative quote; then enable one explicitly confirmed purchase with reconciliation and same-account visibility. Supported third-party deployment/version agreement, T9G8 mapping and history remain dependencies. See the [integration plan](jiomart-account-integration-plan.md).

## What exists locally

- Detailed [technical PRD](technical-prd.md), maintained with current verified setup evidence.
- [Agent experience/build plan](whatsapp-agent-experience-plan.md) and [sanitized real-account API validation](commerce-api-validation-2026-10-18.md).
- Original [bilateral note](reference/Instinct_for_SmartBazaar_Bilateral_Note_2026-10-15.pdf), all four pages.
- [Shyam's pilot guidance](reference/shyam-pilot-guidance-2026-10-15.png) and copy hashes.
- Python runtime manifest and local FastAPI login/search POC: webhook transport, encrypted SQLite state/outbox, conversation controller, JioMart adapter, OpenAI adapter, catalog matching, and local unit tests.
- Portable RepoMind skill and generated agent context for the implemented POC.
- Local Git repository with no remote; files remain uncommitted.

A local service and temporary public callback are running. There is no stable deployment, successful WhatsApp recipient delivery, durable inbound job system, real cart/quote/order implementation, or successful end-to-end commerce journey. SQLite is a local POC state store, not the proposed production persistence design.

## Last-observed Meta state

| Item | State |
| --- | --- |
| Facebook | Existing signed-in Ganesh E account reused |
| Developer account | Created by the user |
| App | SmartBazaar POC; ID `1121017983715645` |
| Business portfolio | SmartBazaar POC; ID `1763732898180008`; unverified |
| App publication | Published; UI proof retained locally |
| Test number | `+1 (555) 674-9508` |
| Phone Number ID | `1428286150359816` |
| Test WABA ID | `1719081452521182` |
| Recipient | Number ending 5951 verified and selectable |
| Token | Fresh test token and app secret stored privately; callback receipt works |
| Outbound message | Three greetings accepted then failed delivery; last two report `131031`; no successful recipient delivery |
| Inbound events | Seven events completed; authenticated LOGIN, saved address, and inbound Diet Coke search/reply path verified |
| Backend webhook | Local signed `GET`/`POST /webhooks/whatsapp` handlers exist; Meta accepted callback/subscription setup and two LOGIN events reached the handler |

The app is now published, but Meta's permanent restriction requires business verification before messaging. Billing and verification limitations remain, and outbound delivery continues to fail. Do not claim that verified recipients can already chat with an AI agent or that OTP login is complete. See PRD section 11 for setup URLs and the verification sequence.

## Commerce dependencies

### Historical development setup — 17 October 2026; excluded from the current POC path

The user-created partner organization **SmartBazaar whatsapp bot**, ID `6ad2944aa84d821e2270bdfb`, is verified. The user also created development company **SmartBazaar whatsapp bot**, **Company ID 153**. The listing contains one development company, and its platform dashboard explicitly labels it DEVELOPMENT COMPANY. The dashboard links sales channel **https://sb-wh-ganesh.jiocommerce.io**; API reads identify application `6ad295642243111f24dd498f`.

Accepted and Pending managed-company lists were empty when checked. The Request Company Access form requires an actual Company Id and selected permissions; no request was submitted. Company 153 is development scope, not verified T9G8 scope.

On 17 October 2026, development Company **153**, application **`6ad295642243111f24dd498f`**, and store **2962 / HS-e93f6** were verified. The created **SmartBazaar POC Development** client authenticates against **`https://api.jiocommerce.io`**. A synthetic brand **POC Test Groceries** (24868) and product **POC Test Rice 1 kg** (82146727; SKU `POC-RICE-1KG-001`, size OS) were created. Store stock is **20**, MRP **₹100**, selling price **₹90**. Product, brand and inventory API read-backs returned **200 with one item** after stocking. The storefront guest cart was tested at quantity 1 → 2 → 1, with totals ₹90 → ₹180 → ₹90. No order was placed.

At the last observation, guest checkout was at the mandatory address/name/mobile step and reported a Google Maps loading/configuration error. No customer address or order was submitted. The customer list was empty. Direct storefront API session bootstrap and customer history were untested. These development-checkout gaps are historical, not the next work queue.

Node.js v22.23.1 and npm 10.9.8 are installed, but `fdk` is absent. A read-only SSH check of the official JCP CLI repository returned TF401019 (repository missing or permission denied). No live Fynd commerce connector tools were exposed in this session. Sanitized evidence is retained in `work/commerce-validation/results.json`; secrets remain in a git-ignored local file with restrictive permissions.

The public docs cover the needed API areas and expose a `v1.10.3` platform-version option. The development API host and scope are verified; the real JioMart consumer host and selected calls were subsequently verified on 18 October, while supported version/contract compatibility remains open. See [commerce readiness](commerce-readiness.md) for historical setup evidence and the [current integration plan](jiomart-account-integration-plan.md) for the active validation sequence.

### Unresolved target-store access

Shyam supplied pilot darkstore **T9G8**, the Jio Commerce documentation, and a reference to **version 1.10.3**. Observed consumer OTP/session/address/search calls now work. Still unknown: supported deployment access and version agreement, T9G8/serviceability mapping, version meaning, long-lived session behavior, existing-account history visibility, exact stock/cart/quote behavior, checkout/payment contract and order reconciliation. Development-company tests do not resolve any of those consumer-account dependencies.

Creating a private extension or API client elsewhere does not grant T9G8 access. Creating a JioMart purchase history does not prove that the pilot credentials can retrieve it.

## Product decisions to preserve

- The source concept includes voice notes, handwritten images, purchase-history personalization, reviewed/confirmed cart, OMS routing, and manager voice continuity.
- Text input and detailed validation/reliability controls are proposed implementation additions.
- Real commerce scope is decided: existing JioMart account and real order; a development-store demo cannot satisfy it. Local unit-test fixtures may remain isolated testing tools.
- Manager-voice deferral, providers, supported languages, spend ceiling, and the schedule discrepancy remain open.
- The team manages LLM/hosting/channel engineering; requests to Shyam should focus on SmartBazaar data, APIs/MCP access, identifiers, schemas, validation, and business rules.

## Updating this file

When a fact changes, record the observation date, what was verified, and the non-secret evidence. Update the relevant PRD section and `.ai` context together. Do not replace “pending” with “complete” merely because someone attempted an action. Distinguish API acceptance, actual message delivery, inbound receipt, and successful OMS creation.

The owner subsequently confirmed the product image and button interactions are working in WhatsApp, then asked about a swipeable carousel for multiple products. Carousel support is under review and is not yet implemented.

## Credential and availability hardening — 18 October 2026

The owner requested preventing another silent outage and deferred adding the manager's number. The current owner remains allowlisted. With explicit approvals for policy, identity, asset permissions and persistent credential generation, the `shoppingagent` Employee system user was created and assigned the SmartBazaar app and existing WhatsApp test asset. The credential has WhatsApp messaging/management scopes. A private Graph debug read independently verified `type=SYSTEM_USER`, `is_valid=true`, `expires_at=0`, and `data_access_expires_at=0`. The configured phone read also succeeded. The token was saved privately and the running service restarted; `/readyz` returned HTTP 200 with available credentials and live workers. No scheduled expiry does not mean immunity from revocation or account restrictions. That initial check was incomplete: the owner reported no reply, and Meta rejected sends with OAuth code 10 because the service identity lacked access to the separate messaging account. See the completed verification below.

The implementation now checks credentials at startup and every five minutes, warns within 24 hours of expiry, exposes HTTP 503 readiness for degradation, and records worker/delivery failure signals. Authentication-rejected replies are held encrypted for bounded recovery rather than immediately dropped. They expire after 15 minutes without later replay. This does not make inbound work durable or submit any order.

The initial launchd attempt inside Documents failed with EX_CONFIG 78 before application logs; the exact cause was not proven. The free ngrok plan rejected reuse of its random URL with ERR_NGROK_313. The working manual app/tunnel were restored, and Meta accepted and read back the replacement messages callback. Local supervision remediation is being validated; see [operations](operations-reliability.md). Do not claim the first failed installer attempt provided supervision.


### Completed reliability verification — 18 October 2026

The similarly named WhatsApp asset and messaging account are distinct Meta assets. Phone reads succeeded with the first assignment, while actual sends failed. With explicit owner approval, `shoppingagent` was granted the UI's Everything permission on the existing messaging account; Meta bundles sending with settings/user management and payment-information viewing there. The same token then passed the configured messaging-account read. One latest already-generated failed text reply was retried without rerunning any shopping action; Meta reported delivered. The probe now also checks configured `META_WABA_ID` access and reports `unavailable/account_access` on denial.

The app and tunnel are now supervised by `com.smartbazaar.local-app` and `com.smartbazaar.local-tunnel` LaunchAgents. Their dedicated runtime is `~/Library/Application Support/SmartBazaar`, with its own locked Python dependencies, private `.env`, and encrypted `state/bot.sqlite3`. The original database was migrated with SQLite backup and passed integrity validation; both source and deployed configuration point to that same active database. Source changes require explicit staging. The service uses `caffeinate -i` to prevent idle sleep.

The free ngrok runner discovers its dynamic HTTPS address, waits for the local app, updates the existing Meta messages subscription while preserving fields, and verifies the callback. Request inspection is disabled and HTTP redirects are rejected. Killing the app after draining work produced a new ready process in 2.2 seconds. Killing the tunnel produced a new process and changed URL, verified Meta callback, and public HTTP 200 readiness in 2.9 seconds. These are observed recovery times, not uptime guarantees. Both LaunchAgents reported running.

After both restart tests, the owner sent LIST and confirmed receiving the reply. The final local suite passed **126 tests**, Ruff, and context validation. Token revocation, Meta outages, host shutdown/lid closure, logout, and network loss remain possible. No independent alert destination or always-on cloud host is configured; inbound work remains memory-only. Cart writes and checkout remain disabled.

## Natural conversation and presentation — 18 October 2026

The owner identified fixed greeting/help responses and the lack of pre-login conversational access. The existing memory rotated structured shopping context daily/after four idle hours, but did not retain recent dialogue. This build routes eligible guest/connected messages through the AI planner, adds natural reply/presentation/action fields, and keeps account tools behind server authentication. Guests may discuss groceries and capabilities without linking; model-selected shopping mutations cannot run in guest scope.

Encrypted session state now retains at most eight sanitized recent messages, capped at 480 characters each. Day/idle/account boundaries rotate it; relinking, LOGOUT, FORGET and NEW SESSION clear it. OTP/login/address controls are excluded. Lists/preferences retain their independent account-scoped lifecycle. The public privacy notice was updated to describe this bounded retention.

Image-free quick replies support model-requested Connect JioMart, help, list, history and search guidance. Opaque IDs are owner/account/location-bound, single-use and expire after 15 minutes. Actual catalog image cards remain available; model-selected text-only search suppresses them. Factual shopping receipts are generated from authoritative tool results, not planner prose.

Verification: **148 tests passed**, Ruff passed. Five synthetic calls to the configured live OpenAI model confirmed a guest greeting with Connect/Help actions, accurate capability guidance, grocery redirection for an unrelated coding request, recall of an earlier curd exchange, and a Hinglish response. No synthetic test was sent to a customer or used a commerce account. The five changed source files were staged to the existing supervised runtime after draining work; private configuration and the active database were preserved. launchd initially needed a second bootstrap after unload finished; it then started successfully. Local and public readiness returned HTTP 200. Human acceptance of the updated greeting/buttons is pending.

## Additional tester setup — 18 October 2026

The owner deferred public access and requested one additional tester. Its number was added only to the private source/runtime allowlists, preserving the existing tester. The supervised app restarted with green readiness. Meta recipient registration was initiated and reached the WhatsApp verification-code step; recipient verification and an actual reply are still pending. Do not treat local allowlisting alone as successful onboarding. No public-access code or mode was deployed.
