"""The real JioMart cart (the only basket), its review and customer-confirmed ordering.

Chosen products go straight into the account's own JioMart cart; there is no separate
shopping list. CHECKOUT reads JioMart's prices back and shows that authoritative basket.
Only a single-use, unexpired button tied to the exact reviewed basket can submit it, and
every submission is recorded before the checkout call so a retry, duplicate tap or crash
can never place a second order.
"""

import asyncio
import hashlib
import json
import logging
import secrets
import time
from typing import Any

from .catalog import normalize_products
from .jiomart import CommerceError
from .receipt import summary_message

log = logging.getLogger("smartbazaar")
# The reviewed-cart snapshot guards every tap, so buttons can outlive a short coffee break.
CONFIRM_TTL = 1800
MAX_REFRESH = 10
PAY_LINK_TTL = 2700
MAX_ADD_BATCH = 20
MAX_REMEMBERED = 40
PENDING_STATES = {"submitting", "unknown"}


def _money(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return f"₹{value:,.2f}".replace(".00", "")


def _line_key(identifiers: dict, size: str | None) -> tuple[str, str] | None:
    uid = identifiers.get("product_uid") or identifiers.get("item_id")
    return (str(uid), str(size)) if uid and size else None


class CheckoutFlow:
    def __init__(self, store, commerce, *, cart_writes=False, checkout=False, max_total=0, public_origin=None,
                 online_payment=False):
        self.store, self.commerce = store, commerce
        self.cart_writes = cart_writes
        self.checkout_enabled = cart_writes and checkout
        # 0 means no ceiling: the customer's tap on the exact reviewed total is the confirmation.
        self.max_total = max_total
        # Returns the bot's current public HTTPS origin (for payment links), or None.
        self.public_origin = public_origin or (lambda: None)
        self.online_payment = online_payment

    # ----- reading -----

    async def view_cart(self, state: dict) -> list[str]:
        if not state.get("location"):
            return ["Choose a delivery address first with ADDRESSES."]
        cart = await self.commerce.get_cart(state["session"], state["location"])
        if cart["items"] and self.cart_writes:
            self._remember_view(state, cart["items"])
            buttons = [self._button(state, "go_checkout", "Checkout", cart),
                       self._button(state, "keep_shopping", "Keep shopping", cart),
                       self._button(state, "empty_cart", "Empty cart", cart)]
            card = self._cart_card(cart, "🛒 These are your cart items:", buttons,
                                   ["To change anything, say ‘remove 2 from cart’ or ‘make 1 two in cart’."],
                                   fallback=self._cart_text(state, cart))
            if card:
                return [card]
        return [self._cart_text(state, cart)]

    def _cart_card(self, cart: dict, intro: str, buttons: list[dict], tail: list[str], *, fallback: str) -> dict | None:
        """The cart as one picture of product thumbnails, item bullets, totals and buttons."""
        items = cart["items"]
        if not any(item.get("image_url") for item in items):
            return None
        bullets = []
        for item in items:
            unavailable = item.get("available") is False
            price = f" — {_money(item['line_total'])}" if item.get("line_total") is not None and not unavailable else ""
            bullets.append(f"• {item['quantity']} × {item.get('name') or 'Item'}{price}" + (" ⚠️ unavailable" if unavailable else ""))
        totals = self._breakup_lines(cart)
        shown = len(bullets)
        while True:
            visible = bullets[:shown] + ([f"…and {len(bullets) - shown} more (send CART for the full list)"] if shown < len(bullets) else [])
            body = "\n".join([intro, *visible, *totals, *tail])
            if len(body) <= 1024 or shown <= 1:
                break
            shown -= 1
        return {
            "type": "cart_card",
            "items": [{"image_url": item.get("image_url"), "quantity": item["quantity"]} for item in items[:9]],
            "body": body[:1024], "buttons": buttons[:3],
            "fallback": fallback + ("\n" + " · ".join(b["title"] for b in buttons) if buttons else ""),
        }

    def _cart_text(self, state: dict, cart: dict, heading: str = "🛒 Your JioMart cart") -> str:
        if not cart["items"]:
            state.pop("cart_view", None)
            return "Your JioMart cart is empty. Tell me what you’d like to buy and I’ll add it."
        self._remember_view(state, cart["items"])
        lines = [heading]
        lines.extend(self._render_item(index, item) for index, item in enumerate(cart["items"], 1))
        lines.extend(self._breakup_lines(cart) or ([f"Total from JioMart: {_money(self._total(cart))}"] if self._total(cart) is not None else []))
        if self.cart_writes:
            hint = "Say ‘remove 2 from cart’, ‘make 1 two in cart’"
            if any(item.get("available") is False for item in cart["items"]):
                hint += " or ‘remove unavailable items’"
            lines.append(hint + ". Send CHECKOUT to review and place the order.")
        else:
            lines.append("Send CHECKOUT to review the order.")
        return "\n".join(lines)

    async def edit_cart(self, state: dict, *, remove: bool, number: int | None = None, name: str | None = None,
                        quantity: int | None = None, unavailable: bool = False) -> list[str]:
        """Remove or re-quantify lines of the real JioMart cart, then show it again."""
        if not self.cart_writes:
            return ["Changing your JioMart cart from chat isn’t switched on yet. You can do it in the JioMart app."]
        if not state.get("location"):
            return ["Choose a delivery address first with ADDRESSES."]
        if not remove and (quantity is None or not 0 <= quantity <= 99):
            return ["How many would you like? Say, for example, ‘make 1 two in cart’."]
        cart = await self.commerce.get_cart(state["session"], state["location"])
        if not cart["items"]:
            return ["Your JioMart cart is empty."]
        targets = self._cart_targets(state, cart["items"], number=number, name=name, unavailable=unavailable)
        if isinstance(targets, str):
            return [targets]
        if not remove and len(targets) != 1:
            return ["Tell me which single cart line to change, for example ‘make 2 three in cart’."]
        operation = "remove_item" if remove or quantity == 0 else "update_item"
        payload = [{**item["identifiers"], "item_size": item.get("item_size"), "item_index": item.get("item_index"),
                    "quantity": item["quantity"] if operation == "remove_item" else quantity}
                   for item in targets]
        try:
            await self.commerce.update_cart(state["session"], state["location"], operation, payload, cart["cart_id"])
        except CommerceError as exc:
            if exc.code == "auth_expired":
                raise
            return ["JioMart didn’t accept that cart change. Send CART to see the current cart."]
        except ValueError:
            return ["I couldn’t identify that cart line safely. Send CART and try again with its number."]
        names = ", ".join(item.get("name") or "item" for item in targets)
        done = f"Removed {names} from your JioMart cart." if operation == "remove_item" else f"Set {names} to {quantity} in your JioMart cart."
        fresh = await self.commerce.get_cart(state["session"], state["location"])
        return [done + "\n\n" + self._cart_text(state, fresh)]

    def _cart_targets(self, state, items, *, number, name, unavailable):
        if unavailable:
            found = [item for item in items if item.get("available") is False]
            return found or "Nothing in your JioMart cart is marked unavailable right now."
        if number is not None:
            view = state.get("cart_view") if isinstance(state.get("cart_view"), dict) else {}
            keys = view.get("keys", []) if view.get("expires", 0) >= time.time() and view.get("binding") == self._binding(state) else []
            if keys:
                if not 1 <= number <= len(keys):
                    return f"Your cart view has {len(keys)} line(s). Send CART to see the numbers."
                found = [item for item in items if str(_line_key(item["identifiers"], item.get("item_size"))) == keys[number - 1]]
                return found[:1] or "That cart line changed. Send CART to see the current cart."
            if not 1 <= number <= len(items):
                return f"Your JioMart cart has {len(items)} line(s). Send CART to see the numbers."
            return [items[number - 1]]
        words = [word for word in (name or "").casefold().split() if len(word) > 1]
        if not words:
            return "Which item? Send CART and tell me its number."
        found = [item for item in items if all(word in (item.get("name") or "").casefold() for word in words)]
        if not found:
            return f"I couldn’t find “{name}” in your JioMart cart. Send CART to see what’s in it."
        if len(found) > 1:
            self._remember_view(state, found)
            return "Which one?\n" + "\n".join(self._render_item(i, item) for i, item in enumerate(found, 1)) + "\nReply with ‘remove 1 from cart’ (or another number)."
        return found

    def _remember_view(self, state: dict, items: list[dict]) -> None:
        state["cart_view"] = {
            "keys": [str(_line_key(item["identifiers"], item.get("item_size"))) for item in items],
            # Names and quantities let the planner resolve "one more milk" against the real cart.
            "lines": [{"line": i, "name": (item.get("name") or "Item")[:120], "quantity": item["quantity"]}
                      for i, item in enumerate(items[:30], 1)],
            "expires": time.time() + 6 * 3600, "binding": self._binding(state),
        }

    def view_context(self, state: dict) -> list[dict]:
        view = state.get("cart_view") if isinstance(state.get("cart_view"), dict) else {}
        if view.get("expires", 0) < time.time() or view.get("binding") != self._binding(state):
            return []
        return [line for line in view.get("lines", []) if isinstance(line, dict)]

    async def status(self, owner: str, state: dict, account: str) -> list[str]:
        latest = self.store.latest_attempt(owner, account)
        if not latest:
            return ["You haven’t placed an order through this chat yet."]
        attempt_id, attempt_state, data = latest
        order_id = data.get("order_id")
        if attempt_state == "awaiting_payment" and data.get("pay_token"):
            link = self.store.payment_link(data["pay_token"])
            origin = self.public_origin()
            if link and origin:
                # A JioMart payment page opens once; if it was already opened, a fresh CHECKOUT is the retry.
                return [f"Order {order_id} is still waiting for payment. If you already opened its payment page "
                        "and closed it, send CHECKOUT and choose Pay online again (that starts a fresh order).",
                        self._pay_message(order_id, data.get("total"), f"{origin}/pay/{data['pay_token']}")]
            return [f"Order {order_id} is still waiting for payment and its payment link has expired. "
                    "JioMart will cancel it if it stays unpaid. Send CHECKOUT to order again."]
        if order_id:
            try:
                order = await self.commerce.get_order(state["session"], order_id)
            except CommerceError as exc:
                if exc.code == "auth_expired":
                    raise
                return [f"Order {order_id} was placed. I couldn’t read its latest status right now; the JioMart app shows it under My Orders."]
            return [f"Order {order_id}: {order.get('status') or 'placed'}." + (
                f"\n{order['detail']}" if order.get("detail") else ""
            )]
        if attempt_state in PENDING_STATES:
            found = await self._reconcile(state, attempt_id, attempt_state, data)
            if found:
                return [f"Good news: JioMart did create your order {found}. Send ORDER STATUS any time for updates."]
            return [
                "I couldn’t confirm whether your last order went through. Please check My Orders in the "
                "JioMart app before trying again. I won’t submit another order until this is cleared — "
                "send CLEAR ORDER CHECK once you’ve confirmed there is no new order."
            ]
        if attempt_state == "failed":
            return ["Your last order attempt was not placed. Send CHECKOUT to review your cart again."]
        return ["Your last order was placed. Check My Orders in the JioMart app for its status."]

    def has_pending(self, owner: str, account: str) -> bool:
        latest = self.store.latest_attempt(owner, account)
        return bool(latest and latest[1] in PENDING_STATES)

    def needs_status(self, owner: str, account: str) -> bool:
        """An unresolved or unpaid order from this chat should answer ORDER STATUS first."""
        latest = self.store.latest_attempt(owner, account)
        return bool(latest and latest[1] in PENDING_STATES | {"awaiting_payment"})

    async def clear_pending(self, owner: str, state: dict, account: str) -> list[str]:
        latest = self.store.latest_attempt(owner, account)
        if not latest or latest[1] not in PENDING_STATES:
            return ["There’s no unresolved order check."]
        found = await self._reconcile(state, *latest)
        if found:
            return [f"I found that order: JioMart created {found}, so I won’t clear it. Send ORDER STATUS for updates."]
        self.store.update_attempt(latest[0], "dismissed", latest[2])
        return ["Cleared. Send CHECKOUT when you want to review your cart again."]

    async def _reconcile(self, state: dict, attempt_id: str, attempt_state: str, data: dict) -> str | None:
        """Look for an order JioMart created for an unconfirmed submission (same total, after it began)."""
        try:
            orders = await self.commerce.list_orders(state["session"])
        except CommerceError as exc:
            if exc.code == "auth_expired":
                raise
            return None
        except AttributeError:
            return None
        started = data.get("at", 0) - 120
        matches = [o for o in orders if (o.get("created_at") or 0) >= started
                   and isinstance(o.get("total"), (int, float)) and abs(o["total"] - (data.get("total") or -1)) < 0.5]
        if len(matches) != 1:
            return None
        data["order_id"] = matches[0]["order_id"]
        self.store.update_attempt(attempt_id, "awaiting_payment" if data.get("method") == "online" else "placed", data)
        return data["order_id"]

    # ----- adding to the cart and review -----

    async def prepare(self, owner: str, state: dict, memory: dict, account: str) -> list:
        """Review the real cart exactly as it is; nothing is added here."""
        if not self.cart_writes:
            return ["Ordering from chat isn’t switched on yet, so you can finish in the JioMart app. No order has been placed."]
        location = state.get("location")
        if not location:
            return ["Choose a delivery address first with ADDRESSES."]
        latest = self.store.latest_attempt(owner, account)
        if latest and latest[1] in PENDING_STATES:
            return await self.status(owner, state, account)
        cart = await self.commerce.get_cart(state["session"], location)
        if not cart["items"]:
            return ["Your JioMart cart is empty. Tell me what you’d like to buy and I’ll add it."]
        state["address_selected"] = False
        if location.get("id") and cart["cart_id"]:
            try:
                # Pricing delivery for the saved address makes the review total final.
                await self.commerce.select_address(state["session"], location, cart["cart_id"], location["id"])
                state["address_selected"] = True
            except CommerceError as exc:
                if exc.code == "auth_expired":
                    raise
        return await self._review(state, memory)

    async def add_products(self, state: dict, memory: dict, lines: list[dict]) -> dict:
        """Put chosen products straight into the account's real JioMart cart.

        ``lines`` are ``{"product": …, "quantity": n}``. A product already in the cart is left
        as it is (the customer changes it with a cart edit), and the same product asked for
        twice is one line at the larger quantity. Returns the added and already-present names,
        notes for the customer, and an ``error`` message when JioMart refused the add.
        """
        result: dict[str, Any] = {"added": [], "already": [], "notes": [], "error": None}
        if not self.cart_writes:
            result["error"] = "Adding to your JioMart cart from chat isn’t switched on yet. You can add it in the JioMart app."
            return result
        if not lines:
            return result
        await self._refresh_cart_refs(state, lines)
        wanted, notes = self._wanted_lines(lines)
        result["notes"] = notes
        if not wanted:
            return result
        session, location = state["session"], state["location"]
        cart = await self.commerce.get_cart(session, location)
        existing = {key for item in cart["items"] if (key := _line_key(item["identifiers"], item.get("item_size")))}
        adds = [{**line["ids"], "quantity": line["quantity"]} for key, line in wanted.items() if key not in existing]
        try:
            for start in range(0, len(adds), MAX_ADD_BATCH):
                await self.commerce.add_cart_items(session, location, adds[start:start + MAX_ADD_BATCH], cart["cart_id"])
        except CommerceError as exc:
            if exc.code == "auth_expired":
                raise
            result["error"] = "JioMart didn’t accept that item, so I stopped. Send CART to see what’s in it now."
            return result
        except ValueError:
            result["error"] = "That item couldn’t be matched safely, so I stopped. Send CART to see what’s in it now."
            return result
        # Remember what each cart line is, so "remember line 2 for curd" can name a real product.
        known = memory.setdefault("cart_products", {})
        for key, line in wanted.items():
            known[str(key)] = line["product"]
        while len(known) > MAX_REMEMBERED:
            known.pop(next(iter(known)))
        result["added"] = [line["quantity_name"] for key, line in wanted.items() if key not in existing]
        result["already"] = [line["name"] for key, line in wanted.items() if key in existing]
        state["just_added"] = [line["name"] for key, line in wanted.items() if key not in existing]
        return result

    async def cart_item(self, state: dict, number: int) -> dict | str:
        """The product on line ``number`` of the last cart view (for ‘remember line 2’)."""
        cart = await self.commerce.get_cart(state["session"], state["location"])
        found = self._cart_targets(state, cart["items"], number=number, name=None, unavailable=False)
        return found[0] if isinstance(found, list) else found

    async def _review(self, state, memory) -> list:
        cart = await self.commerce.get_cart(state["session"], state["location"])
        added = set(state.pop("just_added", []) or [])
        lines = ["🛒 Your JioMart cart"]
        self._remember_view(state, cart["items"])
        for index, item in enumerate(cart["items"], 1):
            fresh = " (just added)" if item.get("name") in added else ""
            lines.append(self._render_item(index, item) + fresh)
        lines.extend(self._breakup_lines(cart))
        total = self._total(cart)
        address = state.get("location", {})
        lines.append(f"Deliver to: {address.get('label') or 'Saved address'}, {address.get('city') or ''} {address.get('pincode', '')}".replace(" ,", ","))
        lines.append("To change anything: ‘remove 2 from cart’, ‘make 1 two in cart’ or EMPTY CART.")

        blockers = []
        unavailable = [item for item in cart["items"] if item.get("available") is False]
        if unavailable:
            blockers.append(f"{len(unavailable)} item(s) can’t be delivered right now. Send ‘remove unavailable items’ first.")
        if not cart["is_valid"] and not unavailable:
            blockers.append("JioMart reports this cart isn’t ready for checkout yet.")
        if total is None:
            blockers.append("JioMart didn’t return a total, so I can’t place this order.")
        elif self.max_total and total > self.max_total:
            blockers.append(f"The total {_money(total)} is above the {_money(self.max_total)} limit for orders from this chat. "
                            "Remove something (say ‘remove 1 from cart’), then CHECKOUT again.")
        if not address.get("id"):
            blockers.append("Choose a saved delivery address with ADDRESSES before ordering.")
        elif state.pop("address_selected", True) is False:
            blockers.append("JioMart didn’t accept the delivery address step, so I can’t place the order yet. Try CHECKOUT again in a moment.")
        if cart.get("coverage") != "complete":
            blockers.append("Some cart lines couldn’t be read, so I can’t safely place this order.")
        methods: list[str] = []
        if self.checkout_enabled and not blockers:
            methods, payment_blockers = await self._payment_choices(state, cart, total)
            blockers.extend(payment_blockers)

        # Categories and amounts only: why a review could not offer payment.
        log.info("checkout_review lines=%s unavailable=%s valid=%s coverage=%s total=%s address_id=%s blockers=%s",
                 len(cart["items"]), len(unavailable), cart.get("is_valid"), cart.get("coverage"), total,
                 bool(address.get("id")), len(blockers))
        messages: list = ["\n".join(lines)]
        state.pop("checkout", None)
        deliver = lines[-2]
        if not self.checkout_enabled:
            closing = ["Placing the order from WhatsApp isn’t switched on yet, so you can finish in the JioMart app."]
            buttons: list[dict] = []
        elif blockers:
            closing = blockers
            buttons = ([self._button(state, "remove_unavailable", "Remove unavailable", cart)] if unavailable else [])
            buttons += [self._button(state, "keep_shopping", "Keep shopping", cart),
                        self._button(state, "empty_cart", "Empty cart", cart)]
        else:
            titles = {"online": f"Pay online {_money(total)}"[:20], "cod": "Cash on delivery"}
            buttons = [self._button(state, "place_order", titles[m], cart, method=m) for m in methods]
            options = {
                "online": "• Pay online: JioPay opens in your browser (UPI, card or netbanking).",
                "cod": "• Cash on delivery: pay when it arrives.",
            }
            closing = ["How would you like to pay?", *(options[m] for m in methods),
                       "Tapping a payment option places the order. These buttons expire in 30 minutes."]
        if self.checkout_enabled and blockers:
            # Why checkout stopped comes first, so it never reads like the plain cart view again.
            reason = "⚠️ Can’t place this order yet:\n" + "\n".join(blockers)
            card = self._cart_card(cart, reason + "\n\nIn your cart:", buttons, [deliver],
                                   fallback=reason + "\n\n" + "\n".join(lines))
            if card:
                return [card]
            body = reason + "\n\n" + "\n".join(lines)
            return [self._prompt(body, buttons)] if len(body) <= 1024 else messages + [self._prompt(reason, buttons)]
        intro = "🧾 Ready to order. Please check and choose how to pay:" if buttons else "🛒 These are your cart items:"
        card = self._cart_card(cart, intro, buttons, [deliver, *closing], fallback="\n".join(lines + closing))
        if card:
            return [card]
        if not buttons:
            return messages + ["\n".join(closing)]
        messages.append(self._prompt(
            f"Ready to order these {len(cart['items'])} item(s) for {_money(total)}. " + "\n".join(closing), buttons[:3]))
        return messages

    async def empty_cart(self, state: dict) -> list[str]:
        if not self.cart_writes:
            return ["Changing your JioMart cart from chat isn’t switched on yet. You can do it in the JioMart app."]
        if not state.get("location"):
            return ["Choose a delivery address first with ADDRESSES."]
        cart = await self.commerce.get_cart(state["session"], state["location"])
        if not cart["items"]:
            return ["Your JioMart cart is already empty."]
        removals = [{**item["identifiers"], "item_size": item.get("item_size"), "item_index": item.get("item_index"),
                     "quantity": item["quantity"]} for item in cart["items"]]
        try:
            await self.commerce.update_cart(state["session"], state["location"], "remove_item", removals, cart["cart_id"])
        except CommerceError as exc:
            if exc.code == "auth_expired":
                raise
            return ["JioMart didn’t let me empty the cart. You can clear it in the JioMart app."]
        except ValueError:
            return ["I couldn’t identify those cart lines safely. You can clear the cart in the JioMart app."]
        state.pop("checkout", None)
        state.pop("cart_view", None)
        return [f"Emptied your JioMart cart ({len(removals)} item(s) removed). Tell me what you’d like to buy."]

    @staticmethod
    def _line_ref_key(line: dict) -> tuple[str, str] | None:
        ref = (line.get("product") or {}).get("cart")
        return (str(ref.get("item_id")), str(ref.get("item_size"))) if isinstance(ref, dict) else None

    async def _payment_choices(self, state, cart, total) -> tuple[list[str], list[str]]:
        try:
            modes = await self.commerce.payment_modes(state["session"], state["location"], cart["cart_id"], total)
        except CommerceError as exc:
            if exc.code == "auth_expired":
                raise
            return [], ["I couldn’t check payment options with JioMart. Your items are in your JioMart cart; try CHECKOUT again shortly."]
        methods = []
        if self.online_payment and self.commerce.online_payment(modes) and self.public_origin():
            methods.append("online")
        if self.commerce.cash_on_delivery(modes):
            methods.append("cod")
        if not methods:
            return [], ["JioMart isn’t offering a payment method I can use from chat for this order. Your items are in your JioMart cart, so you can pay in the JioMart app."]
        return methods, []

    # ----- confirmed submission -----

    async def handle_button(self, owner: str, state: dict, memory: dict, account: str, action_id: str,
                            recipient: str | None = None) -> list:
        pending = state.get("checkout", {})
        action = pending.get("actions", {}).pop(action_id, None) if isinstance(pending, dict) else None
        if not isinstance(action, dict) or action.get("expires", 0) < time.time() or action.get("binding") != self._binding(state):
            state.pop("checkout", None)
            return ["That checkout button has expired. Send CHECKOUT to review your cart again."]
        if action["kind"] in {"go_checkout", "empty_cart", "keep_shopping", "remove_unavailable"}:
            # Navigation only; the checkout review it opens has its own fresh confirmation.
            state.pop("checkout", None)
            if action["kind"] == "keep_shopping":
                return ["What else would you like to add? Type it, or send a voice note or a photo of your list."]
            if action["kind"] == "empty_cart":
                return await self.empty_cart(state)
            if action["kind"] == "remove_unavailable":
                return await self.edit_cart(state, remove=True, unavailable=True)
            return await self.prepare(owner, state, memory, account)
        cart = await self.commerce.get_cart(state["session"], state["location"])
        if self._snapshot(cart, state) != action["snapshot"]:
            state.pop("checkout", None)
            return ["Your JioMart cart changed since that review (items, prices or availability). Send CHECKOUT to review it again."]
        if action["kind"] == "remove_others":
            state.pop("checkout", None)
            return ["That button is no longer used. Send CART to see your cart and ‘remove 2 from cart’ to remove a line."]
        if action["kind"] != "place_order" or not self.checkout_enabled:
            return ["That action isn’t available. Send CHECKOUT to review your cart again."]
        state.pop("checkout", None)
        return await self._place(owner, state, memory, account, cart, action_id,
                                 method=action.get("method") or "cod", recipient=recipient)

    async def _place(self, owner, state, memory, account, cart, confirmation, *, method="cod", recipient=None) -> list:
        latest = self.store.latest_attempt(owner, account)
        if latest and latest[1] in PENDING_STATES:
            return await self.status(owner, state, account)
        origin = self.public_origin() if method == "online" else None
        if method == "online" and not origin:
            return ["Online payment links aren’t available right now, so nothing was ordered. Send CHECKOUT to choose again."]
        total = self._total(cart)
        record = {
            "cart_id": cart["cart_id"], "total": total, "at": time.time(), "method": method,
            "recipient": recipient, "notified": None,
            "lines": [{"name": item.get("name"), "quantity": item["quantity"], "line_total": item.get("line_total")}
                      for item in cart["items"]],
            "breakup": self._breakup_lines(cart),
            "ordered_keys": [list(key) for item in cart["items"] if (key := _line_key(item["identifiers"], item.get("item_size")))],
        }
        attempt_id = self.store.begin_attempt(confirmation, owner, account, record)
        if attempt_id is None:
            return ["That order confirmation was already used. Send ORDER STATUS to check it."]
        # The submission finishes and records its outcome even if this chat turn is cancelled.
        task = asyncio.ensure_future(self._submit(state, memory, cart, attempt_id, record, origin))
        return await asyncio.shield(task)

    async def _submit(self, state, memory, cart, attempt_id, record, origin) -> list:
        method, total = record["method"], record["total"]
        pay_token = secrets.token_urlsafe(24) if method == "online" else None
        try:
            result = await self.commerce.checkout(
                state["session"], state["location"],
                cart_id=cart["cart_id"], address_id=state["location"]["id"], amount=total,
                method=method, callback_url=f"{origin}/paid/{pay_token}" if pay_token else None,
            )
        except CommerceError as exc:
            if exc.code in {"auth_expired", "rejected", "changed"} or (
                exc.code != "unknown" and exc.http_status in {400, 404, 409, 422}
            ):
                # JioMart answered with a definite rejection; nothing was created.
                self.store.update_attempt(attempt_id, "failed", {**record, "reason": exc.diagnostic_code})
                if exc.code == "auth_expired":
                    raise
                return ["JioMart didn’t accept this order, so nothing was placed. Send CART to check your cart, then CHECKOUT to try again."]
            self.store.update_attempt(attempt_id, "unknown", record)
            return [
                "I couldn’t confirm whether JioMart placed this order. Please don’t retry yet: check My Orders "
                "in the JioMart app, or send ORDER STATUS. I won’t submit another order until this is resolved."
            ]
        except BaseException:
            # Unexpected failure or cancellation mid-submit: the order may exist, so block retries.
            self.store.update_attempt(attempt_id, "unknown", record)
            raise
        order_id = result.get("order_id")
        if not order_id:
            self.store.update_attempt(attempt_id, "unknown", record)
            return ["JioMart responded without an order number, so I can’t confirm this order. Check My Orders in the JioMart app before trying again."]
        record["order_id"] = order_id
        # Earlier unpaid online orders from this chat are abandoned; stop following them.
        self.store.supersede_unpaid(*self._attempt_owner(attempt_id), keep=attempt_id)
        warning = await self._total_warning(state, order_id, total)
        if method == "online":
            payment = result.get("payment")
            if payment:
                self.store.save_payment_link(pay_token, attempt_id, {**payment, "order_id": order_id, "total": total},
                                             time.time() + PAY_LINK_TTL)
                record["pay_token"] = pay_token
            # The list is cleared once JioMart shows the order paid (see the order watcher).
            self.store.update_attempt(attempt_id, "awaiting_payment", record)
            if not payment:
                return [f"Order {order_id} is created, but JioMart didn’t return a payment page. Pay for it from My Orders "
                        "in the JioMart app, otherwise JioMart will cancel it. I’ll keep an eye on it here."]
            return ([warning] if warning else []) + [self._pay_message(order_id, total, f"{origin}/pay/{pay_token}")]
        clear_ordered_lines(memory, record)
        self.store.update_attempt(attempt_id, "placed", record)
        summary = summary_message(order_id, record)
        return [f"✅ Order placed! JioMart order {order_id} for {_money(total)}, cash on delivery. "
                "I’ll message you when it’s out for delivery and when it’s delivered. Ask “where is my order?” any time."
                + (f"\n\n{warning}" if warning else "")] + ([summary] if summary else [])

    def _attempt_owner(self, attempt_id: str) -> tuple[str, str]:
        row = self.store.get_attempt(attempt_id)
        return (row[0], row[1]) if row else ("", "")

    async def _total_warning(self, state, order_id, confirmed) -> str | None:
        """Compare the created order's total with the confirmed one (e.g. a fee added at submission)."""
        try:
            order = await self.commerce.get_order_detail(state["session"], order_id)
        except (CommerceError, AttributeError):
            return None
        actual = order.get("total")
        if not isinstance(actual, (int, float)) or not isinstance(confirmed, (int, float)) or abs(actual - confirmed) < 0.5:
            return None
        log.warning("order_total_mismatch delta=%.2f", actual - confirmed)
        return (f"Heads up: JioMart shows this order’s total as {_money(actual)}, not the {_money(confirmed)} you confirmed. "
                "If that’s not OK, cancel it in the JioMart app.")

    @staticmethod
    def _pay_message(order_id: str, total, url: str) -> dict:
        body = (f"Order {order_id} is created. Pay {_money(total)} on JioPay to confirm it.\n"
                "The page opens in your browser and works for 45 minutes. After paying, come back here: "
                "I’ll confirm as soon as JioMart shows it paid.")
        return {"type": "cta_url", "body": body, "button": f"Pay {_money(total)}"[:20], "url": url,
                "fallback": body + "\n" + url}

    # ----- helpers -----

    async def _refresh_cart_refs(self, state: dict, lines: list[dict]) -> None:
        """Re-find remembered products by exact catalog ID to obtain cart identifiers."""
        stale = [line for line in lines
                 if line.get("product") and not isinstance(line["product"].get("cart"), dict)][:MAX_REFRESH]
        for line in stale:
            product = line["product"]
            try:
                result = await self.commerce.search(product["name"], state["location"], state["session"])
            except CommerceError as exc:
                if exc.code == "auth_expired":
                    raise
                continue
            match = next((p for p in normalize_products(result.get("items", []))
                          if p["id"] == product.get("id") and p.get("cart")), None)
            if match:
                # Same catalog ID only; never swap in a different product here.
                line["product"] = {**product, **match}

    def _wanted_lines(self, lines: list[dict]) -> tuple[dict, list[str]]:
        """Chosen products keyed by cart identity; duplicates never add up."""
        wanted: dict[tuple[str, str], dict] = {}
        notes: list[str] = []
        stale = []
        for line in lines:
            product = line.get("product") or {}
            ref = product.get("cart")
            if not product:
                continue
            if not isinstance(ref, dict):
                stale.append(product.get("name", "item"))
                continue
            key = (str(ref["item_id"]), str(ref["item_size"]))
            # The same product asked for twice is one line at the larger quantity, not the sum.
            quantity = max(wanted.get(key, {}).get("quantity", 0), line.get("quantity") or 1)
            limits = ref.get("limits", {})
            cap = min([v for v in (limits.get("maximum"), limits.get("max_in_order")) if v] or [99])
            if quantity > cap:
                notes.append(f"JioMart allows at most {cap} of {product['name']} per order, so I used {cap}.")
                quantity = cap
            minimum = limits.get("minimum") or 1
            if quantity < minimum:
                notes.append(f"{product['name']} has a minimum of {minimum}, so I used {minimum}.")
                quantity = minimum
            wanted[key] = {
                "quantity": quantity, "name": product.get("name", "item"),
                "quantity_name": f"{quantity} × {product.get('name', 'item')}",
                "product": {k: product.get(k) for k in ("id", "name", "brand", "pack", "category", "url", "image_url")},
                "ids": {k: ref.get(k) for k in ("item_id", "item_size", "seller_id", "vertical_code")},
            }
        if stale:
            notes.append("Couldn’t find these on JioMart right now: " + ", ".join(stale[:4]) + ".")
        return wanted, notes

    def _button(self, state, kind, title, cart, method=None) -> dict:
        pending = state.setdefault("checkout", {"actions": {}})
        action_id = "sbco_" + secrets.token_urlsafe(18)
        pending["actions"][action_id] = {
            "kind": kind, "snapshot": self._snapshot(cart, state), "binding": self._binding(state),
            "expires": time.time() + CONFIRM_TTL, "method": method,
        }
        return {"id": action_id, "title": title}

    @staticmethod
    def _prompt(body: str, buttons: list[dict]) -> dict:
        fallback = body + "\nButtons didn’t load? Send CHECKOUT again."
        return {"type": "quick_reply", "body": body[:1024], "buttons": buttons[:3], "fallback": fallback}

    @staticmethod
    def _binding(state: dict) -> str:
        session, location = state.get("session", {}), state.get("location", {})
        return f"{session.get('account_reference', '')}:{location.get('id', '')}:{location.get('pincode', '')}"

    @classmethod
    def _snapshot(cls, cart: dict, state: dict) -> str:
        """Hash exactly what the customer sees: lines, quantities, prices, total, address."""
        content = {
            "cart": cart.get("cart_id"),
            "lines": sorted(
                [str(_line_key(i["identifiers"], i.get("item_size"))), i["quantity"], i.get("line_total"), i.get("available")]
                for i in cart["items"]
            ),
            "total": cls._total(cart),
            "valid": cart.get("is_valid"),
            "address": state.get("location", {}).get("id"),
        }
        return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _total(cart: dict) -> float | None:
        for entry in cart.get("price_breakup", {}).get("raw", []):
            if entry["name"] == "total":
                return entry["amount"]
        return None

    @staticmethod
    def _breakup_lines(cart: dict) -> list[str]:
        """Items, savings and every charge, ending with the total; no fee is ever hidden or invented."""
        raw = {entry["name"]: entry["amount"] for entry in cart.get("price_breakup", {}).get("raw", [])}
        charges = {"delivery_charge": "Delivery", "cod_charge": "Cash on delivery fee",
                   "convenience_fee": "Convenience fee"}
        savings = {"discount": "Discount", "coupon": "Coupon"}
        subtotal, total = raw.get("subtotal"), raw.get("total")
        discount = raw.get("discount") or 0
        charged = sum(raw.get(name) or 0 for name in charges) + (raw.get("coupon") or 0)
        # JioMart's subtotal is often the selling price, MRP with the discount already taken off. Adding
        # the discount again would invent a matching "other charge" (₹650 off, then ₹650 of fees).
        included = bool(discount) and subtotal is not None and (
            (raw.get("mrp_total") is not None and abs(raw["mrp_total"] + discount - subtotal) < 0.5)
            or (total is not None and abs(subtotal + charged - total) < 0.5
                and abs(subtotal + discount + charged - total) >= 0.5))
        rows = []
        if subtotal is not None:
            rows.append(f"Items: {_money(subtotal)}" + (f" (you save {_money(-discount)})" if included else ""))
        rows.extend(f"{label}: {_money(raw[name])}" for name, label in savings.items()
                    if raw.get(name) and not (name == "discount" and included))
        rows.extend(f"{label}: {_money(raw[name])}" for name, label in charges.items() if raw.get(name))
        if total is not None and subtotal is not None:
            # JioMart also adds charges under names we don't map (handling, small-cart fees, …).
            explained = subtotal + charged + (0 if included else discount)
            other = round(total - explained, 2)
            if other >= 0.5:
                rows.append(f"Delivery & other charges: {_money(other)}")
        if total is not None:
            rows.append(f"Total: {_money(total)}")
        return rows

    @staticmethod
    def _render_item(index: int, item: dict) -> str:
        name = item.get("name") or "Item"
        unavailable = item.get("available") is False
        price = f" — {_money(item['line_total'])}" if item.get("line_total") is not None and not unavailable else ""
        flag = " ⚠️ unavailable" if unavailable else ""
        return f"{index}. {item['quantity']} × {name}{price}{flag}"


def clear_ordered_lines(memory: dict, record: dict) -> None:
    """Note a placed order for this account (the cart itself is emptied by JioMart)."""
    orders = memory.setdefault("orders", [])
    if record.get("order_id") not in {order.get("order_id") for order in orders}:
        orders.insert(0, {"order_id": record.get("order_id"), "total": record.get("total"), "at": record.get("at")})
        del orders[5:]
