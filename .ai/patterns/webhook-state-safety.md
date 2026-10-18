# Webhook and local-state safety pattern

## Overview

The POC accepts an external event only after layered validation, then separates fast receipt from potentially slow conversation work. This prevents arbitrary callers, another WABA, a non-test sender, a stale capture, or a duplicate provider event from reaching the model or commerce adapter. The pattern is implemented by `smartbazaar/app.py`, `smartbazaar/storage.py`, and `smartbazaar/whatsapp.py`.

The state store is intentionally minimal. It retains only a bounded sanitized conversation window rather than full raw inbound messages, excludes OTPs, and allows encrypted session state and delivery retry payloads to survive a normal process restart. It is not a durable inbound work queue or a production multi-process database.

## Where used

- [Login and search POC](../modules/login-search-poc.md): `app.py` verifies the HMAC before JSON parsing, filters WABA/phone/sender values, and claims the event before adding it to `asyncio.Queue`.
- [Login and search POC](../modules/login-search-poc.md): `storage.py` encrypts session/outbox payloads and stores only HMAC digests for event and delivery lookup keys.

## Rules

The raw webhook body must be capped and verified with `X-Hub-Signature-256` before it is decoded. The configured WABA and phone-number identifiers and the test-sender allowlist are separate checks; a valid signature alone is not authority to start a customer conversation. Provider timestamps and event IDs limit replay and duplicate work.

Persist the event claim before queueing it, but never persist raw text merely to make a memory queue restartable. On process restart, the store marks in-progress events interrupted and the customer must resend. This tradeoff avoids retaining more customer content than this POC needs; a later production queue must meet the PRD's durable inbound-event requirement.

Outbox content is encrypted and retry state is isolated from conversation work. A delivery retry must only send an already-created message; it cannot repeat OTP verification, search, cart mutation, payment, or any future order action.

## Gotchas

Do not treat a HTTP 200 from the Meta messages endpoint as customer delivery. The outbox marks provider acceptance separately and incoming status webhooks may later report sent, delivered, read, or failed. Likewise, a configured callback and successful subscription API response are not evidence that a human inbound message reached the application.

Configuration readiness is not provider authentication. An observed outbound HTTP 401 changes `/healthz` to `status=degraded` and `outbound_transport=auth_rejected`; a later accepted send restores `available`. This safe status is process-local, starts `not_observed`, and does not retain raw provider errors or probe on each health request. Other delivery failures remain represented in outbox state/log codes.

Do not reuse the Fernet key casually. It encrypts persisted sessions/outbox records; replacing it makes existing records unreadable. Do not log decrypt failures with the payload, and do not log raw exception text from external services.

## Related patterns

- [Bounded external adapters](bounded-external-adapters.md) handles calls after the webhook has been safely accepted.

## OpenWA variant

`smartbazaar/openwa.py` and the OpenWA ingress in `app.py` preserve the same body limit, raw-body HMAC verification, tester allowlist and deduplication boundary. Use `X-OpenWA-Signature` and the signed session/event identity. Only canonical direct-chat phone JIDs are accepted; never interpret an unresolved `@lid` as a phone number. Freshness uses the provider message timestamp. Events and owner digests include provider/session scope. Separate local databases also prevent pending Meta replies from crossing to OpenWA.

The gateway is a separate trusted local process with its own linked-device credentials. Its ordinary persistence defaults differ from our application: the temporary runtime uses a documented local privacy patch to omit message bodies and webhook replay payloads from the gateway database. This trades durable gateway replay for avoiding OTP copies. It does not control WhatsApp browser/device history. See [runbook](../../docs/openwa-poc.md).

Meta reply buttons use the provider-supplied `button_reply.id`, never the visible button title as authority. IDs are resolved only in the receiving owner's encrypted action map with expiry and account/location binding. A tap never grants a cart/order capability. Structured card outbox records retain the same encryption and delivery-status lifecycle as text.

## Shopping memory and media ingress

Meta audio/image events retain only a minimal media ID/type reference in the process-local queue. The worker checks connected-account and OTP state before provider download. Plain text, product taps and media retain the same signature/allowlist/deduplication boundary; OpenWA media remains unsupported.

`shopping_memory` is encrypted separately from authentication state and keyed by owner plus application/account digest. It survives credential TTL, but never crosses to a newly linked account. Local list edits increment a version and invalidate pending choices; delivery-area changes also clear cached options. LOGOUT deletes local access and owner memory. FORGET affects current-account shopping memory, while the separate encrypted delivery-retention policy remains in force. This is still single-process state, not cross-replica transaction locking.

The reliability build extends readiness with cached startup/periodic credential checks, worker liveness and recent delivery failures. `/readyz` returns 503 when degraded while `/healthz` remains reachable. HTTP 401/403 puts an encrypted reply into `auth_wait` for bounded one-minute retries, expiring after 15 minutes; expired waiting messages are not sent after credential recovery. No inbound action is replayed by this mechanism. It cannot warn the customer over the same broken messaging channel; independent operator alerting remains a deployment requirement.

Quick-reply navigation reuses signed Meta button IDs but uses the `sbqa_` namespace and an owner-scoped encrypted map. IDs are single-use, expire in 15 minutes, and bind to the current account/location. A foreign or stale ID fails closed. Connect taps route through the existing OTP/cooldown logic. Do not interpret button labels as commands or allow model-created arbitrary IDs/actions.

The conversational window is an encrypted, sanitized subset of recent dialogue, not an inbound replay queue. It cannot be used to reconstruct/retry a shopping action after a crash. Account linking, logout and memory reset clear it; address and authentication records never become model history. The public privacy notice describes the eight-message/480-character bound and existing session retention.

## Commerce buttons, order attempts and pay pages — 18 October 2026

Where used: `checkout.py`, `orders.py`, `order_watch.py`, `conversation.py`, `app.py` and `storage.py` in the [module](../modules/login-search-poc.md).

- **Namespaced, owner-bound buttons.** `sbco_` checkout buttons and `sbor_` order buttons are handled in `Conversation._dispatch` before any model call, with the owner's own session and memory (`_account_button`). Checkout buttons are single-use, expire in 10 minutes, and bind to account reference, saved address and pincode, the list version and a SHA-256 snapshot of the reviewed cart. Order buttons are single-use, expire in 15 minutes and bind to the account reference. Stale, foreign or replayed IDs fail closed, and button labels never act as commands.
- **Persist before acting, once.** `Store.begin_attempt` inserts the `order_attempts` row keyed by an HMAC of the button ID before the checkout call (`INSERT OR IGNORE`). A redelivered webhook, a second tap or a concurrent worker cannot submit twice; webhook event deduplication and the owner lock still apply first. Submitting/unknown attempts block new submissions. Nothing ever retries checkout automatically.
- **Survive the handler timeout.** The inbox worker's 150-second `wait_for` can cancel the conversation, but the submission runs in a shielded task that records its own outcome. A failed or timed-out `sbco_` input gets a dedicated recovery reply (“please don’t tap again; send ORDER STATUS”) instead of the generic “check LIST and retry”.
- **Outbox only for pushes.** Order-watcher messages enter the same encrypted outbox with event IDs of the form `watch:<attempt>:<stage>:<minute>`. Delivery retries resend only those messages; they never re-read or re-submit an order. Updates outside the 23.5-hour window are stored as `missed_notice` on the attempt rather than sent.
- **Background readers do not extend sessions.** The watcher saves refreshed cookies with `Store.save(owner, state, touch=False)`, which keeps the existing `updated` timestamp, so polling cannot keep a 24-hour sign-in alive.
- **Pay pages are not webhooks.** `/pay/{token}` and `/paid/{token}` are unsigned public routes. They accept only a 20–64 character URL-safe token, look it up by HMAC digest, and serve nothing for unknown, expired or settled attempts. They also send `no-store`/`noindex`/no-framing headers. `/paid` is navigation only and triggers a status read; it never changes order state. The payment-link origin comes from `PUBLIC_BASE_URL` or the local ngrok agent API, never from request headers such as `X-Forwarded-Host`.

## Late-evening additions — 18 October 2026

- **List replies use the same ID boundary.** Meta `interactive.list_reply.id` is accepted exactly like `button_reply.id` (1–128 characters, prefixed `/button:`). `sbch_` row tokens are resolved only against the owner's encrypted shopping memory for the current list version; unknown tokens become an expired choice. Row titles are never treated as commands.
- **Acknowledgement is fire-and-forget.** The read receipt plus typing indicator runs as a background task per inbound Meta message. It catches transport errors and changes no state.
- **The pay page uses the owner lock and never extends a session.** When `/pay/{token}` opens JioMart's payment session, it takes the same per-owner lock as conversation work and saves refreshed cookies with `touch=False`. It returns only the provider redirect or form, never the account cookie. Superseded and settled attempts have no links, so their tokens 404.
- **Rich messages degrade to stored text.** A `cart_card` is converted to an uploaded image only at send time. Any non-auth failure replaces the encrypted outbox payload with its pre-rendered fallback (`fallback_to_text`), so retries never re-run cart or order work.
- **Drain before deploys, including `sending`.** Restart returns `sending` rows to `pending`, so a message accepted by Meta just before the restart is sent again (observed live as a duplicate cart card). Deploy only when no event is `processing` and no outbox row is `pending` or `sending`.

## Changes on 19 October 2026

Implemented and locally tested; not deployed. Where used: `conversation.py`, `checkout.py`, `orders.py`, `session_keepalive.py`, `app.py`, `storage.py` and `whatsapp.py` in the [module](../modules/login-search-poc.md). These supersede the lifetimes stated above.

- **Longer-lived, still single-use buttons.** `sbqa_` and `sbpc_` entries in `button_actions` expire after 24 hours (was 15 minutes), with at most 40 kept (oldest dropped). `sbco_` checkout buttons expire after 30 minutes (was 10) and no longer carry a list version; `place_order` still re-reads the cart and compares the snapshot, and the whole checkout map is cleared on any checkout tap or new review. `sbor_` order buttons expire after 24 hours (was 15 minutes; at most 15 kept). Every button is popped on use, and binding checks (account, and delivery area where relevant) still fail closed. A longer lifetime is acceptable because no tap can submit an order without the snapshot match, and a cancellation needs a separate confirmation tap.
- **Server-chosen shortcuts on every reply.** `_with_buttons` adds `sbqa_` buttons chosen by `_next_steps` from server state, never from model output; guests get no account shortcuts. A Resend OTP shortcut goes through the normal LOGIN cooldown and hourly limit, to the mobile number of the open challenge.
- **Background renewal uses the same lock and never extends a session.** `session_keepalive.run_keepalive_once` acquires the owner's lock from the shared `app.state.locks` map, so a renewal cannot interleave with that customer's message handling, `/pay` or the order watcher. It always saves with `touch=False`. It selects sessions by `last_inbound`, not by `updated`, so it renews only sessions a customer used within the TTL. Logs carry an exception class or a fixed event name, never the owner, account or token.
- **Voice is customer input; photos are not.** Voice transcripts enter `Conversation.handle` like typed text (`voice=True` only adds the transcript prefix). Photos keep the `shopping_only` restriction. During an OTP challenge the worker refuses all media before download, so an OTP can only be typed.
- **Documents in the outbox.** A `document` payload holds a base64 PDF inside the encrypted outbox row. The transport validates the filename pattern, `%PDF` signature and 5 MB cap at send time, and a failure falls back to the stored text; retries resend the same bytes and never re-fetch an invoice or rebuild a summary.
- **Session lifetime.** Sessions now live 30 days after the customer's last message (was 24 hours). The longer lifetime raises the cost of a stolen database or key; the Fernet key rules above apply unchanged.
