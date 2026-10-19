import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet

from smartbazaar.agent import Intent, ShoppingLine
from smartbazaar.catalog import Selection
from smartbazaar.conversation import Conversation
from smartbazaar.memory import canonical_query, remembered_choice, roll_session
from smartbazaar.storage import Store
from smartbazaar.tools import ShoppingTools

PRODUCTS = [
    {"id": "amul", "name": "Amul Curd 500 g", "brand": "Amul", "pack": "500 g", "sellable": True},
    {"id": "mother", "name": "Mother Dairy Curd 400 g", "brand": "Mother Dairy", "pack": "400 g", "sellable": True},
]


class Commerce:
    def __init__(self, products=None):
        self.products = PRODUCTS if products is None else products
        self.queries = []
        self.history_calls = 0

    async def search(self, query, location, session):
        self.queries.append(query)
        return {"items": self.products}

    async def get_purchase_history(self, session):
        self.history_calls += 1
        return {"status": "unresolved", "items": []}


class Agent:
    async def rank(self, query, products):
        return Selection(exact_ids=[p["id"] for p in products])


class Cart:
    """The real JioMart cart, as the tools see it: adds go straight in."""

    cart_writes = True

    def __init__(self, items=None):
        self.adds = []
        self.items = items or []

    async def add_products(self, state, memory, lines):
        self.adds.append([(line["product"]["id"], line["quantity"]) for line in lines])
        return {"added": [f"{line['quantity']} × {line['product']['name']}" for line in lines],
                "already": [], "notes": [], "error": None}

    async def cart_item(self, state, number):
        return self.items[number - 1] if 0 < number <= len(self.items) else "Send CART to see the numbers."


def tools_for(commerce, cart=None):
    return ShoppingTools(commerce, Agent(), cart or Cart())


@pytest.fixture
def state():
    return {"session": {"account_reference": "account-a"}, "location": {"pincode": "400053"}}


@pytest.fixture
def memory():
    return {"preferences": {"curd": {"product": PRODUCTS[0], "source": "explicit", "expires": time.time() + 1000}}}


@pytest.mark.asyncio
async def test_dahi_uses_saved_choice_but_current_brand_wins(state, memory):
    commerce, cart = Commerce(), Cart()
    tools = tools_for(commerce, cart)
    reply = await tools.execute(Intent(action="add", items=[ShoppingLine(query="dahi")]), state, memory)
    assert commerce.queries == ["Amul Curd 500 g"]
    assert cart.adds == [[("amul", 1)]]
    assert "1 × Amul Curd 500 g (your saved choice)" in reply[0]
    await tools.execute(Intent(action="add", items=[ShoppingLine(query="Mother Dairy curd")]), state, memory)
    assert commerce.queries[-1] == "mother dairy curd"
    assert cart.adds[-1] == [("mother", 1)]
    assert commerce.history_calls == 1


@pytest.mark.asyncio
async def test_missing_preferred_sku_never_silently_substitutes(state, memory):
    cart = Cart()
    tools = tools_for(Commerce([PRODUCTS[1]]), cart)
    asked = await tools.execute(Intent(action="add", items=[ShoppingLine(query="curd", quantity=2)]), state, memory)
    assert cart.adds == [] and asked[-1]["type"] == "list"
    await tools.execute(Intent(action="choose", choice_number=1), state, memory)
    assert cart.adds == [[("mother", 2)]]
    assert memory["preferences"]["curd"]["product"]["id"] == "amul"


@pytest.mark.asyncio
async def test_stale_choice_after_location_change_cannot_apply(state):
    cart = Cart()
    tools = tools_for(Commerce(), cart)
    memory = {}
    await tools.execute(Intent(action="add", items=[ShoppingLine(query="curd")]), state, memory)
    state["location"]["pincode"] = "400001"
    result = await tools.execute(Intent(action="choose", choice_number=1), state, memory)
    assert "earlier request" in result[0]
    assert cart.adds == [] and "pending" not in memory


@pytest.mark.asyncio
async def test_empty_history_has_no_fake_preference_and_nothing_is_kept_for_later(state):
    cart = Cart()
    tools = tools_for(Commerce([]), cart)
    memory = {}
    reply = await tools.execute(Intent(action="history"), state, memory)
    assert "doesn’t mean" in reply[0]
    assert remembered_choice(memory, "curd", time.time()) is None
    replies = await tools.execute(Intent(action="add", items=[ShoppingLine(query="curd"), ShoppingLine(query="bread")]), state, memory)
    assert [r for r in replies if "couldn’t find a clear match" in r] == replies and len(replies) == 2
    assert cart.adds == [] and "pending" not in memory and "draft" not in memory


def test_daily_rotation_bounds_context_drops_old_lists_and_keeps_preferences(memory):
    now = datetime(2026, 10, 18, 23, 59, tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()
    memory["preferences"]["curd"]["expires"] = now + 180 * 86400
    roll_session(memory, now)
    memory["active"]["queries"] = ["curd"]
    memory["draft"] = {"version": 4, "lines": [{"query": "curd", "quantity": 1}]}
    memory["choices"] = {"secret": "stale choice"}
    memory["pending"] = {"items": [{"query": "bread"}], "expires": now + 60}
    assert roll_session(memory, now + 120)
    assert memory["active"]["queries"] == []
    # A list from an older build and an expired question never survive.
    assert not {"draft", "choices", "pending"} & memory.keys()
    assert memory["preferences"]["curd"]["product"]["id"] == "amul"
    assert canonical_query("दही") == "curd"


def test_memory_is_encrypted_account_and_owner_scoped_survives_auth_expiry(tmp_path):
    store = Store(str(tmp_path / "test.db"), Fernet.generate_key().decode())
    a = store.memory_key({"account_reference": "account-a"})
    b = store.memory_key({"account_reference": "account-b"})
    store.save_memory("owner-a", a, {"favorite": "Amul curd"})
    store.save("owner-a", {"session": {"account_reference": "account-a"}})
    store.db.execute("UPDATE sessions SET updated=0")
    assert store.get("owner-a") == {}
    assert store.get_memory("owner-a", a)["favorite"] == "Amul curd"
    assert store.get_memory("owner-b", a) == store.get_memory("owner-a", b) == {}
    raw = store.db.execute("SELECT data FROM shopping_memory").fetchone()[0]
    assert b"Amul" not in raw
    store.forget_memory("owner-a")
    assert store.get_memory("owner-a", a) == {}
    store.close()


@pytest.mark.asyncio
async def test_list_shows_the_jiomart_cart_and_logout_clears_memory(tmp_path, state):
    class CartCommerce(Commerce):
        async def get_cart(self, session, location):
            return {"cart_id": "c", "items": [], "price_breakup": {"raw": []}, "is_valid": True, "coverage": "complete"}

    store = Store(str(tmp_path / "test.db"), Fernet.generate_key().decode())
    store.save("owner", state)
    key = store.memory_key(state["session"])
    store.save_memory("owner", key, {"draft": {"version": 1, "lines": [{"query": "curd", "quantity": 1}]}})
    conversation = Conversation(store, CartCommerce(), Agent())
    result = await conversation.handle("owner", "sender", "LIST")
    assert "Your JioMart cart is empty" in result[0]["body"] and "curd" not in result[0]["body"]
    await conversation.handle("owner", "sender", "LOGOUT")
    assert store.get_memory("owner", key) == {}
    store.close()


@pytest.mark.asyncio
async def test_multi_item_questions_are_asked_one_at_a_time(state):
    cart = Cart()
    tools = tools_for(Commerce(), cart)
    memory = {}
    await tools.execute(Intent(action="add", items=[ShoppingLine(query="curd"), ShoppingLine(query="curd", quantity=2)]), state, memory)
    replies = await tools.execute(Intent(action="choose", choice_number=1), state, memory)
    assert len(replies) == 2 and replies[1]["type"] == "list"
    assert len(memory["pending"]["items"]) == 1
    await tools.execute(Intent(action="choose", choice_number=2), state, memory)
    assert cart.adds == [[("amul", 1)], [("mother", 2)]]
    assert "pending" not in memory


@pytest.mark.asyncio
async def test_skip_drops_only_the_current_question(state):
    cart = Cart()
    tools = tools_for(Commerce(), cart)
    memory = {}
    await tools.execute(Intent(action="add", items=[ShoppingLine(query="curd"), ShoppingLine(query="dahi", quantity=2)]), state, memory)
    skipped = await tools.execute(Intent(action="skip"), state, memory)
    assert skipped[0] == "Skipped curd." and skipped[1]["body"].startswith("Which curd would you like?")
    assert memory["pending"]["items"][0]["quantity"] == 2
    await tools.execute(Intent(action="skip"), state, memory)
    assert cart.adds == [] and "pending" not in memory
    assert "nothing waiting" in (await tools.execute(Intent(action="skip"), state, memory))[0]


@pytest.mark.asyncio
async def test_remember_names_a_real_cart_line(state):
    item = {"identifiers": {"product_uid": "amul"}, "item_size": "OS", "name": "Amul Curd 500 g", "quantity": 1}
    cart = Cart(items=[item])
    tools = tools_for(Commerce(), cart)
    memory = {"cart_products": {str(("amul", "OS")): PRODUCTS[0]}}
    saved = await tools.execute(Intent(action="remember", line_number=1, preference_category="curd"), state, memory)
    assert saved[0].startswith("I’ll remember Amul Curd 500 g for curd")
    assert memory["preferences"]["curd"]["product"]["id"] == "amul"
    wrong = await tools.execute(Intent(action="remember", line_number=1, preference_category="bread"), state, memory)
    assert "doesn’t look like bread" in wrong[0]


@pytest.mark.asyncio
async def test_location_change_drops_open_questions(tmp_path, state):
    store = Store(str(tmp_path / "test.db"), Fernet.generate_key().decode())
    store.save("owner", {**state, "focus": {"product": PRODUCTS[0], "pincode": "400053", "expires": time.time() + 60}})
    key = store.memory_key(state["session"])
    store.save_memory("owner", key, {"pending": {"items": [{"query": "curd", "quantity": 1, "products": PRODUCTS}],
                                                 "pincode": "400053", "expires": time.time() + 60}})
    await Conversation(store, Commerce(), Agent()).handle("owner", "sender", "pincode 400001")
    assert "pending" not in store.get_memory("owner", key)
    assert "focus" not in store.get("owner")
    store.close()


@pytest.mark.asyncio
async def test_media_cannot_trigger_model_selected_logout(tmp_path, state):
    class HostileAgent:
        async def interpret(self, text, context):
            return Intent(action="logout")

    store = Store(str(tmp_path / "test.db"), Fernet.generate_key().decode())
    store.save("owner", state)
    reply = await Conversation(store, Commerce(), HostileAgent()).handle("owner", "sender", "find milk", shopping_only=True)
    assert "use text or a voice note for other actions" in reply[0]["body"]
    assert store.get("owner")["session"]
    store.close()


@pytest.mark.asyncio
async def test_forget_specific_preference_keeps_other_preferences(state, memory):
    memory['preferences']['milk'] = memory['preferences']['curd'].copy()
    await tools_for(Commerce()).execute(Intent(action='forget', preference_category='curd'), state, memory)
    assert 'curd' not in memory['preferences']
    assert 'milk' in memory['preferences']


@pytest.mark.asyncio
async def test_unavailable_history_does_not_prevent_shopping(state, memory):
    from smartbazaar.jiomart import CommerceError

    class HistoryUnavailable(Commerce):
        async def get_purchase_history(self, session):
            raise CommerceError('auth_expired', 'Unavailable')

    cart = Cart()
    await tools_for(HistoryUnavailable(), cart).execute(Intent(action='add', items=[ShoppingLine(query='curd')]), state, memory)
    assert memory['history']['status'] == 'unavailable'
    assert cart.adds == [[('amul', 1)]]


@pytest.mark.asyncio
async def test_forget_suppresses_import_until_explicit_history(state):
    commerce = Commerce()
    tools = tools_for(commerce)
    memory = {}
    await tools.execute(Intent(action='forget'), state, memory)
    await tools.refresh_history(state, memory)
    assert commerce.history_calls == 0
    await tools.execute(Intent(action='history'), state, memory)
    assert commerce.history_calls == 1
    assert not memory.get('history_import_disabled')


def test_history_requires_recent_dated_evidence_and_respects_current_request():
    now = time.time()
    memory = {"history": {"checked_at": now, "items": [{**PRODUCTS[0], "purchased_at": now - 86400}]}}
    assert remembered_choice(memory, "curd", now)["product"]["id"] == "amul"
    assert remembered_choice(memory, "Mother Dairy curd", now) is None
    memory["history"]["items"][0]["purchased_at"] = now - 181 * 86400
    assert remembered_choice(memory, "curd", now) is None
    memory["history"]["items"][0].pop("purchased_at")
    assert remembered_choice(memory, "curd", now) is None


def test_long_replies_preserve_all_lines_within_channel_limit():
    from smartbazaar.app import chunk_replies

    summary = "\n".join(f"{i}. 1 × product-{i}-" + "x" * 190 for i in range(30))
    chunks = chunk_replies([summary])
    assert len(chunks) > 1
    assert all(len(chunk) <= 3900 for chunk in chunks)
    assert "\n".join(chunks) == summary


def order(order_id, days_ago, *items, cancelled=False):
    return {"order_id": order_id, "created_at": time.time() - days_ago * 86400, "status": "Cancelled" if cancelled else "Delivered",
            "shipments": [{"state": "cancelled" if cancelled else "delivered"}],
            "items": [{"item_id": i, "name": n, "brand": b, "size": "OS", "quantity": 1} for i, n, b in items]}


AMUL = ("amul", "Amul Curd 500 g", "Amul")
MOTHER = ("mother", "Mother Dairy Curd 400 g", "Mother Dairy")


class OrdersCommerce(Commerce):
    def __init__(self, orders, details=None, products=None):
        super().__init__(products)
        self.orders, self.details, self.detail_calls = orders, details or {}, []

    async def list_orders(self, session):
        return self.orders

    async def get_order_detail(self, session, order_id):
        self.detail_calls.append(order_id)
        return self.details[order_id]


@pytest.mark.asyncio
async def test_history_comes_from_the_accounts_own_orders_most_bought_first(state):
    commerce = OrdersCommerce(
        [order("o1", 2, MOTHER), order("o2", 10, AMUL), order("o3", 20, AMUL),
         order("o4", 1, ("nestle", "Nestle Dahi 400 g", "Nestle"), cancelled=True),
         {"order_id": "o5", "created_at": time.time() - 30 * 86400, "status": "Delivered", "shipments": [], "items": []}],
        details={"o5": order("o5", 30, AMUL)},
    )
    memory = {}
    await tools_for(commerce).refresh_history(state, memory, force=True)

    items = memory["history"]["items"]
    # Bought in three orders beats bought once more recently; cancelled orders never count.
    assert [(item["id"], item["orders"]) for item in items] == [("amul", 3), ("mother", 1)]
    assert commerce.detail_calls == ["o5"] and commerce.history_calls == 0


@pytest.mark.asyncio
async def test_dahi_means_your_usual_from_past_orders(state):
    cart = Cart()
    commerce = OrdersCommerce([order("o1", 3, AMUL), order("o2", 9, AMUL)])
    reply = await tools_for(commerce, cart).execute(Intent(action="add", items=[ShoppingLine(query="dahi")]), state, {})

    assert commerce.queries == ["Amul Curd 500 g"]
    assert cart.adds == [[("amul", 1)]]
    assert "1 × Amul Curd 500 g (your usual)" in reply[0]


@pytest.mark.asyncio
async def test_a_choice_puts_something_bought_before_first(state):
    # Bought too long ago to pick it automatically, but still worth pointing out.
    commerce = OrdersCommerce([order("o1", 200, MOTHER)])
    asked = await tools_for(commerce).execute(Intent(action="add", items=[ShoppingLine(query="curd")]), state, {})

    menu = asked[-1]
    assert menu["rows"][0]["title"].startswith("Mother Dairy Curd")
    assert "1. Mother Dairy Curd 400 g — 400 g (you've bought this before)" in menu["body"]


def test_a_chat_order_counts_as_purchase_evidence_straight_away():
    from smartbazaar.checkout import clear_ordered_lines

    memory = {"cart_products": {str(("amul", "OS")): {"id": "amul", "name": "Amul Curd 500 g", "brand": "Amul"}}}
    clear_ordered_lines(memory, {"order_id": "FY1", "at": time.time(), "ordered_keys": [["amul", "OS"]]})
    clear_ordered_lines(memory, {"order_id": "FY1", "at": time.time(), "ordered_keys": [["amul", "OS"]]})
    assert [(i["id"], i["orders"]) for i in memory["history"]["items"]] == [("amul", 1)]

    clear_ordered_lines(memory, {"order_id": "FY2", "at": time.time(), "ordered_keys": [["amul", "OS"]]})
    assert memory["history"]["items"][0]["orders"] == 2
    assert remembered_choice(memory, "dahi", time.time())["product"]["id"] == "amul"


MASTI_RAW = {"id": "masti", "name": "Amul Masti Dahi 400 g", "brand": "Amul", "pack": "400 g", "category": "Curd", "sellable": False}
MOTHER_RAW = {"id": "mother", "name": "Mother Dairy Curd 400 g", "brand": "Mother Dairy", "pack": "400 g", "category": "Curd", "sellable": True}
NESTLE_RAW = {"id": "nestle", "name": "Nestle a+ Dahi 400 g", "brand": "Nestle", "pack": "400 g", "category": "Curd", "sellable": True}
BUTTER_RAW = {"id": "butter", "name": "Amul Butter 100 g", "brand": "Amul", "pack": "100 g", "category": "Butter", "sellable": True}


class ShelfCommerce(OrdersCommerce):
    """Search lists what the shelf has for each query; the account's own orders are its history."""

    def __init__(self, shelf, orders=()):
        super().__init__(list(orders))
        self.shelf = shelf

    async def search(self, query, location, session):
        self.queries.append(query)
        return {"items": self.shelf.get(query, [])}


@pytest.mark.asyncio
async def test_an_unavailable_product_offers_labelled_substitutes_bought_before_first(state):
    cart = Cart()
    commerce = ShelfCommerce({"amul masti curd 400 g": [MASTI_RAW, BUTTER_RAW, NESTLE_RAW],
                              "Mother Dairy Curd 400 g": [MOTHER_RAW]}, orders=[order("o1", 3, MOTHER)])
    memory = {}
    tools = tools_for(commerce, cart)

    asked = await tools.execute(Intent(action="add", items=[ShoppingLine(query="amul masti dahi 400 g")]), state, memory)

    menu = asked[-1]
    # Something bought before is looked up once by name and comes first; then the same category.
    # Butter is never offered for dahi, and nothing is added until the customer taps.
    assert commerce.queries == ["amul masti curd 400 g", "Mother Dairy Curd 400 g"]
    assert menu["body"].startswith(
        "Amul Masti Dahi 400 g is unavailable. Would one of these do?\n"
        "1. Mother Dairy Curd 400 g — 400 g (you've bought this before)\n"
        "2. Nestle a+ Dahi 400 g — 400 g\n"
        "Tap a number to add it to your cart instead, or Skip.")
    assert menu["rows"][-1]["description"] == "Don’t add a substitute"
    assert cart.adds == []

    await tools.execute(Intent(action="choose", choice_number=2), state, memory)
    assert cart.adds == [[("nestle", 1)]] and "pending" not in memory


@pytest.mark.asyncio
async def test_a_product_not_listed_offers_only_same_kind_substitutes(state):
    cart = Cart()
    tools = tools_for(ShelfCommerce({"amul masti curd 400 g": [BUTTER_RAW, NESTLE_RAW]}), cart)
    memory = {}
    asked = await tools.execute(Intent(action="add", items=[ShoppingLine(query="amul masti dahi 400 g")]), state, memory)

    # A search miss is “couldn’t find”, never a stock claim.
    assert asked[-1]["body"].startswith("I couldn’t find “amul masti curd 400 g” near you. Would one of these do?\n"
                                        "1. Nestle a+ Dahi 400 g")
    assert [p["id"] for p in memory["pending"]["items"][0]["products"]] == ["nestle"]

    nothing = await tools_for(ShelfCommerce({"amul masti curd 400 g": [BUTTER_RAW]}), cart).execute(
        Intent(action="add", items=[ShoppingLine(query="amul masti dahi 400 g")]), state, memory)
    assert nothing == ["I couldn’t find a clear match for “amul masti curd 400 g” near you. Tell me a brand or pack size and I’ll look again."]
    assert cart.adds == [] and "pending" not in memory


@pytest.mark.asyncio
async def test_when_the_usual_is_unavailable_the_choice_says_so(state):
    cart = Cart()
    commerce = ShelfCommerce({"Amul Masti Dahi 400 g": [MASTI_RAW, MOTHER_RAW, NESTLE_RAW]},
                             orders=[order("o1", 3, ("masti", "Amul Masti Dahi 400 g", "Amul"))])
    memory = {}
    asked = await tools_for(commerce, cart).execute(Intent(action="add", items=[ShoppingLine(query="dahi")]), state, memory)

    assert commerce.queries == ["Amul Masti Dahi 400 g"]
    assert asked[-1]["body"].startswith("Your usual Amul Masti Dahi 400 g is unavailable. Would one of these do?\n"
                                        "1. Mother Dairy Curd 400 g")
    assert cart.adds == []
