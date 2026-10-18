import json

import httpx
import pytest
from cryptography.fernet import Fernet

from smartbazaar.conversation import Conversation
from smartbazaar.jiomart import CommerceError, JioMartClient
from smartbazaar.orders import OrdersFlow
from smartbazaar.storage import Store

APP_ID = "app-1"
OWNER = "owner-1"
SESSION = {"account_reference": "user-1", "application_id": APP_ID, "cookies": []}
LOCATION = {"id": "addr-1", "label": "Home", "city": "Mumbai", "pincode": "400053",
            "country": "India", "country_iso_code": "IN"}


def docs_order(order_id="FY1", *, value="delivery_done", title="Delivered", item_id=7519959):
    """Shaped like the Storefront OrderSchema/Shipments/Bags documentation."""
    return {
        "order_id": order_id,
        "order_created_ts": "2026-05-21T10:00:00Z",
        "breakup_values": [{"name": "subtotal", "value": 70}, {"name": "total", "value": 106}],
        "user_info": {"mobile": "9000000001", "name": "Private Person"},
        "shipments": [{
            "shipment_id": "SH1",
            "shipment_status": {"value": value, "title": title, "hex_code": "#000"},
            "tracking_details": [
                {"status": "Order placed", "time": "2026-05-21T10:00:00Z", "is_passed": True, "is_current": False},
                {"status": "Out for delivery", "time": "2026-05-21T15:00:00Z", "is_passed": True, "is_current": True},
                {"status": "Delivered", "time": "", "is_passed": False, "is_current": False},
            ],
            "promise": {"timestamp": {"min": "2026-05-21T16:00:00Z", "max": "2026-05-21T18:00:00Z"}},
            "delivery_date": "2026-05-21T17:30:00Z",
            "prices": {"amount_paid": 106},
            "bags": [{
                "quantity": 2,
                "item": {"id": item_id, "name": "Coca Cola Zero Sugar Drink 300 ml", "size": "OS",
                         "brand": {"name": "coke zero"}},
                "prices": {"price_effective": 72},
            }, {
                "quantity": 1,
                "item": {"id": 555, "name": "Amul Masti Dahi 400 g", "size": "OS"},
                "prices": {"price_effective": 34},
            }],
        }],
    }


def client(handler):
    return JioMartClient(APP_ID, application_token="t", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_list_orders_uses_my_orders_params_and_returns_safe_summaries(caplog):
    seen = []

    async def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"items": [docs_order()], "page": {"current": 1, "size": 12, "has_next": False}})

    with caplog.at_level("INFO", logger="smartbazaar"):
        orders = await client(handler).list_orders(dict(SESSION))

    assert seen == [{"page_no": "1", "page_size": "12", "status": "1"}]
    order = orders[0]
    assert order["order_id"] == "FY1" and order["total"] == 106 and order["status"] == "Delivered"
    assert order["item_count"] == 3
    assert order["items"][0] == {"item_id": "7519959", "name": "Coca Cola Zero Sugar Drink 300 ml",
                                 "size": "OS", "brand": "coke zero", "quantity": 2, "price": 72,
                                 "bag_id": None, "line_number": None, "identifier": None}
    shipment = order["shipments"][0]
    assert shipment["state"] == "delivered" and shipment["shipment_id"] == "SH1"
    assert [step["status"] for step in shipment["timeline"]] == ["Order placed", "Out for delivery", "Delivered"]
    assert "9000000001" not in json.dumps(order) and "Private Person" not in json.dumps(order)
    shape = [m for m in caplog.messages if m.startswith("orders_shape")]
    assert shape and "user_info" in shape[0] and "9000000001" not in shape[0]


@pytest.mark.asyncio
async def test_track_shipment_returns_only_courier_awb_status_and_time():
    async def handler(request):
        assert request.url.path.endswith("/orders/shipments/SH1/track")
        return httpx.Response(200, json={"success": True, "results": [{
            "account_name": "Shadowfax", "awb": "AWB123", "status": "in_transit",
            "updated_time": "2026-05-21T15:00:00Z", "last_location_recieved_at": "private place",
        }]})

    result = await client(handler).track_shipment(dict(SESSION), "SH1")

    assert result == {"courier": "Shadowfax", "awb": "AWB123", "status": "In transit", "updated_at": 1779375600}


@pytest.mark.asyncio
async def test_history_reads_documented_status_value_and_item_id():
    async def handler(request):
        if request.url.path.endswith("/orders"):
            return httpx.Response(200, json={"items": [docs_order()], "page": {"current": 1, "size": 12, "has_next": False}})
        raise AssertionError("no detail call expected when shipments are present")

    history = await client(handler).get_purchase_history(dict(SESSION))

    assert history["status"] == "available"
    assert {item["id"] for item in history["items"]} == {"7519959", "555"}


@pytest.mark.parametrize(("value", "title", "state"), [
    ("delivery_done", "Delivered", "delivered"), ("out_for_delivery", "Out for delivery", "shipped"),
    ("bag_confirmed", "Confirmed", "processing"), ("cancelled_customer", "Cancelled", "cancelled"),
    ("something_new", "Something", None),
])
def test_shipment_state_maps_oms_values(value, title, state):
    assert JioMartClient._shipment_status({"shipment_status": {"value": value, "title": title}}) == state


class OrdersCommerce:
    def __init__(self, orders=None):
        self.orders = orders if orders is not None else [
            JioMartClient._order_summary(docs_order("FY2", value="out_for_delivery", title="Out for delivery")),
            JioMartClient._order_summary(docs_order("FY1")),
        ]
        self.calls = []

    async def list_orders(self, session):
        self.calls.append("list")
        return self.orders

    async def get_order_detail(self, session, order_id):
        self.calls.append(("detail", order_id))
        if order_id == "LEGACY":
            raise CommerceError("unavailable", "x", stage="order_status", http_status=404)
        return next(order for order in self.orders if order["order_id"] == order_id)

    async def track_shipment(self, session, shipment_id):
        self.calls.append(("track", shipment_id))
        return {"courier": "Shadowfax", "awb": "AWB123", "status": "In transit", "updated_at": None}

    async def search(self, query, location, session):
        self.calls.append(("search", query))
        if "Dahi" in query:
            return {"items": [{"uid": 999, "name": "Other Dahi", "sizes": ["OS"], "seller_id": 1}]}
        return {"items": [{"uid": 7519959, "name": query, "sizes": ["OS"], "seller_id": 1, "sellable": True,
                           "attributes": {"vertical-code": "GROCERIES"}}]}


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path / "bot.sqlite3"), Fernet.generate_key().decode())
    yield value
    value.close()


def state():
    return {"session": dict(SESSION), "location": dict(LOCATION)}


@pytest.mark.asyncio
async def test_recent_lists_orders_with_buttons_and_remembers_numbering(store):
    flow = OrdersFlow(store, OrdersCommerce())
    current = state()

    replies = await flow.recent(current)

    card = replies[0]
    assert card["type"] == "quick_reply"
    assert "1. 21 May · ₹106 · 3 items · Out for delivery" in card["body"]
    assert [b["title"] for b in card["buttons"]] == ["Track latest", "Reorder latest", "Latest details"]
    assert all(b["id"].startswith("sbor_") for b in card["buttons"])
    assert current["orders_view"]["ids"] == ["FY2", "FY1"]


@pytest.mark.asyncio
async def test_detail_and_track_resolve_numbers_against_the_last_list(store):
    commerce = OrdersCommerce()
    flow = OrdersFlow(store, commerce)
    current = state()
    await flow.recent(current)

    detail = await flow.detail(current, 2)
    tracked = await flow.track(current, 1)

    card = detail[0]
    assert "🧾 Order FY1" in card["body"] and "• 2 × Coca Cola Zero Sugar Drink 300 ml — ₹72" in card["body"]
    assert "Total: ₹106" in card["body"]
    # Cancellation is off by default, so a delivered order offers Track, Invoice and Reorder.
    assert [b["title"] for b in card["buttons"]] == ["Track order", "Invoice", "Reorder"]
    assert "Out for delivery" in tracked[0] and "✓ Order placed" in tracked[0] and "➜ Out for delivery" in tracked[0]
    assert "Courier: Shadowfax · In transit (AWB AWB123)" in tracked[0]
    assert ("track", "SH1") in commerce.calls
    assert commerce.calls.count("list") == 1


@pytest.mark.asyncio
async def test_number_outside_the_list_and_legacy_details_are_explained(store):
    commerce = OrdersCommerce()
    commerce.orders.append({**commerce.orders[1], "order_id": "LEGACY"})
    flow = OrdersFlow(store, commerce)
    current = state()

    too_far = await flow.detail(current, 9)
    legacy = await flow.detail(current, 3)

    assert "only have 3 recent orders" in too_far[0]
    assert "Older orders may only be viewable in the JioMart app" in legacy[0]


class ReorderCart(OrdersCommerce):
    """Orders plus a real-cart double: reordered items land in the cart."""

    def __init__(self):
        super().__init__()
        self.added = []

    async def get_cart(self, session, location):
        items = [{"identifiers": {"product_uid": item["item_id"], "identifier": f"line-{i}"}, "quantity": item["quantity"],
                  "item_size": item["item_size"], "item_index": i, "name": "Coca Cola Zero Sugar Drink 300 ml",
                  "line_total": 40.0, "available": True} for i, item in enumerate(self.added)]
        return {"cart_id": "cart-1", "items": items, "price_breakup": {"raw": []}, "is_valid": True,
                "coverage": "complete"}

    async def add_cart_items(self, session, location, items, cart_id=None):
        self.added.extend(items)
        return {"accepted": True, "partial": False}


def reorder_flow(store, commerce):
    from smartbazaar.checkout import CheckoutFlow

    return OrdersFlow(store, commerce, CheckoutFlow(store, commerce, cart_writes=True))


@pytest.mark.asyncio
async def test_reorder_puts_only_the_same_catalog_items_straight_into_the_cart(store):
    commerce = ReorderCart()
    flow = reorder_flow(store, commerce)
    memory = {}

    replies = await flow.reorder(state(), memory, 2)

    assert [(item["item_id"], item["quantity"], item["seller_id"]) for item in commerce.added] == [("7519959", 2, "1")]
    assert "🛒 Added to your JioMart cart from your" in replies[0]
    assert "• 2 × Coca Cola Zero Sugar Drink 300 ml" in replies[0]
    assert "Not available to add right now: Amul Masti Dahi 400 g" in replies[0]
    assert "today’s prices" in replies[0]
    assert "draft" not in memory

    again = await flow.reorder(state(), memory, 2)
    assert "Already in your cart, left as it is: Coca Cola Zero Sugar Drink 300 ml" in again[0]
    assert len(commerce.added) == 1


@pytest.mark.asyncio
async def test_order_buttons_are_single_use_and_account_bound(store):
    commerce = OrdersCommerce()
    flow = OrdersFlow(store, commerce)
    current = state()
    card = (await flow.recent(current))[0]
    track_id = card["buttons"][0]["id"]

    first = await flow.button(current, {}, track_id)
    replay = await flow.button(current, {}, track_id)
    other_account = {**state(), "session": {**SESSION, "account_reference": "user-2"},
                     "order_actions": dict(current["order_actions"])}
    hijack = await flow.button(other_account, {}, card["buttons"][2]["id"])

    assert "🚚 Order FY2" in first[0]
    assert "expired" in replay[0] and "expired" in hijack[0]


class NoAgent:
    async def interpret(self, text, context):
        raise AssertionError("order commands must not need the planner")


@pytest.mark.asyncio
async def test_conversation_routes_order_commands_and_buttons(store):
    from smartbazaar.checkout import CheckoutFlow

    store.save(OWNER, state())
    commerce = ReorderCart()
    bot = Conversation(store, commerce, NoAgent(), CheckoutFlow(store, commerce, cart_writes=True))

    def body(reply):
        return reply if isinstance(reply, str) else reply["body"]

    listed = await bot.handle(OWNER, "919000000001", "orders")
    history = await bot.handle(OWNER, "919000000001", "history")
    detail = await bot.handle(OWNER, "919000000001", "order 2")
    tracked = await bot.handle(OWNER, "919000000001", "track 1")
    reordered = await bot.handle(OWNER, "919000000001", "reorder 2")
    button = await bot.handle(OWNER, "919000000001", "/button:" + history[0]["buttons"][2]["id"])

    assert listed[0]["type"] == history[0]["type"] == "quick_reply"
    assert "🧾 Order FY1" in body(detail[0])
    assert "🚚 Order FY2" in body(tracked[0])
    assert "Added to your JioMart cart from your" in body(reordered[0])
    assert [b["title"] for b in reordered[-1]["buttons"]] == ["View cart", "Checkout", "Keep shopping"]
    assert [item["item_id"] for item in commerce.added] == ["7519959"]
    assert "🧾 Order FY2" in body(button[0])


class CancelCommerce(OrdersCommerce):
    order_cancel_enabled = True

    def __init__(self, *, cancel_error=None, invoice=None):
        super().__init__()
        self.orders[0]["shipments"][0]["can_cancel"] = True
        self.orders[0]["shipments"][0]["shipment_id"] = "SH2"
        self.cancel_error, self.invoice = cancel_error, invoice
        self.cancelled = []

    async def cancellation_reasons(self, session, shipment_id):
        return [{"id": 3, "text": "Delivery is late"}, {"id": 7, "text": "I have changed my mind"}]

    async def cancel_shipment(self, session, shipment_id, *, reason_id, reason_text):
        if self.cancel_error:
            raise self.cancel_error
        self.cancelled.append((shipment_id, reason_id))
        return {"shipment_id": shipment_id, "status": "cancelled_customer"}

    async def invoice_pdf(self, session, shipment_id):
        if self.invoice is None:
            raise CommerceError("unavailable", "x", stage="invoice", http_status=404, diagnostic_code="invoice_unavailable")
        return {"filename": "x.pdf", "content": self.invoice}


@pytest.mark.asyncio
async def test_cancel_needs_a_confirmation_tap_and_uses_a_neutral_reason(store):
    commerce = CancelCommerce()
    flow = OrdersFlow(store, commerce)
    current = state()

    asked = await flow.cancel(current, 1)
    card = asked[0]
    assert card["body"].startswith("Cancel order FY2") and "can’t be undone" in card["body"]
    assert [b["title"] for b in card["buttons"]] == ["Yes, cancel it", "Keep my order"]
    assert commerce.cancelled == []

    done = await flow.button(current, {}, card["buttons"][0]["id"])
    assert done[0].startswith("Cancellation requested for order FY2")
    assert commerce.cancelled == [("SH2", 7)]
    # The confirmation is single-use.
    assert "expired" in (await flow.button(current, {}, card["buttons"][0]["id"]))[0]
    assert len(commerce.cancelled) == 1


@pytest.mark.asyncio
async def test_cancel_keep_unknown_outcome_and_switched_off(store):
    current = state()
    kept = OrdersFlow(store, CancelCommerce())
    card = (await kept.cancel(current, 1))[0]
    assert "stays as it is" in (await kept.button(current, {}, card["buttons"][1]["id"]))[0]

    unknown = CancelCommerce(cancel_error=CommerceError("unknown", "x", stage="cancel_shipment"))
    flow = OrdersFlow(store, unknown)
    card = (await flow.cancel(current, 1))[0]
    reply = await flow.button(current, {}, card["buttons"][0]["id"])
    assert "couldn’t confirm whether JioMart cancelled order FY2" in reply[0]

    off = OrdersFlow(store, OrdersCommerce())
    assert "isn’t switched on yet" in (await off.cancel(state(), 1))[0]


@pytest.mark.asyncio
async def test_invoice_is_sent_as_a_pdf_or_explained_when_not_ready(store):
    ready = OrdersFlow(store, CancelCommerce(invoice=b"%PDF-1.4 test"))
    replies = await ready.invoice(state(), 1)
    assert replies[0] == "🧾 Here’s the invoice for order FY2."
    assert replies[1]["type"] == "document" and replies[1]["filename"] == "JioMart-invoice-FY2.pdf"

    waiting = OrdersFlow(store, CancelCommerce())
    replies = await waiting.invoice(state(), 1)
    assert "couldn’t get the invoice for order FY2" in replies[0]
