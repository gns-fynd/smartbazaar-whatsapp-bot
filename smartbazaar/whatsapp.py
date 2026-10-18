"""Meta transport. No message bodies, tokens or phone numbers are logged."""

import base64
import binascii
import hashlib
import hmac
import re
from collections.abc import Mapping
from urllib.parse import urlparse

import httpx

from .cart_image import build_collage, fetch_images
from .media import media_reference

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
_FILENAME = re.compile(r"[A-Za-z0-9 ._-]{1,120}\.pdf")


class DeliveryError(Exception):
    def __init__(self, code: str, retry: bool = False):
        self.code, self.retry = code, retry
        super().__init__(code)


def valid_signature(raw: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


class WhatsApp:
    supports_interactive = True

    def __init__(self, token: str, phone_id: str, version: str, transport=None, image_transport=None):
        self.image_transport = image_transport
        self.client = httpx.AsyncClient(
            base_url=f"https://graph.facebook.com/{version}/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
            transport=transport,
        )
        self.phone_id = phone_id

    async def acknowledge(self, message_id: str) -> None:
        """Blue ticks plus a typing indicator while the bot works; best effort, never raises."""
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 200:
            return
        try:
            await self.client.post(f"{self.phone_id}/messages", json={
                "messaging_product": "whatsapp", "status": "read", "message_id": message_id,
                "typing_indicator": {"type": "text"},
            })
        except httpx.HTTPError:
            return

    async def send(self, recipient: str, message: str | Mapping) -> str:
        if isinstance(message, Mapping) and message.get("type") == "cart_card":
            message = await self._cart_card(message)
        elif isinstance(message, Mapping) and message.get("type") == "choice_card":
            try:
                message = await self._choice_card(message)
            except DeliveryError:
                # Pictures are a nicety; the tap menu still lets the customer choose.
                if not isinstance(message.get("list"), Mapping):
                    raise
                message = message["list"]
        elif isinstance(message, Mapping) and message.get("type") == "document":
            message = await self._document(message)
        try:
            payload = self._payload(recipient, message)
        except ValueError:
            raise DeliveryError("invalid_payload") from None
        try:
            response = await self.client.post(
                f"{self.phone_id}/messages",
                json=payload,
            )
        except httpx.TransportError:
            raise DeliveryError("transport", retry=True) from None
        if response.status_code != 200:
            raise DeliveryError(
                f"http_{response.status_code}",
                retry=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            return response.json()["messages"][0]["id"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise DeliveryError("invalid_response") from None

    async def _cart_card(self, message: Mapping) -> dict:
        """Build the cart picture, upload it to WhatsApp, and turn the card into a sendable message."""
        items = [item for item in message.get("items", []) if isinstance(item, Mapping)][:9]
        if not items or not any(item.get("image_url") for item in items):
            raise DeliveryError("no_cart_images")
        images = await fetch_images([str(item.get("image_url") or "") for item in items], self.image_transport)
        if not any(images):
            raise DeliveryError("cart_images_unavailable")
        picture = build_collage(images, [int(item.get("quantity") or 1) for item in items])
        media_id = await self._upload(picture, "image/jpeg", "cart.jpg")
        return {"type": "image_buttons", "media_id": media_id, "body": message.get("body"),
                "buttons": message.get("buttons") or [], "footer": message.get("footer")}

    async def _choice_card(self, message: Mapping) -> dict:
        """Product options as one numbered picture, with a tap button per number."""
        items = [item for item in message.get("items", []) if isinstance(item, Mapping)][:3]
        if not items:
            raise DeliveryError("no_choice_images")
        images = await fetch_images([str(item.get("image_url") or "") for item in items], self.image_transport)
        if not all(images):
            raise DeliveryError("choice_images_unavailable")
        picture = build_collage(images, [1] * len(images), labels=[str(i) for i in range(1, len(images) + 1)])
        media_id = await self._upload(picture, "image/jpeg", "choices.jpg")
        return {"type": "image_buttons", "media_id": media_id, "body": message.get("body"),
                "buttons": message.get("buttons") or []}

    async def _document(self, message: Mapping) -> dict:
        """Upload a PDF the bot built or fetched (invoice, order summary) and send it as a file."""
        filename, data = message.get("filename"), message.get("data")
        if not isinstance(filename, str) or not _FILENAME.fullmatch(filename) or not isinstance(data, str):
            raise DeliveryError("invalid_document")
        try:
            content = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise DeliveryError("invalid_document") from None
        if not content.startswith(b"%PDF") or len(content) > MAX_DOCUMENT_BYTES:
            raise DeliveryError("invalid_document")
        media_id = await self._upload(content, "application/pdf", filename)
        caption = message.get("caption")
        return {"type": "document_id", "media_id": media_id, "filename": filename,
                "caption": caption[:1024] if isinstance(caption, str) else None}

    async def _upload(self, content: bytes, mime: str, filename: str) -> str:
        try:
            response = await self.client.post(
                f"{self.phone_id}/media",
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (filename, content, mime)},
            )
        except httpx.TransportError:
            raise DeliveryError("transport", retry=True) from None
        media_id = response.json().get("id") if response.status_code == 200 else None
        if not isinstance(media_id, str):
            raise DeliveryError(f"media_http_{response.status_code}")
        return media_id

    @staticmethod
    def _image_buttons_payload(common: dict, message: Mapping) -> dict:
        body, buttons, media_id = message.get("body"), message.get("buttons"), message.get("media_id")
        if not isinstance(body, str) or not 1 <= len(body) <= 1024 or not isinstance(media_id, str):
            raise ValueError("Invalid image card")
        if not buttons:
            return {**common, "type": "image", "image": {"id": media_id, "caption": body}}
        interactive = WhatsApp._quick_reply_payload(common, {"body": body, "buttons": buttons,
                                                             "footer": message.get("footer")})
        interactive["interactive"]["header"] = {"type": "image", "image": {"id": media_id}}
        return interactive

    @staticmethod
    def _payload(recipient: str, message: str | Mapping) -> dict:
        common = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
        }
        if isinstance(message, str):
            return {**common, "type": "text", "text": {"preview_url": False, "body": message[:4000]}}
        message_type = message.get("type")
        if message_type == "quick_reply":
            return WhatsApp._quick_reply_payload(common, message)
        if message_type == "cta_url":
            return WhatsApp._cta_url_payload(common, message)
        if message_type == "list":
            return WhatsApp._list_payload(common, message)
        if message_type == "image_buttons":
            return WhatsApp._image_buttons_payload(common, message)
        if message_type == "document_id":
            document = {"id": message.get("media_id"), "filename": message.get("filename")}
            if not isinstance(document["id"], str) or not isinstance(document["filename"], str):
                raise ValueError("Invalid document")
            if message.get("caption"):
                document["caption"] = message["caption"]
            return {**common, "type": "document", "document": document}
        if message_type != "product_card":
            raise ValueError("Unsupported message")
        image_url = message.get("image_url")
        body = message.get("body")
        footer = message.get("footer")
        buttons = message.get("buttons")
        parsed = urlparse(image_url) if isinstance(image_url, str) else None
        if (
            not parsed
            or parsed.scheme != "https"
            or parsed.hostname != "cdn1.jiomartjcp.com"
            or parsed.username
            or parsed.password
            or not parsed.path
            or len(image_url) > 2048
            or not isinstance(body, str)
            or not 1 <= len(body) <= 1024
            or not isinstance(footer, str)
            or len(footer) > 60
            or not isinstance(buttons, list)
            or not 1 <= len(buttons) <= 3
        ):
            raise ValueError("Invalid product card")
        replies = []
        for button in buttons:
            if not isinstance(button, Mapping):
                raise ValueError("Invalid button")
            button_id, title = button.get("id"), button.get("title")
            if (
                not isinstance(button_id, str)
                or not 1 <= len(button_id) <= 256
                or not isinstance(title, str)
                or not 1 <= len(title) <= 20
            ):
                raise ValueError("Invalid button")
            replies.append({"type": "reply", "reply": {"id": button_id, "title": title}})
        return {
            **common,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "header": {"type": "image", "image": {"link": image_url}},
                "body": {"text": body},
                "footer": {"text": footer},
                "action": {"buttons": replies},
            },
        }

    @staticmethod
    def _list_payload(common: dict, message: Mapping) -> dict:
        """A tap-able menu of up to 10 rows (product choices)."""
        body, label, rows = message.get("body"), message.get("button"), message.get("rows")
        if (not isinstance(body, str) or not 1 <= len(body) <= 1024 or not isinstance(label, str)
                or not 1 <= len(label) <= 20 or not isinstance(rows, list) or not 1 <= len(rows) <= 10):
            raise ValueError("Invalid list")
        clean = []
        for row in rows:
            if (not isinstance(row, Mapping) or not isinstance(row.get("id"), str) or not 1 <= len(row["id"]) <= 200
                    or not isinstance(row.get("title"), str) or not 1 <= len(row["title"]) <= 24):
                raise ValueError("Invalid list row")
            entry = {"id": row["id"], "title": row["title"]}
            if isinstance(row.get("description"), str) and row["description"]:
                entry["description"] = row["description"][:72]
            clean.append(entry)
        return {**common, "type": "interactive", "interactive": {
            "type": "list", "body": {"text": body},
            "action": {"button": label, "sections": [{"title": "Options", "rows": clean}]},
        }}

    @staticmethod
    def _cta_url_payload(common: dict, message: Mapping) -> dict:
        """One link button (e.g. "Pay ₹74"); only HTTPS URLs without credentials."""
        body, label, url = message.get("body"), message.get("button"), message.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if (
            not isinstance(body, str) or not 1 <= len(body) <= 1024
            or not isinstance(label, str) or not 1 <= len(label) <= 20
            or not parsed or parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or len(url) > 2000
        ):
            raise ValueError("Invalid link button")
        return {**common, "type": "interactive", "interactive": {
            "type": "cta_url",
            "body": {"text": body},
            "action": {"name": "cta_url", "parameters": {"display_text": label, "url": url}},
        }}

    @staticmethod
    def _quick_reply_payload(common: dict, message: Mapping) -> dict:
        body = message.get("body")
        footer = message.get("footer")
        buttons = message.get("buttons")
        if (
            not isinstance(body, str)
            or not 1 <= len(body) <= 1024
            or footer is not None
            and (not isinstance(footer, str) or len(footer) > 60)
            or not isinstance(buttons, list)
            or not 1 <= len(buttons) <= 3
        ):
            raise ValueError("Invalid quick reply")

        replies = []
        ids, titles = set(), set()
        for button in buttons:
            if not isinstance(button, Mapping):
                raise ValueError("Invalid button")
            button_id, title = button.get("id"), button.get("title")
            if (
                not isinstance(button_id, str)
                or not 1 <= len(button_id) <= 256
                or not isinstance(title, str)
                or not 1 <= len(title) <= 20
                or button_id in ids
                or title in titles
            ):
                raise ValueError("Invalid button")
            ids.add(button_id)
            titles.add(title)
            replies.append({"type": "reply", "reply": {"id": button_id, "title": title}})

        interactive = {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": replies},
        }
        if footer is not None:
            interactive["footer"] = {"text": footer}
        return {**common, "type": "interactive", "interactive": interactive}

    async def close(self):
        await self.client.aclose()


def inbound_media_reference(message):
    """Expose only a validated attachment ID from an already-signed Meta message."""
    return media_reference(message)
