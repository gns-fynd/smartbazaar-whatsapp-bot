import io
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from PIL import Image

from smartbazaar.cart_image import allowed_image, build_collage
from smartbazaar.checkout import CheckoutFlow
from smartbazaar.storage import Store
from smartbazaar.whatsapp import DeliveryError, WhatsApp

IMG = "https://cdn1.jiomartjcp.com/v2/catalog/images/tomato.jpg"


def png(color="red"):
    out = io.BytesIO()
    Image.new("RGB", (400, 300), color).save(out, format="PNG")
    return out.getvalue()


def test_collage_is_a_white_jpeg_grid_and_only_catalog_images_are_fetched():
    picture = build_collage([png(), None, png("green"), png("blue")], [1, 2, 3, 1])
    with Image.open(io.BytesIO(picture)) as image:
        assert image.format == "JPEG"
        assert image.size == (24 + 3 * (240 + 24), 24 + 2 * (240 + 24))
        assert image.getpixel((5, 5)) == (255, 255, 255)
    assert allowed_image(IMG)
    assert not allowed_image("https://evil.example/x.jpg") and not allowed_image("http://cdn1.jiomartjcp.com/x.jpg")


@pytest.mark.asyncio
async def test_cart_card_uploads_one_picture_and_sends_it_as_the_button_header():
    sent = []

    async def graph(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            assert b"cart.jpg" in request.content and b"whatsapp" in request.content
            return httpx.Response(200, json={"id": "media-1"})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid-1"}]})

    async def cdn(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=png())

    client = WhatsApp("token", "phone-1", "v25.0", transport=httpx.MockTransport(graph),
                      image_transport=httpx.MockTransport(cdn))
    card = {"type": "cart_card", "items": [{"image_url": IMG, "quantity": 2}],
            "body": "🛒 These are your cart items:\n• 2 × Tomato — ₹96", "fallback": "text",
            "buttons": [{"id": "sbco_a", "title": "Checkout"}, {"id": "sbco_b", "title": "Empty cart"}]}

    assert await client.send("919000000001", card) == "wamid-1"
    interactive = sent[0]["interactive"]
    assert interactive["header"] == {"type": "image", "image": {"id": "media-1"}}
    assert interactive["body"]["text"].startswith("🛒 These are your cart items")
    assert [b["reply"]["title"] for b in interactive["action"]["buttons"]] == ["Checkout", "Empty cart"]

    with pytest.raises(DeliveryError):
        await client.send("919000000001", {**card, "items": [{"image_url": None, "quantity": 1}]})
    await client.close()


class CardCommerce:
    def __init__(self):
        self.removed = []

    async def get_cart(self, session, location):
        return {"cart_id": "cart-1", "last_modified": "t", "is_valid": True, "coverage": "complete",
                "currency": "INR", "raw_item_count": 1, "checkout_context": {},
                "items": [] if self.removed else [{
                    "identifiers": {"product_uid": "7", "identifier": "l0"}, "quantity": 2, "item_size": "OS",
                    "item_index": 0, "name": "Tomato Hybrid 1 kg", "line_total": 96.0, "available": True, "image_url": IMG}],
                "price_breakup": {"status": "available", "raw": [{"name": "total", "amount": 96.0}], "display": []}}

    async def update_cart(self, session, location, operation, items, cart_id=None):
        self.removed.append((operation, len(items)))
        return {"accepted": True, "partial": False}


@pytest.mark.asyncio
async def test_cart_view_is_a_picture_card_whose_buttons_check_out_or_empty_the_cart(tmp_path):
    store = Store(str(tmp_path / "bot.sqlite3"), Fernet.generate_key().decode())
    state = {"session": {"account_reference": "u1", "cookies": []},
             "location": {"id": "a1", "label": "Home", "city": "Mumbai", "pincode": "400053"}}
    commerce = CardCommerce()
    flow = CheckoutFlow(store, commerce, cart_writes=True)

    card = (await flow.view_cart(state))[0]

    assert card["type"] == "cart_card" and card["items"] == [{"image_url": IMG, "quantity": 2}]
    assert "• 2 × Tomato Hybrid 1 kg — ₹96" in card["body"] and "Total: ₹96" in card["body"]
    assert [b["title"] for b in card["buttons"]] == ["Checkout", "Keep shopping", "Empty cart"]

    emptied = await flow.handle_button("owner", state, {}, "acct", card["buttons"][2]["id"])
    assert emptied[0].startswith("Emptied your JioMart cart") and commerce.removed == [("remove_item", 1)]
    store.close()


@pytest.mark.asyncio
async def test_acknowledge_marks_read_with_typing_and_never_raises():
    seen = []

    async def graph(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    client = WhatsApp("token", "phone-1", "v25.0", transport=httpx.MockTransport(graph))
    await client.acknowledge("wamid.ABC")
    await client.acknowledge("")
    assert seen == [{"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.ABC",
                     "typing_indicator": {"type": "text"}}]

    async def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    offline = WhatsApp("token", "phone-1", "v25.0", transport=httpx.MockTransport(down))
    await offline.acknowledge("wamid.ABC")
    await client.close()
    await offline.close()


def graph_recorder(sent, uploads):
    async def graph(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            uploads.append(request.content)
            return httpx.Response(200, json={"id": f"media-{len(uploads)}"})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid-1"}]})
    return graph


CHOICE = {"type": "choice_card", "items": [{"image_url": IMG}, {"image_url": IMG}],
          "body": "Which paneer would you like?\n1. Chitale Paneer 200 g\n2. iD Paneer 200 g",
          "buttons": [{"id": "sbch_1", "title": "1. Chitale 200 g"}, {"id": "sbch_2", "title": "2. iD 200 g"},
                      {"id": "sbch_s", "title": "Skip"}],
          "list": {"type": "list", "body": "Which paneer would you like?", "button": "Choose",
                   "rows": [{"id": "sbch_1", "title": "Chitale Paneer 200 g"}]},
          "fallback": "text"}


@pytest.mark.asyncio
async def test_choice_card_is_one_numbered_picture_with_tap_buttons_or_falls_back_to_the_menu():
    sent, uploads = [], []

    async def cdn(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=png())

    client = WhatsApp("token", "phone-1", "v25.0", transport=httpx.MockTransport(graph_recorder(sent, uploads)),
                      image_transport=httpx.MockTransport(cdn))
    await client.send("919000000001", CHOICE)
    interactive = sent[0]["interactive"]
    assert interactive["type"] == "button" and interactive["header"]["image"] == {"id": "media-1"}
    assert [b["reply"]["title"] for b in interactive["action"]["buttons"]] == ["1. Chitale 200 g", "2. iD 200 g", "Skip"]
    assert b"choices.jpg" in uploads[0]

    async def broken_cdn(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    offline = WhatsApp("token", "phone-1", "v25.0", transport=httpx.MockTransport(graph_recorder(sent, uploads)),
                       image_transport=httpx.MockTransport(broken_cdn))
    await offline.send("919000000001", CHOICE)
    assert sent[1]["interactive"]["type"] == "list" and len(uploads) == 1
    await client.close()
    await offline.close()


def test_numbered_collage_draws_a_badge_per_option():
    plain = build_collage([png("white"), png("white")], [1, 1])
    numbered = build_collage([png("white"), png("white")], [1, 1], labels=["1", "2"])
    with Image.open(io.BytesIO(plain)) as a, Image.open(io.BytesIO(numbered)) as b:
        assert a.size == b.size
        # The top-left badge is dark on the numbered picture only.
        assert sum(a.getpixel((24 + 30, 24 + 30))) > 600 and sum(b.getpixel((24 + 30, 24 + 30))) < 400


@pytest.mark.asyncio
async def test_document_is_uploaded_as_a_pdf_and_sent_as_a_file():
    import base64

    from smartbazaar.receipt import summary_message

    record = {"lines": [{"name": "Coca Cola Zero 300 ml", "quantity": 2, "line_total": 72.0}],
              "breakup": ["Items: ₹72", "Total: ₹72"], "method": "cod", "at": 1792330000}
    message = summary_message("FY-ORDER-1", record)
    assert message["filename"] == "JioMart-order-FY-ORDER-1.pdf"
    assert base64.b64decode(message["data"]).startswith(b"%PDF")
    assert summary_message("FY-ORDER-1", {"lines": []}) is None

    sent, uploads = [], []
    client = WhatsApp("token", "phone-1", "v25.0", transport=httpx.MockTransport(graph_recorder(sent, uploads)))
    await client.send("919000000001", message)
    assert b"application/pdf" in uploads[0]
    assert sent[0]["type"] == "document"
    assert sent[0]["document"] == {"id": "media-1", "filename": "JioMart-order-FY-ORDER-1.pdf",
                                   "caption": "Order summary · FY-ORDER-1"}

    for bad in ({**message, "filename": "../x.pdf"}, {**message, "data": base64.b64encode(b"not a pdf").decode()}):
        with pytest.raises(DeliveryError):
            await client.send("919000000001", bad)
    await client.close()


@pytest.mark.asyncio
async def test_blocked_checkout_card_leads_with_the_reason_not_the_cart(tmp_path):
    store = Store(str(tmp_path / "bot.sqlite3"), Fernet.generate_key().decode())
    state = {"session": {"account_reference": "u1", "cookies": []},
             "location": {"id": "a1", "label": "Home", "city": "Mumbai", "pincode": "400053"}}

    class Commerce(CardCommerce):
        async def select_address(self, *args):
            return None

    flow = CheckoutFlow(store, Commerce(), cart_writes=True, checkout=True, max_total=50)

    card = (await flow.prepare("owner", state, {}, "acct"))[0]

    assert card["type"] == "cart_card"
    assert card["body"].startswith("⚠️ Can’t place this order yet:\nThe total ₹96 is above the ₹50 limit")
    assert "In your cart:\n• 2 × Tomato Hybrid 1 kg — ₹96" in card["body"]
    assert [b["title"] for b in card["buttons"]] == ["Keep shopping", "Empty cart"]
    store.close()
