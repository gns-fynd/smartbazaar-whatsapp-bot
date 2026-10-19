# Local POC data model

## Overview

The local POC has a small SQLite schema created directly by `Store`, rather than migrations or a production ORM. It exists to retain only what the current shopping journey needs: encrypted per-customer state, deduplicated provider-event state, encrypted outbound messages, and bounded account-scoped shopping memory. Since the 18 October evening build it also holds local order-attempt and payment-link records for checkout. It is not the PRD's future transactional cart/order model: the cart itself lives in JioMart.

`sessions`, `shopping_memory.data`, `outbox.data`, `order_attempts.data` and `payment_links.data` are encrypted with Fernet before storage. Indexed identifiers are HMAC digests, so the database does not retain provider event IDs or Meta message IDs in plaintext. Raw inbound message text is intentionally absent; this makes a restart require customer resend.

## Tables

| Table | Owner | Key fields | Purpose and lifecycle |
| --- | --- | --- | --- |
| `sessions` | `Store` | digest owner, encrypted `data`, `updated` | Holds conversation state such as the isolated JioMart session, selected address/location and recent queries. Expired rows are deleted by TTL cleanup; the TTL is `SESSION_TTL_SECONDS` (30 days since 19 October, 24 hours before), counted from the last `touch=True` save, which happens on customer messages. |
| `shopping_memory` | `Store` | owner/account composite key, encrypted `data`, `updated` | Stores active day/queries, open product questions, cart-line product details, recent chat orders, explicit preferences and normalized purchase evidence independently of credential TTL (until 18 October also the local draft list); inactive records pruned after 180 days. |
| `events` | `Store` | HMAC event ID, owner, state, `created` | Claims provider events and records processing/completed/interrupted state. In-progress rows are changed to interrupted on startup; rows are pruned after seven days. |
| `outbox` | `Store` | HMAC outbox ID, encrypted recipient/text `data`, state, attempts, retry time, digested provider ID, delivery | Makes outbound messages recoverable and retryable without retaining provider IDs or customer text in cleartext. Since 19 October a payload can be a `document` (base64 PDF up to 5 MB, filename, caption, text fallback). |
| `order_attempts` | `Store` (written by `CheckoutFlow`, `OrderWatcher`, `Conversation`) | `id` = HMAC of `order:` + checkout button ID; `owner`, `account` digests; `state`; encrypted `data`; `created`, `updated` | One row per confirmed checkout button, inserted as `submitting` before the checkout call (`INSERT OR IGNORE`, so a button submits once). Holds the outcome and the watcher's progress. Pruned 30 days after creation. |
| `payment_links` | `Store` (written by `CheckoutFlow`; read by `/pay`, `/paid`, `CheckoutFlow.status`) | `id` = HMAC of `pay:` + random URL token; `attempt`; encrypted `data`; `expires` | Payment hand-off for an online order: vetted `api_link`, `fields` (the checkout's safe scalar `data` fields: JioOnePay `merchant_order_id`/`merchant_transaction_id`, or a legacy `base64_html`), order ID and total. Valid 45 minutes. Deleted when the attempt leaves `awaiting_payment` or is superseded, and pruned one day after expiry. The raw token is never stored. |

## Relationships and flow

Meta owner digests use configured WABA ID plus sender. OpenWA owner digests use an explicit provider prefix, configured gateway session ID and canonical direct-chat ID. One owner maps to at most one `sessions` row and can have many `events`. One event can produce several outbox rows, using an event/index-derived digest. This is a local indexing convention, not a foreign-key-enforced relational model.

The state flow is `claimed -> processing -> completed`; startup changes `processing` to `interrupted`. Outbox flow is `pending -> sending -> sent` or `failed`; startup returns `sending` to `pending`. Provider delivery statuses are additional metadata on sent rows. The only retry limit present is an outbox attempt cap; there is no retry scheduler for inbound work.

## Constraints and limits

The database path defaults to `work/runtime/bot.sqlite3`, uses WAL mode, and its directory/file permissions are restricted by the process. The deployment filesystem and key management remain local-POC assumptions. There is no database migration, backup, multi-process coordination, account-consent schema, authoritative upstream cart version, quote, order attempt, payment or audit-trace table. A local draft version exists inside shopping memory. (Since the evening build, `order_attempts` and `payment_links` exist, created with `CREATE TABLE IF NOT EXISTS`. There is still no upstream cart version, stored quote, payment ledger or audit trace; the reviewed-cart snapshot hash lives only in session-state button records.)

The PRD's proposed durable entities remain future requirements. Do not map a `sessions` row to an authoritative customer or claim it satisfies the POC's final tenant isolation/order-idempotency acceptance criteria.

OpenWA is run with a separate `work/runtime/openwa-bot.sqlite3` file. Switching the transport does not migrate authenticated customer state or pending messages. Fresh OTP linking is required on the new provider. Its gateway keeps its linked-device authentication in separate private local files, outside this application schema.

Product browsing adds a bounded `button_actions` map inside the encrypted session JSON, not a new table. Opaque action IDs bind to the owner/account/location and expire after 15 minutes. The encrypted outbox accepts structured `message` objects as well as legacy `text` records. This preserves queued text compatibility while allowing image/button payloads. No cart-selection entity is introduced.

## Shopping memory payload

The composite primary key combines the existing tenant/channel owner digest with a digest of JioMart application ID and verified account reference. Original legacy consumer sessions have a known application fallback; newly verified sessions persist application scope. Switching accounts changes the memory key. Credentials remain in `sessions`, never this payload.

- `active`: Asia/Kolkata day, last activity timestamp, at most five product queries. Day rollover/four-hour idle resets it and cached choice options.
- `draft` (retired 19 October; deleted by `roll_session` when found): monotonic local `version`, up to 30 lines with query, quantity, selected product or unresolved marker, and selection provenance. Per-line options were bounded to three and expired after 15 minutes.
- `choices` (retired 19 October; deleted by `roll_session`): one displayed line/options reference bound to local version, pincode and expiry.
- `pending` (since 19 October): `{items: [{query, quantity, products ≤3, substitute?}], pincode, expires, tokens}`. `substitute` (later on 19 October) marks a substitute question: `{note}` from an add, or `{note, replaces}` from Find substitutes, where `replaces` is the unavailable cart line's `str((product_uid, item_size))` key removed only after the tapped option is in the cart. Open product questions from the latest add request, asked one at a time; `expires` is 6 hours after creation; `tokens` maps the current question's opaque `sbch_` IDs to option indexes (`-1` = Skip). Any newer add request replaces it.
- `cart_products` (since 19 October): up to 40 entries keyed by `str((item_id, item_size))` with minimal product fields (`id`, `name`, `brand`, `pack`, `category`, `url`, `image_url`), so a cart line can be remembered as a preference.
- `orders`: at most five `{order_id, total, at}` records of chat-placed orders (written by `clear_ordered_lines`).
- `preferences`: at most 50 canonical category keys, minimal selected product fields, explicit source and expiry. Each expires after 180 days.
- `history`: availability status, checked-at timestamp and at most 50 sanitized delivered purchase records with source order/date. Only eligible recent evidence can resolve a choice. Empty history is unresolved. FORGET clears payload and suppresses automatic reimport until an explicit HISTORY request.

`get_memory/save_memory/forget_memory` are Store-owned operations. Controller/tools mutate in-memory copies under the existing owner lock, then save encrypted state. A partial multi-line request retains completed draft work if a later operation fails. There is no distributed transaction with JioMart.

Logout explicitly deletes owner memory; account expiry deletes only authentication state. Retention cleanup runs during store startup/hourly outbox work. These policies do not remove WhatsApp messages, upstream JioMart data, or still-retained encrypted outbound messages.

## Authentication-rejected outbound replies

The existing outbox `state` also admits `auth_wait`. A rejected 401/403 reply is retried
once per minute for no more than 15 minutes from its original creation. `next_outbound`
marks expired waiting records failed before selecting a send, so later credential
recovery cannot replay them. The encrypted payload and original event ID stay unchanged;
only reply delivery is retried, never the user's shopping action. Other transient-error
retries retain their existing attempt cap. Probe/worker readiness evidence is process-local,
not a new durable table.

## Bounded conversational window

`sessions.data.dialogue` holds `{day, scope, last_active, turns}` inside the existing encrypted session payload. Scope is `guest` or the account/application digest; it never supplies model-selectable identity. `turns` contains at most eight `{role, content}` messages, capped at 480 characters each after common contact/credential redaction. Rotation uses Asia/Kolkata day changes or four idle hours. Login/OTP/address controls are excluded; new login, LOGOUT, FORGET and NEW SESSION clear the window. Existing 24-hour inactive session cleanup also applies. No table or migration is added. Account-scoped shopping lists and preferences remain separate and retain their prior lifecycle.

## Order attempts and payment links — 18 October 2026

### `order_attempts.state`

| State | Set by | Meaning |
| --- | --- | --- |
| `submitting` | `begin_attempt` before the checkout call | Submission in progress or interrupted by a crash; blocks new orders |
| `unknown` | `_submit` on transport/5xx, missing order ID, `success:false` with an order ID, or cancellation | Order may exist; blocks new orders until reconciliation or CLEAR ORDER CHECK |
| `failed` | `_submit` on a definite rejection; watcher on `payment_failed` | Nothing placed (or payment failed); `data.reason` holds a bounded diagnostic code |
| `awaiting_payment` | `_submit` for online orders; reconciliation of an online attempt | Order created and unpaid; pay link may exist; watched; ORDER STATUS re-sends a live link |
| `superseded` | `Store.supersede_unpaid` when a newer chat order of the same owner/account is created | Abandoned unpaid online order; links deleted; no longer watched (late evening, 18 October) |
| `placed` | `_submit` for cash on delivery; watcher once an online order is paid; reconciliation | Order created; watched for shipment stages |
| `delivered` / `cancelled` | watcher | Terminal stage pushed |
| `unpaid` | watcher after 3 hours without payment: with a give-up message if JioMart still shows it pending, silently if JioMart will not return the order | Terminal; links deleted; JioMart is expected to cancel |
| `dismissed` | CLEAR ORDER CHECK when reconciliation finds nothing | Customer confirmed no order; unblocks CHECKOUT |
| `watch_ended` | watcher past seven days, including when no session remains to read the order | No further polling |

`Store.watched_attempts` selects only `awaiting_payment`/`placed` rows created within seven days. `latest_attempt(owner, account)` orders by creation and drives pending checks, ORDER STATUS and missed notices.

### `order_attempts.data` (encrypted)

The record holds `cart_id`, confirmed `total`, `at` (start time) and `method` (`cod` or `online`). It also holds `recipient`: the WhatsApp recipient address used for pushes, kept only inside this encrypted payload. `lines` is a list of `{name, quantity}` from the reviewed cart, and `ordered_keys` holds `[item_id, size]` pairs used to clear exactly those list lines. Later fields are `order_id`, `pay_token`, `reason`, and watcher fields `notified` (highest stage pushed), `next_check`, `nudged` and `missed_notice` (text held for the next inbound message).

LOGOUT deletes `sessions` and `shopping_memory` but not attempts. They remain for duplicate-order protection until pruning, and the watcher cannot read orders without a session. Payment links expire on their own.

### Session-state keys (inside encrypted `sessions.data`)

- `checkout`: `{actions: {sbco_…: {kind: place_order|go_checkout|keep_shopping|empty_cart|remove_unavailable, snapshot, binding, expires, method}}}` (19 October: `draft_version` removed, `keep_shopping`/`remove_unavailable` added, expiry 30 minutes). `go_checkout` and `empty_cart` are the CART card's navigation buttons. Earlier builds also issued `remove_others`, now answered only with “no longer used”. `snapshot` is a SHA-256 hex digest of cart ID, sorted line keys/quantities/line totals/availability, total, validity and address ID. `binding` is `account_reference:address_id:pincode`. Entries expire after 10 minutes, and the whole map is cleared on any tap or new review.
- `cart_view`: `{keys, lines, expires, binding}`. Line keys from the last shown cart or disambiguation list, for “remove n from cart”. Since 19 October also `lines` (`{line, name, quantity}`, at most 30) for planner context, and valid 6 hours (was 30 minutes).
- `orders_view`: `{ids, expires, binding}`. Up to five JioMart order IDs from the last ORDERS list, bound to the account reference; valid 6 hours since 19 October (was 30 minutes).
- `order_actions`: `sbor_…` entries `{kind: track|reorder|detail|invoice|cancel|cancel_confirm|keep, order_id, expires, binding}`, single use. Since 19 October at most 15 kept, 24 hours (was nine, 15 minutes).
- `last_inbound`: epoch seconds of the customer's last inbound message, used for the WhatsApp service window and by `Store.active_sessions` for the keep-alive. Background watcher, `/pay` and keep-alive session saves use `touch=False`, so they never extend the session TTL (24 hours until 18 October, 30 days since 19 October).
- Transient keys: `just_added` (product names moved into the cart by this request; popped by the reply or review) and `address_selected` (whether the review's address step succeeded; popped by `_review`).
- The JioMart session's cookie list may change when a token refresh succeeds; the renewed access token is written back into the same cookie.

### Shopping-memory additions

`choices.tokens` maps opaque `sbch_…` row IDs of the current tap menu to option indexes; it is replaced with each new menu and becomes stale when the list version changes. With cart writes on, chosen lines leave `draft.lines` once they move into the JioMart cart, so the list mainly holds unresolved choices.

Selected draft products can carry `product.cart` = `{item_id, item_size, seller_id, store_id, vertical_code, journey, limits: {minimum, maximum, increment, max_in_order}}` from catalog normalization. Lines lacking it are refreshed by exact catalog ID at CHECKOUT. REORDER lines carry `source: "reorder"`. After placement (cash on delivery) or confirmed payment (online), ordered lines are removed with a version bump. `memory.orders` keeps at most five `{order_id, total, at}` records of chat-placed orders.

## Changes on 19 October 2026

Implemented and locally tested; not deployed. No table or column was added and there is no migration: `CREATE TABLE IF NOT EXISTS` schemas are unchanged, and older `draft`/`choices` entries are deleted by `roll_session` on the next message.

- **Session lifetime.** `Store(…, ttl=SESSION_TTL_SECONDS)` now defaults to 2,592,000 seconds (30 days). `Store.active_sessions(active_within=…)` returns unexpired rows that hold a JioMart session and whose `last_inbound` falls within `active_within`; the keep-alive passes the session TTL. Keep-alive saves use `touch=False`.
- **Session-state keys.** `focus` = `{product, pincode, expires}`: the product just shown (single search result, product card tap or View details), 6 hours, valid only for the same pincode. `button_actions` (quick actions `sbqa_` and product buttons `sbpc_`) now expire after 24 hours and keep at most 40 entries; entries carry `binding` = account reference (or mobile) and pincode. Product-button kinds are `add`, `view`, `next` and `search_again`; quick-action kinds include `cancel_login` and `resend_otp`.
- **`order_attempts.data`.** `lines` entries now include `line_total`, and a new `breakup` list holds the review's charge rows (for example “Items: ₹49”, “Total: ₹79”); `receipt.summary_message` uses both. `ordered_keys` is still recorded but no longer clears anything; `clear_ordered_lines` only adds the order to `memory["orders"]`.
- **Outbox `document`.** `{type: "document", filename, data (base64 PDF), caption, fallback}` is stored encrypted like any structured message. `WhatsApp._document` validates `%PDF` and ≤5 MB at send time; on failure the row is replaced by its text fallback.
- **Shopping memory** keys are now `active`, `pending`, `cart_products`, `orders`, `preferences`, `history` and `history_import_disabled`. FORGET clears all of them except that it sets `history_import_disabled` and a `forgotten` history marker.
