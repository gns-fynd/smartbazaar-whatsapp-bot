import pytest
from cryptography.fernet import Fernet

from smartbazaar.agent import Intent
from smartbazaar.catalog import Selection
from smartbazaar.conversation import Conversation
from smartbazaar.storage import Store


class FakeCommerce:
    def __init__(self):
        self.begin_calls = []
        self.verify_calls = []

    async def begin_login(self, mobile):
        self.begin_calls.append(mobile)
        return {"request_id": "challenge-1", "mobile": mobile, "cookies": []}

    async def verify_login(self, challenge, otp):
        self.verify_calls.append((challenge, otp))
        return {"account_reference": "account-1", "mobile": challenge["mobile"], "cookies": []}

    async def get_addresses(self, session):
        return [{"label": "Home", "pincode": "400053", "city": "Mumbai", "is_default": True}]


class FakeAgent:
    def __init__(self):
        self.interpret_calls = []

    async def interpret(self, text, context):
        self.interpret_calls.append((text, context))
        raise AssertionError("OTP and login flows must bypass the AI")


class SearchCommerce(FakeCommerce):
    async def search(self, query, location, session):
        return {
            "items": [
                {
                    "uid": "milk-1",
                    "name": "Amul Taaza Milk 1 L",
                    "price": {"effective": {"min": 74}},
                    "brand": {"name": "Amul"},
                    "sellable": True,
                    "medias": [
                        {
                            "type": "image",
                            "url": "https://cdn1.jiomartjcp.com/catalog/milk.jpg",
                        }
                    ],
                },
                {
                    "uid": "milk-2",
                    "name": "Mother Dairy Cow Milk 500 ml",
                    "price": {"effective": {"min": 30}},
                    "brand": {"name": "Mother Dairy"},
                    "sellable": True,
                    "medias": [
                        {
                            "type": "image",
                            "url": "https://cdn1.jiomartjcp.com/catalog/milk-2.jpg",
                        }
                    ],
                },
            ]
        }


class SearchAgent:
    async def interpret(self, text, context):
        return Intent(action="search", queries=["milk"])

    async def rank(self, query, products):
        return Selection(exact_ids=["milk-1", "milk-2"])


def body(reply):
    return reply if isinstance(reply, str) else reply.get("body", "")


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path / "bot.sqlite3"), Fernet.generate_key().decode())
    yield value
    value.close()


@pytest.mark.asyncio
async def test_otp_bypasses_ai_and_is_never_saved_plaintext(store):
    commerce, agent = FakeCommerce(), FakeAgent()
    conversation = Conversation(store, commerce, agent)

    await conversation.handle("owner-a", "919000000001", "LOGIN 9000000001")
    reply = await conversation.handle("owner-a", "919000000001", "123456")

    assert "Connected" in body(reply[0])
    # Every reply ends with tap-able next steps.
    assert reply[-1]["type"] == "quick_reply" and 2 <= len(reply[-1]["buttons"]) <= 3
    assert commerce.verify_calls[0][1] == "123456"
    assert agent.interpret_calls == []
    assert b"123456" not in store.db.execute("SELECT data FROM sessions").fetchone()[0]


@pytest.mark.asyncio
async def test_new_login_clears_previous_account_context_and_keeps_owners_isolated(store):
    commerce, agent = FakeCommerce(), FakeAgent()
    conversation = Conversation(store, commerce, agent)
    store.save(
        "owner-a",
        {
            "session": {"account_reference": "old-account"},
            "location": {"pincode": "400001"},
            "addresses": [{"pincode": "400001"}],
            "queries": ["old query"],
        },
    )
    store.save("owner-b", {"session": {"account_reference": "other-account"}})

    await conversation.handle("owner-a", "919000000001", "LOGIN 9000000002")

    current = store.get("owner-a")
    assert current["challenge"]["mobile"] == "9000000002"
    assert not {"session", "location", "addresses", "queries"} & current.keys()
    assert store.get("owner-b")["session"]["account_reference"] == "other-account"


@pytest.mark.asyncio
async def test_product_card_actions_are_encrypted_owner_bound_and_expire(store):
    conversation = Conversation(store, SearchCommerce(), SearchAgent())
    state = {
        "session": {"account_reference": "account-a"},
        "location": {"pincode": "400053"},
    }
    store.save("owner-a", state)

    replies = await conversation.handle("owner-a", "919000000001", "find milk")
    card = replies[0]
    assert isinstance(card, dict)
    assert card["type"] == "product_card"
    assert card["image_url"] == "https://cdn1.jiomartjcp.com/catalog/milk.jpg"
    assert [button["title"] for button in card["buttons"]] == ["View details", "Next option"]
    action_id = card["buttons"][0]["id"]
    assert action_id not in store.db.execute("SELECT data FROM sessions").fetchone()[0].decode(errors="ignore")

    store.save(
        "owner-b",
        {"session": {"account_reference": "account-b"}, "location": {"pincode": "400053"}},
    )
    foreign = await conversation.handle("owner-b", "919000000002", f"/button:{action_id}")
    assert "older search" in body(foreign[0]) and "Amul" not in body(foreign[0])

    details = await conversation.handle("owner-a", "919000000001", f"/button:{action_id}")
    assert "Amul Taaza Milk" in body(details[0])
    assert "isn’t in your cart yet" in body(details[0])
    assert [b["title"] for b in details[0]["buttons"]] == ["Keep shopping", "View cart"]

    next_action = card["buttons"][1]["id"]
    saved = store.get("owner-a")
    saved["button_actions"][next_action]["expires"] = 0
    store.save("owner-a", saved)
    expired = await conversation.handle("owner-a", "919000000001", f"/button:{next_action}")
    assert "older search" in body(expired[0])


@pytest.mark.asyncio
async def test_model_can_choose_text_over_available_catalog_image(store):
    class TextAgent(SearchAgent):
        async def interpret(self, text, context):
            return Intent(action="search", queries=["milk"], presentation="text")

    store.save("owner", {"session": {"account_reference": "account-a"}, "location": {"pincode": "400053"}})
    replies = await Conversation(store, SearchCommerce(), TextAgent()).handle("owner", "919000000001", "find milk, text only")
    assert not any(isinstance(reply, dict) and reply["type"] == "product_card" for reply in replies)
    assert "Amul" in body(replies[0])
