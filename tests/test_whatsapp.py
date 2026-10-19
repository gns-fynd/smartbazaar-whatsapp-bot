import json

import httpx
import pytest

from smartbazaar.tts import TTSError
from smartbazaar.whatsapp import DeliveryError, WhatsApp


class FakeTTS:
    def __init__(self, audio=b"OggS" + b"\x00" * 10, mime="audio/ogg", error=None):
        self.audio, self.mime, self.error = audio, mime, error
        self.calls = []

    async def synthesize(self, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return self.audio, self.mime


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


@pytest.mark.asyncio
async def test_voice_note_uploads_ogg_audio_and_sends_it_with_the_voice_flag():
    sent, uploads = [], []

    async def graph(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            uploads.append(request.content)
            return httpx.Response(200, json={"id": "media-voice-1"})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.voice"}]})

    tts = FakeTTS(audio=b"OggS" + b"\x00" * 10, mime="audio/ogg")
    client = WhatsApp("token", "phone-1", "v25.0", transport=httpx.MockTransport(graph), tts=tts)
    message_id = await client.send("919000000001", {"type": "voice_note", "text": "Your order is confirmed."})
    await client.close()

    assert message_id == "wamid.voice"
    assert tts.calls == ["Your order is confirmed."]
    assert b"voice-note.ogg" in uploads[0] and b"audio/ogg" in uploads[0]
    assert sent[0]["type"] == "audio"
    assert sent[0]["audio"] == {"id": "media-voice-1", "voice": True}


@pytest.mark.asyncio
async def test_voice_note_mp3_fallback_is_a_plain_audio_message_without_the_voice_flag():
    sent, uploads = [], []

    async def graph(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            uploads.append(request.content)
            return httpx.Response(200, json={"id": "media-voice-2"})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.voice2"}]})

    tts = FakeTTS(audio=b"ID3mp3-bytes", mime="audio/mpeg")
    client = WhatsApp("token", "phone-1", "v25.0", transport=httpx.MockTransport(graph), tts=tts)
    await client.send("919000000001", {"type": "voice_note", "text": "Hi"})
    await client.close()

    assert b"voice-note.mp3" in uploads[0] and b"audio/mpeg" in uploads[0]
    assert sent[0]["audio"] == {"id": "media-voice-2"}
    assert "voice" not in sent[0]["audio"]


@pytest.mark.asyncio
async def test_voice_note_tts_failure_is_a_non_retryable_delivery_error_without_a_fallback():
    client = WhatsApp("token", "phone-1", "v25.0", transport=httpx.MockTransport(lambda _: None),
                      tts=FakeTTS(error=TTSError("http_500")))
    try:
        with pytest.raises(DeliveryError) as failure:
            await client.send("919000000001", {"type": "voice_note", "text": "Hi"})
    finally:
        await client.close()
    assert failure.value.code == "tts_failed" and failure.value.retry is False


@pytest.mark.asyncio
async def test_voice_note_without_a_configured_tts_client_fails_without_a_network_call():
    client = WhatsApp("token", "phone-1", "v25.0", transport=httpx.MockTransport(lambda _: None))
    try:
        with pytest.raises(DeliveryError) as failure:
            await client.send("919000000001", {"type": "voice_note", "text": "Hi"})
    finally:
        await client.close()
    assert failure.value.code == "tts_failed"
