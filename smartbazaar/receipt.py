"""A one-page order summary PDF, built in memory for the order confirmation message.

It restates only what JioMart confirmed for the order: lines, charges, total and payment
method. It is clearly labelled as a summary, not JioMart's tax invoice, which JioMart
issues later (see ``OrdersFlow.invoice``). Nothing is written to disk.
"""

import base64
import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, MARGIN = 1240, 1754, 90  # A4 at 150 dpi
MAX_LINES = 40
_FONTS = ("DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf", "Arial.ttf")


def _font(size: int):
    for name in _FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _plain(text: str) -> str:
    # Not every host font has the rupee glyph; "Rs." always prints.
    return re.sub(r"₹\s?", "Rs. ", str(text))


def order_summary_pdf(order_id: str, lines: list[dict], totals: list[str], method: str,
                      placed_at: float | None = None) -> bytes:
    """``lines`` are ``{"name", "quantity", "line_total"}``; ``totals`` are ready-made breakup rows."""
    page = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(page)
    title, body, small = _font(52), _font(30), _font(24)
    y = MARGIN
    draw.rectangle([0, 0, WIDTH, 18], fill="#0c5273")
    draw.text((MARGIN, y), "JioMart order summary", fill="#0c5273", font=title)
    y += 80
    when = datetime.fromtimestamp(placed_at or datetime.now().timestamp(), ZoneInfo("Asia/Kolkata"))
    for text in (f"Order {order_id}", f"Placed {when:%d %b %Y, %I:%M %p} IST",
                 "Payment: " + ("Cash on delivery" if method == "cod" else "Paid online (JioPay)")):
        draw.text((MARGIN, y), text, fill="#222222", font=body)
        y += 46
    y += 20
    draw.line([MARGIN, y, WIDTH - MARGIN, y], fill="#d0d5da", width=2)
    y += 30
    for line in lines[:MAX_LINES]:
        name = f"{line.get('quantity', 1)} x {line.get('name') or 'Item'}"
        amount = line.get("line_total")
        price = f"Rs. {amount:,.2f}".replace(".00", "") if isinstance(amount, (int, float)) else ""
        while draw.textlength(name, font=body) > WIDTH - 2 * MARGIN - 220 and len(name) > 4:
            name = name[:-2]
        if name != f"{line.get('quantity', 1)} x {line.get('name') or 'Item'}":
            name = name.rstrip() + "…"
        draw.text((MARGIN, y), name, fill="#222222", font=body)
        draw.text((WIDTH - MARGIN, y), price, fill="#222222", font=body, anchor="ra")
        y += 46
    if len(lines) > MAX_LINES:
        draw.text((MARGIN, y), f"…and {len(lines) - MAX_LINES} more items", fill="#555555", font=small)
        y += 40
    y += 10
    draw.line([MARGIN, y, WIDTH - MARGIN, y], fill="#d0d5da", width=2)
    y += 30
    for row in totals:
        label, _, value = _plain(row).partition(": ")
        bold = label.strip().lower() == "total"
        draw.text((MARGIN, y), label, fill="#000000" if bold else "#333333", font=title if bold else body)
        draw.text((WIDTH - MARGIN, y), value, fill="#000000" if bold else "#333333",
                  font=title if bold else body, anchor="ra")
        y += 70 if bold else 46
    note = ("This is an order summary from the SmartBazaar WhatsApp assistant, not a tax invoice. "
            "JioMart issues the tax invoice once your order is packed; ask for INVOICE in the chat or "
            "open My Orders in the JioMart app.")
    y = HEIGHT - MARGIN - 120
    words, current = note.split(), ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=small) > WIDTH - 2 * MARGIN:
            draw.text((MARGIN, y), current, fill="#666666", font=small)
            y += 34
            current = word
        else:
            current = candidate
    if current:
        draw.text((MARGIN, y), current, fill="#666666", font=small)
    output = io.BytesIO()
    page.save(output, format="PDF", resolution=150)
    return output.getvalue()


def summary_message(order_id: str, record: dict) -> dict | None:
    """The order summary as a WhatsApp document message, or None if there is nothing to show."""
    lines = record.get("lines") or []
    if not order_id or not lines:
        return None
    try:
        content = order_summary_pdf(order_id, lines, record.get("breakup") or [], record.get("method") or "cod",
                                    record.get("at"))
    except (OSError, ValueError):
        return None
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", order_id)[:40] or "order"
    return {"type": "document", "filename": f"JioMart-order-{safe_id}.pdf",
            "data": base64.b64encode(content).decode("ascii"), "caption": f"Order summary · {order_id}",
            "fallback": f"Order summary for {order_id} is in the JioMart app under My Orders."}
