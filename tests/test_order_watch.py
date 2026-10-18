import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_app import FakeAgent, FakeCommerce, FakeWhatsApp, settings, signed_payload, wait_for

from smartbazaar.app import create_app, public_origin
from smartbazaar.conversation import Conversation
from smartbazaar.jiomart import JioMartClient
from smartbazaar.order_watch import OrderWatcher, order_stage
from smartbazaar.storage import Store

OWNER = "owner-1"
SESSION = {"account_reference": "user-1", "application_id": "app-1", "cookies": []}


def order(value, title, *, payment=None, delivered_at=None):
    raw = {
        "order_id": "FY1", "order_created_ts": "2026-10-18T10:00:00Z",
        "breakup_values": [{"name": "total", "value": 74}],
        "payment": {"status": payment, "display_name": "UPI"} if payment else None,
        "shipments": [{
            "shipment_id": "SH1", "shipment_status": {"value": value, "title": title},
            "delivery_date": delivered_at,
            "bags": [{"quantity": 2, "item": {"id": 101, "name": "Sprite 750 ml"}, "prices": {"price_effective": 74}}],
        }],
    }
    return JioMartClient._order_summary(raw)


@pytest.mark.parametrize(("value", "title", "payment", "online", "stage"), [
    ("pending", "Payment pending", "pending", True, "payment_pending"),
    ("pending", "Pending", "pending", False, "unknown"),
    ("refund_pending", "Refund pending", None, True, "unknown"),
    ("placed", "Placed", "paid", True, "confirmed"),
    ("placed", "Placed", None, False, "confirmed"),
    ("placed", "Placed", "failed", True, "confirmed"),
    ("out_for_delivery", "Out for delivery", "paid", True, "out_for_delivery"),
    ("delivery_done", "Delivered", "paid", False, "delivered"),
    ("cancelled_customer", "Cancelled", None, False, "cancelled"),
    ("pending", "Pending", "failed", True, "payment_failed"),
    ("pending", "Pending", "failed", False, "unknown"),
    ("mystery", "Mystery", None, False, "unknown"),
])
def test_order_stage_uses_oms_and_payment_status(value, title, payment, online, stage):
    assert order_stage(order(value, title, payment=payment), awaiting_online=online) == stage


def test_partly_cancelled_order_counts_as_delivered():
    mixed = order("delivery_done", "Delivered")
    mixed["shipments"].append({**mixed["shipments"][0], "state": "cancelled", "value": "cancelled_customer"})
    assert order_stage(mixed) == "delivered"


class SequenceCommerce:
    def __init__(self, orders):
        self.orders = list(orders)
        self.calls = 0

    async def get_order_detail(self, session, order_id):
        self.calls += 1
        return self.orders[min(self.calls, len(self.orders)) - 1]


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path / "bot.sqlite3"), Fernet.generate_key().decode())
    yield value
    value.close()


def attempt(store, *, method="online", state="awaiting_payment", at=None, last_inbound=None):
    account = store.memory_key(SESSION)
    store.save(OWNER, {"session": dict(SESSION), "last_inbound": last_inbound or time.time()})
    attempt_id = store.begin_attempt("confirm-1", OWNER, account, {})
    store.update_attempt(attempt_id, state, {
        "order_id": "FY1", "recipient": "919000000001", "method": method, "notified": None,
        "total": 74, "at": at or time.time(),
    })
    return attempt_id, account


@pytest.mark.asyncio
async def test_online_order_pushes_payment_then_delivery_updates_once_each(store):
    attempt_id, _ = attempt(store)
    commerce = SequenceCommerce([
        order("pending", "Payment pending", payment="pending"),
        order("placed", "Placed", payment="paid"),
        order("out_for_delivery", "Out for delivery", payment="paid"),
        order("out_for_delivery", "Out for delivery", payment="paid"),
        order("delivery_done", "Delivered", payment="paid", delivered_at="2026-10-18T12:15:00Z"),
    ])
    watcher = OrderWatcher(store, commerce)
    sent = []
    for _ in range(5):
        sent += [messages[0] for _, _, messages in await watcher.check({}, only=attempt_id)]

    assert len(sent) == 3
    assert sent[0].startswith("✅ Payment received. Order FY1 for ₹74 is confirmed.")
    assert sent[1].startswith("🛵 Order FY1 is out for delivery!")
    assert sent[2].startswith("📦 Delivered! Order FY1 arrived 18 Oct, 5:45 PM.")
    assert "• 2 × Sprite 750 ml" in sent[2] and "Total: ₹74 (UPI)" in sent[2]
    assert store.get_attempt(attempt_id)[2] == "delivered"
    assert store.watched_attempts() == []


@pytest.mark.asyncio
async def test_cod_confirmation_is_silent_and_session_expiry_is_not_extended(store):
    attempt_id, _ = attempt(store, method="cod", state="placed")
    before = store.db.execute("SELECT updated FROM sessions WHERE owner=?", (OWNER,)).fetchone()[0]
    watcher = OrderWatcher(store, SequenceCommerce([order("placed", "Placed")]))

    assert await watcher.check({}, only=attempt_id) == []

    after = store.db.execute("SELECT updated FROM sessions WHERE owner=?", (OWNER,)).fetchone()[0]
    assert after == before
    assert store.get_attempt(attempt_id)[3]["notified"] == "confirmed"


@pytest.mark.asyncio
async def test_unpaid_order_gets_one_nudge_then_gives_up(store):
    attempt_id, _ = attempt(store, at=time.time() - 40 * 60)
    watcher = OrderWatcher(store, SequenceCommerce([order("pending", "Payment pending", payment="pending")]))

    nudge = await watcher.check({}, only=attempt_id)
    quiet = await watcher.check({}, only=attempt_id)
    give_up = await watcher.check({}, only=attempt_id, now=time.time() + 3 * 3600)

    assert "haven’t seen the payment" in nudge[0][2][0]
    assert quiet == []
    assert "JioMart will likely cancel it" in give_up[0][2][0]
    assert store.get_attempt(attempt_id)[2] == "unpaid"


@pytest.mark.asyncio
async def test_update_outside_whatsapp_window_is_shown_on_the_next_message(store):
    attempt_id, _ = attempt(store, method="cod", state="placed", last_inbound=time.time() - 30 * 3600)
    watcher = OrderWatcher(store, SequenceCommerce([order("delivery_done", "Delivered")]))

    assert await watcher.check({}, only=attempt_id) == []

    class Agent:
        async def interpret(self, text, context):
            from smartbazaar.agent import Intent
            return Intent(action="help", reply="Hi!", presentation="text")

    replies = await Conversation(store, None, Agent()).handle(OWNER, "919000000001", "hi")
    assert replies[0].startswith("Update on your order:\n📦 Delivered! Order FY1")
    again = await Conversation(store, None, Agent()).handle(OWNER, "919000000001", "hi")
    assert not (again[0] if isinstance(again[0], str) else again[0]["body"]).startswith("Update on your order")


def test_public_origin_accepts_only_https_config_or_ngrok_hosts():
    assert public_origin("abc-1.ngrok-free.app") == "https://abc-1.ngrok-free.app"
    assert public_origin("https://bot.example.com/") == "https://bot.example.com"
    assert public_origin("evil.example.com") is None
    assert public_origin("http://bot.example.com") is None


def test_pay_page_posts_jiomarts_form_and_return_page_checks_the_order(tmp_path, monkeypatch):
    config = settings(tmp_path)
    config.whatsapp_display_number = "+1 555 000 9508"
    app = create_app(config, commerce=FakeCommerce(), agent=FakeAgent(), transport=FakeWhatsApp())
    checked = []

    async def fake_check(app_arg, *, only=None):
        checked.append(only)

    monkeypatch.setattr("smartbazaar.app.check_orders", fake_check)
    with TestClient(app) as client:
        token = "t" * 32
        store = app.state.store

        def seed():
            # The app's SQLite connection belongs to its event-loop thread.
            attempt_id = store.begin_attempt("confirm-1", OWNER, "account-1", {})
            store.update_attempt(attempt_id, "awaiting_payment", {"order_id": "FY1"})
            store.save_payment_link(token, attempt_id, {
                "api_link": "https://pay.jio.com/checkout?x=1&y=2", "base64_html": "PGh0bWw+<'",
                "order_id": "FY1", "total": 74,
            }, time.time() + 600)
            return attempt_id

        attempt_id = client.portal.call(seed)
        page = client.get(f"/pay/{token}")
        assert page.status_code == 200
        assert "action='https://pay.jio.com/checkout?x=1&amp;y=2'" in page.text
        assert "name='base64_html' value='PGh0bWw+&lt;&#x27;'" in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert "form-action" not in page.headers["content-security-policy"]
        assert page.headers["cache-control"] == "no-store"

        assert client.get("/pay/" + "x" * 32).status_code == 404

        back = client.post(f"/paid/{token}", data={"status": "SUCCESS"})
        assert back.status_code == 200 and "https://wa.me/15550009508" in back.text
        wait_for(lambda: checked == [attempt_id])

        # Once the order is settled, its payment page can never be used again.
        client.portal.call(lambda: store.update_attempt(attempt_id, "placed", {"order_id": "FY1"}))
        assert client.get(f"/pay/{token}").status_code == 404


def test_public_origin_comes_from_the_local_tunnel_never_request_headers(tmp_path):
    config = settings(tmp_path)
    config.jiomart_cart_writes = config.jiomart_checkout = True

    async def tunnel():
        return "https://abc-1.ngrok-free.app"

    app = create_app(config, commerce=FakeCommerce(), agent=FakeAgent(), transport=FakeWhatsApp(),
                     tunnel_lookup=tunnel)
    with TestClient(app) as client:
        assert app.state.public_origin == "https://abc-1.ngrok-free.app"
        raw, headers = signed_payload("app-secret")
        client.post("/webhooks/whatsapp", content=raw,
                    headers={**headers, "x-forwarded-host": "evil-2.ngrok-free.app"})
        assert app.state.public_origin == "https://abc-1.ngrok-free.app"


def test_a_new_order_supersedes_older_unpaid_ones(store):
    account = store.memory_key(SESSION)
    old = store.begin_attempt("c-old", OWNER, account, {})
    store.update_attempt(old, "awaiting_payment", {"order_id": "OLD"})
    store.save_payment_link("t" * 30, old, {"api_link": "https://www.jiomart.com/x"}, time.time() + 600)
    new = store.begin_attempt("c-new", OWNER, account, {})

    assert store.supersede_unpaid(OWNER, account, keep=new) == 1
    assert store.get_attempt(old)[2] == "superseded"
    assert store.payment_link("t" * 30) is None


@pytest.mark.asyncio
async def test_unreadable_unpaid_order_is_dropped_quietly_after_the_payment_window(store):
    from smartbazaar.jiomart import CommerceError

    attempt_id, _ = attempt(store, at=time.time() - 4 * 3600)

    class NotFound:
        async def get_order_detail(self, session, order_id):
            raise CommerceError("unavailable", "x", stage="order_status", http_status=404)

    assert await OrderWatcher(store, NotFound()).check({}, only=attempt_id) == []
    assert store.get_attempt(attempt_id)[2] == "unpaid"
