import json

import httpx
import pytest

from smartbazaar.openwa import OpenWA
from smartbazaar.whatsapp import DeliveryError


@pytest.mark.asyncio
async def test_openwa_send_text_uses_documented_session_route_and_response_shape():
    request = None

    def handler(received):
        nonlocal request
        request = received
        return httpx.Response(201, json={"messageId": "outbound-1", "timestamp": 1})

    client = OpenWA(
        "http://openwa.test/api",
        "private-key",
        "c2369496-31f4-4f37-b3dd-f353580fe7ca",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.send("919000000001@c.us", "hello") == "outbound-1"
    finally:
        await client.close()
    assert request.url.path == "/api/sessions/c2369496-31f4-4f37-b3dd-f353580fe7ca/messages/send-text"
    assert request.headers["x-api-key"] == "private-key"
    assert json.loads(request.content) == {
        "chatId": "919000000001@c.us",
        "text": "hello",
        "linkPreview": False,
    }


@pytest.mark.asyncio
async def test_openwa_send_text_retries_transport_or_server_failures_only():
    client = OpenWA(
        "http://openwa.test/api",
        "private-key",
        "c2369496-31f4-4f37-b3dd-f353580fe7ca",
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    try:
        with pytest.raises(DeliveryError) as failure:
            await client.send("919000000001@c.us", "hello")
    finally:
        await client.close()
    assert failure.value.code == "http_503"
    assert failure.value.retry is True
