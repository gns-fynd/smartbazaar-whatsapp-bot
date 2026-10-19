"""Account control and scoped shopping orchestration; authentication bypasses the planner."""

import re
import secrets
import time
from collections.abc import Mapping
from typing import Any

from .agent import AgentError, Intent
from .catalog import Selection, normalize_products, product_link, render_search
from .checkout import CheckoutFlow
from .composer import Composer
from .jiomart import CommerceError
from .memory import recent_turns, remember_exchange, roll_session
from .orders import OrdersFlow
from .tools import ShoppingTools
from .tts import spoken_text

# Tap targets stay valid for a day; anything account-changing re-checks its own state.
BUTTON_TTL = 24 * 3600
MAX_BUTTON_ACTIONS = 40
LABELS = {
    "connect": "Connect JioMart", "help": "What can you do?", "history": "My orders", "orders": "My orders",
    "search": "Keep shopping", "reorder": "Reorder last order", "cart": "View cart", "checkout": "Checkout",
    "track": "Track my order", "addresses": "Change address", "cancel_login": "Cancel login",
    "resend_otp": "Resend OTP",
}
COMMANDS = {
    "connect": "LOGIN", "help": "What can you do for me?", "history": "orders", "orders": "orders",
    "search": "keep shopping", "reorder": "reorder 1", "cart": "cart", "checkout": "checkout",
    "track": "track 1", "addresses": "addresses", "cancel_login": "cancel", "resend_otp": "login",
}
ACCOUNT_ONLY = {"history", "orders", "reorder", "cart", "checkout", "track", "addresses", "search"}
VIEWED_ADDS = {"add to cart", "add it", "add this", "add that", "add it to cart", "add this to cart",
               "add it to my cart", "add this to my cart", "buy it", "buy this", "i want this", "i'll take it"}
# Card types whose "body" is the reply's spoken-aloud text for a voice-note reply.
VOICE_TEXT_TYPES = {"quick_reply", "list", "choice_card", "cart_card", "product_card"}


class Written(list):
    """Replies the model already wrote (greetings, help); they are not rewritten again."""


def mobile_number(value: str) -> str | None:
    digits = re.sub(r"[\s()+-]", "", value)
    if re.fullmatch(r"[6-9]\d{9}", digits):
        return digits
    if re.fullmatch(r"91[6-9]\d{9}", digits):
        return digits[2:]
    return None


class Conversation:
    def __init__(self, store, commerce, agent, checkout=None, voice_replies: str = "off"):
        self.store, self.commerce, self.agent = store, commerce, agent
        self.checkout = checkout or CheckoutFlow(store, commerce)
        self.tools = ShoppingTools(commerce, agent, self.checkout)
        self.orders = OrdersFlow(store, commerce, self.checkout)
        self.composer = Composer(agent)
        # "off" | "voice" (only for a transcribed voice note) | "always"; the caller resolves
        # this against whether a TTS key is configured, so Conversation never sees that secret.
        self.voice_replies = voice_replies
        # The action behind the reply being built, per owner (owners are processed one at a time).
        self._last_action: dict[str, str] = {}

    async def handle(self, owner: str, sender: str, text: str, *, shopping_only: bool = False,
                     voice: bool = False) -> list[str | dict[str, Any]]:
        state = self.store.get(owner)
        # WhatsApp allows free-form order updates only within 24 hours of the customer's last message.
        state["last_inbound"] = time.time()
        self.store.save(owner, state)
        self._last_action.pop(owner, None)
        replies = await self._dispatch(owner, sender, text, shopping_only=shopping_only)
        if self._composable(text, replies):
            # Tools supply the facts; the model writes the reply, and the composer checks it.
            customer = "(tapped a button)" if text.startswith("/button:") else text
            replies = await self.composer.apply(customer, replies)
        # Captured before the "heard" quote or buttons are added, so the spoken text is only
        # ever the bot's own answer, never an echo of the transcript or a tap target.
        to_speak = self._voice_text(replies) if self._voice_reply_enabled(voice) else None
        if voice and replies:
            # Show what was heard, so a mis-heard voice note is obvious.
            heard = "🎤 “" + " ".join(text.split())[:300] + "”"
            if isinstance(replies[0], str) and len(heard) + len(replies[0]) < 1000:
                replies = [heard + "\n\n" + replies[0]] + replies[1:]
            else:
                replies = [heard] + replies
        replies = self._with_buttons(owner, replies)
        if to_speak:
            spoken = spoken_text(to_speak)
            if spoken:
                # After the text: synthesis takes a few seconds, and the text with its buttons
                # should never wait for it. The voice note follows as the spoken version.
                replies = replies + [{"type": "voice_note", "text": spoken}]
        notice = self._missed_notice(owner)
        return ([notice] + replies) if notice else replies

    def _voice_reply_enabled(self, voice: bool) -> bool:
        if self.voice_replies == "always":
            return True
        return self.voice_replies == "voice" and voice

    @staticmethod
    def _voice_text(replies) -> str | None:
        """The reply's main text: the first str, or the ``body`` of a text-bearing card."""
        if not replies:
            return None
        first = replies[0]
        if isinstance(first, str):
            return first
        if isinstance(first, Mapping) and first.get("type") in VOICE_TEXT_TYPES:
            body = first.get("body")
            return body if isinstance(body, str) else None
        return None

    def _composable(self, text: str, replies) -> bool:
        """Account, OTP and address replies stay deterministic; model-written replies aren't redone."""
        if not replies or isinstance(replies, Written):
            return False
        if text.startswith(("/button:sbco_", "/button:sbor_", "/button:sbpc_")):
            return True
        return self._can_remember(text)

    def _choice_command(self, owner: str, token: str) -> str:
        state = self.store.get(owner)
        try:
            account = self.store.memory_key(state["session"])
        except (KeyError, ValueError):
            return "choose 0"
        tokens = self.store.get_memory(owner, account).get("pending", {}).get("tokens", {})
        index = tokens.get(token) if isinstance(tokens, dict) else None
        if index == -1:
            return "skip"
        return f"choose {index + 1}" if isinstance(index, int) else "choose 0"

    def _missed_notice(self, owner: str) -> str | None:
        """Deliver an order update that arrived outside WhatsApp's messaging window."""
        state = self.store.get(owner)
        if not state.get("session"):
            return None
        try:
            account = self.store.memory_key(state["session"])
        except ValueError:
            return None
        latest = self.store.latest_attempt(owner, account)
        if not latest or not latest[2].get("missed_notice"):
            return None
        data = latest[2]
        notice = data.pop("missed_notice")
        self.store.update_attempt(latest[0], latest[1], data)
        return "Update on your order:\n" + notice

    async def _dispatch(self, owner: str, sender: str, text: str, *, shopping_only: bool = False) -> list[str | dict[str, Any]]:
        state = self.store.get(owner)
        if text.startswith("/button:sbco_"):
            return await self._checkout_button(owner, sender, text.removeprefix("/button:"))
        if text.startswith("/button:sbch_"):
            # A tapped product row becomes the same deterministic choice as typing its number.
            text = self._choice_command(owner, text.removeprefix("/button:"))
        if text.startswith("/button:sbor_"):
            return await self._account_button(owner, text.removeprefix("/button:"), self.orders.button)
        if text.startswith("/button:sbqa_"):
            command = self._quick_action(owner, state, text.removeprefix("/button:"), time.time())
            if command is None:
                return ["That shortcut has expired. Tell me what you’d like to do and I’ll help."]
            text = command
        before = self.store.get(owner)
        replies = await self._handle(owner, sender, text, shopping_only=shopping_only)
        state = self.store.get(owner)
        # Authentication, address selection and memory deletion never become dialogue.
        if state.get("dialogue") and not before.get("challenge") and self._can_remember(text):
            summary = "\n".join(r if isinstance(r, str) else r.get("body", "") for r in replies)
            remember_exchange(state, time.time(), self._dialogue_scope(state), text, summary)
            self.store.save(owner, state)
        return replies

    async def _handle(self, owner: str, sender: str, text: str, *, shopping_only: bool = False) -> list[str | dict[str, Any]]:
        text = text.strip()
        state = self.store.get(owner)
        now = time.time()
        if len(text) > 1500:
            return ["Please send a shorter request with up to five products."]
        # Hard commands and sensitive fields never reach an LLM.
        lower = text.casefold()
        if shopping_only and (
            not state.get("session") or state.get("challenge")
            or not lower.startswith(("find ", "buy "))
        ):
            return ["Please use text or a voice note for account changes. A photo can only supply shopping items."]
        if lower.startswith("/button:"):
            result = self._button_reply(owner, state, text.removeprefix("/button:"), now)
            if not isinstance(result, str):
                return result
            # A product button that stands for a command (Add to cart) continues as that command.
            text, lower = result, result.casefold()
            state = self.store.get(owner)
        if lower in {"logout", "disconnect", "log out"}:
            self.store.forget_memory(owner)
            self.store.delete(owner)
            return [
                "Disconnected. I’ve removed this bot’s saved JioMart session and memory. Your JioMart cart is unchanged."
            ]
        if lower in {"cancel", "stop"}:
            state.pop("challenge", None)
            state.pop("pending_queries", None)
            self.store.save(owner, state)
            self._drop_pending(owner, state)
            return ["Stopped the current request. You can search again or send LOGIN when ready."]
        login = re.fullmatch(r"(?:login|log in|connect|send otp)(?:\s+([\d ()+-]+))?", text, re.I)
        phone = mobile_number(text)
        if login or phone:
            requested = mobile_number(login.group(1) or sender) if login else phone
            if not requested:
                return [
                    "Please send LOGIN followed by the 10-digit Indian mobile number on your JioMart account."
                ]
            return await self._begin(owner, state, requested, now)

        challenge = state.get("challenge")
        if challenge:
            if now > state.get("challenge_expires", 0):
                state.pop("challenge", None)
                self.store.save(owner, state)
                return ["That login attempt has expired. Send LOGIN for a new OTP."]
            if not re.fullmatch(r"\d{4,8}", text):
                return [
                    "Please send only the OTP from JioMart, or CANCEL to stop login. I won’t send it to the AI."
                ]
            state["otp_attempts"] = state.get("otp_attempts", 0) + 1
            if state["otp_attempts"] > 5:
                state.pop("challenge", None)
                self.store.save(owner, state)
                return ["Too many attempts. Send LOGIN to start again after the cooldown."]
            self.store.save(owner, state)
            try:
                state["session"] = await self.commerce.verify_login(challenge, text)
                state.pop("challenge", None)
                self.store.save(owner, state)
                addresses = await self.commerce.get_addresses(state["session"])
                state["addresses"] = addresses
                default = next((a for a in addresses if a.get("is_default")), None)
                if default or len(addresses) == 1:
                    state["location"] = default or addresses[0]
                self.store.save(owner, state)
                location = state.get("location")
                if location:
                    reply = f"Connected to your JioMart account. Searching for pincode {location['pincode']}.\nTell me what you’d like to buy, or send a voice note or product photo. Send CART any time to see your JioMart cart and ADDRESSES to change the delivery area."
                else:
                    reply = "Connected to your JioMart account. Send your 6-digit pincode for product search, or ADDRESSES to choose a saved address."
                return [reply]
            except CommerceError as exc:
                self.store.save(owner, state)
                if exc.code == "auth_expired":
                    return self._handle_commerce_error(owner, state, exc)
                if state.get("session") and not state.get("challenge"):
                    return [
                        "You’re connected, but I couldn’t load saved addresses. Send your 6-digit pincode to search."
                    ]
                return [self._commerce_message(exc)]

        # Unsolicited codes, phone-like strings and credentials never enter model context.
        if re.fullmatch(r"\d{4,8}", text) and not re.fullmatch(r"[1-9]\d{5}", text):
            return ["There’s no active OTP request. Send LOGIN to connect your account."]
        if re.search(
            r"\b(?:otp|password|api[_ -]?key|access[_ -]?token)\b|sk-(?:proj-)?[A-Za-z0-9_-]{12,}",
            text,
            re.I,
        ):
            return [
                "Use LOGIN to start secure account connection. Please don’t send passwords or API keys here."
            ]
        if re.search(r"(?<!\d)\+?\d[\d ()-]{8,}\d(?!\d)", text):
            return [
                "For account connection, send LOGIN followed by your registered mobile number. For search, send a product name."
            ]

        pin = re.fullmatch(r"(?:(?:pin(?:code)?|postal code)\s*:?\s*)?([1-9]\d{5})", text, re.I)
        if pin:
            return self._location(owner, state, pin.group(1))
        if not state.get("session"):
            if lower in {"forget", "forget me", "new session"}:
                state.pop("dialogue", None)
                state.pop("button_actions", None)
                self.store.save(owner, state)
                return ["I’ve cleared our recent chat context. What would you like help with?"]
            try:
                intent = await self.agent.interpret(text, self._chat_context(state, now))
                # Guests can converse, but no planner decision authorizes commerce access.
                replies = self._conversational_reply(intent, state, connected=False)
            except AgentError:
                replies = self._quick_reply(
                    "I’m having trouble responding right now. I can help with JioMart groceries, product searches and your JioMart cart. You can connect your account when you’re ready.",
                    ["connect", "help"], state,
                )
            self.store.save(owner, state)
            return replies
        if lower in {"addresses", "address", "change address"}:
            try:
                addresses = await self.commerce.get_addresses(state["session"])
                state["addresses"] = addresses
                self.store.save(owner, state)
                if not addresses:
                    return [
                        "No saved search address was returned. Send PINCODE followed by your 6-digit pincode."
                    ]
                lines = ["Choose a saved delivery area:"]
                for i, address in enumerate(addresses[:10], 1):
                    lines.append(
                        f"{i}. {address.get('label', 'Address')} — {address.get('city', '')}, {address['pincode']}"
                    )
                lines.append("Reply ADDRESS 1 (or another number), or send a pincode.")
                return ["\n".join(lines)]
            except CommerceError as exc:
                return self._handle_commerce_error(owner, state, exc)
        address_pick = re.fullmatch(r"address\s+(\d{1,2})", lower)
        if address_pick:
            index = int(address_pick.group(1)) - 1
            addresses = state.get("addresses", [])
            if not 0 <= index < min(len(addresses), 10):
                return [
                    "That address choice isn’t available. Send ADDRESSES to see your saved choices."
                ]
            state["location"] = addresses[index]
            self._invalidate_location(owner, state)
            state.pop("button_actions", None)
            self.store.save(owner, state)
            return [
                f"Searching for pincode {state['location']['pincode']}. What would you like to find?"
            ]
        try:
            account = self.store.memory_key(state["session"])
        except ValueError:
            return ["Please send LOGIN to reconnect your account before shopping."]
        memory = self.store.get_memory(owner, account)
        if roll_session(memory, now):
            state.pop("queries", None)
            state.pop("button_actions", None)
        commands = {
            "list": "cart", "my list": "cart", "cart": "cart", "my cart": "cart", "view cart": "cart",
            "show cart": "cart", "show my cart": "cart",
            "history": "orders", "orders": "orders", "my orders": "orders", "order history": "orders",
            "past orders": "orders", "memory": "memory", "forget": "forget", "forget me": "forget",
            "new session": "new_session", "checkout": "checkout", "check out": "checkout", "place order": "checkout",
            "order status": "order_status", "track": "track", "track order": "track", "track my order": "track",
            "empty cart": "empty_cart", "clear cart": "empty_cart", "empty my cart": "empty_cart",
            "reorder": "reorder", "order again": "reorder", "skip": "skip",
            "cancel order": "cancel_order", "cancel my order": "cancel_order",
            "invoice": "invoice", "my invoice": "invoice", "send invoice": "invoice", "bill": "invoice",
        }
        cart_command = re.fullmatch(r"remove (?:(unavailable)(?: items?)?|(\d{1,2}) from (?:my |the )?cart)", lower)
        numbered = re.fullmatch(r"(order|track|reorder|invoice|cancel order)\s+(\d{1,2})", lower)
        chosen = re.fullmatch(r"(?:choose|option|pick)\s+(\d)", lower)
        if lower == "clear order check":
            return await self.checkout.clear_pending(owner, state, account)
        if lower in {"keep shopping", "continue shopping", "shop"}:
            self._last_action[owner] = "keep_shopping"
            return ["What would you like to add? Type it, send a voice note, or a photo of your list."]
        if lower in {"clear list", "clear my list", "skip all"}:
            memory.pop("pending", None)
            self._last_action[owner] = "skip"
            return ["Okay, I won’t ask about those items. Your JioMart cart is unchanged."]
        chat_context = self._chat_context(state, now)
        try:
            if chosen:
                number = int(chosen.group(1))
                intent = Intent(action="choose", choice_number=number if 1 <= number <= 3 else None)
            elif numbered and 1 <= int(numbered.group(2)) <= 12:
                kind = {"order": "order_status", "track": "track", "reorder": "reorder", "invoice": "invoice",
                        "cancel order": "cancel_order"}[numbered.group(1)]
                intent = Intent(action=kind, order_number=int(numbered.group(2)))
            elif cart_command and (cart_command.group(1) or 1 <= int(cart_command.group(2)) <= 30):
                intent = Intent(action="cart_remove", remove_unavailable=bool(cart_command.group(1)),
                                line_number=int(cart_command.group(2)) if cart_command.group(2) else None)
            elif lower in VIEWED_ADDS:
                intent = Intent(action="add", viewed=True)
            else:
                intent = Intent(action=commands[lower]) if lower in commands else await self.agent.interpret(
                    text,
                    {**self.tools.context(memory, state), **chat_context, "cart": self.checkout.view_context(state),
                     "pincode": state.get("location", {}).get("pincode")},
                )
            self._last_action[owner] = intent.action
            if shopping_only and intent.action not in {"search", "add"}:
                return ["Please send shopping items in the photo, and use text or a voice note for other actions."]
            if intent.action in {"checkout", "cart", "cart_remove", "cart_quantity", "order_status", "orders",
                                 "history", "track", "reorder", "cancel_order", "invoice"}:
                replies = await self._commerce_action(intent, owner, state, memory, account)
                return replies if intent.action == "reorder" else self._present_results(replies, intent, state)
            if intent.action in {"add", "choose", "skip", "remember", "memory", "forget", "new_session"}:
                replies = await self.tools.execute(intent, state, memory)
                if intent.action in {"forget", "new_session"}:
                    state.pop("dialogue", None)
                    state.pop("button_actions", None)
                if (intent.action == "add" and intent.checkout_after and self.checkout.cart_writes
                        and not memory.get("pending")):
                    # "Add it and check out": the review follows the receipt in the same reply.
                    replies = replies + await self.checkout.prepare(owner, state, memory, account)
                return self._present_results(replies, intent, state)
            if intent.action == "empty_cart":
                return await self.checkout.empty_cart(state)
            if intent.action == "set_pincode" and intent.pincode:
                self.store.save_memory(owner, account, memory)
                reply = self._location(owner, state, intent.pincode)
                memory = self.store.get_memory(owner, account)
                return reply
            if intent.action == "logout":
                self.store.forget_memory(owner)
                memory.clear()
                self.store.delete(owner)
                return [
                    "Disconnected. This bot’s saved session and search context have been removed."
                ]
            if intent.action == "login":
                return self._conversational_reply(intent, state, connected=True)
            if intent.action != "search" or not intent.queries:
                return self._conversational_reply(intent, state, connected=True)
            if not state.get("location"):
                return ["Send your 6-digit delivery pincode first so I can search the right area."]
            replies: list[str | dict[str, Any]] = []
            shown = []
            for query in intent.queries:
                result = await self.commerce.search(query, state["location"], state["session"])
                products = normalize_products(result.get("items", []))
                if not products:
                    replies.append(
                        f"{query}: No matching listings were returned for this area. That does not establish stock availability."
                    )
                    continue
                selection = await self.agent.rank(query, products)
                card = None if intent.presentation == "text" else self._product_card(query, products, selection, state)
                if card:
                    shown.append(card["product"])
                replies.append(
                    card
                    if card
                    else f"*{query}*\n"
                    + render_search(query, products, selection, state["location"]["pincode"])
                )
            memory.setdefault("active", {})["queries"] = intent.queries
            state["queries"] = intent.queries
            if len(shown) == 1:
                # "Add it" right after a single result means that product.
                self.tools.remember_focus(state, shown[0])
            self.store.save(owner, state)
            if re.search(r"\b(order|buy|purchase|cart)\b", lower):
                replies.insert(
                    0,
                    "Here’s what I found. Tap Add to cart on the one you want, then CHECKOUT to order."
                    if self.checkout.cart_writes
                    else "I can find these products now. This version doesn’t add to cart or place orders.",
                )
            return self._present_results(replies, intent, state)
        except AgentError:
            return [
                "I couldn’t interpret or filter that search just now. Please try again in a moment."
            ]
        except CommerceError as exc:
            return self._handle_commerce_error(owner, state, exc)
        finally:
            if self.store.get(owner).get("session"):
                self.store.save_memory(owner, account, memory)
                self.store.save(owner, state)

    async def _commerce_action(self, intent, owner, state, memory, account):
        action, number = intent.action, intent.order_number
        if action == "cart":
            return await self.checkout.view_cart(state)
        if action in {"cart_remove", "cart_quantity"}:
            return await self.checkout.edit_cart(
                state, remove=action == "cart_remove", number=intent.line_number, name=intent.cart_item,
                quantity=intent.quantity, unavailable=intent.remove_unavailable,
            )
        if action in {"orders", "history"}:
            # Asking for history explicitly lifts FORGET's block on importing purchase evidence.
            memory.pop("history_import_disabled", None)
            return await self.orders.recent(state)
        if action == "track":
            return await self.orders.track(state, number)
        if action == "reorder":
            # Reordered products go straight into the cart, like any other add.
            return await self.orders.reorder(state, memory, number)
        if action == "cancel_order":
            return await self.orders.cancel(state, number)
        if action == "invoice":
            return await self.orders.invoice(state, number)
        if action == "order_status":
            # An unresolved submission from this chat outranks any older order.
            if self.checkout.needs_status(owner, account):
                return await self.checkout.status(owner, state, account)
            return await self.orders.detail(state, number)
        return await self.checkout.prepare(owner, state, memory, account)

    async def _checkout_button(self, owner, sender, action_id):
        async def press(state, memory, button_id):
            return await self.checkout.handle_button(owner, state, memory, account_for(state), button_id,
                                                     recipient=sender)

        def account_for(state):
            return self.store.memory_key(state["session"])

        return await self._account_button(owner, action_id, press)

    async def _account_button(self, owner, action_id, press):
        """Run an account-bound button with the owner's session and memory, then persist both."""
        state = self.store.get(owner)
        if not state.get("session"):
            return ["Connect your JioMart account first. No order has been placed."]
        try:
            account = self.store.memory_key(state["session"])
        except ValueError:
            return ["Please send LOGIN to reconnect your account before ordering."]
        memory = self.store.get_memory(owner, account)
        try:
            return await press(state, memory, action_id)
        except CommerceError as exc:
            return self._handle_commerce_error(owner, state, exc)
        finally:
            if self.store.get(owner).get("session"):
                self.store.save_memory(owner, account, memory)
                self.store.save(owner, state)

    async def _begin(self, owner, state, mobile, now):
        if now - state.get("last_login", 0) < 60:
            return ["Please wait a minute before requesting another OTP."]
        history = [t for t in state.get("login_requests", []) if t > now - 3600]
        if len(history) >= 5:
            return ["The hourly login limit has been reached. Please try again later."]
        history.append(now)
        state.update(last_login=now, login_requests=history)
        # An explicit new link may never fall back to the previous customer's session.
        for key in ("session", "location", "addresses", "queries", "challenge", "button_actions", "dialogue"):
            state.pop(key, None)
        self.store.save(owner, state)
        try:
            challenge = await self.commerce.begin_login(mobile)
            state.update(challenge=challenge, challenge_expires=now + 300, otp_attempts=0)
            self.store.save(owner, state)
            return [
                f"JioMart sent an OTP to the number ending {mobile[-4:]}. Reply with only that code within 5 minutes. It will go directly to the login handler, outside the AI."
            ]
        except CommerceError as exc:
            return [self._commerce_message(exc)]

    def _invalidate_location(self, owner, state):
        # Product questions were priced for the old area; the cart itself lives on JioMart.
        state.pop("focus", None)
        self._drop_pending(owner, state)

    def _drop_pending(self, owner, state):
        if not state.get("session", {}).get("account_reference"):
            return
        account = self.store.memory_key(state["session"])
        memory = self.store.get_memory(owner, account)
        if memory.pop("pending", None) is not None:
            self.store.save_memory(owner, account, memory)

    def _dialogue_scope(self, state):
        if state.get("session", {}).get("account_reference"):
            return self.store.memory_key(state["session"])
        return "guest"

    def _chat_context(self, state, now):
        return {"connected": bool(state.get("session")),
                "recent_turns": recent_turns(state, now, self._dialogue_scope(state))}

    @staticmethod
    def _can_remember(text):
        return not re.search(
            r"^(?:/?button:|login\b|log in\b|connect\b|send otp\b|logout\b|disconnect\b|log out\b|cancel$|stop$|addresses?\b|pin(?:code)?\b|forget\b|new session$)|"
            r"\b(?:otp|password|api[_ -]?key|access[_ -]?token)\b|sk-(?:proj-)?[A-Za-z0-9_-]{12,}|"
            r"^\d+$|(?<!\d)\+?\d[\d ()-]{8,}\d(?!\d)", text.strip(), re.I,
        )

    def _conversational_reply(self, intent, state, *, connected):
        return Written(self._conversation_messages(intent, state, connected=connected))

    def _conversation_messages(self, intent, state, *, connected):
        body = intent.reply.strip()
        if not body:
            body = ("I can find groceries, add them to your JioMart cart and place the order. What would you like?"
                    if connected else "Hi! I can help you shop for groceries on JioMart. Connect your account when you’re ready to find products and fill your cart.")
        actions = list(intent.quick_actions)
        if intent.action == "login":
            actions = ["connect"]
            body = body[:700] + "\nUse Connect JioMart for this WhatsApp number, or type LOGIN followed by your registered mobile number."
        elif not connected and intent.action not in {"help", "unsupported"}:
            # No guest tool is executed, including a model-selected add/checkout.
            body = body[:700] + "\nConnect your JioMart account first to work with products or your cart. No items have been added and no order has been placed."
            actions = ["connect", "help"]
        if intent.presentation == "text" and intent.action != "login":
            return [body]
        if intent.presentation in {"auto", "buttons"} and not actions and intent.action == "help":
            # Welcome menu: the three things a returning shopper most often wants.
            actions = ["reorder", "cart", "track"] if connected else ["connect", "help"]
        return self._quick_reply(body, actions, state)

    def _present_results(self, replies, intent, state):
        # Evidence stays authoritative; the model selects only a supported format.
        if intent.presentation == "buttons" and intent.quick_actions and replies and isinstance(replies[-1], str):
            if len(replies[-1]) <= 1024:
                return replies[:-1] + self._quick_reply(replies[-1], intent.quick_actions, state)
        return replies

    def _with_buttons(self, owner, replies):
        """Every reply ends with two or three next steps the customer can tap."""
        if not replies:
            return replies
        last = replies[-1]
        if isinstance(last, dict) and last.get("type") != "document":
            return replies
        state = self.store.get(owner)
        actions = self._next_steps(owner, state)
        if not actions:
            return replies
        index = len(replies) - 1
        while index >= 0 and isinstance(replies[index], dict) and replies[index].get("type") == "document":
            index -= 1
        target = replies[index] if index >= 0 else None
        if isinstance(target, str) and len(target) <= 1024:
            converted = self._quick_reply(target, actions, state)
            self.store.save(owner, state)
            return replies[:index] + converted + replies[index + 1:]
        prompt = self._quick_reply("What would you like to do next?", actions, state)
        self.store.save(owner, state)
        return replies + prompt if isinstance(prompt[0], dict) else replies

    def _next_steps(self, owner, state) -> list[str]:
        """Context-aware shortcuts for the reply just built."""
        if not state.get("session"):
            return ["resend_otp", "cancel_login"] if state.get("challenge") else ["connect", "help"]
        if not state.get("location"):
            return ["addresses", "help"]
        action = self._last_action.pop(owner, None)
        if action in {"orders", "history", "order_status", "track", "cancel_order", "invoice"}:
            return ["orders", "track", "search"]
        if action == "empty_cart":
            return ["search", "reorder", "orders"]
        if action in {"checkout", "cart"}:
            return ["search", "cart", "orders"]
        if action in {"add", "choose", "skip", "reorder", "cart_remove", "cart_quantity", "keep_shopping"}:
            return ["cart", "checkout", "search"]
        return ["search", "cart", "orders"]

    def _quick_reply(self, body, requested, state):
        buttons = []
        for name in dict.fromkeys(requested):
            name = "orders" if name == "history" else name
            if name not in LABELS or (not state.get("session") and name in ACCOUNT_ONLY):
                continue
            if any(button["title"] == LABELS[name] for button in buttons):
                continue
            buttons.append({"id": self._quick_button(state, name), "title": LABELS[name]})
            if len(buttons) == 3:
                break
        if not buttons or len(body) > 1024:
            return [body]
        fallback = body + "\n" + " · ".join(f"{LABELS[name]}: {COMMANDS[name].upper()}" for name in dict.fromkeys(requested)
                                            if name in COMMANDS and name in LABELS
                                            and (state.get("session") or name not in ACCOUNT_ONLY))
        return [{"type": "quick_reply", "body": body, "buttons": buttons, "fallback": fallback}]

    def _quick_button(self, state, name) -> str:
        return self._add_action(state, "sbqa_", {"kind": name})

    def _quick_action(self, owner, state, action_id, now):
        actions = self._action_state(state)
        action = actions.pop(action_id, None)
        state["button_actions"] = actions
        self.store.save(owner, state)
        if (not isinstance(action, dict) or action.get("expires", 0) < now
                or action.get("binding") != self._binding(state)):
            return None
        kind = action.get("kind")
        mobile = (state.get("challenge") or {}).get("mobile")
        if kind == "resend_otp" and isinstance(mobile, str) and mobile_number(mobile):
            # Resend to the number being linked, which may differ from this WhatsApp number.
            return f"login {mobile_number(mobile)}"
        return COMMANDS.get("orders" if kind == "history" else kind)

    def _location(self, owner, state, pincode):
        # Only search context changes. This does not write a saved JioMart address.
        state["location"] = {"pincode": pincode, "country": "India", "country_iso_code": "IN"}
        self._invalidate_location(owner, state)
        state.pop("button_actions", None)
        self.store.save(owner, state)
        return [
            f"Search area set to pincode {pincode}. "
            + (
                "What would you like to find?"
                if state.get("session")
                else "Send LOGIN to connect your JioMart account."
            )
        ]

    def _product_card(self, query, products, selection, state, index=0):
        """Create one safe, expiring interactive card from ranked catalog evidence."""
        from .catalog import sanitize_selection

        safe = sanitize_selection(query, products, selection)
        ids = safe.exact_ids or safe.alternative_ids
        if not ids or index >= len(ids):
            return None
        by_id = {str(product["id"]): product for product in products}
        candidates = [by_id[item_id] for item_id in ids if item_id in by_id]
        if index >= len(candidates):
            return None
        product = candidates[index]
        image_url = product.get("image_url")
        if not isinstance(image_url, str):
            return None
        pincode = state.get("location", {}).get("pincode")
        if not isinstance(pincode, str) or not state.get("session"):
            return None
        exact = bool(safe.exact_ids)
        price = self._price(product.get("price"))
        body = product["name"]
        if price:
            body += f"\n{price}"
        if not exact:
            body = f"Alternative for {query}:\n{body}"
        else:
            body = f"Match for {query}:\n{body}"
        body += f"\nPincode {pincode}"
        buttons = []
        if self.checkout.cart_writes:
            buttons.append({"id": self._add_action(state, "sbpc_", {"kind": "add", "query": query, "product": product}),
                            "title": "Add to cart"})
        buttons.append({"id": self._add_action(state, "sbpc_", {"kind": "view", "query": query, "product": product}),
                        "title": "View details"})
        if index + 1 < len(candidates):
            next_id = self._add_action(state, "sbpc_", {
                "kind": "next", "query": query, "products": candidates, "index": index + 1, "exact": exact,
            })
            buttons.append({"id": next_id, "title": "Next option"})
        else:
            buttons.append({"id": self._add_action(state, "sbpc_", {"kind": "search_again"}), "title": "Search again"})
        footer = "Listing price · Tap Add to cart to buy" if self.checkout.cart_writes else "Listing price · Not added to cart"
        return {
            "type": "product_card",
            "image_url": image_url,
            "body": body[:1024],
            "footer": footer[:60],
            "buttons": buttons[:3],
            "product": product,
            "fallback": f"*{query}*\n{body}\n{footer}",
        }

    def _button_reply(self, owner, state, action_id, now):
        """Product card taps. Returns replies, or a command string to continue with (Add to cart)."""
        if not state.get("session"):
            return ["Connect your JioMart account before choosing a product."]
        if not re.fullmatch(r"sbpc_[A-Za-z0-9_-]{16,64}", action_id):
            return ["That product choice is no longer available. Please search again."]
        actions = self._action_state(state)
        action = actions.pop(action_id, None)
        state["button_actions"] = actions
        if not isinstance(action, dict) or action.get("expires", 0) < now:
            self.store.save(owner, state)
            return ["That product button is from an older search. Tell me what you’d like and I’ll look again."]
        binding = action.get("binding")
        if binding != self._binding(state):
            self.store.save(owner, state)
            return ["That product choice is no longer valid for this account or delivery area."]
        kind = action.get("kind")
        if kind in {"view", "add"}:
            product = action.get("product")
            if not isinstance(product, dict):
                self.store.save(owner, state)
                return ["That product choice is no longer available. Please search again."]
            self.tools.remember_focus(state, product)
            if kind == "add":
                self.store.save(owner, state)
                return "add to cart"
            reply = self._details(state, action.get("query", "Product"), product)
            self.store.save(owner, state)
            return [reply]
        if kind == "next":
            products = action.get("products")
            query = action.get("query")
            index = action.get("index")
            if not isinstance(products, list) or not isinstance(query, str) or not isinstance(index, int):
                self.store.save(owner, state)
                return ["That product choice is no longer available. Please search again."]
            # Candidates were accepted by the deterministic relevance gate before persistence.
            selection = Selection(
                exact_ids=[str(item["id"]) for item in products] if action.get("exact") else [],
                alternative_ids=[]
                if action.get("exact")
                else [str(item["id"]) for item in products],
            )
            card = self._product_card(query, products, selection, state, index)
            if card:
                self.tools.remember_focus(state, card["product"])
            self.store.save(owner, state)
            return [card] if card else ["There are no more matching options. Please search again."]
        self.store.save(owner, state)
        self._last_action[owner] = "keep_shopping"
        return ["Tell me what product you’d like to search for next."]

    @staticmethod
    def _price(value):
        return f"₹{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _details(self, state, query, product):
        lines = [f"*{product.get('name', query)}*"]
        if product.get("brand"):
            lines.append(f"Brand: {product['brand']}")
        if product.get("pack"):
            lines.append(f"Pack: {product['pack']}")
        price = Conversation._price(product.get("price"))
        if price:
            lines.append(f"Current listing price: {price}")
        link = product_link(product)
        if link:
            lines.append(f"See it on JioMart: {link}")
        lines.append("Listing information only; it isn’t in your cart yet.")
        body = "\n".join(lines)
        buttons = []
        if self.checkout.cart_writes:
            buttons.append({"id": self._add_action(state, "sbpc_", {"kind": "add", "query": query, "product": product}),
                            "title": "Add to cart"})
        buttons.append({"id": self._quick_button(state, "search"), "title": LABELS["search"]})
        buttons.append({"id": self._quick_button(state, "cart"), "title": LABELS["cart"]})
        return {"type": "quick_reply", "body": body[:1024], "buttons": buttons, "fallback": body}

    @staticmethod
    def _binding(state):
        session = state.get("session", {})
        location = state.get("location", {})
        reference = session.get("account_reference") or session.get("mobile")
        pincode = location.get("pincode")
        return f"{reference or ''}:{pincode or ''}"

    def _action_state(self, state):
        now = time.time()
        actions = state.get("button_actions")
        if not isinstance(actions, dict):
            actions = {}
        actions = {
            key: value
            for key, value in actions.items()
            if isinstance(key, str)
            and isinstance(value, dict)
            and value.get("expires", 0) >= now
        }
        if len(actions) > MAX_BUTTON_ACTIONS:
            oldest = sorted(actions, key=lambda key: actions[key].get("expires", 0))[: len(actions) - MAX_BUTTON_ACTIONS]
            for key in oldest:
                actions.pop(key, None)
        return actions

    def _add_action(self, state, prefix, action) -> str:
        """Store an opaque, owner/account/area-bound button target and return its ID."""
        actions = self._action_state(state)
        action_id = prefix + secrets.token_urlsafe(18)
        actions[action_id] = {**action, "expires": time.time() + BUTTON_TTL, "binding": self._binding(state)}
        state["button_actions"] = actions
        return action_id

    def _handle_commerce_error(self, owner, state, exc):
        if exc.code == "auth_expired":
            state.pop("session", None)
            state.pop("button_actions", None)
        self.store.save(owner, state)
        return [self._commerce_message(exc)]

    @staticmethod
    def _commerce_message(exc):
        return {
            "auth_expired": "Your JioMart session has expired. Send LOGIN to reconnect.",
            "invalid_otp": "JioMart couldn’t verify that code. Please check it and try again, or wait a minute and send LOGIN for a new one.",
            "rate_limited": "JioMart is limiting requests. Please wait a little and try again.",
            "invalid_location": "JioMart couldn’t search this pincode. Try a saved address with ADDRESSES, or another pincode.",
        }.get(
            exc.code,
            "JioMart couldn’t complete that request right now. Please try again in a moment.",
        )
