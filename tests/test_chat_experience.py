import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet

from smartbazaar.agent import AgentError, Intent
from smartbazaar.conversation import Conversation
from smartbazaar.memory import recent_turns, remember_exchange
from smartbazaar.storage import Store


class Planner:
    def __init__(self, *results):
        self.results = iter(results)
        self.calls = []

    async def interpret(self, text, context):
        self.calls.append((text, context))
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


class Commerce:
    def __init__(self):
        self.logins = []

    async def begin_login(self, mobile):
        self.logins.append(mobile)
        return {"request_id": "test", "mobile": mobile}


@pytest.fixture
def store(tmp_path):
    store = Store(str(tmp_path / "test.db"), Fernet.generate_key().decode())
    yield store
    store.close()


def body(reply):
    return reply if isinstance(reply, str) else reply.get("body", "")


def greeting():
    return Intent(action="help", reply="Hello! What groceries are on your mind?", presentation="buttons", quick_actions=["connect", "help"])


@pytest.mark.asyncio
async def test_guest_ai_greeting_context_and_connect_button_are_owner_bound(store):
    agent, commerce = Planner(greeting(), Intent(action="help", reply="We were talking about groceries.", presentation="text")), Commerce()
    conversation = Conversation(store, commerce, agent)
    card = (await conversation.handle("a", "919000000001", "hi"))[0]
    assert card["body"] == "Hello! What groceries are on your mind?"
    assert card["type"] == "quick_reply"
    assert agent.calls[0][1]["connected"] is False
    assert not commerce.logins
    reply = await conversation.handle("a", "919000000001", "what were we discussing?")
    assert body(reply[0]) == "We were talking about groceries."
    # Even a text-only answer ends with next steps; a guest gets Connect / help.
    assert [b["title"] for b in reply[0]["buttons"]] == ["Connect JioMart", "What can you do?"]
    assert agent.calls[1][1]["recent_turns"][0]["content"] == "hi"
    raw = store.db.execute("SELECT data FROM sessions WHERE owner='a'").fetchone()[0]
    assert b"groceries" not in raw

    action = card["buttons"][0]["id"]
    assert "expired" in body((await conversation.handle("b", "919000000002", f"/button:{action}"))[0])
    assert not commerce.logins
    await conversation.handle("a", "919000000001", f"/button:{action}")
    assert commerce.logins == ["9000000001"]
    assert "dialogue" not in store.get("a")
    assert "expired" in body((await conversation.handle("a", "919000000001", f"/button:{action}"))[0])
    assert len(commerce.logins) == 1


@pytest.mark.asyncio
async def test_guest_shopping_or_malicious_planner_cannot_execute_account_tools(store):
    agent = Planner(Intent(action="add", reply="I can help you find curd.", quick_actions=["cart", "orders"]),
                    Intent(action="checkout", reply="Let’s connect first."))
    conversation = Conversation(store, Commerce(), agent)
    for text in ("buy curd", "checkout now"):
        card = (await conversation.handle("a", "919000000001", text))[0]
        assert "No items have been added" in card["body"]
        assert card["buttons"][0]["title"] == "Connect JioMart"
    assert store.db.execute("SELECT count(*) FROM shopping_memory").fetchone()[0] == 0
    assert "session" not in store.get("a")


@pytest.mark.asyncio
async def test_connected_greeting_uses_ai_and_pending_context_without_reconnect(store):
    store.save("a", {"session": {"account_reference": "account-a"}, "location": {"pincode": "400053"}})
    key = store.memory_key(store.get("a")["session"])
    store.save_memory("a", key, {"pending": {"items": [{"query": "curd", "quantity": 2, "products": []}],
                                             "pincode": "400053", "expires": time.time() + 600}})
    agent = Planner(Intent(action="help", reply="Welcome back! Want to keep shopping?", presentation="buttons",
                           quick_actions=["search", "cart"]))
    conversation = Conversation(store, Commerce(), agent)
    card = (await conversation.handle("a", "919000000001", "hey"))[0]
    assert "Welcome back!" in card["body"]
    assert agent.calls[0][1]["connected"] is True
    assert agent.calls[0][1]["pending"][0]["query"] == "curd"
    assert [button["title"] for button in card["buttons"]] == ["Keep shopping", "View cart"]
    reply = await conversation.handle("a", "919000000001", "/button:" + card["buttons"][0]["id"])
    assert "What would you like to add?" in body(reply[0])
    assert len(agent.calls) == 1


@pytest.mark.asyncio
async def test_secrets_pending_otp_and_logout_do_not_enter_chat_memory(store):
    agent = Planner(greeting())
    conversation = Conversation(store, Commerce(), agent)
    await conversation.handle("a", "919000000001", "hi")
    for text in ("my password is very-secret", "OTP 123456", "my phone is 9000000001", "1234"):
        await conversation.handle("a", "919000000001", text)
    assert len(agent.calls) == 1
    assert len(store.get("a")["dialogue"]["turns"]) == 2
    await conversation.handle("a", "919000000001", "LOGIN")
    await conversation.handle("a", "919000000001", "hello")
    assert len(agent.calls) == 1
    assert "dialogue" not in store.get("a")
    await conversation.handle("a", "919000000001", "LOGOUT")
    # Only the new Connect/help shortcuts remain; no session, dialogue or memory.
    assert set(store.get("a")) <= {"button_actions", "ai_note_day"}


@pytest.mark.asyncio
async def test_guest_ai_outage_has_working_connect_fallback_and_offtopic_is_text(store):
    agent = Planner(AgentError("unavailable"), Intent(action="unsupported", reply="I can help with groceries; what would you like to shop for?", presentation="text"))
    conversation = Conversation(store, Commerce(), agent)
    card = (await conversation.handle("a", "919000000001", "hello"))[0]
    assert card["type"] == "quick_reply" and card["buttons"][0]["title"] == "Connect JioMart"
    reply = await conversation.handle("a", "919000000001", "write me a sorting algorithm")
    assert "groceries" in body(reply[0])


@pytest.mark.asyncio
async def test_new_session_and_forget_clear_context_without_leaking_to_another_owner(store):
    agent = Planner(greeting(), greeting())
    conversation = Conversation(store, Commerce(), agent)
    await conversation.handle("a", "919000000001", "hi")
    await conversation.handle("b", "919000000002", "hey")
    assert agent.calls[1][1]["recent_turns"] == []
    await conversation.handle("a", "919000000001", "NEW SESSION")
    assert "dialogue" not in store.get("a")
    assert store.get("b")["dialogue"]["turns"]
    await conversation.handle("b", "919000000002", "FORGET")
    assert "dialogue" not in store.get("b")


def test_chat_window_bounds_daily_idle_account_rotation_and_redaction():
    state = {}
    now = datetime(2026, 10, 18, 23, 59, tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()
    for i in range(12):
        remember_exchange(state, now, "account-a", f"question {i} " + "x" * 600, "Answer")
    assert len(recent_turns(state, now, "account-a")) == 8
    assert max(len(t["content"]) for t in state["dialogue"]["turns"]) == 480
    assert recent_turns(state, now + 120, "account-a") == []
    remember_exchange(state, now + 120, "account-a", "keep this", "ok")
    assert recent_turns(state, now + 120 + 4 * 3600 + 1, "account-a") == []
    remember_exchange(state, now, "account-a", "email test@example.com otp 123456 mobile 9000000001", "password=secret")
    content = str(state["dialogue"]["turns"])
    assert all(secret not in content for secret in ("test@example.com", "123456", "9000000001", "secret"))
    assert recent_turns(state, now, "account-b") == []


@pytest.mark.asyncio
async def test_quick_action_expiry_and_account_binding(store):
    agent = Planner(greeting(), greeting())
    conversation = Conversation(store, Commerce(), agent)
    card = (await conversation.handle("a", "919000000001", "hi"))[0]
    action = card["buttons"][0]["id"]
    state = store.get("a")
    state["button_actions"][action]["expires"] = time.time() - 1
    store.save("a", state)
    assert "expired" in body((await conversation.handle("a", "919000000001", f"/button:{action}"))[0])
    card = (await conversation.handle("a", "919000000001", "hi"))[0]
    state = store.get("a")
    state["session"] = {"account_reference": "new-account"}
    store.save("a", state)
    assert "expired" in body((await conversation.handle("a", "919000000001", "/button:" + card["buttons"][0]["id"]))[0])


@pytest.mark.asyncio
async def test_model_selected_buttons_wrap_authoritative_reply_and_forget_clears_dialogue(store):
    store.save("a", {"session": {"account_reference": "account-a"}, "location": {"pincode": "400053"}})
    key = store.memory_key(store.get("a")["session"])
    store.save_memory("a", key, {"preferences": {"curd": {"product": {"name": "Amul Masti Dahi 400 g"},
                                                          "source": "explicit", "expires": time.time() + 600}},
                                 "pending": {"items": [], "pincode": "400053", "expires": time.time() + 600}})
    agent = Planner(Intent(action="memory", reply="Untrusted invented purchase", presentation="buttons", quick_actions=["search"]),
                    Intent(action="forget"))
    conversation = Conversation(store, Commerce(), agent)
    card = (await conversation.handle("a", "919000000001", "what do you remember about me"))[0]
    assert "curd: Amul Masti Dahi 400 g" in card["body"]
    assert "Untrusted invented purchase" not in card["body"]
    assert card["buttons"][0]["title"] == "Keep shopping"
    assert store.get("a")["dialogue"]["turns"]
    await conversation.handle("a", "919000000001", "please forget everything")
    assert "dialogue" not in store.get("a")
    assert not {"preferences", "pending"} & store.get_memory("a", key).keys()


@pytest.mark.asyncio
async def test_voice_note_goes_through_the_normal_path_and_shows_what_was_heard(store):
    agent = Planner(Intent(action="help", reply="I can track your orders and fill your cart.", presentation="text"))
    conversation = Conversation(store, Commerce(), agent)

    reply = await conversation.handle("a", "919000000001", "what can you do for me", voice=True)

    assert agent.calls[0][0] == "what can you do for me"
    assert body(reply[0]).startswith("🎤 “what can you do for me”\n\nI can track your orders")
    assert reply[-1]["type"] == "quick_reply"


@pytest.mark.asyncio
async def test_voice_reply_adds_one_voice_note_after_the_text_only_for_a_transcribed_voice_note(store):
    agent = Planner(Intent(action="help", reply="I can track your orders and fill your cart.", presentation="text"))
    conversation = Conversation(store, Commerce(), agent, voice_replies="voice")

    reply = await conversation.handle("a", "919000000001", "what can you do for me", voice=True)

    # The spoken text is the bot's own answer, not the "🎤 heard" echo or a tap target, and it
    # follows the text so the buttons never wait for speech synthesis.
    # The raw answer plus what the customer said; the sender rewrites it as natural speech.
    assert reply[-1] == {"type": "voice_note", "text": "I can track your orders and fill your cart.",
                         "context": "what can you do for me", "language": "en"}
    assert body(reply[0]).startswith("🎤 “what can you do for me”\n\nI can track your orders")
    assert reply[-2]["type"] == "quick_reply"


@pytest.mark.asyncio
async def test_voice_reply_in_voice_only_mode_is_skipped_for_typed_text(store):
    agent = Planner(Intent(action="help", reply="I can track your orders and fill your cart.", presentation="text"))
    conversation = Conversation(store, Commerce(), agent, voice_replies="voice")

    reply = await conversation.handle("a", "919000000001", "what can you do for me")

    assert all(not (isinstance(r, dict) and r.get("type") == "voice_note") for r in reply)


@pytest.mark.asyncio
async def test_voice_reply_always_mode_speaks_even_a_typed_reply(store):
    agent = Planner(Intent(action="help", reply="I can track your orders and fill your cart.", presentation="text"))
    conversation = Conversation(store, Commerce(), agent, voice_replies="always")

    reply = await conversation.handle("a", "919000000001", "what can you do for me")

    assert reply[-1] == {"type": "voice_note", "text": "I can track your orders and fill your cart.",
                         "context": "", "language": "en"}


@pytest.mark.asyncio
async def test_voice_reply_defaults_off_so_a_configured_voice_note_setting_is_required(store):
    agent = Planner(Intent(action="help", reply="I can track your orders and fill your cart.", presentation="text"))
    conversation = Conversation(store, Commerce(), agent)  # no voice_replies kwarg: defaults off

    reply = await conversation.handle("a", "919000000001", "what can you do for me", voice=True)

    assert all(not (isinstance(r, dict) and r.get("type") == "voice_note") for r in reply)


@pytest.mark.asyncio
async def test_ai_disclosure_is_a_daily_footer_and_buttons_follow_the_customers_language(store):
    agent = Planner(greeting(), greeting(), greeting())
    conversation = Conversation(store, Commerce(), agent)

    first = (await conversation.handle("a", "919000000001", "mujhe dahi chahiye bhai"))[0]
    # Hinglish in, Hinglish buttons and disclosure out; the reply text itself is untouched.
    assert first["body"] == "Hello! What groceries are on your mind?"
    assert first["footer"] == "🤖 Main SmartBazaar ka AI shopping assistant hoon."
    assert [b["title"] for b in first["buttons"]] == ["JioMart connect karo", "Aap kya karte ho?"]

    second = (await conversation.handle("a", "919000000001", "hello"))[0]
    assert "footer" not in second  # once a day is enough
    assert [b["title"] for b in second["buttons"]] == ["Connect JioMart", "What can you do?"]

    third = (await conversation.handle("b", "919000000002", "दूध चाहिए"))[0]
    assert third["footer"].startswith("🤖 मैं SmartBazaar") and third["buttons"][0]["title"] == "JioMart जोड़ें"
