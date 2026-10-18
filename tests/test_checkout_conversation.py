import pytest
from cryptography.fernet import Fernet

from smartbazaar.agent import Intent
from smartbazaar.checkout import CheckoutFlow
from smartbazaar.conversation import Conversation
from smartbazaar.jiomart import JioMartClient
from smartbazaar.storage import Store

OWNER = "owner-1"
SESSION = {"account_reference": "account-1", "application_id": "685945f46c8c7aee3f3af605", "cookies": []}
LOCATION = {"id": "addr-1", "label": "Home", "city": "Mumbai", "pincode": "400053",
            "country": "India", "country_iso_code": "IN"}
PRODUCT = {
    "id": "101", "name": "Coca Cola Zero 300 ml",
    "cart": {"item_id": "101", "item_size": "OS", "seller_id": "1", "store_id": "10495",
             "journey": "standard", "limits": {"minimum": 1, "maximum": 4, "increment": 1, "max_in_order": 4}},
}


class CartCommerce:
    cash_on_delivery = staticmethod(JioMartClient.cash_on_delivery)
    online_payment = staticmethod(JioMartClient.online_payment)

    def __init__(self):
        self.lines = []
        self.checkouts = []
        self.selected = []

    async def get_cart(self, session, location):
        total = sum(line["line_total"] for line in self.lines)
        return {
            "cart_id": "cart-1", "last_modified": "t", "items": [dict(line) for line in self.lines],
            "price_breakup": {"status": "available", "raw": [{"name": "total", "amount": total}], "display": []},
            "currency": "INR", "is_valid": True, "coverage": "complete", "raw_item_count": len(self.lines),
            "checkout_context": {"enabled": False, "missing": []},
        }

    async def add_cart_items(self, session, location, items, cart_id=None):
        assert "store_id" not in items[0] and items[0]["seller_id"] == "1"
        for item in items:
            self.lines.append({
                "identifiers": {"product_uid": item["item_id"], "identifier": f"line-{len(self.lines)}"},
                "quantity": item["quantity"], "item_size": item["item_size"], "item_index": len(self.lines),
                "name": PRODUCT["name"], "line_total": 36.0 * item["quantity"], "available": True,
            })
        return {"accepted": True, "partial": False}

    async def update_cart(self, session, location, operation, items, cart_id=None):
        raise AssertionError("no update expected")

    async def select_address(self, session, location, cart_id, address_id):
        self.selected.append((cart_id, address_id))

    async def payment_modes(self, session, location, cart_id, amount):
        return [{"name": "JIOPP", "code": "JIOPP", "aggregator": "Jio", "merchant_code": "JIOPP"},
                {"name": "COD", "code": "COD", "aggregator": "Fynd", "merchant_code": "COD"}]

    async def checkout(self, session, location, *, cart_id, address_id, amount, method="cod", callback_url=None):
        assert method == "cod" and callback_url is None
        self.checkouts.append((cart_id, address_id, amount))
        return {"order_id": "FY-ORDER-1"}

    async def get_order(self, session, order_id):
        return {"status": "processing", "detail": ""}

    def placed_order(self):
        return {"order_id": "FY-ORDER-1", "created_at": 1792330000, "total": 72.0, "status": "Placed",
                "item_count": 2, "items": [{"item_id": "101", "name": PRODUCT["name"], "size": "OS",
                                            "brand": None, "quantity": 2, "price": 72.0}],
                "shipments": [], "complete": True}

    async def list_orders(self, session):
        return [self.placed_order()] if self.checkouts else []

    async def get_order_detail(self, session, order_id):
        assert order_id == "FY-ORDER-1"
        return self.placed_order()


class NoAgent:
    async def interpret(self, text, context):
        raise AssertionError("commands must not need the planner")


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path / "bot.sqlite3"), Fernet.generate_key().decode())
    yield value
    value.close()


def connected(store):
    store.save(OWNER, {"session": dict(SESSION), "location": dict(LOCATION)})
    return store.memory_key(SESSION)


def body(reply):
    return reply if isinstance(reply, str) else reply.get("body", "")


async def in_cart(commerce, quantity=2):
    ids = {key: PRODUCT["cart"][key] for key in ("item_id", "item_size", "seller_id")}
    await commerce.add_cart_items(None, None, [{**ids, "quantity": quantity}])


def conversation(store, commerce, *, writes=True, checkout=True):
    flow = CheckoutFlow(store, commerce, cart_writes=writes, checkout=checkout, max_total=1500)
    return Conversation(store, commerce, NoAgent(), flow)


@pytest.mark.asyncio
async def test_checkout_command_reviews_the_cart_and_button_places_one_order(store):
    account = connected(store)
    commerce = CartCommerce()
    await in_cart(commerce)
    bot = conversation(store, commerce)

    replies = await bot.handle(OWNER, "919000000001", "checkout")

    assert "2 × Coca Cola Zero 300 ml — ₹72" in replies[0]
    prompt = replies[-1]
    assert prompt["type"] == "quick_reply"
    place = prompt["buttons"][0]
    # The test fake offers JioPay too, but without a public origin only cash on delivery is usable.
    assert place["id"].startswith("sbco_") and place["title"] == "Cash on delivery"

    placed = await bot.handle(OWNER, "919000000001", "/button:" + place["id"])

    assert commerce.selected == [("cart-1", "addr-1")]
    assert commerce.checkouts == [("cart-1", "addr-1", 72.0)]
    assert "FY-ORDER-1" in body(placed[0])
    # The confirmation ends with the order summary PDF; its text carries the next-step buttons.
    assert placed[0]["type"] == "quick_reply" and placed[-1]["type"] == "document"
    assert store.latest_attempt(OWNER, account)[1] == "placed"
    assert store.get_memory(OWNER, account)["orders"][0]["order_id"] == "FY-ORDER-1"

    again = await bot.handle(OWNER, "919000000001", "/button:" + place["id"])
    assert "expired" in body(again[0])
    assert len(commerce.checkouts) == 1

    status = await bot.handle(OWNER, "919000000001", "order status")
    assert body(status[0]).startswith("🧾 Order FY-ORDER-1")
    assert "Status: Placed" in body(status[0]) and "2 × Coca Cola Zero 300 ml — ₹72" in body(status[0])


@pytest.mark.asyncio
async def test_without_cart_writes_the_cart_is_read_only_and_adds_are_refused(store):
    connected(store)
    commerce = CartCommerce()
    await in_cart(commerce)
    bot = conversation(store, commerce, writes=False)

    replies = await bot.handle(OWNER, "919000000001", "cart")

    assert body(replies[0]).startswith("🛒 Your JioMart cart") and "Send CHECKOUT to review the order." in body(replies[0])
    added = await bot.handle(OWNER, "919000000001", "add it")
    assert "Which product would you like?" in body(added[0]) or "isn’t switched on" in body(added[0])
    assert len(commerce.lines) == 1


@pytest.mark.asyncio
async def test_planner_checkout_intent_is_routed_to_the_cart_flow(store):
    connected(store)
    commerce = CartCommerce()
    await in_cart(commerce, quantity=1)

    class CheckoutAgent:
        async def interpret(self, text, context):
            assert context["pending"] == [] and context["viewed"] is None
            return Intent(action="checkout")

    flow = CheckoutFlow(store, commerce, cart_writes=True, checkout=False)
    bot = Conversation(store, commerce, CheckoutAgent(), flow)

    replies = await bot.handle(OWNER, "919000000001", "let's wrap this up")

    assert len(commerce.lines) == 1
    assert "finish in the JioMart app" in body(replies[-1])
    assert commerce.checkouts == []


@pytest.mark.asyncio
async def test_checkout_button_requires_a_connected_session(store):
    commerce = CartCommerce()
    bot = conversation(store, commerce)

    replies = await bot.handle(OWNER, "919000000001", "/button:sbco_" + "x" * 24)

    assert "No order has been placed" in body(replies[0])
    assert commerce.checkouts == []


@pytest.mark.asyncio
async def test_a_list_saved_by_an_older_build_is_dropped_never_copied_into_the_cart(store):
    account = connected(store)
    store.save_memory(OWNER, account, {"draft": {"version": 1, "lines": [
        {"query": "bread", "quantity": 1, "product": None, "source": "explicit_request"},
        {"query": "coke zero", "quantity": 1, "product": PRODUCT, "source": "explicit_request"},
    ]}})
    commerce = CartCommerce()

    replies = await conversation(store, commerce).handle(OWNER, "919000000001", "cart")

    assert "Your JioMart cart is empty" in body(replies[0])
    assert commerce.lines == []
    assert "draft" not in store.get_memory(OWNER, account)


class EditCommerce(CartCommerce):
    def __init__(self):
        super().__init__()
        self.updates = []
        self.lines = [
            {"identifiers": {"product_uid": "201", "identifier": "line-a"}, "quantity": 1, "item_size": "OS",
             "item_index": 0, "name": "Amul Fruit & Nut Dark Chocolate 150 g", "line_total": 0, "available": False},
            {"identifiers": {"product_uid": "202", "identifier": "line-b"}, "quantity": 1, "item_size": "OS",
             "item_index": 1, "name": "Sprite 750 ml", "line_total": 37.0, "available": True},
            {"identifiers": {"product_uid": "203", "identifier": "line-c"}, "quantity": 1, "item_size": "OS",
             "item_index": 2, "name": "Sprite 2 L", "line_total": 90.0, "available": True},
        ]

    async def get_cart(self, session, location):
        cart = await super().get_cart(session, location)
        cart["price_breakup"]["raw"] = [{"name": "delivery_charge", "amount": 30},
                                        {"name": "total", "amount": sum(i["line_total"] for i in self.lines) + 30}]
        cart["is_valid"] = all(line["available"] for line in self.lines)
        return cart

    async def update_cart(self, session, location, operation, items, cart_id=None):
        self.updates.append((operation, [(i["identifier"], i["item_index"], i["quantity"]) for i in items]))
        keys = {i["identifier"] for i in items}
        if operation == "remove_item":
            self.lines = [line for line in self.lines if line["identifiers"]["identifier"] not in keys]
        else:
            for line in self.lines:
                if line["identifiers"]["identifier"] in keys:
                    line["quantity"] = items[0]["quantity"]
                    line["line_total"] = 37.0 * items[0]["quantity"]
        return {"accepted": True, "partial": False}


@pytest.mark.asyncio
async def test_cart_view_hides_unavailable_price_and_offers_edits(store):
    connected(store)
    replies = await conversation(store, EditCommerce(), checkout=False).handle(OWNER, "919000000001", "cart")
    text = body(replies[0])

    assert "1. 1 × Amul Fruit & Nut Dark Chocolate 150 g ⚠️ unavailable" in text
    assert "₹0" not in text
    assert "Delivery: ₹30" in text and "Total: ₹157" in text
    assert "remove unavailable items" in text


@pytest.mark.asyncio
async def test_remove_unavailable_and_numbered_removal_edit_the_real_cart(store):
    connected(store)
    commerce = EditCommerce()
    bot = conversation(store, commerce, checkout=False)

    removed = await bot.handle(OWNER, "919000000001", "remove unavailable items")
    numbered = await bot.handle(OWNER, "919000000001", "remove 2 from cart")

    assert commerce.updates[0] == ("remove_item", [("line-a", 0, 1)])
    assert body(removed[0]).startswith("Removed Amul Fruit & Nut Dark Chocolate 150 g from your JioMart cart.")
    # Numbers follow the cart view shown in the previous reply (Sprite 750 ml, Sprite 2 L).
    assert commerce.updates[1] == ("remove_item", [("line-c", 2, 1)])
    assert "Removed Sprite 2 L" in body(numbered[0])
    assert [line["name"] for line in commerce.lines] == ["Sprite 750 ml"]


@pytest.mark.asyncio
async def test_planner_cart_edits_by_name_ask_when_ambiguous_and_change_quantity(store):
    connected(store)
    commerce = EditCommerce()
    intents = iter([
        Intent(action="cart_remove", cart_item="sprite"),
        Intent(action="cart_quantity", cart_item="sprite 750", quantity=3),
    ])

    class EditAgent:
        async def interpret(self, text, context):
            return next(intents)

    bot = Conversation(store, commerce, EditAgent(), CheckoutFlow(store, commerce, cart_writes=True))
    ambiguous = await bot.handle(OWNER, "919000000001", "remove the sprite from my cart")
    changed = await bot.handle(OWNER, "919000000001", "make the small sprite three")

    assert body(ambiguous[0]).startswith("Which one?") and commerce.updates[0] == ("update_item", [("line-b", 1, 3)])
    assert "Set Sprite 750 ml to 3 in your JioMart cart." in body(changed[0])


@pytest.mark.asyncio
async def test_cart_edits_need_the_cart_write_switch(store):
    connected(store)
    commerce = EditCommerce()
    replies = await conversation(store, commerce, writes=False).handle(OWNER, "919000000001", "remove unavailable items")

    assert "isn’t switched on" in body(replies[0])
    assert commerce.updates == []


COKE = {"uid": 101, "name": PRODUCT["name"], "sizes": ["OS"], "seller_id": 1, "store_ids": [10495], "sellable": True,
        "slug": "coca-cola-zero-300-ml-101", "price": {"effective": {"min": 36}},
        "medias": [{"type": "image", "url": "https://cdn1.jiomartjcp.com/coke.jpg"}]}
PANEER = [
    {"uid": 501, "name": "Chitale Paneer 200 g", "sizes": ["OS"], "seller_id": 1, "sellable": True,
     "price": {"effective": {"min": 93}}, "brand": {"name": "Chitale"}, "pack": "200 g"},
    {"uid": 502, "name": "iD Natural Paneer 200 g", "sizes": ["OS"], "seller_id": 1, "sellable": True,
     "price": {"effective": {"min": 106}}, "brand": {"name": "iD"}, "pack": "200 g"},
]


def shop_agent(*intents, exact=None):
    from smartbazaar.catalog import Selection

    queue = iter(intents)

    class Agent:
        async def interpret(self, text, context):
            self.context = context
            return next(queue)

        async def rank(self, query, products):
            return Selection(exact_ids=exact.get(query, []) if exact else [p["id"] for p in products][:3])

    return Agent()


def catalog(results):
    async def search(query, location, session):
        return {"items": results.get(query, [])}
    return search


@pytest.mark.asyncio
async def test_add_puts_a_confident_match_straight_into_the_cart_and_empty_cart_works(store):
    from smartbazaar.agent import ShoppingLine

    account = connected(store)
    commerce = EditCommerce()
    commerce.search = catalog({"coke zero": [COKE]})
    agent = shop_agent(Intent(action="add", items=[ShoppingLine(query="coke zero", quantity=1)]))
    bot = Conversation(store, commerce, agent, CheckoutFlow(store, commerce, cart_writes=True))

    added = await bot.handle(OWNER, "919000000001", "add a coke zero")

    assert "🛒 Added to your JioMart cart:\n• 1 × Coca Cola Zero 300 ml" in body(added[0])
    assert [b["title"] for b in added[-1]["buttons"]] == ["View cart", "Checkout", "Keep shopping"]
    assert "draft" not in store.get_memory(OWNER, account) and "pending" not in store.get_memory(OWNER, account)
    assert "101" in [line["identifiers"]["product_uid"] for line in commerce.lines]

    emptied = await bot.handle(OWNER, "919000000001", "empty cart")
    assert body(emptied[0]).startswith("Emptied your JioMart cart (4 item(s) removed)")
    assert commerce.lines == []


@pytest.mark.asyncio
async def test_choices_without_pictures_are_a_tap_menu_and_a_tapped_row_adds_that_product(store):
    from smartbazaar.agent import ShoppingLine

    connected(store)
    commerce = EditCommerce()
    commerce.lines = []
    commerce.search = catalog({"paneer": PANEER})
    agent = shop_agent(Intent(action="add", items=[ShoppingLine(query="paneer", quantity=1)]))
    bot = Conversation(store, commerce, agent, CheckoutFlow(store, commerce, cart_writes=True))
    asked = await bot.handle(OWNER, "919000000001", "add paneer")
    menu = next(r for r in asked if isinstance(r, dict) and r.get("type") == "list")

    assert [row["title"] for row in menu["rows"]] == ["Chitale Paneer 200 g", "iD Natural Paneer 200 g", "Skip this item"]
    assert menu["rows"][1]["description"].startswith("₹106")

    picked = await bot.handle(OWNER, "919000000001", "/button:" + menu["rows"][1]["id"])

    assert [line["identifiers"]["product_uid"] for line in commerce.lines] == ["502"]
    assert "Added to your JioMart cart:\n• 1 × iD Natural Paneer 200 g" in body(picked[0])


@pytest.mark.asyncio
async def test_choices_with_pictures_are_numbered_buttons_with_skip(store):
    from smartbazaar.agent import ShoppingLine

    account = connected(store)
    commerce = EditCommerce()
    commerce.lines = []
    pictured = [{**p, "medias": [{"type": "image", "url": f"https://cdn1.jiomartjcp.com/{p['uid']}.jpg"}]} for p in PANEER]
    commerce.search = catalog({"paneer": pictured})
    agent = shop_agent(Intent(action="add", items=[ShoppingLine(query="paneer", quantity=1)]))
    bot = Conversation(store, commerce, agent, CheckoutFlow(store, commerce, cart_writes=True))

    asked = await bot.handle(OWNER, "919000000001", "add paneer")
    card = asked[-1]

    assert card["type"] == "choice_card" and len(card["items"]) == 2
    assert [b["title"] for b in card["buttons"]] == ["1. Chitale 200 g", "2. iD 200 g", "Skip"]
    assert "1. Chitale Paneer 200 g — 200 g · ₹93" in card["body"]
    assert card["list"]["type"] == "list"  # used if the pictures can't be built

    skipped = await bot.handle(OWNER, "919000000001", "/button:" + card["buttons"][2]["id"])
    assert body(skipped[0]).startswith("Skipped paneer.")
    assert commerce.lines == [] and "pending" not in store.get_memory(OWNER, account)


@pytest.mark.asyncio
async def test_a_newer_request_drops_older_unanswered_questions(store):
    from smartbazaar.agent import ShoppingLine

    account = connected(store)
    commerce = EditCommerce()
    commerce.lines = []
    bread = [{**item, "uid": item["uid"] + 100, "name": item["name"].replace("Paneer", "Bread")} for item in PANEER]
    commerce.search = catalog({"paneer": PANEER, "bread": bread, "coke zero": [COKE]})
    agent = shop_agent(
        Intent(action="add", items=[ShoppingLine(query="paneer"), ShoppingLine(query="bread")]),
        Intent(action="add", items=[ShoppingLine(query="coke zero")]),
    )
    bot = Conversation(store, commerce, agent, CheckoutFlow(store, commerce, cart_writes=True))

    first = await bot.handle(OWNER, "919000000001", "add paneer and bread")
    assert "Which paneer would you like? (1 of 2 to choose)" in first[-1]["body"]
    old_choice = first[-1]["rows"][0]["id"]

    second = await bot.handle(OWNER, "919000000001", "add a coke zero")

    # Only the new request is answered: no "Which bread?" or list of old items comes back.
    assert "Coca Cola Zero 300 ml" in body(second[0]) and len(second) == 1
    assert "pending" not in store.get_memory(OWNER, account)
    stale = await bot.handle(OWNER, "919000000001", "/button:" + old_choice)
    assert "earlier request" in body(stale[0])
    assert [line["identifiers"]["product_uid"] for line in commerce.lines] == ["101"]


@pytest.mark.asyncio
async def test_product_card_add_button_and_add_it_put_the_shown_product_in_the_cart(store):
    connected(store)
    commerce = EditCommerce()
    commerce.lines = []
    commerce.search = catalog({"coke zero": [COKE]})
    agent = shop_agent(Intent(action="search", queries=["coke zero"]),
                       Intent(action="add", viewed=True, checkout_after=True))
    bot = Conversation(store, commerce, agent, CheckoutFlow(store, commerce, cart_writes=True, checkout=False))

    found = await bot.handle(OWNER, "919000000001", "show me coke zero")
    card = found[0]
    assert card["type"] == "product_card"
    assert [b["title"] for b in card["buttons"]] == ["Add to cart", "View details", "Search again"]

    details = await bot.handle(OWNER, "919000000001", "/button:" + card["buttons"][1]["id"])
    assert "See it on JioMart: https://www.jiomart.com/product/coca-cola-zero-300-ml-101" in details[0]["body"]
    assert [b["title"] for b in details[0]["buttons"]] == ["Add to cart", "Keep shopping", "View cart"]

    tapped = await bot.handle(OWNER, "919000000001", "/button:" + details[0]["buttons"][0]["id"])
    assert "• 1 × Coca Cola Zero 300 ml" in body(tapped[0])

    # "Ok add it and checkout": the shown product again (already there), then the review.
    both = await bot.handle(OWNER, "919000000001", "ok add it and checkout")
    assert "Already in your cart" in body(both[0])
    assert any("finish in the JioMart app" in body(reply) for reply in both[1:])
    assert len(commerce.lines) == 1


@pytest.mark.asyncio
async def test_connected_greeting_offers_reorder_cart_and_track(store):
    connected(store)

    class Greeter:
        async def interpret(self, text, context):
            return Intent(action="help", reply="Hi Ganesh! What shall we get today?")

    bot = Conversation(store, CartCommerce(), Greeter(), CheckoutFlow(store, CartCommerce(), cart_writes=True))
    replies = await bot.handle(OWNER, "919000000001", "hi")

    assert [b["title"] for b in replies[0]["buttons"]] == ["Reorder last order", "View cart", "Track my order"]
    tapped = bot._quick_action(OWNER, store.get(OWNER), replies[0]["buttons"][2]["id"], __import__("time").time())
    assert tapped == "track 1"


@pytest.mark.asyncio
async def test_an_old_choice_button_never_discards_the_question_being_asked_now(store):
    from smartbazaar.agent import ShoppingLine

    account = connected(store)
    commerce = EditCommerce()
    commerce.lines = []
    commerce.search = catalog({"paneer": PANEER})
    agent = shop_agent(Intent(action="add", items=[ShoppingLine(query="paneer")]),
                       Intent(action="add", items=[ShoppingLine(query="paneer")]))
    bot = Conversation(store, commerce, agent, CheckoutFlow(store, commerce, cart_writes=True))
    first = await bot.handle(OWNER, "919000000001", "add paneer")
    second = await bot.handle(OWNER, "919000000001", "add paneer please")

    stale = await bot.handle(OWNER, "919000000001", "/button:" + first[-1]["rows"][0]["id"])

    assert body(stale[0]) == "That choice is from an earlier request."
    assert stale[-1]["type"] == "list" and "Which paneer" in stale[-1]["body"]
    assert store.get_memory(OWNER, account)["pending"]["items"][0]["query"] == "paneer"
    picked = await bot.handle(OWNER, "919000000001", "/button:" + stale[-1]["rows"][0]["id"])
    assert "• 1 × Chitale Paneer 200 g" in body(picked[0]) and second
