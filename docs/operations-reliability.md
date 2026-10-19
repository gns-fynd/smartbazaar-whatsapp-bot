# WhatsApp availability and recovery

The bot must distinguish a live HTTP process from a usable messaging channel. An expired Meta token previously allowed inbound processing while rejecting every outbound reply. Renewing a developer user token is a temporary recovery, not an unattended deployment solution.

## Credential checks

`smartbazaar/channel_health.py` checks the configured Meta app/token, phone and messaging account at startup and every five minutes (configurable from 60–3600 seconds). It uses read-only Graph calls, verifies app identity, messaging scope and access to the configured `META_WABA_ID`, and considers both token and data-access expiry. It warns within 24 hours of the earliest positive expiry. Zero expiry means no scheduled expiry, not immunity from revocation or policy/account restrictions. Secrets and upstream response text are excluded from diagnostics.

`GET /healthz` remains HTTP 200 for liveness and includes safe credential, worker and delivery observations. `GET /readyz` returns 503 for degraded readiness, including expiring/invalid credentials, stale checks, failed workers and observed outbound failures. These routes inspect cached state, not a new Graph call on every request. A successful send is provider acceptance; only a receipt or human confirmation proves delivery. Readiness does not establish JioMart checkout availability.

An authentication-rejected outbound reply enters encrypted `auth_wait`, retried once a minute for at most 15 minutes from creation. Expired waiting replies fail without being sent after recovery. This retry sends an already-generated reply and never reruns a shopping action or OTP verification. Older failed replies are not replayed. Other transient errors keep the existing bounded retry policy.

## Local service supervision

`scripts/local_service.py` prepares and installs app/tunnel LaunchAgents with crash restart. Runtime evidence and active paths belong in [project status](project-status.md); successful plist creation alone does not prove a running service. A local Mac still requires a logged-in user, power and working internet. Optional `caffeinate -i` prevents idle system sleep while the app runs, not lid closure, shutdown, forced sleep or network failure.

The attempted first deployment inside Documents exited with launchd EX_CONFIG 78 before emitting application logs. The exact macOS cause was not proven. A fixed `--url` attempt also failed with ngrok ERR_NGROK_313 on the free plan. The manual service was restored and its new callback verified through Meta. Do not retry an unreserved random ngrok URL as if it were a reserved stable domain.

For an unattended manager-facing deployment, use an always-on host, stable public HTTPS endpoint, supervised workers, durable inbound jobs, and an independent alert destination. The current app can expose a degraded readiness signal even when WhatsApp itself cannot send; it cannot deliver an outage warning over an invalid WhatsApp credential. No external alert destination has been configured.

### Active local installation

The staged runtime is `~/Library/Application Support/SmartBazaar`; its `.venv`, `smartbazaar/`, `scripts/`, private `.env`, encrypted `state/bot.sqlite3`, and `work/local-service-logs/` are independent of the source checkout. Source and runtime configurations reference the same active database. Do not start a second app against it. Do not construct `Store` merely to inspect live state; use a read-only SQLite connection because `Store` resets interrupted states on startup.

Run `uv run python scripts/local_service.py status` from the source checkout to verify both jobs are running. `prepare` previews plists; `install` registers them; `uninstall` stops only these marked jobs. The installed configuration is equivalent to:

```sh
python scripts/local_service.py install \
  --runtime-root "$HOME/Library/Application Support/SmartBazaar" \
  --ngrok-path /opt/homebrew/bin/ngrok --caffeinate
```

Omit `--public-url` on the current free plan. `scripts/tunnel_runner.py` starts ngrok with request inspection disabled, discovers the exact localhost:8080 tunnel, waits for app workers, then updates and verifies only the existing Meta app subscription. It preserves existing fields and rejects ambiguous subscriptions. Graph credentials use headers, redirects are rejected, and diagnostics omit provider bodies. Child exits cause the wrapper to exit so launchd restarts it and reconciles any changed URL. There is no permanent public URL on this setup.

Before deploying source changes, run tests, drain work, stop the managed app, wait until launchd reports it unloaded and port 8080 is free, copy changed source into the runtime, then bootstrap it and check local/public readiness and actual delivery. An immediate bootstrap while unload is still finishing can return code 5; confirm the job has gone before retrying. Never overwrite `.env` or the database with repository defaults. Dependency changes must be installed into the staged virtualenv from the lock file. The active private configuration is the staged `.env`; maintain the source `.env` consistently for operational probes. The 18 October app/tunnel restart tests and a post-test user reply succeeded; see status for evidence.

**Drain rule (late evening, 18 October).** "Drain work" means waiting until no inbound event is `processing` and no outbox row is `pending` **or `sending`**. A redeploy during a mid-send caused one duplicate cart card live. On restart, `Store` returns a `sending` row to `pending`, so a message Meta had already accepted was sent again. Check the counts with a read-only SQLite connection or the `events`/`outbox` counts in `/healthz`; do not construct `Store`. Also avoid restarting while a checkout tap is being submitted (an `order_attempts` row in `submitting`).

**Dependencies in the runtime virtualenv.** The picture cart card added Pillow (`pillow>=11` in `pyproject.toml`). The staged runtime has its own `.venv` and no project manifest, so install from the lock file without overwriting runtime files:

```sh
uv export --frozen --no-dev --no-hashes > "$TMPDIR/smartbazaar-requirements.txt"
uv pip install --python "$HOME/Library/Application Support/SmartBazaar/.venv/bin/python" \
  -r "$TMPDIR/smartbazaar-requirements.txt"
```

Run this from the source checkout before copying source that imports a new package, then restart. Without Pillow the app fails at import (`whatsapp.py` imports `cart_image.py`).

### Meta permission pitfall

The similarly named WhatsApp asset and messaging account are separate. Reading a phone and validating a token did not establish sending access. The deployed identity has owner-approved app access, WhatsApp Messages access, and the messaging account's Everything permission because that UI bundles sending with settings/user management and payment-information viewing. Account access is now included in the probe; successful reads still do not prove recipient delivery.

## Demo acceptance

Before sharing the bot, confirm the sender is allowlisted, the Meta test recipient is configured, readiness is green, and an actual inbound message receives a delivered response. The owner deferred adding the manager's number. The current owner remains allowlisted. Test voice/images and business flows independently of channel availability. Cart writes and checkout were disabled until the evening of 18 October; they are now **on** in the runtime (see below). Every tester can therefore change their real JioMart cart and place real orders.

## Real cart, checkout and payment pages — 18 October 2026

### Settings

| Setting | Default | Effect |
|---|---|---|
| `JIOMART_CART_WRITES` | `false` | Real JioMart cart adds/updates/removals and saved-address selection; CART shows the real cart. When off, CART shows the local list and CHECKOUT only explains |
| `JIOMART_CHECKOUT` | `false` | Payment options, order submission and the order-watch worker. Ignored unless cart writes are on |
| `CHECKOUT_MAX_TOTAL` | `0` = no limit (0–200000; was 1500 until 19 Oct) | Optional largest JioMart total, in rupees, the bot may submit after confirmation. The owner removed the default cap on 19 Oct: every order already needs a payment tap on the exact reviewed total |
| `PUBLIC_BASE_URL` | empty | Stable `https://` origin for payment links. When empty, the app asks the local ngrok agent API (`http://127.0.0.1:4040/api/tunnels`) for the HTTPS tunnel serving port 8080, rechecking at most once a minute. Request headers are never used |
| `WHATSAPP_DISPLAY_NUMBER` | empty | Digits for the “Back to WhatsApp” `wa.me` button on the return page; otherwise `whatsapp://` |
| `ORDER_WATCH_INTERVAL_SECONDS` | `20` (5–600) | Order-watcher polling interval |
| `JIOMART_ONLINE_PAYMENT` | `false` | Offer “Pay online” (JIOPP/JioOnePay) on the review card. The bot opens JioMart's payment session server-side with the account's sign-in. Needs checkout on and a public origin; set to `true` in the runtime on 18 October |

Earlier note: `.env.example` listed only the first three settings. As of the late-evening build it lists all of them, including `JIOMART_ONLINE_PAYMENT`. Never commit the private `.env`. As of 21:47 IST on 18 October, the runtime `.env` in `~/Library/Application Support/SmartBazaar` has cart writes and checkout **on**. To stop real-account mutations, set both flags to `false` in that runtime `.env` and restart the managed app using the deployment steps above. Attempts and links already in the database are kept: with checkout off, the order watcher no longer runs, and pay links are still served until they expire.

`/healthz` and `/readyz` report `cart_writes_enabled`, `checkout_enabled` and `scope` (`shopping_cart_checkout` when cart writes are on, otherwise `shopping_draft_memory_media`). Green readiness still says nothing about JioMart checkout availability.

### Payment and return pages

- `GET /pay/{token}` looks up an encrypted, 45-minute `payment_links` row by token digest. Unknown or expired tokens, and attempts no longer awaiting payment, get a 404 page telling the customer to send ORDER STATUS or CHECKOUT. Pages are `no-store`, `noindex` and no-framing, and the CSP has no `form-action` limit because the provider may redirect onward to a bank or UPI page.
  - *Earlier path, still used when the stored fields contain `base64_html`:* the page auto-submits JioMart's form from the browser to the vetted `api_link`, with a manual “Continue to payment” button.
  - *Current JioOnePay path (late evening, proven live):* under the owner's lock, the app reads the account session and calls JioMart's `payment_session` server-side with the stored merchant IDs. It then sends the browser a 303 redirect or an auto-submitting provider form, and saves refreshed cookies without extending the sign-in. With no session it returns 409 (“send LOGIN, then ORDER STATUS”); if JioMart opens no payment page it returns 502 (“nothing was charged”). A payment session opens only once, so a second visit may fail.
- `GET|POST /paid/{token}` is the callback/return page. It schedules an immediate order check for that attempt and shows “Back to WhatsApp”. It never marks payment itself; only JioMart's order record does.
- These routes are public on the tunnel by design. Tokens are random, single-order and short-lived, and links are deleted once the order settles.

### ngrok caveats

- The free ngrok plan shows a browser warning page before serving `/pay`, so the customer must tap through it before JioPay opens. Meta webhooks are unaffected. A reserved domain or `PUBLIC_BASE_URL` on a stable host removes this.
- The dynamic tunnel URL changes when the tunnel restarts. Payment links already sent then stop working, and online payment is not offered while no tunnel origin is known. The public pay route was observed reachable through ngrok on 18 October.

### Order watcher and logs

The watcher runs only when checkout is enabled, inside the app process, and pushes through the normal encrypted outbox. It never extends a customer's sign-in. If the session has expired or the customer logged out, it retries quietly and stops after the seven-day watch period. An unpaid online order that JioMart will not return is dropped quietly after 3 hours (`unpaid`, links deleted). Any new chat order marks the account's older unpaid online attempts `superseded`. The app logger writes INFO to stderr, which is `work/local-service-logs/app.stderr.log` in the supervised runtime. Useful events: `commerce_failed stage=… status=… code=…` (plus a bounded `upstream=` hint for commerce mutation stages only), `payment_modes offered=…`, `payment_link_host_rejected`, `checkout_payment_form_missing`, `orders_listed`, `orders_shape` (field names only), `order_watch stage=…`, `order_total_mismatch`, `order_watch_failed`, and `public_origin updated|unavailable`. Late-evening events: `redirect stage=… to=<host><path>`, `payment_session hop=… status=… host=… type=…`, `payment_session form host=… fields=<names>`, `pay_session_failed`, `checkout_payment_form fields=<names>`, `session_refreshed`/`session_refresh_failed`, `orders_source`/`order_status_source host=storefront`, `checkout_review` (counts and flags only), `compose message=ai|draft prompt=ai|draft`, `compose_failed` and `outbound_card_fallback code=…`. These events are designed to exclude customer identifiers, cookies and order contents; keep it that way when adding events.

Before each live order: confirm readiness, a known public origin and the cap. (The first live order, online and paid by UPI, was placed on 18 October; see status.) Remember that cash on delivery creates the order immediately on tap and an online order is created before payment. An unpaid online order is left for JioMart to cancel. If an outcome is unknown, check My Orders in the JioMart app before sending CLEAR ORDER CHECK.

## Session keep-alive, order cancellation and invoices — 19 October 2026

Implemented and locally tested (307 tests); **not yet staged to the supervised runtime.** See [status](project-status.md).

### New and changed settings

| Setting | Default | Effect |
|---|---|---|
| `SESSION_TTL_SECONDS` | `2592000` (30 days; was `86400`) | Local session lifetime, counted from the customer's last message. Background readers (watcher, `/pay`, keep-alive) save with `touch=False` and never extend it |
| `SESSION_KEEPALIVE_INTERVAL_SECONDS` | `600` (60–3600) | How often the keep-alive loop renews stored JioMart sessions. The loop runs in every app process, whatever the commerce flags |
| `JIOMART_ORDER_CANCEL` | `false` | CANCEL ORDER n and the order card's Cancel order button. Independent of the cart-write and checkout flags. Uses documented routes that JioMart may not accept; keep it off until verified |

`.env.example` lists all three. To stop customer-initiated cancellations, set `JIOMART_ORDER_CANCEL=false` in the runtime `.env` and restart as usual. Invoices need no flag (they are reads).

### Deploying this build

- Stage the new modules `smartbazaar/receipt.py` and `smartbazaar/session_keepalive.py` with the changed source; no new package is required (`receipt.py` uses Pillow, already installed for the cart picture). Follow the drain rule above.
- If the runtime `.env` sets `SESSION_TTL_SECONDS` explicitly, decide whether to keep it; otherwise the 30-day default applies.
- The live JioMart session had already expired on 19 October, so the owner must send LOGIN once after the deploy. Then watch the log for `session_refreshed variant=…` roughly hourly and for the absence of hourly HTTP 401s; that is the live check for the fix.
- Order buttons now last 24 hours and checkout buttons 30 minutes. Buttons issued by the old build keep their old expiry.

### New log events

All exclude customer identifiers, tokens, cookies and order contents.

- `session_refreshed variant=body|cookie`: a refresh succeeded with the documented body or with the cookie-only fallback.
- `session_refresh_failed status=<n> variant=<v>`, `session_refresh_failed reason=transport variant=<v>`, `session_refresh_failed reason=no_access_token cookies=<names>`: a refresh failed (cookie names only, never values).
- `session_keepalive_failed type=<exception class>`: one session's renewal raised; the loop continues with the next session.
- `session_keepalive_expired`: a session could not be renewed (no access token, or refresh failed). The session stays stored and is tried again on every cycle (so a dead session repeats this line, usually with a refresh request, every interval until the customer logs in again or the session ages out); the customer is asked to LOGIN when it is next used.
- `session_keepalive_cycle_failed type=<exception class>`: a whole keep-alive cycle failed; the next cycle runs on schedule.
- `order_cancel_failed code=<diagnostic>`: JioMart rejected a cancellation or its outcome is unknown.
- `invoice_unavailable code=<diagnostic>`: JioMart had no invoice yet or the download was refused (for example `invoice_host_blocked`, `invoice_too_large`, `invoice_not_pdf`).

Documents (invoices and order summaries) travel as base64 PDFs inside the encrypted outbox payload, up to 5 MB each, so a pending outbox row can now be much larger than a text reply. A failed document send falls back to its text; check `outbound_card_fallback code=…`.

## Credential rotation

Use the dedicated Employee-role system user with access restricted to the bot's app and messaging asset. Store its generated token only in the private service configuration. Validate it with the read-only probe, restart the service after draining active work, and verify recipient delivery. Keep app secrets and the session-encryption key unchanged. A no-scheduled-expiry system token remains revocable in Meta; do not describe it as an uptime guarantee.

## Render hosting — 19 October 2026

The owner chose Render to move the bot off the laptop. Implemented and locally verified (the image was built and booted with dummy settings; nothing has been deployed to Render yet):

- `Dockerfile` (Python 3.12 slim, dependencies from `uv.lock`, DejaVu fonts for the order-summary PDF) and `scripts/docker-entrypoint.sh`. The entrypoint makes the database directory on the mounted disk private and owned by the `app` user (uid 10001). It then starts uvicorn on `0.0.0.0:$PORT` as that user.
- `render.yaml` Blueprint: one web service in `singapore` (`DATABASE_PATH=/var/data/bot.sqlite3`). The owner chose Render's **free plan** to avoid a payment card, so there is no disk; a `starter` plan with a 1 GB disk at `/var/data` is left commented in the file. Health check is `/healthz`, not `/readyz`: `/readyz` returns 503 on degraded channel checks, and Render would then refuse or restart a working deploy. `autoDeploy: false`. Secrets are `sync: false`, so Render asks for them in the dashboard and they are never in the repository. The non-secret flags match the live runtime: cart writes, checkout and online payment on; cancel off; no order limit.
- Pay links: when `PUBLIC_BASE_URL` is empty, the app uses Render's own `RENDER_EXTERNAL_URL`, so no manual URL is needed and ngrok is never consulted.
- Local check: `/healthz` 200 on Render's port, Meta webhook verification answered, process uid 10001, database files `0600` in a `0700` directory on the mounted volume, PDF font DejaVu, pay-link origin taken from `RENDER_EXTERNAL_URL`.

### Constraints

- **Exactly one instance.** The order watcher, session keep-alive and per-customer locks run in the process, and a Render service with a disk cannot scale out anyway. A deploy stops the old instance before starting the new one: there is a short gap, and inbound messages being processed at that moment are lost (outbound replies are persisted).
- **Free plan limits.** (1) The service sleeps after 15 minutes without inbound traffic, which pauses the order watcher and the JioMart sign-in keep-alive. `.github/workflows/keep-awake.yml` pings `/healthz` every 5 minutes once the repository variable `RENDER_HEALTH_URL` is set to `https://<service>.onrender.com/healthz`. A 24/7 service uses about 744 of the workspace's 750 free hours a month, and GitHub may delay scheduled runs. (2) There is no disk: the encrypted database resets whenever Render restarts or redeploys the instance. Customers then LOGIN again, open product questions and buttons are lost, and the chat-order records that guard against duplicate submissions start empty. Use the commented `starter` plan and disk (or Postgres) before real customers rely on it.
- **Region risk (unverified).** Render has no India region. JioMart may refuse requests from non-Indian IP addresses. Right after the first deploy, send LOGIN. If the OTP never arrives and the Render log shows `commerce_failed stage=begin_login status=403`, JioMart is blocking Singapore. The fix is then an India-hosted service or egress, not a code change.
- **Data location.** Encrypted customer sessions and order records live on Render's disk in Singapore. Confirm this is acceptable to Fynd before real customers use it.

### Cutover from the Mac (owner steps, in this order)

1. Push the repository to GitHub (`git push`).
2. In Render: New → Blueprint → Public Git Repository `https://github.com/gns-fynd/smartbazaar-whatsapp-bot` (no GitHub connection needed). Enter the secret values from the Mac runtime's private `.env`. Create the service and wait for the deploy to finish; `https://<service>.onrender.com/healthz` should return 200. Then set the `RENDER_HEALTH_URL` repository variable on GitHub so the keep-awake job starts.
3. Stop the Mac services first: `uv run python scripts/local_service.py uninstall` from the source checkout. The Mac tunnel runner re-points Meta's webhook to ngrok every time it starts, so it must be gone before step 4.
4. Meta App Dashboard → WhatsApp → Configuration → Webhook: Callback URL `https://<service>.onrender.com/webhooks/whatsapp`, Verify token = `WEBHOOK_VERIFY_TOKEN`. Verify and save, and keep the `messages` field subscribed.
5. On WhatsApp send LOGIN (the new database starts empty), then search and add something; `/readyz` should turn 200 once the channel check passes.

The Mac database is not migrated by default: customers log in again, and earlier chat-order attempts stay on the Mac. To keep them, copy `state/bot.sqlite3` (taken with `sqlite3 … ".backup"` while the Mac app is stopped) onto the disk with Render's shell or SSH. Use the same `SESSION_ENCRYPTION_KEY`.

### Deploys and rollback

Deploy from the Render dashboard ("Manual Deploy") when `/healthz` shows no `processing` events and no `pending` or `sending` outbox rows, and no order is being submitted. Roll back with Render's "Rollback" to a previous deploy; the disk is kept across deploys and rollbacks.

### Voice replies (Fish Audio) — 19 October 2026

Settings: `FISH_AUDIO_API_KEY` (secret; voice replies are off without it), `FISH_AUDIO_VOICE_ID` (the store manager's cloned voice, a Fish `reference_id`; empty uses Fish's default voice), `FISH_AUDIO_MODEL` (default `s2.1-pro-free`, Fish's free fair-use tier with no SLA; check Fish's pricing page for how long it stays free), and `VOICE_REPLIES` (`voice` default: reply with a voice note only to voice notes; `always`; `off`). Set the key and voice id in Render's Environment. Logs: `voice_note_sent`, `tts_failed code=…` (never the text, key or audio). A failed voice note is dropped (non-retryable, no text resend) and does not degrade `/readyz`. Each voice reply adds one Fish call (about 4 seconds) and one WhatsApp media upload after the text reply is already sent.

Natural speech (19 October): each voice reply now also makes one OpenAI `spoken_reply` call (bounded to 8 seconds) before the Fish call. It falls back to the plain cleaned text when that call fails or the script fails the number check.
