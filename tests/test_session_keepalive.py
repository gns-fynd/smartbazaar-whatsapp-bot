import asyncio
import contextlib
import sys
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parents[1]))

from smartbazaar import session_keepalive  # noqa: I001
from smartbazaar.storage import Store


class FakeCommerce:
    """Controllable stand-in for JioMartClient.keep_alive."""

    def __init__(self, *, outcome: bool = True, error: Exception | None = None, mutate: bool = False):
        self.calls: list[dict] = []
        self.outcome = outcome
        self.error = error
        self.mutate = mutate

    async def keep_alive(self, session: dict) -> bool:
        self.calls.append(dict(session))
        if self.error is not None:
            raise self.error
        if self.mutate:
            session["cookies"] = [{"name": "access_token", "value": "renewed", "domain": "", "path": "/"}]
        return self.outcome


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path / "bot.sqlite3"), Fernet.generate_key().decode())
    yield value
    value.close()


def _updated(store: Store, owner: str) -> float:
    row = store.db.execute("SELECT updated FROM sessions WHERE owner=?", (owner,)).fetchone()
    return row[0]


@pytest.mark.asyncio
async def test_run_keepalive_once_only_renews_sessions_with_a_session_and_recent_activity(store):
    now = time.time()
    store.save("owner-active", {"session": {"cookies": []}, "last_inbound": now})
    store.save("owner-stale", {"session": {"cookies": []}, "last_inbound": now - 2 * session_keepalive.ACTIVE_WINDOW_SECONDS})
    store.save("owner-no-session", {"last_inbound": now})
    commerce = FakeCommerce()

    await session_keepalive.run_keepalive_once(store, commerce, {})

    assert len(commerce.calls) == 1


@pytest.mark.asyncio
async def test_run_keepalive_once_persists_renewed_cookies_without_extending_the_session_ttl(store):
    owner = "owner-1"
    store.save(owner, {"session": {"cookies": []}, "last_inbound": time.time()})
    before = _updated(store, owner)
    commerce = FakeCommerce(mutate=True)

    await session_keepalive.run_keepalive_once(store, commerce, {})

    state = store.get(owner)
    assert state["session"]["cookies"][0]["value"] == "renewed"
    assert _updated(store, owner) == before


@pytest.mark.asyncio
async def test_run_keepalive_once_logs_a_refresh_failure_by_category_only(store, caplog):
    owner = "owner-1"
    store.save(owner, {"session": {"cookies": []}, "last_inbound": time.time()})
    commerce = FakeCommerce(error=RuntimeError("secret token xyz for " + owner))

    with caplog.at_level("WARNING", logger="smartbazaar"):
        await session_keepalive.run_keepalive_once(store, commerce, {})

    assert "session_keepalive_failed type=RuntimeError" in caplog.text
    assert "secret" not in caplog.text
    assert owner not in caplog.text


@pytest.mark.asyncio
async def test_run_keepalive_once_logs_an_expired_session_without_identifying_it(store, caplog):
    owner = "owner-1"
    store.save(owner, {"session": {"cookies": []}, "last_inbound": time.time()})
    commerce = FakeCommerce(outcome=False)

    with caplog.at_level("INFO", logger="smartbazaar"):
        await session_keepalive.run_keepalive_once(store, commerce, {})

    assert "session_keepalive_expired" in caplog.text
    assert owner not in caplog.text


@pytest.mark.asyncio
async def test_run_keepalive_once_does_not_crash_the_cycle_on_one_failing_session(store):
    store.save("owner-bad", {"session": {"cookies": []}, "last_inbound": time.time()})
    store.save("owner-good", {"session": {"cookies": []}, "last_inbound": time.time()})

    class SelectiveCommerce:
        def __init__(self):
            self.calls = 0

        async def keep_alive(self, session):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            return True

    commerce = SelectiveCommerce()
    await session_keepalive.run_keepalive_once(store, commerce, {})
    assert commerce.calls == 2


@pytest.mark.asyncio
async def test_run_keepalive_once_waits_for_the_owner_lock_app_uses_for_messages(store):
    owner = "owner-1"
    store.save(owner, {"session": {"cookies": []}, "last_inbound": time.time()})
    commerce = FakeCommerce()
    lock = asyncio.Lock()
    locks = {owner: lock}
    await lock.acquire()

    task = asyncio.create_task(session_keepalive.run_keepalive_once(store, commerce, locks))
    await asyncio.sleep(0.05)
    assert commerce.calls == []  # still blocked behind the "message in flight" lock
    lock.release()
    await task

    assert len(commerce.calls) == 1


@pytest.mark.asyncio
async def test_keepalive_loop_runs_each_cycle_and_survives_a_failing_cycle(monkeypatch):
    calls = []

    async def fake_run_keepalive_once(store, commerce, locks, *, active_within):
        calls.append(active_within)
        if len(calls) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(session_keepalive, "run_keepalive_once", fake_run_keepalive_once)
    task = asyncio.create_task(
        session_keepalive.keepalive_loop(None, None, {}, interval=0.01, active_within=42)
    )
    try:
        async def until_two_calls():
            while len(calls) < 2:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(until_two_calls(), timeout=2)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert calls[0] == 42
    assert len(calls) >= 2
