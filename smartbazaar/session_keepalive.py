"""Background proactive renewal of live JioMart sessions.

JioMart's access JWT lives about an hour; without renewal a signed-in customer's next
request can land after it has expired and fail. `JioMartClient.keep_alive` renews a
session's token before that happens, using the same cookies the chat flow already
persists. This loop finds recently active sessions and renews each one under the same
per-owner lock ``app.py`` uses for message processing, so a renewal never races a
customer's own request, and saves with ``touch=False`` so a silent background renewal
never extends the session's own TTL (see ``order_watch.py`` for the same pattern).
"""

import asyncio
import logging

log = logging.getLogger("smartbazaar")

INTERVAL_SECONDS = 10 * 60
# "Recent customer activity": sessions a customer hasn't used in this long are left alone
# until they write again, even though the session itself may stay valid for much longer.
ACTIVE_WINDOW_SECONDS = 24 * 3600


async def run_keepalive_once(store, commerce, locks: dict, *, active_within: float = ACTIVE_WINDOW_SECONDS) -> None:
    """Renew every live, recently active session once."""
    for owner, _ in store.active_sessions(active_within=active_within):
        async with locks.setdefault(owner, asyncio.Lock()):
            state = store.get(owner)
            session = state.get("session")
            if not session:
                continue
            try:
                alive = await commerce.keep_alive(session)
            except Exception as exc:
                # Category only: never the owner, account reference or any token/cookie value.
                log.warning("session_keepalive_failed type=%s", type(exc).__name__)
                continue
            finally:
                store.save(owner, state, touch=False)
            if not alive:
                log.info("session_keepalive_expired")


async def keepalive_loop(store, commerce, locks: dict, *, interval: float = INTERVAL_SECONDS,
                          active_within: float = ACTIVE_WINDOW_SECONDS) -> None:
    """Renew live sessions on a fixed interval until cancelled."""
    while True:
        await asyncio.sleep(interval)
        try:
            await run_keepalive_once(store, commerce, locks, active_within=active_within)
        except Exception as exc:
            log.warning("session_keepalive_cycle_failed type=%s", type(exc).__name__)
