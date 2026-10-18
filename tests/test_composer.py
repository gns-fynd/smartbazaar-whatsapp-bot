import pytest
from cryptography.fernet import Fernet

from smartbazaar.composer import Composer, faithful
from smartbazaar.conversation import Conversation
from smartbazaar.storage import Store

DRAFT = (
    "🛒 Your JioMart cart\n"
    "1. 1 × Tomato Hybrid 1 kg pack — ₹48\n"
    "2. 1 × iD Natural Paneer 200 g (Pouch) — ₹106\n"
    "Items: ₹154\nTotal: ₹184\nDiscount: ₹-34"
)
GOOD = (
    "Here’s your cart 😊\n"
    "1 × Tomato Hybrid 1 kg pack — ₹48\n"
    "1 × iD Natural Paneer 200 g (Pouch) — ₹106\n"
    "Items ₹154, discount ₹-34, so you pay *₹184*."
)


def test_faithful_accepts_a_rewrite_that_keeps_every_fact():
    assert faithful(DRAFT, GOOD)


@pytest.mark.parametrize("bad", [
    GOOD.replace("₹106", "₹160"),                                   # changed price
    GOOD.replace("1 × iD Natural Paneer 200 g (Pouch) — ₹106\n", ""),  # dropped line (and amount)
    GOOD + " Delivery is ₹0 today!",                                 # invented amount
    GOOD.replace("1 × Tomato", "2 × Tomato"),                        # changed quantity
    "",                                                               # empty
])
def test_faithful_rejects_changed_dropped_or_invented_facts(bad):
    assert not faithful(DRAFT, bad)


def test_faithful_requires_order_ids():
    draft = "✅ Order placed! JioMart order JM6AD4FE16012F2BA0B5 for ₹214, cash on delivery."
    assert faithful(draft, "Done! Your order JM6AD4FE16012F2BA0B5 (₹214, cash on delivery) is placed ✅")
    assert not faithful(draft, "Done! Your order (₹214) is placed ✅")


class Writer:
    def __init__(self, message, prompt="", fail=False):
        self.message, self.prompt, self.fail, self.calls = message, prompt, fail, []

    async def compose_reply(self, customer, draft, button_draft=""):
        self.calls.append((customer, draft, button_draft))
        if self.fail:
            raise RuntimeError("model down")
        return {"message": self.message, "button_prompt": self.prompt}


@pytest.mark.asyncio
async def test_composer_merges_drafts_and_rewords_the_button_prompt_only():
    buttons = {"type": "quick_reply", "body": "Ready to order for ₹184. How would you like to pay?",
               "buttons": [{"id": "sbco_x", "title": "Cash on delivery"}], "fallback": "Ready to order for ₹184. How would you like to pay?\nmore"}
    writer = Writer(GOOD, "All set: ₹184 in total. How do you want to pay?")

    replies = await Composer(writer).apply("checkout karo", [DRAFT, "Second draft line.", buttons])

    assert replies[0] == GOOD and len(replies) == 2
    assert replies[1]["buttons"] == buttons["buttons"]
    assert replies[1]["body"] == "All set: ₹184 in total. How do you want to pay?"
    assert writer.calls[0][0] == "checkout karo"


@pytest.mark.asyncio
async def test_composer_falls_back_to_verified_drafts():
    unfaithful = await Composer(Writer(GOOD.replace("₹106", "₹99"))).apply("hi", [DRAFT])
    failing = await Composer(Writer(GOOD, fail=True)).apply("hi", [DRAFT])
    no_model = await Composer(object()).apply("hi", [DRAFT])

    assert unfaithful == [DRAFT] and failing == [DRAFT] and no_model == [DRAFT]


class ComposingAgent(Writer):
    async def interpret(self, text, context):
        from smartbazaar.agent import Intent
        return Intent(action="help", reply="Hi! I can help with groceries.", presentation="text")


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path / "bot.sqlite3"), Fernet.generate_key().decode())
    yield value
    value.close()


class LoginCommerce:
    async def begin_login(self, mobile):
        return {"request_id": "r1", "mobile": mobile, "cookies": []}

    async def verify_login(self, challenge, otp):
        return {"account_reference": "a1", "mobile": challenge["mobile"], "cookies": []}

    async def get_addresses(self, session):
        return [{"id": "addr-1", "label": "Home", "pincode": "400053", "city": "Mumbai", "is_default": True}]


@pytest.mark.asyncio
async def test_login_and_model_written_replies_are_never_recomposed(store):
    agent = ComposingAgent("rewritten")
    bot = Conversation(store, LoginCommerce(), agent)

    login = await bot.handle("owner-1", "919000000001", "LOGIN 9000000001")
    otp = await bot.handle("owner-1", "919000000001", "123456")
    store.delete("owner-1")
    greeting = await bot.handle("owner-1", "919000000001", "hello")

    body = [r if isinstance(r, str) else r["body"] for r in (login[0], greeting[0])]
    assert "OTP" in body[0] and otp and body[1] != "rewritten"
    assert agent.calls == []
