import sys
import time
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parents[1]))

from smartbazaar.catalog import normalize_products  # noqa: I001
from smartbazaar.checkout import CheckoutFlow
from smartbazaar.jiomart import CommerceError, JioMartClient
from smartbazaar.storage import Store


# ----- fixtures / fakes -----


def make_state(**location_overrides):
    location = {
        "id": "addr-1",
        "label": "Home",
        "city": "Mumbai",
        "pincode": "400053",
        "country": "India",
        "country_iso_code": "IN",
    }
    location.update(location_overrides)
    return {
        "session": {
            "account_reference": "user-1",
            "application_id": "685945f46c8c7aee3f3af605",
            "cookies": [],
        },
        "location": location,
    }


def make_line(query, product_id, quantity, *, name=None, item_size="OS", seller_id="1",
              store_id="10495", vertical_code="GROCERIES", minimum=1, maximum=4, increment=1,
              max_in_order=4, has_cart=True):
    product = {"id": product_id, "name": name or query}
    product["cart"] = {
        "item_id": product_id, "item_size": item_size, "seller_id": seller_id, "store_id": store_id,
        "vertical_code": vertical_code, "journey": "standard",
        "limits": {"minimum": minimum, "maximum": maximum, "increment": increment, "max_in_order": max_in_order},
    } if has_cart else None
    return {"query": query, "quantity": quantity, "product": product, "source": "explicit_request"}


def make_memory(lines=None):
    # The JioMart cart is the only basket; memory holds no list lines.
    return {}


def make_cart_line(product_id, quantity, item_index, name, price, *, item_size="OS", available=True):
    return {
        "identifiers": {"product_uid": product_id, "identifier": f"line-{item_index}"},
        "quantity": quantity,
        "item_size": item_size,
        "item_index": item_index,
        "name": name,
        "line_total": price * quantity,
        "available": available,
    }


DEFAULT_COD_MODES = [{"name": "COD", "code": "COD", "aggregator": "Fynd", "merchant_code": "COD"}]


class FakeCommerce:
    """Mutable in-memory JioMart cart/checkout double matching _normalise_cart's shape."""

    cash_on_delivery = staticmethod(JioMartClient.cash_on_delivery)

    online_payment = staticmethod(JioMartClient.online_payment)

    def __init__(self, lines=None, catalog=None, *, coverage="complete", is_valid=True,
                 omit_total=False, cart_id="cart-1", checkout_result=None, checkout_error=None,
                 order_status=None, payment_modes_result=None, payment_modes_error=None,
                 search_results=None, search_error=None):
        self.lines = [dict(line) for line in (lines or [])]
        self.catalog = catalog or {}
        self.coverage = coverage
        self.is_valid = is_valid
        self.omit_total = omit_total
        self.cart_id = cart_id
        self.checkout_result = checkout_result
        self.checkout_error = checkout_error
        self.order_status = order_status or {"status": "placed"}
        self.payment_modes_result = payment_modes_result if payment_modes_result is not None else DEFAULT_COD_MODES
        self.payment_modes_error = payment_modes_error
        self.search_results = search_results or {}
        self.search_error = search_error
        self._next_index = len(self.lines)
        self.calls = {
            "get_cart": 0, "add": [], "update": [], "select_address": [],
            "payment_modes": [], "checkout": [], "get_order": [], "search": [],
        }

    async def get_cart(self, session, location):
        self.calls["get_cart"] += 1
        subtotal = sum(line.get("line_total") or 0 for line in self.lines)
        raw = [{"name": "subtotal", "amount": subtotal}]
        if not self.omit_total:
            raw.append({"name": "total", "amount": subtotal})
        return {
            "cart_id": self.cart_id,
            "last_modified": "2026-10-18T10:00:00Z",
            "items": [dict(line) for line in self.lines],
            "price_breakup": {"status": "available", "raw": raw, "display": []},
            "currency": "INR",
            "is_valid": self.is_valid,
            "coverage": self.coverage,
            "raw_item_count": len(self.lines),
            "checkout_context": {"enabled": False, "missing": []},
        }

    async def add_cart_items(self, session, location, items, cart_id=None):
        self.calls["add"].append({"items": items, "cart_id": cart_id})
        for item in items:
            item_id = str(item["item_id"])
            info = self.catalog.get(item_id, {})
            quantity = item["quantity"]
            price = info.get("price", 0.0)
            line = make_cart_line(
                item_id, quantity, self._next_index, info.get("name", f"Product {item_id}"), price,
                item_size=item["item_size"],
            )
            self._next_index += 1
            self.lines.append(line)
        return {"accepted": True, "partial": False}

    async def update_cart(self, session, location, operation, items, cart_id=None):
        self.calls["update"].append({"operation": operation, "items": items, "cart_id": cart_id})
        if operation == "update_item":
            for item in items:
                for line in self.lines:
                    if line.get("item_index") == item.get("item_index"):
                        quantity = item["quantity"]
                        info = self.catalog.get(line["identifiers"].get("product_uid"), {})
                        unit_price = info.get("price")
                        if unit_price is None:
                            previous_quantity = line["quantity"] or 1
                            unit_price = (line.get("line_total") or 0) / previous_quantity
                        line["quantity"] = quantity
                        line["line_total"] = unit_price * quantity
        elif operation == "remove_item":
            indexes = {item.get("item_index") for item in items}
            self.lines = [line for line in self.lines if line.get("item_index") not in indexes]
        return {"accepted": True, "partial": False}

    async def select_address(self, session, location, cart_id, address_id):
        self.calls["select_address"].append({"cart_id": cart_id, "address_id": address_id})

    async def payment_modes(self, session, location, cart_id, amount):
        self.calls["payment_modes"].append({"cart_id": cart_id, "amount": amount})
        if self.payment_modes_error is not None:
            raise self.payment_modes_error
        return [dict(mode) for mode in self.payment_modes_result]

    async def checkout(self, session, location, *, cart_id, address_id, amount, method="cod", callback_url=None):
        self.calls["checkout"].append({"cart_id": cart_id, "address_id": address_id, "amount": amount})
        self.calls.setdefault("checkout_methods", []).append((method, callback_url))
        if self.checkout_error is not None:
            raise self.checkout_error
        return dict(self.checkout_result or {"order_id": "FY123"})

    async def get_order(self, session, order_id):
        self.calls["get_order"].append(order_id)
        return dict(self.order_status)

    async def search(self, query, location, session):
        self.calls["search"].append(query)
        if self.search_error is not None:
            raise self.search_error
        return {"items": self.search_results.get(query, [])}


def new_store(tmp_path, name="test.db"):
    return Store(str(tmp_path / name), Fernet.generate_key().decode())


# ----- 1. cart_writes disabled -----


@pytest.mark.asyncio
async def test_cart_writes_disabled_blocks_prepare_without_commerce_calls(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory()
    commerce = FakeCommerce(lines=[])
    account = store.memory_key(state["session"])
    flow = CheckoutFlow(store, commerce, cart_writes=False)

    reply = await flow.prepare("owner", state, memory, account)
    added = await flow.add_products(state, memory, [make_line("coke zero", "101", 1)])

    assert "isn’t switched on yet" in reply[0]
    assert "isn’t switched on yet" in added["error"] and added["added"] == []
    assert commerce.calls == {
        "get_cart": 0, "add": [], "update": [], "select_address": [],
        "payment_modes": [], "checkout": [], "get_order": [], "search": [],
    }


# ----- 2. the JioMart cart is the basket: products go straight in, once -----


@pytest.mark.asyncio
async def test_add_products_puts_new_products_in_the_cart_and_leaves_existing_lines_alone(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory()
    commerce = FakeCommerce(
        lines=[
            make_cart_line("101", 1, 0, "Coca Cola Zero 300 ml", 36.0),
            make_cart_line("999", 3, 1, "Other Item", 30.0),
        ],
        catalog={
            "101": {"name": "Coca Cola Zero 300 ml", "price": 36.0},
            "102": {"name": "Maggi Noodles", "price": 14.0},
        },
    )
    account = store.memory_key(state["session"])
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=False)

    result = await flow.add_products(state, memory, [
        make_line("coke zero", "101", 2, name="Coca Cola Zero 300 ml"),
        make_line("maggi", "102", 1, name="Maggi Noodles", seller_id="5"),
    ])

    assert commerce.calls["add"] == [{
        "items": [{"item_id": "102", "item_size": "OS", "seller_id": "5", "vertical_code": "GROCERIES", "quantity": 1}],
        "cart_id": "cart-1",
    }]
    # A product already in the cart is never re-quantified by an add.
    assert commerce.calls["update"] == []
    assert result == {"added": ["1 × Maggi Noodles"], "already": ["Coca Cola Zero 300 ml"], "notes": [], "error": None}
    # Each cart line remembers its product, for "remember cart line 2 for noodles".
    assert memory["cart_products"][str(("102", "OS"))]["name"] == "Maggi Noodles"

    reply = await flow.prepare("owner", state, memory, account)
    text = reply[0]
    assert "1 × Coca Cola Zero 300 ml" in text and "1 × Maggi Noodles — ₹14 (just added)" in text
    assert "3 × Other Item" in text
    assert commerce.calls["select_address"] == [{"cart_id": "cart-1", "address_id": "addr-1"}]
    assert len(commerce.calls["add"]) == 1


@pytest.mark.asyncio
async def test_an_item_removed_from_the_cart_is_never_re_added_by_the_next_checkout(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory()
    commerce = FakeCommerce(lines=[], catalog={"201": {"name": "Whey Protein 907 g", "price": 4317.0}})
    account = store.memory_key(state["session"])
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=False)

    await flow.add_products(state, memory, [make_line("whey", "201", 1, name="Whey Protein 907 g")])
    commerce.lines = []  # the customer removed it from the cart
    second = await flow.prepare("owner", state, memory, account)

    assert len(commerce.calls["add"]) == 1
    assert "Your JioMart cart is empty" in second[0]


@pytest.mark.asyncio
async def test_plain_checkout_reviews_the_cart_without_adding_anything(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    memory = {"draft": {"version": 1, "lines": [make_line("whey", "201", 1, name="Whey Protein 907 g")]}}
    commerce = FakeCommerce(lines=[make_cart_line("301", 1, 0, "Tomato 1 kg", 48.0)],
                            catalog={"201": {"name": "Whey Protein 907 g", "price": 4317.0}})
    account = store.memory_key(state["session"])
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=False)

    reply = await flow.prepare("owner", state, memory, account)

    # Even a list saved by an older build is never copied into the cart.
    assert commerce.calls["add"] == []
    assert "1 × Tomato 1 kg" in reply[0] and "Whey" not in reply[0]


def test_breakup_shows_unmapped_charges_so_the_total_always_adds_up():
    cart = {"price_breakup": {"raw": [{"name": "subtotal", "amount": 49}, {"name": "discount", "amount": -3},
                                      {"name": "handling_fee", "amount": 33}, {"name": "total", "amount": 79}]}}
    assert CheckoutFlow._breakup_lines(cart) == [
        "Items: ₹49", "Discount: ₹-3", "Delivery & other charges: ₹33", "Total: ₹79"]


# ----- 3. quantity cap, duplicates and stale products -----


@pytest.mark.asyncio
async def test_add_products_caps_quantity_and_never_sums_duplicates(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory()
    commerce = FakeCommerce(lines=[], catalog={"101": {"name": "Coca Cola Zero 300 ml", "price": 36.0},
                                               "301": {"name": "Tomato 1 kg", "price": 48.0}})
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=False)

    result = await flow.add_products(state, memory, [
        make_line("tomato", "301", 1, name="Tomato 1 kg"),
        make_line("tomato", "301", 1, name="Tomato 1 kg"),
        make_line("tomato", "301", 1, name="Tomato 1 kg"),
        make_line("coke zero", "101", 10, name="Coca Cola Zero 300 ml"),
        make_line("milk", "103", 1, name="Amul Milk", has_cart=False),
    ])

    added = {item["item_id"]: item["quantity"] for item in commerce.calls["add"][0]["items"]}
    assert added == {"301": 1, "101": 4}
    assert "JioMart allows at most 4 of Coca Cola Zero 300 ml per order, so I used 4." in result["notes"]
    assert "Couldn’t find these on JioMart right now: Amul Milk." in result["notes"]
    assert result["added"] == ["1 × Tomato 1 kg", "4 × Coca Cola Zero 300 ml"]


# ----- 3b. stale cart refs are silently re-resolved by exact catalog id -----


@pytest.mark.asyncio
async def test_add_products_auto_refreshes_a_stale_product_by_re_searching_its_name(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory()
    raw_item = {
        "uid": "103", "name": "Amul Milk 500 ml", "sizes": ["OS"], "seller_id": "9",
        "moq": {"minimum": 1, "maximum": 2, "increment_unit": 1},
        "attributes": {"max-qty-in-order": 2, "vertical-code": "GROCERIES"},
    }
    commerce = FakeCommerce(lines=[], search_results={"Amul Milk": [raw_item]})
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=False)

    result = await flow.add_products(state, memory, [make_line("milk", "103", 1, name="Amul Milk", has_cart=False)])

    assert commerce.calls["search"] == ["Amul Milk"]
    assert commerce.calls["add"] == [{
        "items": [{"item_id": "103", "item_size": "OS", "seller_id": "9", "vertical_code": "GROCERIES", "quantity": 1}],
        "cart_id": "cart-1",
    }]
    assert result["notes"] == [] and result["added"] == ["1 × Amul Milk 500 ml"]


@pytest.mark.asyncio
async def test_add_products_propagates_auth_expired_from_the_background_refresh_search(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    commerce = FakeCommerce(
        lines=[], search_error=CommerceError("auth_expired", "Sign-in has expired.", stage="search")
    )
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=False)

    with pytest.raises(CommerceError) as failure:
        await flow.add_products(state, {}, [make_line("milk", "103", 1, name="Amul Milk", has_cart=False)])
    assert failure.value.code == "auth_expired"


# ----- 4. place order button presence vs checkout disabled -----


@pytest.mark.asyncio
async def test_checkout_enabled_adds_place_order_button_disabled_omits_it(tmp_path):
    store = new_store(tmp_path)

    def build(**flags):
        state = make_state()
        memory = make_memory()
        commerce = FakeCommerce(
            lines=[make_cart_line("101", 1, 0, "Coca Cola Zero 300 ml", 36.0)],
            catalog={"101": {"name": "Coca Cola Zero 300 ml", "price": 36.0}},
        )
        account = store.memory_key(state["session"])
        flow = CheckoutFlow(store, commerce, cart_writes=True, max_total=1500, **flags)
        return flow, state, memory, account

    flow_on, state_on, memory_on, account_on = build(checkout=True)
    reply_on = await flow_on.prepare("owner-on", state_on, memory_on, account_on)
    last_on = reply_on[-1]
    assert isinstance(last_on, dict) and last_on["type"] == "quick_reply"
    buttons = last_on["buttons"]
    assert any(button["title"] == "Cash on delivery" for button in buttons)
    assert all(len(button["title"]) <= 20 for button in buttons)
    assert "fallback" in last_on

    flow_off, state_off, memory_off, account_off = build(checkout=False)
    reply_off = await flow_off.prepare("owner-off", state_off, memory_off, account_off)
    last_off = reply_off[-1]
    assert not isinstance(last_off, dict)
    assert "finish in the JioMart app" in last_off
    assert "Place order" not in last_off


# ----- 5. blockers suppress the place-order button -----


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_substring"),
    [
        ("total_too_high", "up to ₹10"),
        ("unavailable_item", "be delivered right now"),
        ("missing_address_id", "Choose a saved delivery address with ADDRESSES"),
        ("partial_coverage", "safely place this order"),
        ("missing_total", "return a total"),
        ("no_cod_available", "isn’t offering a payment method"),
        ("payment_options_unavailable", "check payment options with JioMart"),
    ],
)
async def test_blockers_suppress_the_place_order_button(tmp_path, kind, expected_substring):
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory()
    account = store.memory_key(state["session"])

    if kind == "missing_address_id":
        del state["location"]["id"]

    line = make_cart_line(
        "101", 1, 0, "Coca Cola Zero 300 ml", 36.0, available=kind != "unavailable_item"
    )
    payment_kwargs = {}
    if kind == "no_cod_available":
        payment_kwargs["payment_modes_result"] = [
            {"name": "UPI", "code": "UPI", "aggregator": "Fynd", "merchant_code": "UPI"}
        ]
    if kind == "payment_options_unavailable":
        payment_kwargs["payment_modes_error"] = CommerceError(
            "unavailable", "Unable to complete the request.",
            stage="payment_options", http_status=500, diagnostic_code="http_5xx",
        )
    commerce = FakeCommerce(
        lines=[line],
        catalog={"101": {"name": "Coca Cola Zero 300 ml", "price": 36.0}},
        coverage="partial" if kind == "partial_coverage" else "complete",
        omit_total=kind == "missing_total",
        **payment_kwargs,
    )
    max_total = 10 if kind == "total_too_high" else 1500
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=True, max_total=max_total)

    reply = await flow.prepare("owner", state, memory, account)

    # A blocked review explains why and offers ways forward, never a payment button.
    prompt = reply[-1]
    assert prompt["type"] == "quick_reply" and expected_substring in prompt["body"]
    titles = [button["title"] for button in prompt["buttons"]]
    assert titles[-2:] == ["Keep shopping", "Empty cart"]
    assert ("Remove unavailable" in titles) == (kind == "unavailable_item")
    assert not any(title.startswith(("Pay", "Cash")) for title in titles)


# ----- 6. confirmed place-order is single-use -----


@pytest.mark.asyncio
async def test_place_order_confirms_once_records_attempt_and_rejects_replay(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory()
    commerce = FakeCommerce(
        lines=[make_cart_line("101", 1, 0, "Coca Cola Zero 300 ml", 36.0)],
        catalog={"101": {"name": "Coca Cola Zero 300 ml", "price": 36.0}},
        checkout_result={"order_id": "FY123"},
    )
    account = store.memory_key(state["session"])
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=True, max_total=1500)

    reply = await flow.prepare("owner", state, memory, account)
    action_id = reply[-1]["buttons"][0]["id"]

    confirm_reply = await flow.handle_button("owner", state, memory, account, action_id)

    assert commerce.calls["checkout"] == [{"cart_id": "cart-1", "address_id": "addr-1", "amount": 36.0}]
    attempt_id, attempt_state, data = store.latest_attempt("owner", account)
    assert attempt_state == "placed"
    assert data["order_id"] == "FY123"
    assert "FY123" in confirm_reply[0]
    assert "Order placed" in confirm_reply[0]
    # The confirmation carries an order summary PDF.
    assert confirm_reply[1]["type"] == "document" and confirm_reply[1]["filename"] == "JioMart-order-FY123.pdf"
    assert memory["orders"][0]["order_id"] == "FY123"

    replay_reply = await flow.handle_button("owner", state, memory, account, action_id)
    assert "expired" in replay_reply[0]
    assert len(commerce.calls["checkout"]) == 1

    # A duplicate confirmation id (e.g. a retried webhook) is also rejected at the store level.
    first = store.begin_attempt("dup-confirm-1", "owner", account, {"foo": "bar"})
    second = store.begin_attempt("dup-confirm-1", "owner", account, {"foo": "bar"})
    assert first is not None
    assert second is None


# ----- 7. snapshot / binding protection -----


async def _prepared_button(tmp_path, *, name="test.db"):
    store = new_store(tmp_path, name)
    state = make_state()
    memory = make_memory()
    commerce = FakeCommerce(
        lines=[make_cart_line("101", 1, 0, "Coca Cola Zero 300 ml", 36.0)],
        catalog={"101": {"name": "Coca Cola Zero 300 ml", "price": 36.0}},
    )
    account = store.memory_key(state["session"])
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=True, max_total=1500)
    reply = await flow.prepare("owner", state, memory, account)
    action_id = reply[-1]["buttons"][0]["id"]
    return flow, state, memory, account, commerce, action_id


@pytest.mark.asyncio
async def test_cart_change_before_tap_blocks_checkout(tmp_path):
    flow, state, memory, account, commerce, action_id = await _prepared_button(tmp_path)

    commerce.lines[0]["quantity"] = 2
    commerce.lines[0]["line_total"] = 72.0

    reply = await flow.handle_button("owner", state, memory, account, action_id)

    assert commerce.calls["checkout"] == []
    assert "cart changed since that review" in reply[0]


@pytest.mark.asyncio
async def test_expired_confirmation_blocks_checkout(tmp_path):
    flow, state, memory, account, commerce, action_id = await _prepared_button(tmp_path)

    state["checkout"]["actions"][action_id]["expires"] = time.time() - 1

    reply = await flow.handle_button("owner", state, memory, account, action_id)

    assert commerce.calls["checkout"] == []
    assert "expired" in reply[0]


@pytest.mark.asyncio
async def test_address_binding_change_before_tap_blocks_checkout(tmp_path):
    flow, state, memory, account, commerce, action_id = await _prepared_button(tmp_path)

    state["location"]["id"] = "addr-2"

    reply = await flow.handle_button("owner", state, memory, account, action_id)

    assert commerce.calls["checkout"] == []
    assert "expired" in reply[0]


# ----- 8. checkout failures -----


@pytest.mark.asyncio
async def test_checkout_definite_rejection_marks_attempt_failed(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory()
    commerce = FakeCommerce(
        lines=[make_cart_line("101", 1, 0, "Coca Cola Zero 300 ml", 36.0)],
        catalog={"101": {"name": "Coca Cola Zero 300 ml", "price": 36.0}},
        checkout_error=CommerceError(
            "rejected", "JioMart did not accept that cart change.",
            stage="checkout", http_status=400, diagnostic_code="cart_rejected",
        ),
    )
    account = store.memory_key(state["session"])
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=True, max_total=1500)

    reply = await flow.prepare("owner", state, memory, account)
    action_id = reply[-1]["buttons"][0]["id"]

    confirm_reply = await flow.handle_button("owner", state, memory, account, action_id)

    assert "nothing was placed" in confirm_reply[0]
    _, attempt_state, _ = store.latest_attempt("owner", account)
    assert attempt_state == "failed"


@pytest.mark.asyncio
async def test_checkout_code_changed_also_marks_attempt_failed(tmp_path):
    """The adapter's own checkout() raises code "changed" when the total moved before submit."""
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory()
    commerce = FakeCommerce(
        lines=[make_cart_line("101", 1, 0, "Coca Cola Zero 300 ml", 36.0)],
        catalog={"101": {"name": "Coca Cola Zero 300 ml", "price": 36.0}},
        checkout_error=CommerceError(
            "changed", "The cart changed before checkout.",
            stage="checkout_precheck", http_status=None, diagnostic_code="total_changed",
        ),
    )
    account = store.memory_key(state["session"])
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=True, max_total=1500)

    reply = await flow.prepare("owner", state, memory, account)
    action_id = reply[-1]["buttons"][0]["id"]

    confirm_reply = await flow.handle_button("owner", state, memory, account, action_id)

    assert "nothing was placed" in confirm_reply[0]
    _, attempt_state, _ = store.latest_attempt("owner", account)
    assert attempt_state == "failed"


@pytest.mark.asyncio
async def test_checkout_transport_failure_blocks_further_orders_until_cleared(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory()
    commerce = FakeCommerce(
        lines=[make_cart_line("101", 1, 0, "Coca Cola Zero 300 ml", 36.0)],
        catalog={"101": {"name": "Coca Cola Zero 300 ml", "price": 36.0}},
        checkout_error=CommerceError(
            "unavailable", "JioMart is unavailable. Please try again.",
            stage="checkout", http_status=None, diagnostic_code="transport_error",
        ),
    )
    account = store.memory_key(state["session"])
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=True, max_total=1500)

    reply = await flow.prepare("owner", state, memory, account)
    action_id = reply[-1]["buttons"][0]["id"]

    confirm_reply = await flow.handle_button("owner", state, memory, account, action_id)
    assert "confirm whether JioMart placed this order" in confirm_reply[0]
    _, attempt_state, _ = store.latest_attempt("owner", account)
    assert attempt_state == "unknown"

    blocked_reply = await flow.prepare("owner", state, memory, account)
    assert "send CLEAR ORDER CHECK" in blocked_reply[0]
    assert len(commerce.calls["checkout"]) == 1

    cleared_reply = await flow.clear_pending("owner", state, account)
    assert "Cleared" in cleared_reply[0]

    unblocked_reply = await flow.prepare("owner", state, memory, account)
    assert "🛒" in unblocked_reply[0]
    assert len(commerce.calls["checkout"]) == 1


# ----- 9. payment url -----


@pytest.mark.asyncio
async def test_online_payment_creates_one_order_with_a_single_pay_link(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory()
    form = {"api_link": "https://pay.jio.com/checkout", "base64_html": "PGh0bWw+"}
    commerce = FakeCommerce(
        lines=[make_cart_line("101", 1, 0, "Coca Cola Zero 300 ml", 36.0)],
        catalog={"101": {"name": "Coca Cola Zero 300 ml", "price": 36.0}},
        checkout_result={"order_id": "FY999", "payment": form},
        payment_modes_result=[
            {"name": "JIOPP", "code": "JIOPP", "aggregator": "Jio", "merchant_code": "JIOPP",
             "api_link": form["api_link"]},
            *DEFAULT_COD_MODES,
        ],
    )
    account = store.memory_key(state["session"])
    origin = "https://bot.ngrok-free.app"
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=True, max_total=1500,
                        public_origin=lambda: origin, online_payment=True)

    reply = await flow.prepare("owner", state, memory, account)
    titles = [button["title"] for button in reply[-1]["buttons"]]
    assert titles[:2] == ["Pay online ₹36", "Cash on delivery"]
    assert "Pay online: JioPay opens in your browser" in reply[-1]["body"]

    confirm = await flow.handle_button("owner", state, memory, account, reply[-1]["buttons"][0]["id"],
                                       recipient="919000000001")

    card = confirm[0]
    assert card["type"] == "cta_url" and card["button"] == "Pay ₹36"
    assert card["url"].startswith(origin + "/pay/") and card["url"] in card["fallback"]
    token = card["url"].rsplit("/", 1)[1]
    method, callback = commerce.calls["checkout_methods"][0]
    assert method == "online" and callback == f"{origin}/paid/{token}"
    attempt_id, link = store.payment_link(token)
    assert link == {**form, "order_id": "FY999", "total": 36.0}
    _, attempt_state, data = store.latest_attempt("owner", account)
    assert attempt_state == "awaiting_payment"
    assert data["recipient"] == "919000000001" and data["method"] == "online" and data["pay_token"] == token

    again = await flow.status("owner", state, account)
    assert "send CHECKOUT and choose Pay online again" in again[0]
    assert again[1]["url"] == card["url"]


@pytest.mark.asyncio
async def test_online_payment_is_not_offered_without_a_public_origin(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory()
    commerce = FakeCommerce(
        lines=[make_cart_line("101", 1, 0, "Coca Cola Zero 300 ml", 36.0)],
        catalog={"101": {"name": "Coca Cola Zero 300 ml", "price": 36.0}},
        payment_modes_result=[{"name": "JIOPP", "code": "JIOPP", "aggregator": "Jio", "merchant_code": "JIOPP",
                               "api_link": "https://pay.jio.com/checkout"}],
    )
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=True, max_total=1500)

    reply = await flow.prepare("owner", state, memory, store.memory_key(state["session"]))

    assert "isn’t offering a payment method" in reply[-1]["body"]
    assert commerce.calls["checkout"] == []


# ----- 10. retired remove-others buttons -----


@pytest.mark.asyncio
async def test_an_old_remove_others_button_changes_nothing(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    memory = make_memory([])
    commerce = FakeCommerce(lines=[make_cart_line("999", 3, 0, "Other Item", 30.0)])
    account = store.memory_key(state["session"])
    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=True, max_total=1500)
    cart = await commerce.get_cart(state["session"], state["location"])
    button = flow._button(state, "remove_others", "Remove other items", cart, memory)

    reply = await flow.handle_button("owner", state, memory, account, button["id"])

    assert "no longer used" in reply[0]
    assert commerce.calls["update"] == []


# ----- 11. status -----


@pytest.mark.asyncio
async def test_status_without_attempts_and_with_placed_order(tmp_path):
    store = new_store(tmp_path)
    state = make_state()
    commerce = FakeCommerce(lines=[], order_status={"status": "placed"})
    account = store.memory_key(state["session"])
    flow = CheckoutFlow(store, commerce)

    empty_reply = await flow.status("owner", state, account)
    assert "placed an order through this chat yet" in empty_reply[0]

    attempt_id = store.begin_attempt("confirm-status-1", "owner", account, {"order_id": "FY123"})
    store.update_attempt(attempt_id, "placed", {"order_id": "FY123"})

    placed_reply = await flow.status("owner", state, account)
    assert commerce.calls["get_order"] == ["FY123"]
    assert placed_reply == ["Order FY123: placed."]


# ----- 12. adapter unit tests (no network) -----


def test_add_items_requires_listing_identifiers_and_normalizes_digit_strings():
    items = [{"item_id": "101", "item_size": "OS", "seller_id": "5", "quantity": 2, "vertical_code": "GROCERIES"}]
    assert JioMartClient._add_items(items) == [{
        "item_id": 101, "item_size": "OS", "quantity": 2,
        "meta": {"vertical_code": "GROCERIES", "compute_delivery_fee": True}, "seller_id": 5,
    }]

    # An unrecognised/missing vertical code falls back to "default", never store_id.
    defaulted = JioMartClient._add_items(
        [{"item_id": "101", "item_size": "OS", "seller_id": "5", "quantity": 1}]
    )
    assert defaulted[0]["meta"]["vertical_code"] == "default"
    assert "store_id" not in defaulted[0]

    one_size = [{"item_id": "101", "item_size": "ONE SIZE", "seller_id": "5", "quantity": 1}]
    assert JioMartClient._add_items(one_size)[0]["item_size"] == "ONE SIZE"

    with pytest.raises(ValueError, match="quantity"):
        JioMartClient._add_items([{"item_id": "101", "item_size": "OS", "seller_id": "5", "quantity": 0}])
    with pytest.raises(ValueError, match="quantity"):
        JioMartClient._add_items([{"item_id": "101", "item_size": "OS", "seller_id": "5", "quantity": 100}])
    with pytest.raises(ValueError, match="seller"):
        JioMartClient._add_items([{"item_id": "101", "item_size": "OS", "quantity": 1}])


def test_update_items_requires_identifier_and_index_and_emits_contract_shape():
    items = [{"item_id": "101", "item_size": "OS", "quantity": 3, "identifier": "line-1", "item_index": 0}]
    assert JioMartClient._update_items(items) == [{
        "item_id": 101, "item_size": "OS", "quantity": 3,
        "meta": {"vertical_code": "default", "compute_delivery_fee": True},
        "identifiers": {"identifier": "line-1"}, "item_index": 0,
    }]

    with pytest.raises(ValueError, match="identifier"):
        JioMartClient._update_items(
            [{"item_id": "101", "item_size": "OS", "quantity": 1, "item_index": 0}]
        )
    with pytest.raises(ValueError, match="identifier"):
        JioMartClient._update_items(
            [{"item_id": "101", "item_size": "OS", "quantity": 1, "identifier": "line-1"}]
        )
    # A normal (non-removal) update still rejects a zero quantity.
    with pytest.raises(ValueError, match="quantity"):
        JioMartClient._update_items(
            [{"item_id": "101", "item_size": "OS", "quantity": 0, "identifier": "line-1", "item_index": 0}]
        )


def test_typed_cart_items_removal_forces_zero_quantity_like_the_website():
    items = [{"item_id": "101", "item_size": "OS", "quantity": 3, "identifier": "line-1", "item_index": 0}]
    result = JioMartClient._typed_cart_items(items, require_existing=True, removal=True)
    assert result[0]["quantity"] == 0

    # removal also accepts an input quantity of 0, unlike a normal update.
    zero_input = [{"item_id": "101", "item_size": "OS", "quantity": 0, "identifier": "line-1", "item_index": 0}]
    assert JioMartClient._typed_cart_items(zero_input, require_existing=True, removal=True)[0]["quantity"] == 0


def test_cart_item_normalizes_docs_shaped_item_and_detects_out_of_stock():
    item = {
        "quantity": 2, "res_item_index": 0,
        "product": {"uid": 101, "name": "Coke"},
        "article": {"uid": "a1", "size": "OS", "seller": {"uid": 1}, "store": {"uid": 10495}},
        "identifiers": {"identifier": "line-1"},
        "price": {"converted": {"effective": 72}},
        "availability": {"is_valid": True, "deliverable": True, "out_of_stock": False},
    }

    result = JioMartClient._cart_item(item)

    assert result["name"] == "Coke"
    assert result["line_total"] == 72
    assert result["available"] is True
    assert result["item_index"] == 0
    assert result["identifiers"]["product_uid"] == "101"
    assert result["identifiers"]["identifier"] == "line-1"

    item["availability"]["out_of_stock"] = True
    assert JioMartClient._cart_item(item)["available"] is False


def test_normalise_address_keeps_saved_address_id():
    result = JioMartClient._normalise_address({"id": "addr-1", "pincode": "400053"})
    assert result["id"] == "addr-1"
    assert result["pincode"] == "400053"


def test_cash_on_delivery_requires_a_direct_cod_code():
    assert JioMartClient.cash_on_delivery(
        [{"name": "COD", "code": "COD", "aggregator": "Fynd", "merchant_code": "COD"}]
    ) is not None
    assert JioMartClient.cash_on_delivery(
        [{"name": "JIOPP_COD", "code": "COD", "aggregator": "Fynd", "merchant_code": "COD"}]
    ) is not None
    assert JioMartClient.cash_on_delivery([{"name": "COD", "code": "UPI"}]) is None
    assert JioMartClient.cash_on_delivery([{"name": "UPI", "code": "UPI"}]) is None
    assert JioMartClient.cash_on_delivery([]) is None


# ----- 12b. checkout() / select_address() / payment_modes() over the wire -----

API_ID = "app-1"
LOCATION = {"country": "India", "country_iso_code": "IN", "pincode": "400053"}
ADAPTER_SESSION = {"cookies": [], "account_reference": "user-1", "application_id": API_ID}


def adapter_client(handler, *, writes=True, checkout=True):
    return JioMartClient(
        API_ID, application_token="test-token", transport=httpx.MockTransport(handler),
        enable_cart_writes=writes, enable_checkout=checkout,
    )


def _session_response() -> httpx.Response:
    return httpx.Response(200, json={"user": {"user_id": "user-1"}})


def _cart_response(total) -> httpx.Response:
    return httpx.Response(200, json={
        "success": True, "cart_id": "cart-99", "last_modified": "2026-10-18T10:00:00Z",
        "is_valid": True, "items": [],
        "breakup_values": {"raw": {"total": total}, "display": {}},
        "currency": {"code": "INR"},
    })


def _payment_options_response(modes) -> httpx.Response:
    return httpx.Response(200, json={"payment_options": {"payment_option": modes}})


@pytest.mark.asyncio
async def test_checkout_requires_both_cart_writes_and_checkout_flags():
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made before the feature gate")

    api = adapter_client(handler, writes=True, checkout=False)
    with pytest.raises(CommerceError) as failure:
        await api.checkout(dict(ADAPTER_SESSION), LOCATION, cart_id="cart-99", address_id="addr-1", amount=100)
    assert failure.value.diagnostic_code == "checkout_disabled"


@pytest.mark.asyncio
async def test_checkout_selects_address_verifies_total_and_places_cod_order():
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/authentication/v1.0/session"):
            return _session_response()
        if path.endswith("/cart/v1.0/select-address"):
            assert request.method == "POST"
            return httpx.Response(200, json={"success": True})
        if path.endswith("/cart/v1.0/detail"):
            return _cart_response(100)
        if path.endswith("/payment/v1.0/payment/options"):
            return _payment_options_response(
                [{"name": "COD", "list": [{"code": "COD", "aggregator_name": "Fynd", "merchant_code": "COD"}]}]
            )
        if path.endswith("/cart/v1.0/meta"):
            assert request.method == "PUT"
            return httpx.Response(200, json={"success": True})
        if path.endswith("/cart/v2.0/checkout"):
            assert request.method == "POST"
            return httpx.Response(200, json={"success": True, "order_id": "FY555"})
        raise AssertionError(f"unexpected request {request.method} {path}")

    api = adapter_client(handler)
    result = await api.checkout(dict(ADAPTER_SESSION), LOCATION, cart_id="cart-99", address_id="addr-1", amount=100)
    assert result == {"order_id": "FY555", "payment": None}


@pytest.mark.asyncio
async def test_checkout_raises_changed_when_cart_total_moved_before_submission():
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/authentication/v1.0/session"):
            return _session_response()
        if path.endswith("/cart/v1.0/select-address"):
            return httpx.Response(200, json={"success": True})
        if path.endswith("/cart/v1.0/detail"):
            return _cart_response(150)
        raise AssertionError(f"unexpected request {request.method} {path}")

    api = adapter_client(handler)
    with pytest.raises(CommerceError) as failure:
        await api.checkout(dict(ADAPTER_SESSION), LOCATION, cart_id="cart-99", address_id="addr-1", amount=100)
    assert failure.value.code == "changed"


@pytest.mark.asyncio
async def test_checkout_raises_rejected_when_cash_on_delivery_is_unavailable():
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/authentication/v1.0/session"):
            return _session_response()
        if path.endswith("/cart/v1.0/select-address"):
            return httpx.Response(200, json={"success": True})
        if path.endswith("/cart/v1.0/detail"):
            return _cart_response(100)
        if path.endswith("/payment/v1.0/payment/options"):
            return _payment_options_response(
                [{"name": "UPI", "list": [{"code": "UPI", "aggregator_name": "Fynd", "merchant_code": "UPI"}]}]
            )
        raise AssertionError(f"unexpected request {request.method} {path}")

    api = adapter_client(handler)
    with pytest.raises(CommerceError) as failure:
        await api.checkout(dict(ADAPTER_SESSION), LOCATION, cart_id="cart-99", address_id="addr-1", amount=100)
    assert failure.value.code == "rejected"
    assert failure.value.diagnostic_code == "cod_unavailable"


# ----- 13. normalize_products cart reference -----


def test_normalize_products_builds_cart_reference_with_optional_store_and_vertical():
    raw_item = {
        "uid": 101,
        "name": "Coca Cola Zero 300 ml",
        "sizes": ["OS"],
        "seller_id": "1",
        "store_ids": ["10495"],
        "moq": {"minimum": 1, "maximum": 4, "increment_unit": 1},
        "attributes": {"max-qty-in-order": 4, "vertical-code": "GROCERIES"},
    }

    products = normalize_products([raw_item])

    assert products[0]["cart"] == {
        "item_id": "101", "item_size": "OS", "seller_id": "1", "store_id": "10495",
        "vertical_code": "GROCERIES", "journey": None,
        "limits": {"minimum": 1, "maximum": 4, "increment": 1, "max_in_order": 4},
    }

    # store_ids is now merely informational: a missing store still yields a usable cart ref.
    missing_store = normalize_products([{**raw_item, "store_ids": []}])
    assert missing_store[0]["cart"]["store_id"] is None

    # seller_id remains required.
    missing_seller = normalize_products([{**raw_item, "seller_id": None}])
    assert missing_seller[0]["cart"] is None
