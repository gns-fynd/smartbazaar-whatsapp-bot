"""Temporary OpenWA transport. Secrets and chat identifiers never reach logs."""

from urllib.parse import quote

import httpx

from .whatsapp import DeliveryError


class OpenWA:
    """OpenWA's documented send-text client for one linked WhatsApp session."""

    def __init__(self, base_url: str, api_key: str, session_id: str, transport=None):
        self.session_id = session_id
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"X-API-Key": api_key},
            timeout=20,
            transport=transport,
        )

    async def send(self, recipient: str, text: str) -> str:
        try:
            response = await self.client.post(
                f"sessions/{quote(self.session_id, safe='')}/messages/send-text",
                json={"chatId": recipient, "text": text[:4096], "linkPreview": False},
            )
        except httpx.TransportError:
            raise DeliveryError("transport", retry=True) from None
        if not 200 <= response.status_code < 300:
            raise DeliveryError(
                f"http_{response.status_code}",
                retry=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            message_id = response.json()["messageId"]
        except (ValueError, KeyError, TypeError):
            raise DeliveryError("invalid_response") from None
        if not isinstance(message_id, str) or not message_id:
            raise DeliveryError("invalid_response")
        return message_id

    async def close(self):
        await self.client.aclose()
