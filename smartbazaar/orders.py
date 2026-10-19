"""Order history, status, tracking and reorder for the linked JioMart account.

All facts come from JioMart's order APIs for the signed-in account. Numbers such
as ORDER 2 refer to the last list shown in this chat; reorder only re-finds the
same catalog items and puts them into the JioMart cart, never straight into an order.
"""

import base64
import logging
import re
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import i18n
from .catalog import normalize_products
from .jiomart import CommerceError

log = logging.getLogger("smartbazaar")
VIEW_TTL = 6 * 3600
ACTION_TTL = 24 * 3600
MAX_LISTED = 5
MAX_REORDER = 12
IST = ZoneInfo("Asia/Kolkata")


def _date(epoch, *, with_time=False) -> str:
    if not isinstance(epoch, (int, float)):
        return "date unknown"
    moment = datetime.fromtimestamp(epoch, IST)
    label = f"{moment.day} {moment:%b}"
    if moment.year != datetime.now(IST).year:
        label += f" {moment.year}"
    if with_time:
        label += f", {moment:%I:%M %p}".replace(" 0", " ")
    return label


def _money(value) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "total unavailable"
    return f"₹{value:,.2f}".replace(".00", "")


class OrdersFlow:
    def __init__(self, store, commerce, cart=None):
        self.store, self.commerce, self.cart = store, commerce, cart

    async def recent(self, state: dict) -> list:
        orders = await self.commerce.list_orders(state["session"])
        if not orders:
            return ["I couldn’t find any orders on this JioMart account. If the JioMart app shows orders, tell me and I’ll look into it."]
        shown = orders[:MAX_LISTED]
        state["orders_view"] = {
            "ids": [order["order_id"] for order in shown],
            "expires": time.time() + VIEW_TTL, "binding": self._binding(state),
        }
        lines = ["📦 Your recent JioMart orders"]
        for index, order in enumerate(shown, 1):
            count = f"{order['item_count']} item{'s' if order['item_count'] != 1 else ''}" if order["item_count"] else "items not listed"
            lines.append(f"{index}. {_date(order['created_at'])} · {_money(order['total'])} · {count} · {order['status'] or 'status unavailable'}")
        lines.append("Reply ORDER 1 for details, TRACK 1 to track it, or REORDER 1 to put its items back in your cart.")
        latest = shown[0]["order_id"]
        buttons = [
            self._button(state, "track", latest, "Track latest"),
            self._button(state, "reorder", latest, "Reorder latest"),
            self._button(state, "detail", latest, "Latest details"),
        ]
        body = "\n".join(lines)
        if len(body) > 1024:
            return [body]
        return [{"type": "quick_reply", "body": body, "buttons": buttons, "fallback": body}]

    async def detail(self, state: dict, number: int | None) -> list[str]:
        order = await self._order(state, number)
        if isinstance(order, str):
            return [order]
        lines = [f"🧾 Order {order['order_id']} · {_date(order['created_at'])}",
                 f"Status: {order['status'] or 'unavailable'}"]
        expected = next((s["expected_at"] for s in order["shipments"] if s["expected_at"] and s["state"] not in {"delivered", "cancelled"}), None)
        if expected:
            lines.append(f"Expected by {_date(expected, with_time=True)}")
        if order["items"]:
            lines.append("Items:")
            for item in order["items"][:25]:
                price = f" — {_money(item['price'])}" if item["price"] is not None else ""
                lines.append(f"• {item['quantity']} × {item['name']}{price}")
            if len(order["items"]) > 25:
                lines.append(f"…and {len(order['items']) - 25} more")
        else:
            lines.append("JioMart didn’t share the item list for this order.")
        lines.append(f"Total: {_money(order['total'])}")
        body = "\n".join(lines)
        if len(body) > 1024:
            return [body]
        # The order's own next steps; each button is bound to this order and account.
        buttons = [self._button(state, "track", order["order_id"], "Track order"),
                   self._button(state, "invoice", order["order_id"], "Invoice")]
        if self._cancellable(order):
            buttons.append(self._button(state, "cancel", order["order_id"], "Cancel order"))
        else:
            buttons.append(self._button(state, "reorder", order["order_id"], "Reorder"))
        return [{"type": "quick_reply", "body": body, "buttons": buttons, "fallback": body}]

    async def track(self, state: dict, number: int | None) -> list[str]:
        order = await self._order(state, number)
        if isinstance(order, str):
            return [order]
        if not order["shipments"]:
            return [f"Order {order['order_id']}: {order['status'] or 'status unavailable'}. JioMart didn’t share shipment tracking for it."]
        lines = [f"🚚 Order {order['order_id']} · {_date(order['created_at'])}"]
        for index, shipment in enumerate(order["shipments"][:5], 1):
            prefix = f"Shipment {index}: " if len(order["shipments"]) > 1 else ""
            lines.append(f"{prefix}{shipment['status'] or 'Status unavailable'}")
            if shipment["state"] == "delivered" and shipment["delivered_at"]:
                lines.append(f"Delivered on {_date(shipment['delivered_at'], with_time=True)}")
            elif shipment["expected_at"]:
                lines.append(f"Expected by {_date(shipment['expected_at'], with_time=True)}")
            for step in [s for s in shipment["timeline"] if s["passed"] or s["current"]][-4:]:
                when = f" — {_date(step['time'], with_time=True)}" if step["time"] else ""
                lines.append(f"  {'➜' if step['current'] else '✓'} {step['status']}{when}")
            if shipment["shipment_id"] and shipment["state"] in {"shipped", "processing", None}:
                try:
                    courier = await self.commerce.track_shipment(state["session"], shipment["shipment_id"])
                except CommerceError as exc:
                    if exc.code == "auth_expired":
                        raise
                    courier = {}
                details = [part for part in (courier.get("courier"), courier.get("status")) if part]
                if details:
                    lines.append("  Courier: " + " · ".join(details) + (f" (AWB {courier['awb']})" if courier.get("awb") else ""))
        return ["\n".join(lines)]

    async def reorder(self, state: dict, memory: dict, number: int | None) -> list[str]:
        if not state.get("location"):
            return ["Choose a delivery address first with ADDRESSES, then try REORDER again."]
        order = await self._order(state, number)
        if isinstance(order, str):
            return [order]
        items = [item for item in order["items"] if item["item_id"]]
        if not items:
            return ["JioMart didn’t share the items for this order, so I can’t reorder it here. You can use Order again in the JioMart app."]
        lines, missing, seen = [], [], set()
        for item in items[:MAX_REORDER]:
            if item["item_id"] in seen:
                continue
            seen.add(item["item_id"])
            product = await self._current_product(state, item)
            if product is None:
                missing.append(item["name"])
                continue
            lines.append({"product": product, "quantity": min(item["quantity"], 99)})
        result = await self.cart.add_products(state, memory, lines) if lines else {"added": [], "already": [], "notes": [], "error": None}
        if result["error"]:
            return [result["error"]]
        text = []
        if result["added"]:
            text.append(f"🛒 Added to your JioMart cart from your {_date(order['created_at'])} order:")
            text.extend(f"• {entry}" for entry in result["added"])
        if result["already"]:
            text.append("Already in your cart, left as it is: " + ", ".join(result["already"][:8]))
        if missing:
            text.append("Not available to add right now: " + ", ".join(missing[:8]))
        text.extend(result["notes"])
        if len(items) > MAX_REORDER:
            text.append(f"I looked at the first {MAX_REORDER} items of that order.")
        if result["added"]:
            text.append("I used today’s prices, which may differ from that order.")
        elif not text:
            text.append("Nothing from that order could be added.")
        return ["\n".join(text)]

    async def cancel(self, state: dict, number: int | None) -> list:
        """Ask to confirm a cancellation; only the confirmation button cancels."""
        if not getattr(self.commerce, "order_cancel_enabled", False):
            return ["Cancelling from chat isn’t switched on yet. You can cancel from My Orders in the JioMart app."]
        order = await self._order(state, number)
        if isinstance(order, str):
            return [order]
        if not self._cancellable(order):
            return [f"Order {order['order_id']} ({order['status'] or 'status unavailable'}) can’t be cancelled from "
                    "here any more. If something’s wrong, open My Orders in the JioMart app for help."]
        count = len(order["items"])
        body = (f"Cancel order {order['order_id']} from {_date(order['created_at'])}"
                f" ({count} item{'s' if count != 1 else ''}, {_money(order['total'])})?\n"
                "This can’t be undone. If you paid online, JioMart refunds it to the original payment method.")
        buttons = [self._button(state, "cancel_confirm", order["order_id"], "Yes, cancel it"),
                   self._button(state, "keep", order["order_id"], "Keep my order")]
        return [{"type": "quick_reply", "body": body, "buttons": buttons, "fallback": body + "\nReply CANCEL ORDER again to confirm in the app."}]

    async def _confirm_cancel(self, state: dict, order_id: str) -> list:
        try:
            order = await self.commerce.get_order_detail(state["session"], order_id)
        except CommerceError as exc:
            if exc.code == "auth_expired":
                raise
            return ["I couldn’t read that order right now, so nothing was cancelled. Try again in a moment."]
        shipments = [s for s in order["shipments"] if s.get("can_cancel") is True and s.get("shipment_id")]
        if not shipments:
            return [f"Order {order_id} can’t be cancelled any more ({order['status'] or 'status unavailable'}). Nothing was changed."]
        done = 0
        for shipment in shipments:
            try:
                reasons = await self.commerce.cancellation_reasons(state["session"], shipment["shipment_id"])
                reason = self._reason(reasons)
                if reason is None:
                    return ["JioMart didn’t offer a cancellation reason I can use, so nothing was cancelled. "
                            "You can cancel from My Orders in the JioMart app."]
                await self.commerce.cancel_shipment(state["session"], shipment["shipment_id"],
                                                    reason_id=reason["id"], reason_text=reason["text"])
                done += 1
            except CommerceError as exc:
                if exc.code == "auth_expired":
                    raise
                log.warning("order_cancel_failed code=%s", exc.diagnostic_code or exc.code)
                if exc.code == "unknown" or done:
                    return [f"I couldn’t confirm whether JioMart cancelled order {order_id}. Please check My Orders "
                            "in the JioMart app before trying again; I won’t retry it automatically."]
                return [f"JioMart didn’t accept the cancellation for order {order_id}, so nothing changed. "
                        "You can try from My Orders in the JioMart app."]
        return [f"Cancellation requested for order {order_id}. JioMart will confirm it shortly and I’ll message you "
                "when it shows as cancelled. Any online payment goes back to the original payment method."]

    async def invoice(self, state: dict, number: int | None) -> list:
        order = await self._order(state, number)
        if isinstance(order, str):
            return [order]
        shipments = [s for s in order["shipments"] if s.get("shipment_id")]
        if not shipments or all(s.get("invoice_available") is False for s in shipments):
            return [f"JioMart hasn’t issued the invoice for order {order['order_id']} yet. It’s usually ready once the "
                    "order is packed; ask again later or find it under My Orders in the JioMart app."]
        documents = []
        for shipment in shipments[:3]:
            if shipment.get("invoice_available") is False:
                continue
            document = await self.invoice_document(state, order["order_id"], shipment["shipment_id"])
            if isinstance(document, dict):
                documents.append(document)
        if not documents:
            return [f"I couldn’t get the invoice for order {order['order_id']} from JioMart right now. You can "
                    "download it from My Orders in the JioMart app."]
        return [f"🧾 Here’s the invoice for order {order['order_id']}."] + documents

    async def invoice_document(self, state: dict, order_id: str, shipment_id: str) -> dict | None:
        """JioMart's tax invoice for one shipment as a WhatsApp document, or None if unavailable."""
        try:
            invoice = await self.commerce.invoice_pdf(state["session"], shipment_id)
        except CommerceError as exc:
            if exc.code == "auth_expired":
                raise
            log.info("invoice_unavailable code=%s", exc.diagnostic_code or exc.code)
            return None
        except (AttributeError, ValueError):
            return None
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", order_id)[:40] or "order"
        return {"type": "document", "filename": f"JioMart-invoice-{safe_id}.pdf",
                "data": base64.b64encode(invoice["content"]).decode("ascii"), "caption": f"Invoice · {order_id}",
                "fallback": f"The invoice for {order_id} is under My Orders in the JioMart app."}

    def _cancellable(self, order: dict) -> bool:
        return getattr(self.commerce, "order_cancel_enabled", False) and any(
            s.get("can_cancel") is True for s in order.get("shipments", []))

    @staticmethod
    def _reason(reasons: list[dict]) -> dict | None:
        """A neutral customer reason: changed mind / not needed / ordered by mistake, else the first."""
        for words in (("changed", "mind"), ("not", "required"), ("not", "needed"), ("mistake",), ("other",)):
            found = next((r for r in reasons if all(w in r["text"].casefold() for w in words)), None)
            if found:
                return found
        return reasons[0] if reasons else None

    async def button(self, state: dict, memory: dict, action_id: str) -> list | None:
        actions = state.get("order_actions") if isinstance(state.get("order_actions"), dict) else {}
        action = actions.pop(action_id, None)
        state["order_actions"] = actions
        if not isinstance(action, dict) or action.get("expires", 0) < time.time() or action.get("binding") != self._binding(state):
            return ["That order button has expired. Send ORDERS to see your orders again."]
        state["orders_view"] = {"ids": [action["order_id"]], "expires": time.time() + VIEW_TTL,
                                "binding": self._binding(state)}
        if action["kind"] == "track":
            return await self.track(state, 1)
        if action["kind"] == "reorder":
            return await self.reorder(state, memory, 1)
        if action["kind"] == "invoice":
            return await self.invoice(state, 1)
        if action["kind"] == "cancel":
            return await self.cancel(state, 1)
        if action["kind"] == "cancel_confirm":
            return await self._confirm_cancel(state, action["order_id"])
        if action["kind"] == "keep":
            return [f"Okay, order {action['order_id']} stays as it is."]
        return await self.detail(state, 1)

    # ----- helpers -----

    async def _order(self, state: dict, number: int | None):
        """Resolve ORDER n against the last list shown, else the newest orders."""
        index = (number or 1) - 1
        view = state.get("orders_view") if isinstance(state.get("orders_view"), dict) else {}
        ids = view.get("ids", []) if view.get("expires", 0) >= time.time() and view.get("binding") == self._binding(state) else []
        if not ids:
            orders = await self.commerce.list_orders(state["session"])
            if not orders:
                return "I couldn’t find any orders on this JioMart account."
            ids = [order["order_id"] for order in orders[:MAX_LISTED]]
            state["orders_view"] = {"ids": ids, "expires": time.time() + VIEW_TTL, "binding": self._binding(state)}
        if not 0 <= index < len(ids):
            return f"I only have {len(ids)} recent order{'s' if len(ids) != 1 else ''} listed. Send ORDERS to see them."
        try:
            return await self.commerce.get_order_detail(state["session"], ids[index])
        except CommerceError as exc:
            if exc.code == "auth_expired":
                raise
            return "JioMart didn’t return the details for that order. Older orders may only be viewable in the JioMart app."

    async def _current_product(self, state: dict, item: dict) -> dict | None:
        try:
            result = await self.commerce.search(item["name"], state["location"], state["session"])
        except CommerceError as exc:
            if exc.code == "auth_expired":
                raise
            return None
        # Same catalog item only, still listed as sellable here; never a lookalike.
        return next((p for p in normalize_products(result.get("items", []))
                     if p["id"] == item["item_id"] and p.get("sellable") is not False and p.get("cart")), None)

    def _button(self, state: dict, kind: str, order_id: str, title: str) -> dict:
        actions = state.get("order_actions") if isinstance(state.get("order_actions"), dict) else {}
        now = time.time()
        actions = {key: value for key, value in actions.items() if isinstance(value, dict) and value.get("expires", 0) >= now}
        action_id = "sbor_" + secrets.token_urlsafe(18)
        actions[action_id] = {"kind": kind, "order_id": order_id, "expires": now + ACTION_TTL,
                              "binding": self._binding(state)}
        state["order_actions"] = dict(list(actions.items())[-15:])
        return {"id": action_id, "title": i18n.label(title, state)}

    @staticmethod
    def _binding(state: dict) -> str:
        return str(state.get("session", {}).get("account_reference", ""))
