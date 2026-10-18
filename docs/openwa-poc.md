# Temporary OpenWA connection

**Retired on 18 October 2026 at the user’s request after Meta business verification succeeded.** The gateway session was logged out, its process stopped, and the agent switched back to Meta with the original database. Setup notes below are historical.

Decision, 18 October 2026: use self-hosted OpenWA for an internal login/search demo while the existing Meta test account remains restricted. This changes only the WhatsApp transport. It does not enable cart, payment or ordering.

## Source and boundary

Upstream: https://github.com/rmyndharis/OpenWA at commit `a3fc0ae4056c704eb51125e44b6511b79a69d4f2` (version 0.25.0). The ignored checkout is `work/openwa`. Node 22.23.1 and npm 10.9.8 installed the lockfile and built the API/dashboard locally. Docker was available as a CLI but its daemon was stopped; the local Node route avoids changing that runtime.

OpenWA uses an unofficial linked-device client. Use a dedicated test WhatsApp account and link it through WhatsApp > Linked devices. It cannot unlock the existing restricted Meta test number. WhatsApp-side restrictions, linking failures and protocol changes remain possible. A working QR screen is not proof of message delivery.

## Local topology

- OpenWA API/dashboard: `http://127.0.0.1:2785`; API prefix `/api`.
- Shopping agent: `http://127.0.0.1:8080`.
- Gateway webhook: `http://127.0.0.1:8080/webhooks/openwa`, signed with a separate random secret.
- `CHANNEL_PROVIDER=openwa` selects the transport; the default is `meta`.
- Use `work/runtime/openwa-bot.sqlite3` for this provider. Preserve `work/runtime/bot.sqlite3` for Meta. Do not transfer a customer session or old pending outbox automatically.
- Gateway secrets live in ignored `work/openwa/.env`; shopping credentials remain in private root `.env`. Never print either file.

The application continues to allow only configured testers. The linked account is the bot account; the tester sends it messages from a different account. Unknown LID identities are refused rather than interpreted as phone numbers.

## Acceptance

1. Link the dedicated account by QR and confirm the gateway reaches connected state.
2. Send HELP from an allowlisted tester and confirm the reply arrives.
3. Send LOGIN, then the fresh JioMart SMS OTP in that same chat.
4. Confirm the session and saved location load, then send “find Diet Coke”.
5. Confirm a concise exact-match or clearly labelled alternative reply arrives.

Do not claim this acceptance passed before a real recipient confirms delivery. Provider acceptance alone is insufficient.

## Rollback

Stop the shopping service before switching. Set `CHANNEL_PROVIDER=meta` and restore `DATABASE_PATH=work/runtime/bot.sqlite3`, keeping the existing Meta settings. Restart the service. The Meta account restriction will still apply. Stop OpenWA when no longer needed; unlink its device in WhatsApp to revoke that connection.

## Gateway privacy patch and start

Apply [the local patch](openwa-local.patch) to the pinned upstream checkout before building. It binds the service to loopback by default and adds `OPENWA_POC_PRIVACY_MODE=true`: the message body write transformer stores an empty body, durable webhook outbox payloads are skipped, Redis webhook delivery is disabled, and failed webhook replay payload retention is forced off. This is a local patch, not an upstream feature claim. It trades durable gateway replay for memory-only delivery. The linked browser/WhatsApp device history remains outside this database guarantee.

Keep `LOG_LEVEL=error` to suppress the upstream first-run API-key banner; private bootstrap credentials are saved under `work/openwa/data`. Keep `QUEUE_ENABLED=false`, `REDIS_ENABLED=false`, `WEBHOOK_FAILURE_PAYLOAD_RETENTION_HOURS=0`, `DATABASE_LOGGING=false`, `CHAT_MEDIA_ARCHIVE_ENABLED=false`, `MEDIA_DOWNLOAD_ENABLED=false`, and `SSRF_ALLOWED_HOSTS=127.0.0.1`. `MESSAGE_INLINE_MEDIA` accepts only `inline` or `archive`; `archive` also requires media archiving, so leave it at the default `inline` with media download disabled for this text-only demo.

From `work/openwa`, run `npm ci --no-audit --no-fund`, `npm run build:all`, then `node dist/main` with owner-only local logs. The private environment pins the installed Chrome executable and disables remote WhatsApp HTML version pinning (`WWEBJS_WEB_VERSION=off`). The dashboard is served at the loopback URL above.

Local validation: the Python adapter suite passed 41 tests with Ruff clean. The gateway privacy patch passed 24 targeted tests plus a direct SQLite body-write check and an outbox no-write check. These checks do not establish live WhatsApp delivery.

## Last observed runtime — 18 October 2026

The gateway API and shopping service are running locally. A `shopping-poc` session was created; its generated UUID is stored only in private runtime configuration. A session/chat-scoped operator key and signed webhook were configured. The gateway's webhook test returned `success: true`, HTTP 200; unsigned calls were rejected with 401 and inactive Meta POST calls with 404. Shopping health has no missing configuration. The session reached `qr_ready`; Chrome was left at the QR dialog for the owner to link a dedicated account. Live account linking, recipient delivery and fresh JioMart login/search through this provider are still pending.

Services currently run in terminal execution sessions, not an installed launch agent. Keep them running during the demo; rebooting or closing the runtime may stop them. Run the gateway with `node dist/main` from its checkout and the shopping service with the README uvicorn command to restart. The loopback webhook does not require ngrok.

### Account linked

The owner confirmed linking after a QR refresh; the gateway API reported `ready`. A connection message to the single configured tester was accepted by OpenWA. Recipient delivery is not yet confirmed, and the new provider has not yet completed a real inbound LOGIN/search.
