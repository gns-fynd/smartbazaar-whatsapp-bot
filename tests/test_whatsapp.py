import json

import httpx
import pytest

from smartbazaar.whatsapp import DeliveryError, WhatsApp


@pytest.mark.asyncio
async def test_product_card_uses_image_header_and_safe_reply_buttons():
    request = None

    def handler(received):
        nonlocal request
        request = received
        return httpx.Response(200, json={"messages": [{"id": "wamid.card"}]})

    client = WhatsApp("private-token", "phone-id", "v25.0", transport=httpx.MockTransport(handler))
    try:
        message_id = await client.send(
            "919000000001",
            {
                "type": "product_card",
                "image_url": "https://cdn1.jiomartjcp.com/catalog/milk.jpg",
                "body": "Match for milk:\nAmul Taaza Milk 1 L\n₹74\nPincode 400053",
                "footer": "Listing price · Not added to cart",
                "buttons": [
                    {"id": "sbpc_abcdefghijklmnopqrstuv", "title": "View details"},
                    {"id": "sbpc_zyxwvutsrqponmlkjihgf", "title": "Next option"},
                ],
            },
        )
    finally:
        await client.close()

    assert message_id == "wamid.card"
    payload = json.loads(request.content)
    assert payload["type"] == "interactive"
    assert payload["interactive"]["header"] == {
        "type": "image",
        "image": {"link": "https://cdn1.jiomartjcp.com/catalog/milk.jpg"},
    }
    assert payload["interactive"]["action"]["buttons"][0]["reply"]["title"] == "View details"


@pytest.mark.asyncio
async def test_product_card_rejects_untrusted_image_urls():
    client = WhatsApp("private-token", "phone-id", "v25.0", transport=httpx.MockTransport(lambda _: None))
    try:
        with pytest.raises(DeliveryError) as failure:
            await client.send(
                "919000000001",
                {
                    "type": "product_card",
                    "image_url": "https://example.test/not-a-catalog-image.jpg",
                    "body": "Product",
                    "footer": "Listing only",
                    "buttons": [{"id": "sbpc_abcdefghijklmnopqrstuv", "title": "View details"}],
                },
            )
    finally:
        await client.close()
    assert failure.value.code == "invalid_payload"


@pytest.mark.asyncio
async def test_quick_reply_uses_button_interactive_without_header_and_ignores_fallback():
    request = None

    def handler(received):
        nonlocal request
        request = received
        return httpx.Response(200, json={"messages": [{"id": "wamid.quick-reply"}]})

    client = WhatsApp("private-token", "phone-id", "v25.0", transport=httpx.MockTransport(handler))
    try:
        message_id = await client.send(
            "919000000001",
            {
                "type": "quick_reply",
                "body": "Would you like to continue?",
                "footer": "Choose one option",
                "buttons": [
                    {"id": "continue", "title": "Continue"},
                    {"id": "cancel", "title": "Cancel"},
                ],
                "fallback": "Would you like to continue? Reply CONTINUE or CANCEL.",
            },
        )
    finally:
        await client.close()

    assert message_id == "wamid.quick-reply"
    payload = json.loads(request.content)
    assert payload["type"] == "interactive"
    assert payload["interactive"] == {
        "type": "button",
        "body": {"text": "Would you like to continue?"},
        "footer": {"text": "Choose one option"},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": "continue", "title": "Continue"}},
                {"type": "reply", "reply": {"id": "cancel", "title": "Cancel"}},
            ]
        },
    }
    assert "header" not in payload["interactive"]
    assert "fallback" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {"type": "quick_reply", "body": "", "buttons": [{"id": "one", "title": "One"}]},
        {"type": "quick_reply", "body": "x" * 1025, "buttons": [{"id": "one", "title": "One"}]},
        {"type": "quick_reply", "body": "Choose", "buttons": []},
        {
            "type": "quick_reply",
            "body": "Choose",
            "buttons": [
                {"id": "one", "title": "One"},
                {"id": "two", "title": "Two"},
                {"id": "three", "title": "Three"},
                {"id": "four", "title": "Four"},
            ],
        },
        {
            "type": "quick_reply",
            "body": "Choose",
            "buttons": [{"id": "same", "title": "One"}, {"id": "same", "title": "Two"}],
        },
        {
            "type": "quick_reply",
            "body": "Choose",
            "buttons": [{"id": "one", "title": "Same"}, {"id": "two", "title": "Same"}],
        },
        {
            "type": "quick_reply",
            "body": "Choose",
            "footer": "x" * 61,
            "buttons": [{"id": "one", "title": "One"}],
        },
    ],
)
async def test_quick_reply_rejects_invalid_limits_and_duplicate_buttons(message):
    client = WhatsApp("private-token", "phone-id", "v25.0", transport=httpx.MockTransport(lambda _: None))
    try:
        with pytest.raises(DeliveryError) as failure:
            await client.send("919000000001", message)
    finally:
        await client.close()

    assert failure.value.code == "invalid_payload"
