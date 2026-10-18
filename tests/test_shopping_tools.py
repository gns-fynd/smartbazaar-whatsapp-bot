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
