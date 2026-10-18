# WhatsApp login and catalog search

## What users can do

An allowlisted test sender can submit a signed Meta webhook event, begin an OTP link to an existing JioMart account, select a saved location or pincode, and ask for text product searches. Search returns listing evidence only and does not assert quantity stock. With `JIOMART_CART_WRITES` on, a resolved product goes straight into the account's real JioMart cart (the only basket), and CART edits or empties it. With `JIOMART_CHECKOUT` also on, CHECKOUT reviews the cart and a tapped payment button places an order. Payment is JioPay through a server-opened payment session (needs `JIOMART_ONLINE_PAYMENT`) or cash on delivery, and the bot follows the order. Replies are AI-written from tool facts under a fact check. Live on 18 October: cart edits and one paid online order; see the late-evening section below.

Current build (19 October 2026; implemented and locally tested, not deployed or verified on WhatsApp): there is no local shopping list. A product needing a choice is a short-lived question, asked with numbered pictures or a tap menu; LIST shows the cart. Every reply ends with two or three next-step buttons, voice notes work for every command, product cards have Add to cart and a JioMart product link, the sender can ask for an INVOICE and (with `JIOMART_ORDER_CANCEL`) CANCEL ORDER, order confirmations carry an order-summary PDF, and JioMart sessions are renewed in the background. See the section at the end.

This feature exists locally; the real WhatsApp path now authenticates the JioMart account and completes an inbound Diet Coke search through AI and the adapter using one saved address. The owner confirmed recipient search and product-card/button delivery after verification; earlier `131031` restrictions are historical. Business-initiated billing remains separately flagged.

## API endpoints

### `GET /healthz`

Returns configuration readiness, the scope (`shopping_draft_memory_media`, or `shopping_cart_checkout` when cart writes are on), explicit `cart_writes_enabled`/`checkout_enabled` flags, configured model name, and aggregate local store counts. `outbound_transport` starts as `not_observed`, becomes `auth_rejected` after HTTP 401 (top-level status `degraded`), and becomes `available` after provider acceptance. These observations are process-local and do not prove recipient delivery; health reads make no provider calls. It does not report secrets or customer records. `GET /privacy` serves the factual private-test data-handling notice needed for the Meta publication workflow; its existence does not publish the app.

The reliability build adds startup and five-minute cached credential checks (identity, scope, token/data expiry, configured phone and messaging-account access) through `channel_health.py`. A 24-hour expiry warning, stale/failed checks, dead workers and observed delivery failure also degrade status. `/readyz` returns the same safe readiness information with HTTP 503 when degraded; `/healthz` remains the liveness HTTP 200. The scheduler performs bounded reads; neither health endpoint triggers sends or probes. See [operations](../../docs/operations-reliability.md).

### `GET /webhooks/whatsapp`

Handles Meta's verification challenge only when the configured private verify token matches using constant-time comparison. The current temporary HTTPS callback was configured and verified; it is not a stable deployment.

### `POST /webhooks/whatsapp`

Reads a capped request body, validates `X-Hub-Signature-256`, filters to the configured WABA/phone-number IDs and allowlisted senders, rejects stale messages, records deduplication state, then queues text for in-process handling. Status receipts update the encrypted outbox delivery state. Signed audio/image references enter the authenticated media pipeline; unsupported attachment types receive a helpful text response.

### `GET /pay/{token}`

Looks up an unexpired, encrypted payment link by token digest. If its order attempt is still `awaiting_payment`, it returns a `no-store`, no-framing payment hand-off; otherwise it returns a 404 page pointing back to WhatsApp. For a stored `base64_html` form the page auto-submits it to the vetted `api_link` (earlier path). For JioOnePay merchant IDs (current, live-proven), the bot opens JioMart's `payment_session` with the account's own cookie under the owner lock. It returns a 303 redirect or an auto-submitting provider form, 409 when the account session is gone, or 502 when JioMart opens no payment page.

### `GET|POST /paid/{token}`

The JioPay callback/return page. For a known token it schedules an immediate order check for that attempt, then shows a “Back to WhatsApp” link (`wa.me/<WHATSAPP_DISPLAY_NUMBER>` or `whatsapp://`). It never marks payment itself.

## Modules involved

- [Login and search POC](../modules/login-search-poc.md) — implements every current runtime component in the `smartbazaar` package, including `tools.py`, `checkout.py`, `orders.py`, `order_watch.py`, `composer.py`, `cart_image.py`, `receipt.py` and `session_keepalive.py`.
- [Webhook and persistence pattern](../patterns/webhook-state-safety.md) — covers the ingress boundary and local state rules.
- [External adapter pattern](../patterns/bounded-external-adapters.md) — covers model and JioMart boundary calls.

## Local data

`sessions` stores encrypted per-owner conversation state, including a JioMart session only after OTP verification (kept 30 days after the customer's last message since 19 October). `events` stores a keyed event digest and processing state, while `outbox` stores encrypted outbound recipient/text data and non-secret delivery metadata. `order_attempts` stores one encrypted record per confirmed checkout button, and `payment_links` stores encrypted, expiring JioPay forms. See [the data model](../architecture/data-model.md).

## User flow

1. Meta calls the signed webhook. The server rejects bad signatures, foreign identifiers, non-allowlisted senders, stale input, oversized bodies, and duplicate event IDs before model or commerce work.
2. A bounded in-memory queue delivers the message to one of two workers. The event digest has already been persisted, but raw input is intentionally not stored; a restart means the sender must resend.
3. `Conversation.handle` processes `LOGIN`, OTP, pincode/address and help commands before calling the model. OTP, credentials and suspicious phone-like values never enter model context.
4. `ShoppingAgent.interpret` returns a structured bounded intent. Search calls `JioMartClient.search`, normalizes candidates, ranks a small shortlist, applies deterministic identity/relevance checks, and renders a cautious reply.
5. The reply is encrypted into the persistent outbox. The outbox worker attempts Meta delivery and records acceptance/delivery state without treating it as a commerce action.

## Business rules

- OTP belongs solely to the commerce adapter; it never enters an AI prompt or application log.
- Current identity matching rejects category mismatches and does not silently substitute Diet Coke, Coke Zero, regular Coke, Pepsi, merchandise, syrup, or energy drinks.
- Exact listing, `sellable`, and price fields are candidate evidence. They are not stock, serviceability, cart or order evidence.
- The feature limits inbound text and model candidates/outputs. The model cannot select account IDs, URLs or arbitrary product identifiers. It may request typed cart actions (`checkout`, `cart_remove`, `cart_quantity`), which the server executes only when its flags allow, against server-held cart lines.
- Rule since 19 October: there is no local list. Confident matches, tapped Add to cart, “add it” and REORDER go straight into the cart; a product needing a choice is a question that expires after 6 hours and is replaced by any newer add request. Older rules follow. Earlier rule: buy/order requests prepared a local list and only CHECKOUT moved it into the cart. Late-evening 18 October rule with cart writes on: the JioMart cart is the only basket. Add/replace/choose move only that request's resolved lines into it. Products already there keep their cart quantity, and duplicates use the larger quantity, never the sum. CHECKOUT never adds; ADD MY LIST does. Only a tapped, unexpired, single-use payment button for the exact reviewed basket places an order. A typed “yes” never does.
- Customer-facing text may be model-written, but only if every ₹ amount, order ID and “N × Product” line of the tool draft survives and no amount is added. Otherwise the draft is sent. Buttons, IDs and links are never model-written.
- No order is submitted while a previous attempt is submitting or unknown. No order is submitted above `CHECKOUT_MAX_TOTAL` when it is set (default 0 = no limit since 19 October), with an unavailable or unread line, or without a saved address.
- Payment success comes only from JioMart's order record, never from the browser returning to `/paid`.
- An order is cancelled only from a tapped “Yes, cancel it” button, only with `JIOMART_ORDER_CANCEL` on and only for shipments JioMart marks cancellable. An uncertain cancellation is never retried.
- An invoice is JioMart's own document, fetched only from an allowlisted HTTPS host. The bot's order summary is labelled as not a tax invoice.

## Error scenarios

| Condition | Current behavior |
| --- | --- |
| Missing/bad Meta signature | Reject request with 403 before parsing or queueing. |
| Queue full | Return 503 so the provider can retry; do not silently drop a newly claimed message. |
| Process restart during inbound handling | The event becomes interrupted; the raw message is not retained and must be resent. |
| JioMart/session failure | Return a safe, non-secret user message; do not claim a product is unavailable. |
| OpenAI unavailable or malformed output | Return a safe inability-to-interpret/rank outcome; do not fabricate search success. |
| Delivery failure | Persist a retryable/non-retryable outbox outcome; it does not repeat commerce work. |
| Cart write rejected by JioMart | Stop at that step; tell the customer to send CART to see what's in it; nothing is ordered. (Earlier: “check CART or LIST”.) |
| Cart, prices, availability, address or list changed after review | The payment button is refused; the customer re-runs CHECKOUT. |
| Checkout rejected before or at the POST | Attempt `failed`; “nothing was placed”. |
| Checkout outcome uncertain (timeout/5xx, no order number, `success:false` with an order number) | Attempt `unknown`; further orders are blocked until ORDER STATUS finds the order or CLEAR ORDER CHECK. |
| Handler timeout or crash during a checkout tap | Submission continues in a shielded task and records its outcome. The customer is told not to tap again and to send ORDER STATUS. |
| No public origin | Online payment is not offered; cash on delivery may still be offered. |
| Order update outside WhatsApp's window | Stored and shown with the next reply. |
| Answering a product question after it expired, after a location change or after a newer request | “That choice is from an earlier request”; nothing is added. |
| Cancellation switched off, not cancellable, rejected or uncertain | Says so; nothing is retried; uncertain outcomes point to My Orders. |
| Invoice not generated yet or download refused | “JioMart hasn’t issued the invoice yet” or points to My Orders; nothing else changes. |
| JioMart session refresh fails | The keep-alive logs it and tries again next interval; the next request asks the customer to LOGIN. |

## Related work

The [experience plan](../../docs/whatsapp-agent-experience-plan.md) describes the desired later cart and confirmation behavior. The [PRD](../../docs/technical-prd.md) retains final confirmation, same-account order visibility and duplicate-order prevention as acceptance requirements, none of which this feature implements. (Earlier build. The evening build implements button-bound confirmation and local duplicate-order prevention; same-account visibility of a chat-placed order is unverified. See PRD section 23.)

The login adapter now follows the documented new-login sequence: OTP verification, verification-cookie continuity, and authoritative `GET /session` identity validation. It no longer performs an immediate bodyless token refresh, because refresh is documented as a separate expired-token operation with additional request requirements. Safe stage/status/code diagnostics were added for the next live attempt; they retain no provider body or authentication/customer material. This is a contract-supported repair, not proof that the prior live failure occurred at refresh because its raw response was intentionally not retained. Local regression tests pass; the corrected runtime subsequently authenticated the account, loaded a saved default address and completed the inbound AI search.

## Temporary OpenWA channel

The user selected OpenWA for the internal weekend demo. `smartbazaar/openwa.py` implements outgoing text; `app.py` accepts signed `/webhooks/openwa` events only when `CHANNEL_PROVIDER=openwa`. The Meta provider remains the default. OpenWA requires its API base URL/key, generated session UUID and HMAC secret in private configuration; account linking is done by scanning a QR using a dedicated bot account. Tester identity and events are provider/session-scoped; raw unresolved LIDs, groups and outgoing messages do not enter the conversation. The live deployment uses a separate database and requires fresh JioMart authentication. See [runbook](../../docs/openwa-poc.md) for current acceptance status.

## Current transport update

The user retired OpenWA after Meta verification succeeded on 18 October. OpenWA is logged out and stopped; the runtime uses Meta with its original database. Graph confirms business verification, with BUSINESS/APP AVAILABLE and only WABA payment flag 141006 remaining. A post-verification text send was accepted; recipient search/card delivery is established by the owner. Earlier OpenWA runtime/verification-blocker descriptions are historical.

## Interactive product replies

Search can produce a structured product card using `catalog.py` image normalization, `Conversation._product_card`, the encrypted outbox and `WhatsApp._payload`. Cards use the real allowlisted catalog image, one leading validated product per query, listing price and browse-only reply buttons. Plain text remains the fallback for missing images and transports without interactive support. `app.py` routes Meta `interactive.button_reply` IDs into the deterministic conversation handler before the language model.

Button actions live in encrypted per-owner session state with random opaque IDs and a 15-minute expiry, binding to the account and delivery context. They return details or the next existing candidate, never add to cart or purchase. Unknown/stale/other-owner IDs fail closed. The finite candidate set and compact card prevent an entire broad search page from flooding the chat. See [reply formats](../../docs/whatsapp-reply-formats.md).

## Multimodal shopping and remembered choices

The existing feature now includes typed list tools and account-scoped memory. “Buy dahi” becomes a curd request; an applicable saved SKU can resolve an unspecified brand. Explicit current requests override preferences. If multiple exact choices remain or a preferred SKU is missing, the draft preserves the line and asks for a numbered choice. Multi-item requests retain other lines and advance to the next choice. LIST reviews quantities; replace/remove/set-quantity edits increment the draft version. No upstream cart or order side effect is claimed.

HISTORY attempts bounded authenticated order reads; empty/unusable responses remain unresolved. (Since the evening build, HISTORY is an alias of ORDERS and lists recent orders; the purchase-history refresh still runs automatically during adds, but no chat command forces it or lifts FORGET's suppression.) MEMORY inspects saved preferences; explicit remember requests store selected products; FORGET clears preferences/history/draft without disconnecting. Daily or idle rotation clears only short-term context. Auth expiry preserves account memory for re-login; switching accounts cannot inherit it.

Voice notes use Whisper and photos use structured vision extraction after login. Ambiguous media asks for clearer input. Media cannot invoke account, settings or checkout commands. See [agent design](../../docs/agent-architecture.md), [memory schema](../architecture/data-model.md), and [adapter boundaries](../patterns/bounded-external-adapters.md).

## Guest conversation and adaptive replies

Before login, greetings, help, brief social exchanges and grocery questions reach OpenAI with a minimal connection flag and sanitized recent dialogue. Account/product operations invite connection without executing tools. Unrelated questions receive a short grocery-focused redirection. During an OTP challenge the secure code handler still takes precedence.

The AI chooses text or helpful quick actions; product searches can use actual catalog images or text when requested. A server-generated Connect JioMart button starts login for the sender; help/list/history/search-navigation buttons resolve through encrypted owner-bound, expiring state. No checkout capability is introduced. Recent exchanges reset daily/after four idle hours; lists/preferences retain their separate lifecycle.

## Real cart, checkout, payment and order updates

Implemented and locally tested on 18 October 2026 behind `JIOMART_CART_WRITES`/`JIOMART_CHECKOUT` (on in the runtime since 21:47 IST). Live-verified so far: a real CART read. Everything else in these flows awaits live acceptance; see [status](../../docs/project-status.md).

### Flow A: CART and cart edits

1. CART (or “my cart”) reads the real cart after an authoritative session check and renders numbered lines, unavailable flags, JioMart's fee breakup and total. With cart writes off, CART shows the local list instead and makes no JioMart call.
2. “remove 2 from cart”, “remove unavailable items”, or a planner `cart_remove`/`cart_quantity` (by line number or product name, plus quantity) edits the real cart. Numbers resolve against the last shown cart view (30 minutes, account/address-bound). Ambiguous names ask which line, and quantity changes need exactly one line.
3. The bot re-reads and re-shows the cart. A JioMart rejection says so without guessing.

### Flow B: CHECKOUT review

Steps 2–6 below describe the evening build and are kept as history; the late-evening Flow B′ replaces them.

1. CHECKOUT (also “place order”, “add to cart”, or a planner `checkout`) first shows the status of any submitting/unknown attempt instead of continuing.
2. Older list products without cart identifiers are re-searched and accepted only if the exact catalog ID returns.
3. Missing lines are added with the listing's item, size, seller and vertical. Changed quantities are updated within JioMart limits, unrelated cart lines are kept, and the saved address is selected.
4. The review lists the bot's lines first, then “Already in your JioMart cart (not from this chat)”, followed by notes, fee breakup, total and delivery address.
5. Blockers (unavailable lines, invalid or partially read cart, missing total, total over the cap, no saved address, no usable payment option) replace the payment buttons. A “Remove other items” button may still appear.
6. Otherwise the customer gets up to three buttons: “Pay online ₹X” (only with a JioPay option and a public origin), “Cash on delivery” and optionally “Remove other items”, each expiring in 10 minutes.

### Flow C: confirmed order

1. A tap is accepted once, only before expiry, only for the same account/address and list version, and only if the re-read cart matches the reviewed snapshot.
2. An `order_attempts` row is written as `submitting`. The checkout POST runs in a shielded task, after address re-selection, a total re-check, payment option lookup and best-effort attribution meta.
3. Cash on delivery: the order is `placed`, ordered list lines are cleared, and the reply gives the order number and total. Any post-creation total mismatch is warned.
4. Online: the order is `awaiting_payment`. The reply is a `cta_url` “Pay ₹X” button to `/pay/{token}` (valid 45 minutes), which opens JioPay. If JioMart returned no form, the customer is told to pay from My Orders.
5. Rejection gives `failed` and “nothing was placed”. Uncertainty gives `unknown`, a “please don’t retry; check My Orders or send ORDER STATUS” reply, and a block on new orders.
6. ORDER STATUS while unknown tries reconciliation against the order list. CLEAR ORDER CHECK dismisses the block only if reconciliation finds nothing.

### Flow D: order updates

1. Every `ORDER_WATCH_INTERVAL_SECONDS` (and immediately after `/paid`), the watcher reads each chat-placed order from the last seven days.
2. It pushes once per stage: payment confirmed (online only), shipped, out for delivery, delivered (items, total, payment mode) and cancelled (with a refund note for online orders). Unpaid online orders get a nudge at 20 minutes and a give-up at 3 hours.
3. Settling an online order revokes its pay links and clears its ordered list lines.
4. If the customer's last message is more than 23.5 hours old, the update is stored and prefixed to the next reply.

### Flow E: orders, tracking and reorder

1. ORDERS/HISTORY/“my orders” lists up to five recent orders (date, total, item count, status) with Track latest / Reorder latest / Latest details buttons.
2. ORDER n shows status, expected time, items and total; TRACK n shows the timeline and courier/AWB; REORDER n adds still-listed items with the same catalog IDs to the shopping list and reminds the customer to CHECKOUT.
3. If the list is empty, the bot says it found no orders and asks the customer to say so if the JioMart app shows some. The account's two older orders currently fall in this case (legacy order history).

## Late evening 18 October — basket, payment session, composed replies

Live on the real account: CART reads, adds, removals, quantity changes and EMPTY CART. One online order (JM6AD4FE…) was created by a single tap, paid by UPI, pushed as “✅ Payment received”, listed by ORDERS and visible in the JioMart app. Composed replies and the picture cart card were delivered. Unverified: cash on delivery, shipment-stage pushes, the corrected session refresh. See [status](../../docs/project-status.md).

### Flow A′: building the basket

1. “buy dahi”/“add 2 milk” runs the list tools. A clear match is chosen; several candidates produce a **tap menu** (interactive list, rows “name” / “₹price · pack · brand”).
2. Tapping a row (`sbch_…`) or typing “choose 2”/“option 2”/“pick 2” resolves the line deterministically.
3. With cart writes on, the lines this request created or resolved move into the JioMart cart and leave the list. The reply says “🛒 Added to your JioMart cart: …” or “Already in your cart, left as it is”.
4. ADD MY LIST moves every chosen list line and shows the review; CLEAR LIST drops list lines (the cart is unchanged); EMPTY CART removes every cart line.
5. CART shows a **picture card** (thumbnails with ×N badges, bullets, charge breakup) with Checkout and Empty cart buttons; text when images are unavailable.

### Flow B′: CHECKOUT review (current)

1. A submitting/unknown attempt is shown first. Otherwise nothing is added; the saved address is selected, and if that step fails, a separate address blocker is shown instead of an item error.
2. The review card lists every cart line (“just added” marked), the balanced charge breakup (“Delivery & other charges” for unmapped fees), Total and the delivery address. Stale list items are not mentioned.
3. Blockers replace payment buttons; otherwise **Pay online ₹X** (with `JIOMART_ONLINE_PAYMENT`, a JIOPP option and a public origin) and/or **Cash on delivery**, valid 10 minutes.

### Flow C′: paying online

1. The tap creates the JioMart order (`awaiting_payment`) and a `cta_url` “Pay ₹X” button to `/pay/{token}` (45 minutes). Older unpaid chat orders become `superseded`.
2. Opening the link makes the bot open JioMart's payment session with the account cookie. The browser lands on the provider page (live: `payments.jio.com`); a session opens only once.
3. JioMart's order record, not the browser return, confirms payment; the watcher pushes “✅ Payment received”. Unpaid orders get a nudge at 20 minutes; after 3 hours they are given up (silently if JioMart will not return them).

### Flow F: reply composition and acknowledgement

1. Each inbound Meta message gets a background read receipt with a typing indicator.
2. After tools run, eligible replies are rewritten by the model into one message and the card lead-in is reworded. A deterministic check keeps the verified draft if any amount, order ID or item line changed.
3. Login/OTP/address replies, quick-action and menu taps, and planner-written greetings are not recomposed.

## 19 October 2026 — one basket, buttons, voice, keep-alive, cancel and invoices

Implemented and locally tested (307 tests). Not deployed to the supervised runtime and not verified on WhatsApp or against JioMart; see [status](../../docs/project-status.md) and [ADR 6](../../docs/architecture/decisions/0006-jiomart-cart-is-the-only-basket.md). This supersedes Flows A′ and B′ above where they mention the list, ADD MY LIST, CLEAR LIST or list versions.

### Flow A″: building the basket

1. “buy dahi”, “add 2 milk” or a Hindi request is planned as `add`. Each item is searched and ranked; a saved preference among the exact matches, or a single exact match, goes straight into the JioMart cart. The reply says “🛒 Added to your JioMart cart: …”, “Already in your cart, left as it is: …” and any quantity-limit notes.
2. Items with several candidates become questions, asked one at a time: a numbered picture with buttons (“1. Chitale 200 g”, Skip) when all options have images, otherwise a tap menu with a “Skip this item” row. Tapping or typing “choose 2” adds that product; SKIP or “I don't want bread” drops the question; the next one follows.
3. Any newer add request drops all open questions. They also go on a location change, CANCEL, “clear list”/“skip all”, NEW SESSION or after 6 hours.
4. A product card's Add to cart, or “add it/this” after one product was shown, adds that product. “Add it and check out” adds it and shows the review in the same reply.
5. LIST, “my list” and CART show the cart card (Checkout / Keep shopping / Empty cart). “One more milk” changes the matching cart line's quantity when the cart was shown in the last 6 hours.

### Flow G: next-step buttons

Every reply ends with two or three buttons chosen from the situation: Connect JioMart / What can you do? for guests; Resend OTP / Cancel login during OTP entry; View cart / Checkout / Keep shopping after an add; My orders / Track my order / Keep shopping after an order reply; otherwise Keep shopping / View cart / My orders. Shortcut and product buttons stay valid for 24 hours, checkout buttons for 30 minutes and order buttons for 24 hours; each works once, for the same account (and delivery area where relevant).

### Flow H: voice notes

A voice note is transcribed and handled exactly like typed text, so it can add items, edit the cart, ask for orders or help, also before login. The reply starts with “🎤 “<what was heard>””. Photos still only supply shopping items and need a connected account. During OTP entry the bot asks for the code to be typed and reads no media.

### Flow I: order details, cancellation and invoices

1. ORDER n (or Latest details) shows the order with Track order / Invoice / Cancel order buttons, or Reorder when nothing can be cancelled or cancellation is off.
2. CANCEL ORDER n (or the button) shows a confirmation card, Yes, cancel it / Keep my order. Only Yes cancels: the bot re-reads the order and cancels each cancellable shipment with a neutral reason. A rejection says nothing changed; an uncertain outcome asks the customer to check My Orders and is never retried. The watcher's later cancelled push says “Order X is cancelled.”
3. INVOICE n (or the button) sends JioMart's tax invoice as a PDF when JioMart has issued it, otherwise explains it is not ready.

### Flow J: order-summary PDF

A cash-on-delivery “✅ Order placed” reply and the online “✅ Payment received” push are followed by a one-page PDF summary (lines, charges, total, payment method; “not a tax invoice”). The “Delivered” push attaches JioMart's invoice when available.

### Flow K: staying signed in

Each JioMart request refreshes the access token first when it has 5 minutes or less left. A background loop renews stored sessions every 10 minutes (default) without extending the local 30-day session lifetime. If renewal fails, the next customer request asks for LOGIN. The current live session had already expired on 19 October, so one LOGIN is needed after the deploy.
