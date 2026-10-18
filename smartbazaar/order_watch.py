"""Follow orders placed from chat and push payment and delivery updates.

Stages come only from JioMart's order record for the signed-in account; a browser
returning from the payment page is never taken as proof of payment. WhatsApp only
allows free-form messages within 24 hours of the customer's last message, so an
update outside that window is kept and shown the next time the customer writes.
"""

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .checkout import clear_ordered_lines
from .jiomart import CommerceError
from .orders import OrdersFlow
from .receipt import summary_message

log = logging.getLogger("smartbazaar")
IST = ZoneInfo("Asia/Kolkata")
WATCH_DAYS = 7
SERVICE_WINDOW = 23.5 * 3600
PAYMENT_NUDGE_AFTER = 20 * 60
PAYMENT_GIVE_UP_AFTER = 3 * 3600

PAID = {"paid", "complete", "completed", "success", "successful", "captured", "confirmed"}
FAILED = {"failed", "failure", "declined", "expired"}
POST_PAYMENT = {
    "placed", "bag_confirmed", "bag_packed", "bag_invoiced", "ready_for_dp_assignment", "dp_assigned",
    "bag_picked", "in_transit", "shipped", "out_for_delivery", "delivery_done", "delivered",
}
PENDING = {"pending", "payment_pending", "pending_payment", "initiated"}
RANK = {"payment_pending": 0, "confirmed": 1, "shipped": 2, "out_for_delivery": 3, "delivered": 4, "cancelled": 4}


def order_stage(order: dict, *, awaiting_online: bool = False) -> str:
    """Coarse stage from JioMart's order record.

    Payment stages apply only to an online order still awaiting payment; evidence of
    payment always wins over an earlier failed payment record.
    """
    shipments = order.get("shipments") or []
    states = [s.get("state") for s in shipments]
    values = [s.get("value") or "" for s in shipments]
    payment = order.get("payment_status")
    if shipments and all(state in {"delivered", "cancelled"} for state in states) and "delivered" in states:
        return "delivered"
    if shipments and all(state == "cancelled" for state in states):
        return "cancelled"
    if "out_for_delivery" in values:
        return "out_for_delivery"
    if "shipped" in states:
        return "shipped"
    if payment in PAID or any(value in POST_PAYMENT for value in values):
        return "confirmed"
    if awaiting_online and payment in FAILED:
        return "payment_failed"
    if awaiting_online and (payment in PENDING or any(value in PENDING for value in values)):
        return "payment_pending"
    return "unknown"


def _when(epoch) -> str:
    if not isinstance(epoch, (int, float)):
        return ""
    moment = datetime.fromtimestamp(epoch, IST)
    return f"{moment.day} {moment:%b}, {moment:%I:%M %p}".replace(" 0", " ")


def _money(value) -> str:
    return f"₹{value:,.2f}".replace(".00", "") if isinstance(value, (int, float)) and not isinstance(value, bool) else ""


def stage_message(stage: str, order: dict, data: dict) -> str | None:
    order_id = order["order_id"]
    expected = next((s.get("expected_at") for s in order.get("shipments", []) if s.get("expected_at")), None)
    eta = f" Expected by {_when(expected)}." if expected else ""
    if stage == "confirmed":
        if data.get("method") != "online":
            return None  # The placement reply already confirmed a cash-on-delivery order.
        return (f"✅ Payment received. Order {order_id} for {_money(order.get('total')) or 'your basket'} is confirmed.{eta}\n"
                "I’ll message you when it’s out for delivery and when it’s delivered.")
    if stage == "shipped":
        return f"🚚 Order {order_id} has shipped.{eta}"
    if stage == "out_for_delivery":
        return f"🛵 Order {order_id} is out for delivery!{eta}"
    if stage == "delivered":
        delivered = next((s.get("delivered_at") for s in order.get("shipments", []) if s.get("delivered_at")), None)
        lines = [f"📦 Delivered! Order {order_id}" + (f" arrived {_when(delivered)}." if delivered else " has been delivered.")]
        items = order.get("items") or []
        if items:
            lines.append("Items:")
            lines.extend(f"• {i['quantity']} × {i['name']}" for i in items[:15])
            if len(items) > 15:
                lines.append(f"…and {len(items) - 15} more")
        total = _money(order.get("total"))
        if total:
            lines.append(f"Total: {total}" + (f" ({order['payment_mode']})" if order.get("payment_mode") else ""))
        lines.append("Hope everything arrived fine. Reply REORDER 1 to buy the same again, or tell me if something’s missing.")
        return "\n".join(lines)
    if stage == "cancelled":
        refund = " Any amount you paid goes back to your original payment method." if data.get("method") == "online" else ""
        return f"❌ Order {order_id} is cancelled.{refund} Tell me if you’d like to order anything again."
    if stage == "payment_failed":
        return f"Payment for order {order_id} didn’t go through, so it isn’t confirmed. Send CHECKOUT to try again."
    return None


class OrderWatcher:
    def __init__(self, store, commerce):
        self.store, self.commerce = store, commerce

    async def check(self, locks: dict, *, only: str | None = None, now: float | None = None) -> list[tuple[str, str, list[str]]]:
        """Check due orders; return (event_id, recipient, messages) to enqueue."""
        now = now or time.time()
        outgoing = []
        for attempt_id, owner, account, state_name, data in self.store.watched_attempts(WATCH_DAYS * 86400):
            if only and attempt_id != only:
                continue
            if not only and data.get("next_check", 0) > now:
                continue
            async with locks.setdefault(owner, asyncio.Lock()):
                current = self.store.get_attempt(attempt_id)
                if not current or current[2] != state_name:
                    continue
                try:
                    result = await self._step(attempt_id, owner, account, state_name, current[3], now)
                except Exception as exc:
                    # One unreadable order must not starve the others.
                    log.warning("order_watch_order_failed type=%s", type(exc).__name__)
                    data = current[3]
                    data["next_check"] = now + 600
                    self.store.update_attempt(attempt_id, state_name, data)
                    continue
            if result:
                outgoing.append(result)
        return outgoing

    async def _step(self, attempt_id, owner, account, state_name, data, now):
        order_id, recipient = data.get("order_id"), data.get("recipient")
        session_state = self.store.get(owner)
        if not order_id or not recipient or not session_state.get("session"):
            # Without a live sign-in the order can't be read; stop quietly after the watch period.
            data["next_check"] = now + 600
            self.store.update_attempt(attempt_id, state_name if now - data.get("at", now) < WATCH_DAYS * 86400 else "watch_ended", data)
            return None
        try:
            order = await self.commerce.get_order_detail(session_state["session"], order_id)
        except CommerceError as exc:
            data["next_check"] = now + (1800 if exc.code == "auth_expired" else 300)
            unpaid_too_long = state_name == "awaiting_payment" and now - data.get("at", now) > PAYMENT_GIVE_UP_AFTER
            # JioMart doesn't serve unpaid online orders; after the payment window stop quietly.
            self.store.update_attempt(attempt_id, "unpaid" if unpaid_too_long else state_name, data)
            if unpaid_too_long:
                self.store.delete_payment_links(attempt_id)
            return None
        finally:
            # Refreshed cookies are kept, but a background read never extends the sign-in.
            self.store.save(owner, session_state, touch=False)
        awaiting_online = state_name == "awaiting_payment" and data.get("method") == "online"
        stage = order_stage(order, awaiting_online=awaiting_online)
        log.info("order_watch stage=%s payment=%s values=%s", stage, order.get("payment_status") or "none",
                 ",".join(s.get("value") or "none" for s in order.get("shipments", [])[:5]) or "none")
        notified = data.get("notified")
        message = None
        new_state = state_name
        if awaiting_online and stage in {"payment_pending", "unknown"}:
            age = now - data.get("at", now)
            if age > PAYMENT_GIVE_UP_AFTER:
                new_state, message = "unpaid", (
                    f"I didn’t see a payment for order {order_id}, so JioMart will likely cancel it. "
                    "Nothing more is needed from you. Send CHECKOUT to try again.")
            elif age > PAYMENT_NUDGE_AFTER and not data.get("nudged"):
                data["nudged"] = True
                message = (f"I haven’t seen the payment for order {order_id} yet. If you’ve paid, it can take a few "
                           "minutes to show. Otherwise send ORDER STATUS for the payment link.")
            data["next_check"] = now + (20 if age < PAYMENT_NUDGE_AFTER else 120)
        elif stage == "unknown":
            data["next_check"] = now + 300
        elif notified is None or RANK.get(stage, -1) > RANK.get(notified, -1):
            message = stage_message(stage, order, data)
            data["notified"] = stage
            if state_name == "awaiting_payment" and stage in {"confirmed", "shipped", "out_for_delivery", "delivered"}:
                new_state = "placed"
            if stage in {"delivered", "cancelled"}:
                new_state = "delivered" if stage == "delivered" else "cancelled"
            if stage == "payment_failed":
                new_state = "failed"
            data["next_check"] = now + (180 if stage in {"confirmed", "shipped", "out_for_delivery"} else 600)
        else:
            data["next_check"] = now + 180
        if now - data.get("at", now) > WATCH_DAYS * 86400 and new_state in {"placed", "awaiting_payment"}:
            new_state = "watch_ended"
        if state_name == "awaiting_payment" and new_state != "awaiting_payment":
            # A settled order's payment page must never be payable again.
            self.store.delete_payment_links(attempt_id)
            if new_state == "placed" and data.get("method") == "online":
                memory = self.store.get_memory(owner, account)
                clear_ordered_lines(memory, data)
                self.store.save_memory(owner, account, memory)
        if message and now - session_state.get("last_inbound", 0) > SERVICE_WINDOW:
            # Outside WhatsApp's customer-service window: show it when the customer next writes.
            data["missed_notice"] = message
            message = None
        self.store.update_attempt(attempt_id, new_state, data)
        if not message:
            return None
        messages: list = [message]
        if stage == "confirmed" and data.get("method") == "online":
            # The payment confirmation carries the order summary, like a cash-on-delivery placement.
            summary = summary_message(order_id, data)
            if summary:
                messages.append(summary)
        if stage == "delivered":
            # JioMart's own tax invoice exists by now; attach it when JioMart hands it over.
            shipment = next((s for s in order.get("shipments", [])
                             if s.get("shipment_id") and s.get("invoice_available") is not False), None)
            if shipment:
                try:
                    invoice = await OrdersFlow(self.store, self.commerce).invoice_document(
                        session_state, order_id, shipment["shipment_id"])
                except CommerceError:
                    invoice = None
                self.store.save(owner, session_state, touch=False)
                if invoice:
                    messages.append(invoice)
        return f"watch:{attempt_id}:{data.get('notified') or new_state}:{int(now // 60)}", recipient, messages
