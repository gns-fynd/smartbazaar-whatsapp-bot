"""Isolated consumer auth, catalog, history, and cart adapter.

The endpoints in this module follow a limited, observed contract. Cart writes are
gated off by default, as are checkout and the payment session (see checkout.py).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from base64 import b64encode, urlsafe_b64decode
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

log = logging.getLogger("smartbazaar")


class CommerceError(Exception):
    """A customer-safe commerce failure.

    ``message`` is deliberately generic; response bodies can contain account and
    session information and must never be surfaced through this exception.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "unknown",
        http_status: int | None = None,
        diagnostic_code: str = "unknown",
        upstream: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = self._safe_label(stage)
        self.http_status = http_status if type(http_status) is int else None
        self.diagnostic_code = self._safe_label(diagnostic_code)
        # Provider text is kept only for commerce mutations, never login/session stages.
        self.upstream = (
            self.safe_hint(upstream)
            if self.stage.startswith(self._HINT_STAGES) and not self.stage.endswith("_session") else ""
        )
        log.warning(
            "commerce_failed stage=%s status=%s code=%s" + (" upstream=%s" if self.upstream else "%s"),
            self.stage,
            self.http_status if self.http_status is not None else "none",
            self.diagnostic_code,
            self.upstream,
        )

    _HINT_STAGES = ("cart_write", "checkout", "payment", "select_address", "product_sellers", "cancel_shipment")

    @staticmethod
    def _safe_label(value: object) -> str:
        value = str(value)
        return value if re.fullmatch(r"[a-z0-9_]{1,40}", value) else "unknown"

    @staticmethod
    def safe_hint(value: object) -> str:
        """Bound an upstream error message for operator logs: no long digit runs or symbols."""
        if not isinstance(value, str):
            return ""
        text = re.sub(r"\d{4,}", "#", value)
        text = re.sub(r"[^A-Za-z0-9 .,:;'()/_-]", " ", text)
        return re.sub(r"\s+", " ", text).strip()[:120]


class JioMartClient:
    """JioMart consumer API adapter with no shared cookie jar.

    A new HTTP client is made for every call.  Cookie material is copied from the
    supplied challenge/session dictionary into that client and copied back before
    it closes. Callers are responsible for encrypting those mutable dictionaries
    at rest. Cart writes and checkout each require an explicit constructor gate.
    """

    API_ORIGIN = "https://api.jiomartjcp.com"
    STOREFRONT_ORIGIN = "https://www.jiomart.com"
    _ALLOWED_HOSTS = frozenset({"api.jiomartjcp.com", "www.jiomart.com"})
    _TIMEOUT = httpx.Timeout(10.0, connect=5.0)
    _LOGIN_PATH = "/service/application/user/authentication/v1.0/login/otp"
    _VERIFY_PATH = "/service/application/user/authentication/v1.0/otp/mobile/verify"
    _SESSION_PATH = "/service/application/user/authentication/v1.0/session"
    _REFRESH_PATH = "/service/application/user/authentication/v1.0/token/refresh"
    # Login stages run before a session exists; there is nothing to refresh there.
    _NO_REFRESH_STAGES = ("begin_login", "verify_login", "verify_session", "token_refresh", "app_token")
    # JioMart's own storefront refreshes proactively once under two minutes of life remain;
    # a slightly wider bound here absorbs this bot's own request latency.
    _PROACTIVE_REFRESH_WINDOW = 300
    _ADDRESS_PATH = "/service/application/cart/v1.0/address"
    _CART_DETAIL_PATH = "/service/application/cart/v1.0/detail"
    _SELECT_ADDRESS_PATH = "/service/application/cart/v1.0/select-address"
    _CART_META_PATH = "/service/application/cart/v1.0/meta"
    _PAYMENT_OPTIONS_PATH = "/service/application/payment/v1.0/payment/options"
    _CHECKOUT_PATH = "/service/application/cart/v2.0/checkout"
    # JioMart's own order-status page; COD orders are created without visiting it.
    _CHECKOUT_CALLBACK = "https://www.jiomart.com/cart/order-status"
    _ORDER_PATH = "/service/application/order/v1.0/orders"
    # Shipment status transitions (customer cancellation) live under order-manage, not order.
    _ORDER_MANAGE_SHIPMENTS_PATH = "/service/application/order-manage/v1.0/orders/shipments"
    _PRODUCT_SIZES_PATH = "/api/service/application/catalog/v2.0/products"
    _PRODUCT_SELLERS_PATH = "/api/service/application/catalog/v4.0/products"
    _LEGACY_CONSUMER_APPLICATION_ID = "685945f46c8c7aee3f3af605"
    _SEARCH_PATH = "/api/service/application/catalog/v1.0/products"
    # Documented customer cancellation status; see getShipmentReasons/updateShipmentStatus.
    _CANCEL_STATUS = "cancelled_customer"
    _INVOICE_MAX_BYTES = 5 * 1024 * 1024
    # Invoice files are first-party JioMart/Fynd platform documents; presigned links usually sit on
    # the platform's CDN rather than jiomart.com (unverified live; anything else is refused).
    _INVOICE_HOSTS = ("jiomart.com", "jiomartjcp.com", "fynd.com", "pixelbin.io", "jiocommerce.io")

    def __init__(
        self,
        application_id: str,
        application_token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        enable_cart_writes: bool = False,
        enable_checkout: bool = False,
        enable_order_cancel: bool = False,
    ) -> None:
        if not application_id.strip():
            raise ValueError("application_id is required")
        self.application_id = application_id
        self._application_token = application_token
        self._transport = transport
        self._enable_cart_writes = enable_cart_writes
        self._enable_checkout = enable_cart_writes and enable_checkout
        self._enable_order_cancel = enable_order_cancel

    @property
    def order_cancel_enabled(self) -> bool:
        return self._enable_order_cancel

    async def close(self) -> None:
        """Compatibility hook; clients are deliberately short lived."""

    async def begin_login(self, mobile: str) -> dict[str, Any]:
        mobile = self._normalise_mobile(mobile)
        if not mobile:
            raise ValueError("mobile is required")
        async with self._client() as client:
            response = await self._request(
                client,
                "POST",
                self._api_url(self._LOGIN_PATH),
                stage="begin_login",
                params={"platform": self.application_id},
                json={"mobile": mobile, "country_code": "91", "action": "login"},
            )
            payload = self._json(response, "begin_login")
            if not self._success(payload):
                raise CommerceError(
                    "unavailable",
                    "Unable to start sign-in.",
                    stage="begin_login",
                    http_status=response.status_code,
                    diagnostic_code="response_contract",
                )
            request_id = self._find(payload, "request_id", "requestId", "request_reference")
            if not isinstance(request_id, str) or not request_id:
                raise CommerceError(
                    "unavailable",
                    "Unable to start sign-in.",
                    stage="begin_login",
                    http_status=response.status_code,
                    diagnostic_code="response_contract",
                )
            return {
                "request_id": request_id,
                "mobile": mobile,
                "cookies": self._dump_cookies(client.cookies),
            }

    async def verify_login(self, challenge: dict[str, Any], otp: str) -> dict[str, Any]:
        mobile = self._normalise_mobile(str(challenge.get("mobile", "")))
        request_id = challenge.get("request_id")
        if not mobile or not isinstance(request_id, str) or not request_id or not otp:
            raise ValueError("a valid login challenge and OTP are required")

        async with self._client(challenge.get("cookies")) as client:
            response = await self._request(
                client,
                "POST",
                self._api_url(self._VERIFY_PATH),
                stage="verify_login",
                params={"platform": self.application_id},
                json={
                    "action": "login",
                    "otp": otp,
                    "request_id": request_id,
                },
                error_code="invalid_otp",
            )
            payload = self._json(response, "verify_login")
            if not self._verified_login_response(payload):
                raise CommerceError(
                    "invalid_otp",
                    "The code could not verify this existing account.",
                    stage="verify_login",
                    http_status=response.status_code,
                    diagnostic_code="response_contract",
                )

            # OTP verification establishes the authenticated cookie. Refresh is an
            # expired-token operation with a separate body/header contract, so it
            # must not be inserted into a newly established login sequence.
            session_response = await self._request(
                client, "GET", self._api_url(self._SESSION_PATH), stage="verify_session"
            )
            session_payload = self._json(session_response, "verify_session")
            session_user = self._user_response(session_payload)
            if session_user is None:
                raise CommerceError(
                    "auth_expired",
                    "Sign-in has expired. Please try again.",
                    stage="verify_session",
                    http_status=session_response.status_code,
                    diagnostic_code="response_contract",
                )
            if not self._has_matching_mobile(session_user, mobile):
                raise CommerceError(
                    "invalid_otp",
                    "The code could not verify this existing account.",
                    stage="verify_session",
                    http_status=session_response.status_code,
                    diagnostic_code="identity_mismatch",
                )
            returned_application = self._find(session_user, "application_id", "applicationId")
            if (
                returned_application is not None
                and str(returned_application) != self.application_id
            ):
                raise CommerceError(
                    "invalid_otp",
                    "The code could not verify this existing account.",
                    stage="verify_session",
                    http_status=session_response.status_code,
                    diagnostic_code="application_mismatch",
                )
            account_reference = self._find(session_user, "user_id", "userId", "uid", "_id", "id")
            session = {
                "mobile": mobile,
                "application_id": self.application_id,
                "account_reference": str(account_reference)
                if account_reference is not None
                else "",
                "cookies": self._dump_cookies(client.cookies),
            }
            tokens = self._session_tokens(payload, session_payload)
            if tokens:
                session["tokens"] = tokens
            return session

    async def get_addresses(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        async with self._client(session.get("cookies")) as client:
            response = await self._request(
                client, "GET", self._api_url(self._ADDRESS_PATH), stage="addresses"
            )
            payload = self._json(response, "addresses")
            self._save_cookies(session, client.cookies)
        records = self._list_from(payload, "address", "items", "addresses")
        return [self._normalise_address(item) for item in records if isinstance(item, Mapping)]

    async def search(
        self, query: str, location: dict[str, Any], session: dict[str, Any]
    ) -> dict[str, Any]:
        location_detail = self._location_detail(location)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query is required")
        params = {"q": query.strip(), "page_size": "50", "page_no": "1"}
        headers = {
            "x-location-detail": json.dumps(location_detail, separators=(",", ":")),
            "x-currency": "INR",
        }
        async with self._client(session.get("cookies")) as client:
            response = await self._request(
                client,
                "GET",
                self._storefront_url(self._SEARCH_PATH),
                stage="search",
                params=params,
                headers=headers,
                follow_redirects=False,
                error_code="invalid_location",
            )
            if response.is_redirect:
                redirect_url = self._safe_redirect(response)
                response = await self._request(
                    client,
                    "GET",
                    redirect_url,
                    stage="search_redirect",
                    headers=headers,
                    follow_redirects=False,
                    error_code="invalid_location",
                )
            payload = self._json(response, "search")
            self._save_cookies(session, client.cookies)
        items = self._list_from(payload, "items")
        page = payload.get("page") if isinstance(payload.get("page"), Mapping) else {}
        return {"items": items, "page": dict(page), "coverage": "partial"}

    async def get_purchase_history(self, session: dict[str, Any]) -> dict[str, Any]:
        """Return at most 50 delivered-item evidence records for this session.

        An empty order response is deliberately unresolved: the consumer history
        endpoint was observed returning an empty page for an account with unknown
        history visibility, so it must not become a "no purchases" preference.
        """
        try:
            async with self._client(session.get("cookies")) as client:
                response = await self._request(
                    client,
                    "GET",
                    self._api_url(self._ORDER_PATH),
                    stage="history",
                    # JioMart's My Orders page always sends status=1; without it the list is empty.
                    params={"page_no": "1", "page_size": "12", "status": "1"},
                )
                payload = self._json(response, "history")
                orders, page = self._order_list_payload(payload, response.status_code)
                partial = bool(page["has_next"])
                if len(orders) > 20:
                    orders = orders[:20]
                    partial = True
                hydrated: list[dict[str, Any]] = []
                detail_hydrations = 0
                for order in orders:
                    if isinstance(order.get("shipments"), list):
                        hydrated.append(order)
                        continue
                    order_id = self._safe_id(order.get("order_id"))
                    if detail_hydrations >= 3:
                        partial = True
                        hydrated.append(order)
                        continue
                    if not order_id:
                        partial = True
                        hydrated.append(order)
                        continue
                    try:
                        detail_hydrations += 1
                        detail = await self._request(
                            client,
                            "GET",
                            self._api_url(f"{self._ORDER_PATH}/{quote(order_id, safe='')}"),
                            stage="history_detail",
                        )
                        detail_payload = self._json(detail, "history_detail")
                        hydrated_order = self._order_detail_payload(detail_payload, detail.status_code)
                    except CommerceError as exc:
                        if exc.code == "auth_expired":
                            raise
                        partial = True
                        hydrated.append(order)
                    else:
                        hydrated.append(hydrated_order)
                self._save_cookies(session, client.cookies)
        except CommerceError as exc:
            if exc.code == "auth_expired":
                return {"status": "unavailable", "items": []}
            raise

        if not orders:
            return {"status": "unresolved", "items": []}
        items: list[dict[str, Any]] = []
        for order in hydrated:
            evidence, incomplete = self._delivered_order_items(order)
            partial = partial or incomplete
            items.extend(evidence)
            if len(items) >= 50:
                partial = partial or len(items) > 50
                break
        items.sort(key=lambda item: (item["purchased_at"] is not None, item["purchased_at"] or 0), reverse=True)
        return {"status": "partial" if partial else "available", "items": items[:50]}

    async def list_orders(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the first My Orders page as display-safe summaries, newest first."""
        params = {"page_no": "1", "page_size": "12", "status": "1"}
        async with self._client(session.get("cookies")) as client:
            response = await self._request(client, "GET", self._api_url(self._ORDER_PATH), stage="orders", params=params)
            payload = self._json(response, "orders")
            orders, page = self._order_list_payload(payload, response.status_code)
            if not orders:
                # JioMart's own My Orders page reads through the storefront gateway.
                fallback = await self._storefront_order_get(client, self._ORDER_PATH, "orders", params)
                if fallback is not None and fallback.status_code == 200:
                    alt_orders, alt_page = self._order_list_payload(self._json(fallback, "orders"), fallback.status_code)
                    if alt_orders:
                        orders, page = alt_orders, alt_page
                        log.info("orders_source host=storefront")
            self._save_cookies(session, client.cookies)
        log.info("orders_listed count=%s has_next=%s", len(orders), page["has_next"])
        if orders:
            self._log_order_shape(orders[0])
        summaries = [summary for order in orders[:12] if (summary := self._order_summary(order))]
        return sorted(summaries, key=lambda order: order["created_at"] or 0, reverse=True)

    async def get_order_detail(self, session: dict[str, Any], order_id: str) -> dict[str, Any]:
        """Read one order of the signed-in account as a display-safe summary."""
        safe_order = self._safe_id(order_id)
        if not safe_order:
            raise ValueError("order id is invalid")
        path = f"{self._ORDER_PATH}/{quote(safe_order, safe='')}"
        async with self._client(session.get("cookies")) as client:
            try:
                response = await self._request(client, "GET", self._api_url(path), stage="order_status")
            except CommerceError as exc:
                if exc.http_status != 404:
                    raise
                # Orders JioMart's API host doesn't know may still be served via the storefront gateway.
                response = await self._storefront_order_get(client, path, "order_status", None)
                if response is None or response.status_code != 200:
                    raise
                log.info("order_status_source host=storefront")
            payload = self._json(response, "order_status")
            self._save_cookies(session, client.cookies)
        order = self._order_detail_payload(payload, response.status_code)
        summary = self._order_summary(order)
        if summary is None:
            raise self._contract_error("order_status", response.status_code)
        return summary

    async def get_order(self, session: dict[str, Any], order_id: str) -> dict[str, Any]:
        """Short status for an order this chat placed."""
        order = await self.get_order_detail(session, order_id)
        return {"status": order["status"] or "placed", "detail": ""}

    async def track_shipment(self, session: dict[str, Any], shipment_id: str) -> dict[str, Any]:
        """Courier tracking for one shipment: courier, AWB, latest status and time only."""
        safe_shipment = self._safe_id(shipment_id)
        if not safe_shipment:
            raise ValueError("shipment id is invalid")
        async with self._client(session.get("cookies")) as client:
            response = await self._request(
                client, "GET",
                self._api_url(f"{self._ORDER_PATH}/shipments/{quote(safe_shipment, safe='')}/track"),
                stage="track",
            )
            payload = self._json(response, "track")
            self._save_cookies(session, client.cookies)
        results = payload.get("results")
        if self._failure_envelope(payload) or not isinstance(results, list):
            raise self._contract_error("track", response.status_code)
        latest = next((r for r in results if isinstance(r, Mapping)), {})
        return {
            "courier": self._label(latest.get("account_name")),
            "awb": self._safe_id(latest.get("awb")) or None,
            "status": self._label(latest.get("status") or latest.get("raw_status")),
            "updated_at": self._epoch(self._bounded_text(self._first_value(latest, "updated_time", "updated_at"), 64)),
        }

    def _require_order_cancel(self, stage: str) -> None:
        if not self._enable_order_cancel:
            raise CommerceError(
                "unavailable", "Cancellation is not enabled.", stage=stage, diagnostic_code="order_cancel_disabled"
            )

    async def cancellation_reasons(self, session: dict[str, Any], shipment_id: str) -> list[dict[str, Any]]:
        """Reasons JioMart offers for a customer-initiated shipment cancellation.

        Documented contract: ``GET order/v1.0/orders/shipments/{shipment_id}/reasons``
        (``getShipmentReasons``). Bag-level reasons (``getShipmentBagReasons``) are out of
        scope: this adapter only cancels a whole shipment, never one line within it.
        """
        self._require_order_cancel("cancel_reasons")
        safe_shipment = self._safe_id(shipment_id)
        if not safe_shipment:
            raise ValueError("shipment id is invalid")
        async with self._client(session.get("cookies")) as client:
            response = await self._request(
                client, "GET",
                self._api_url(f"{self._ORDER_PATH}/shipments/{quote(safe_shipment, safe='')}/reasons"),
                stage="cancel_reasons",
            )
            payload = self._json(response, "cancel_reasons")
            self._save_cookies(session, client.cookies)
        if self._failure_envelope(payload) or not isinstance(payload.get("reasons"), list):
            raise self._contract_error("cancel_reasons", response.status_code)
        reasons: list[dict[str, Any]] = []
        for entry in payload["reasons"][:20]:
            if not isinstance(entry, Mapping):
                continue
            reason_id = entry.get("reason_id")
            text = self._bounded_text(entry.get("reason_text"), 160)
            if type(reason_id) is int and text:
                reasons.append({"id": reason_id, "text": text})
        return reasons

    async def cancel_shipment(
        self, session: dict[str, Any], shipment_id: str, *, reason_id: int, reason_text: str
    ) -> dict[str, Any]:
        """Customer-initiated full-shipment cancellation (documented ``updateShipmentStatus``).

        Transitions the whole shipment to ``cancelled_customer``; partial/bag-level
        cancellation is out of scope. Raises ``CommerceError`` with code ``rejected`` when
        JioMart definitely refused, or code ``unknown`` on a transport/5xx failure where the
        outcome cannot be determined -- callers must never retry an ``unknown`` outcome
        automatically. Callers should re-read the order/shipment afterward; this return
        value is acceptance evidence only.
        """
        self._require_order_cancel("cancel_shipment")
        safe_shipment = self._safe_id(shipment_id)
        if not safe_shipment:
            raise ValueError("shipment id is invalid")
        if type(reason_id) is bool or not isinstance(reason_id, int):
            raise ValueError("reason id is invalid")
        safe_reason_text = self._bounded_text(reason_text, 300)
        if not safe_reason_text:
            raise ValueError("reason text is required")
        body = {
            "statuses": [{
                "shipments": [{
                    "identifier": safe_shipment,
                    "status": self._CANCEL_STATUS,
                    "reasons": {"entities": [{"data": {"reason_id": reason_id, "reason_text": safe_reason_text}}]},
                }],
            }],
        }
        async with self._client(session.get("cookies")) as client:
            try:
                response = await self._account_request(
                    client, "PUT",
                    self._api_url(f"{self._ORDER_MANAGE_SHIPMENTS_PATH}/{quote(safe_shipment, safe='')}/status"),
                    stage="cancel_shipment", error_code="cancel_rejected", json=body,
                )
            except CommerceError as exc:
                if exc.diagnostic_code in {"transport_error", "http_5xx"}:
                    # The request may or may not have reached JioMart; never retry blindly.
                    raise CommerceError(
                        "unknown", "JioMart did not confirm the cancellation.", stage="cancel_shipment",
                        http_status=exc.http_status, diagnostic_code=exc.diagnostic_code,
                    ) from exc
                raise
            payload = self._json(response, "cancel_shipment")
            self._save_cookies(session, client.cookies)
        if self._failure_envelope(payload):
            # A definite rejection: nothing changed on JioMart's side.
            raise CommerceError(
                "rejected", "JioMart could not cancel that shipment.", stage="cancel_shipment",
                http_status=response.status_code, diagnostic_code="cancel_rejected",
                upstream=self._first_text(payload, "message", "error_code"),
            )
        return {"shipment_id": safe_shipment, "status": self._CANCEL_STATUS}

    async def invoice_pdf(self, session: dict[str, Any], shipment_id: str) -> dict[str, Any]:
        """Download one shipment's invoice PDF (documented ``getInvoiceByShipmentId``).

        JioMart generates an invoice only once a shipment is invoiced/packed; until then
        this raises ``CommerceError`` with code ``invoice_unavailable``. The returned URL is
        validated (HTTPS, JioMart host only) before it is fetched, and the download is
        bounded to 5 MB and must look like a PDF (content-type or ``%PDF`` magic).
        """
        safe_shipment = self._safe_id(shipment_id)
        if not safe_shipment:
            raise ValueError("shipment id is invalid")
        async with self._client(session.get("cookies")) as client:
            try:
                response = await self._request(
                    client, "GET",
                    self._api_url(f"{self._ORDER_PATH}/shipments/{quote(safe_shipment, safe='')}/invoice"),
                    stage="invoice",
                )
            except CommerceError as exc:
                if exc.http_status == 404:
                    raise CommerceError(
                        "unavailable", "The invoice is not ready yet.", stage="invoice",
                        http_status=404, diagnostic_code="invoice_unavailable",
                    ) from exc
                raise
            payload = self._json(response, "invoice")
            url = self._first_text(payload, "presigned_url", "url")
            if self._failure_envelope(payload) or not url:
                raise CommerceError(
                    "unavailable", "The invoice is not ready yet.", stage="invoice",
                    http_status=response.status_code, diagnostic_code="invoice_unavailable",
                )
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not any(host == h or host.endswith("." + h) for h in self._INVOICE_HOSTS):
                raise CommerceError(
                    "unavailable", "Unable to fetch the invoice.", stage="invoice", diagnostic_code="invoice_host_blocked"
                )
            try:
                async with client.stream("GET", url, follow_redirects=False) as pdf_response:
                    if pdf_response.status_code != 200:
                        raise CommerceError(
                            "unavailable", "Unable to fetch the invoice.", stage="invoice",
                            http_status=pdf_response.status_code, diagnostic_code="invoice_download_failed",
                        )
                    content_type = pdf_response.headers.get("content-type", "").casefold()
                    length = pdf_response.headers.get("content-length")
                    if length and (not length.isdecimal() or int(length) > self._INVOICE_MAX_BYTES):
                        raise CommerceError(
                            "unavailable", "The invoice file is too large.", stage="invoice",
                            diagnostic_code="invoice_too_large",
                        )
                    chunks, size = [], 0
                    async for chunk in pdf_response.aiter_bytes():
                        size += len(chunk)
                        if size > self._INVOICE_MAX_BYTES:
                            raise CommerceError(
                                "unavailable", "The invoice file is too large.", stage="invoice",
                                diagnostic_code="invoice_too_large",
                            )
                        chunks.append(chunk)
            except httpx.HTTPError as exc:
                raise CommerceError(
                    "unavailable", "JioMart is unavailable. Please try again.", stage="invoice",
                    diagnostic_code="transport_error",
                ) from exc
            self._save_cookies(session, client.cookies)
        content = b"".join(chunks)
        if not content or ("pdf" not in content_type and not content.startswith(b"%PDF")):
            raise CommerceError(
                "unavailable", "The invoice is not a valid document.", stage="invoice", diagnostic_code="invoice_not_pdf"
            )
        return {"filename": f"invoice_{safe_shipment}.pdf", "content": content}

    @classmethod
    def _order_summary(cls, order: Mapping[str, Any]) -> dict[str, Any] | None:
        order_id = cls._safe_id(order.get("order_id"))
        if not order_id:
            return None
        created = cls._bounded_text(
            str(cls._first_value(order, "order_created_ts", "order_created_time", "order_date") or ""), 64
        )
        raw_shipments = order.get("shipments") if isinstance(order.get("shipments"), list) else []
        shipments = [s for item in raw_shipments[:20] if (s := cls._shipment_summary(item))]
        items = [item for shipment in shipments for item in shipment["items"]]
        statuses = [shipment["status"] for shipment in shipments if shipment["status"]]
        total = cls._breakup_total(order.get("breakup_values"))
        if total is None:
            paid = [s["amount"] for s in shipments if s["amount"] is not None]
            total = round(sum(paid), 2) if paid and len(paid) == len(shipments) else None
        payments = [order.get("payment")] + (order.get("payment_info") if isinstance(order.get("payment_info"), list) else [])
        payment_states = [
            state for p in payments[:5] if isinstance(p, Mapping)
            and (state := cls._safe_id(str(p.get("status") or "").strip().lower().replace(" ", "_")))
        ]
        modes = [cls._label(p.get("display_name") or p.get("mode")) for p in payments[:5] if isinstance(p, Mapping)]
        return {
            "order_id": order_id,
            "created_at": cls._epoch(created),
            "total": total,
            "payment_status": payment_states[0] if payment_states else None,
            "payment_mode": next((m for m in modes if m), None),
            "status": ", ".join(dict.fromkeys(statuses)) or None,
            "item_count": sum(item["quantity"] for item in items),
            "items": items[:50],
            "shipments": shipments,
            "complete": len(raw_shipments) == len(shipments),
        }

    @classmethod
    def _shipment_summary(cls, shipment: object) -> dict[str, Any] | None:
        if not isinstance(shipment, Mapping):
            return None
        status = shipment.get("shipment_status")
        title = cls._label(status.get("title") if isinstance(status, Mapping) else status)
        timeline = []
        for step in (shipment.get("tracking_details") or [])[:15] if isinstance(shipment.get("tracking_details"), list) else []:
            if isinstance(step, Mapping) and (label := cls._label(step.get("status") or step.get("value"))):
                timeline.append({
                    "status": label,
                    "time": cls._epoch(cls._bounded_text(str(step.get("time") or step.get("created_ts") or ""), 64)),
                    "passed": step.get("is_passed") is True,
                    "current": step.get("is_current") is True,
                })
        promise = shipment.get("promise")
        stamps = promise.get("timestamp") if isinstance(promise, Mapping) else None
        expected = None
        if isinstance(stamps, Mapping):
            expected = cls._epoch(cls._bounded_text(str(stamps.get("max") or stamps.get("min") or ""), 64))
        bags = shipment.get("bags") if isinstance(shipment.get("bags"), list) else []
        prices = shipment.get("prices") if isinstance(shipment.get("prices"), Mapping) else {}
        amount = cls._first_value(prices, "amount_paid", "price_effective", "amount_to_be_collected")
        raw_value = status.get("value") if isinstance(status, Mapping) else None
        # Documented shipment fields: ``can_cancel`` and ``show_download_invoice`` (both booleans);
        # ``invoice.invoice_url`` is a secondary, unverified signal for the same availability.
        invoice = shipment.get("invoice") if isinstance(shipment.get("invoice"), Mapping) else {}
        invoice_url = cls._bounded_text(invoice.get("invoice_url") or shipment.get("invoice_url"), 500)
        invoice_available = cls._tri_bool(shipment.get("show_download_invoice"))
        if invoice_available is None and invoice_url:
            invoice_available = True
        return {
            "shipment_id": cls._safe_id(shipment.get("shipment_id")) or None,
            "status": title,
            "value": cls._safe_id(raw_value.strip().lower()) or None if isinstance(raw_value, str) else None,
            "state": cls._shipment_status(shipment),
            "timeline": timeline,
            "expected_at": expected,
            "delivered_at": cls._epoch(cls._bounded_text(str(shipment.get("delivery_date") or ""), 64)),
            "items": [item for bag in bags[:50] if (item := cls._order_item(bag))],
            "amount": amount if isinstance(amount, (int, float)) and not isinstance(amount, bool) else None,
            "can_cancel": cls._tri_bool(shipment.get("can_cancel")),
            "invoice_available": invoice_available,
        }

    @classmethod
    def _order_item(cls, bag: object) -> dict[str, Any] | None:
        if not isinstance(bag, Mapping):
            return None
        nested = bag.get("item") if isinstance(bag.get("item"), Mapping) else {}
        name = cls._bounded_text(cls._first_value(nested, "name") or cls._first_value(bag, "item_name", "name"), 160)
        item_id = cls._safe_id(cls._first_value(nested, "id", "product_uid") or bag.get("product_uid"))
        quantity = bag.get("quantity")
        if not name or type(quantity) is not int or not 1 <= quantity <= 999:
            return None
        brand = nested.get("brand")
        prices = bag.get("prices") if isinstance(bag.get("prices"), Mapping) else {}
        price = cls._first_value(prices, "price_effective", "amount_paid")
        # Cancellation needs a bag identifier and a line/article identifier; documented
        # locations are ``prices.id`` (order item id) and ``item.code`` (SKU) respectively.
        identifiers = bag.get("identifiers") if isinstance(bag.get("identifiers"), Mapping) else {}
        identifier = (
            cls._safe_id(bag.get("identifier"))
            or cls._safe_id(nested.get("code"))
            or cls._safe_id(identifiers.get("sku_code"))
            or None
        )
        line_number = bag.get("line_number")
        return {
            "item_id": item_id or None,
            "name": name,
            "size": cls._bounded_text(nested.get("size") or bag.get("size"), 40) or None,
            "brand": cls._bounded_text(brand.get("name") if isinstance(brand, Mapping) else brand, 80) or None,
            "quantity": quantity,
            "price": price if isinstance(price, (int, float)) and not isinstance(price, bool) else None,
            "bag_id": cls._safe_id(prices.get("id")) or cls._safe_id(bag.get("bag_id")) or None,
            "line_number": line_number if type(line_number) is int and line_number >= 0 else None,
            "identifier": identifier,
        }

    @staticmethod
    def _breakup_total(values: object) -> float | None:
        if isinstance(values, list):
            for entry in values:
                if isinstance(entry, Mapping) and entry.get("name") in {"total", "amount_paid"}:
                    value = entry.get("value")
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        return value
        return None

    @staticmethod
    def _label(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip().replace("_", " ")
        return text[:1].upper() + text[1:] if re.fullmatch(r"[A-Za-z][A-Za-z ,/&()-]{0,59}", text) else None

    @staticmethod
    def _tri_bool(value: object) -> bool | None:
        """A strict boolean, or ``None`` when JioMart didn't send one."""
        return value if type(value) is bool else None

    _order_shape_logged = False

    @classmethod
    def _log_order_shape(cls, order: Mapping[str, Any]) -> None:
        """Log field names (never values) of one live order once, to keep parsing honest."""
        if cls._order_shape_logged:
            return
        cls._order_shape_logged = True
        shipment = next((s for s in order.get("shipments") or [] if isinstance(s, Mapping)), {})
        bag = next((b for b in shipment.get("bags") or [] if isinstance(b, Mapping)), {}) if isinstance(shipment.get("bags"), list) else {}
        item = bag.get("item") if isinstance(bag.get("item"), Mapping) else {}
        status = shipment.get("shipment_status")
        log.info(
            "orders_shape order=%s shipment=%s status=%s bag=%s item=%s",
            ",".join(sorted(map(str, order))[:60]), ",".join(sorted(map(str, shipment))[:60]),
            ",".join(sorted(map(str, status))) if isinstance(status, Mapping) else type(status).__name__,
            ",".join(sorted(map(str, bag))[:60]), ",".join(sorted(map(str, item))[:60]),
        )

    async def get_product_sizes(
        self, slug: str, session: dict[str, Any], location: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Read bounded, location-scoped product-size evidence without cart changes."""
        product_slug = self._safe_id(slug)
        if not product_slug:
            raise ValueError("product slug is invalid")
        headers = self._catalog_location_headers(location)
        async with self._client(session.get("cookies")) as client:
            response = await self._catalog_get(
                client,
                f"{self._PRODUCT_SIZES_PATH}/{quote(product_slug, safe='')}/sizes",
                stage="product_sizes",
                headers=headers,
            )
            payload = self._json(response, "product_sizes")
            self._save_cookies(session, client.cookies)
        if self._failure_envelope(payload) or not isinstance(payload.get("sizes"), list):
            raise self._contract_error("product_sizes", response.status_code)
        raw_sizes = payload["sizes"]
        sizes = [size for item in raw_sizes[:50] if (size := self._normalise_product_size(item))]
        return {
            "sellable": payload.get("sellable") if type(payload.get("sellable")) is bool else None,
            "sizes": sizes,
            "moq": self._normalise_moq(payload.get("moq")),
            "coverage": "partial" if len(raw_sizes) > 50 or len(sizes) != len(raw_sizes[:50]) else "available",
        }

    async def get_product_sellers(
        self, slug: str, size: str, session: dict[str, Any], location: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Read first-page seller/quantity/price evidence; no cart selection is made."""
        product_slug = self._safe_id(slug)
        product_size = self._safe_id(size)
        if not product_slug or not product_size:
            raise ValueError("product slug and size are required")
        detail = self._location_detail(location)
        headers = self._catalog_location_headers(location)
        async with self._client(session.get("cookies")) as client:
            response = await self._catalog_get(
                client,
                f"{self._PRODUCT_SELLERS_PATH}/{quote(product_slug, safe='')}/sizes/{quote(product_size, safe='')}/sellers",
                stage="product_sellers",
                headers=headers,
                params={"pincode": detail["pincode"], "strategy": "optimal", "page_no": "1", "page_size": "12"},
            )
            payload = self._json(response, "product_sellers")
            self._save_cookies(session, client.cookies)
        if self._failure_envelope(payload) or not isinstance(payload.get("items"), list):
            raise self._contract_error("product_sellers", response.status_code)
        page = self._mapping_from(payload, "page")
        if page is None or type(page.get("has_next")) is not bool:
            raise self._contract_error("product_sellers", response.status_code)
        raw_items = payload["items"]
        sellers = [
            seller
            for item in raw_items[:12]
            if (seller := self._normalise_product_seller(item, product_size))
        ]
        return {
            "items": sellers,
            "coverage": "partial" if page["has_next"] or len(raw_items) > 12 or len(sellers) != len(raw_items[:12]) else "available",
        }

    async def get_cart(
        self, session: dict[str, Any], location: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Read and sanitize the authenticated cart; this method never mutates it."""
        detail = self._location_detail(location)
        async with self._client(session.get("cookies")) as client:
            await self._verify_cart_session(client, session, "cart_session")
            response = await self._request(
                client,
                "GET",
                self._api_url(self._CART_DETAIL_PATH),
                stage="cart",
                params={
                    "i": "true",
                    "b": "true",
                    "c": "true",
                    "area_code": detail["pincode"],
                    "cart_type": "universal",
                    "order_type": "HomeDelivery",
                },
            )
            payload = self._json(response, "cart")
            cart = self._cart_payload(payload, response.status_code)
            self._save_cookies(session, client.cookies)
        return self._normalise_cart(cart)

    async def add_cart_items(
        self,
        session: dict[str, Any],
        location: Mapping[str, Any],
        items: list[Mapping[str, Any]],
        cart_id: str | None = None,
    ) -> dict[str, Any]:
        """Feature-gated, typed cart add shaped like JioMart's own reorder request."""
        self._require_writes("cart_write")
        return await self._write_cart(session, location, "POST", {"items": self._add_items(items)}, cart_id)

    async def update_cart(
        self,
        session: dict[str, Any],
        location: Mapping[str, Any],
        operation: str,
        items: list[Mapping[str, Any]],
        cart_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_writes("cart_write")
        if operation not in {"update_item", "remove_item"}:
            raise ValueError("unsupported cart operation")
        typed = self._typed_cart_items(items, require_existing=True, removal=operation == "remove_item")
        return await self._write_cart(session, location, "PUT", {"operation": operation, "items": typed}, cart_id)

    async def _write_cart(
        self,
        session: dict[str, Any],
        location: Mapping[str, Any],
        method: str,
        body: dict[str, Any],
        cart_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_writes("cart_write")
        detail = self._location_detail(location)
        params = {"i": "true", "b": "true", "area_code": detail["pincode"]}
        if safe_cart := self._safe_id(cart_id):
            params["id"] = safe_cart
        async with self._client(session.get("cookies")) as client:
            await self._verify_cart_session(client, session, "cart_write_session")
            response = await self._account_request(
                client, method, self._api_url(self._CART_DETAIL_PATH), stage="cart_write",
                params=params, json=body, headers=self._catalog_location_headers(location),
            )
            payload = self._json(response, "cart_write")
            self._save_cookies(session, client.cookies)
            if payload.get("success") is False:
                # A definite rejection (stock, quantity, seller) changes nothing we rely on.
                raise CommerceError(
                    "rejected", "JioMart did not accept that cart change.", stage="cart_write",
                    http_status=response.status_code, diagnostic_code="cart_rejected",
                    upstream=self._first_text(payload, "message", "error_code"),
                )
            if self._failure_envelope(payload):
                raise self._contract_error("cart_write", response.status_code)
        # Callers re-read the cart; a write response is acceptance evidence only.
        return {"accepted": True, "partial": payload.get("partial") is True}

    @staticmethod
    def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
        return next((payload[key] for key in keys if isinstance(payload.get(key), str) and payload[key]), "")

    async def select_address(
        self, session: dict[str, Any], location: Mapping[str, Any], cart_id: str, address_id: str
    ) -> None:
        """Attach a saved address so JioMart prices delivery for it; no order is created."""
        self._require_writes("select_address")
        safe_cart, safe_address = self._safe_id(cart_id), self._safe_id(address_id)
        if not safe_cart or not safe_address:
            raise ValueError("cart and address are required")
        async with self._client(session.get("cookies")) as client:
            await self._verify_cart_session(client, session, "select_address_session")
            await self._checkout_step(
                client, "POST", self._SELECT_ADDRESS_PATH, "select_address", location,
                params={"cart_id": safe_cart},
                json={"cart_id": safe_cart, "id": safe_address, "billing_address_id": safe_address},
            )
            self._save_cookies(session, client.cookies)

    async def payment_modes(
        self, session: dict[str, Any], location: Mapping[str, Any], cart_id: str, amount: float
    ) -> list[dict[str, str]]:
        """List payment options for this cart as name/code/aggregator/merchant only."""
        safe_cart = self._safe_id(cart_id)
        if not safe_cart or not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("cart and amount are required")
        async with self._client(session.get("cookies")) as client:
            payload = await self._checkout_step(
                client, "GET", self._PAYMENT_OPTIONS_PATH, "payment_options", location,
                params={"amount": str(round(amount * 100)), "cart_id": safe_cart, "checkout_mode": "self"},
            )
            self._save_cookies(session, client.cookies)
        container = payload.get("payment_options")
        options = container.get("payment_option") if isinstance(container, Mapping) else None
        flows = container.get("payment_flows") if isinstance(container, Mapping) else None
        flows = flows if isinstance(flows, Mapping) else {}
        if not isinstance(options, list):
            raise self._contract_error("payment_options", 200)
        modes = []
        for option in options[:30]:
            if not isinstance(option, Mapping) or not isinstance(option.get("list"), list) or not option["list"]:
                continue
            first = option["list"][0]
            if not isinstance(first, Mapping):
                continue
            mode = {
                "name": self._safe_id(option.get("name")),
                "code": self._safe_id(first.get("code")),
                "aggregator": self._safe_id(first.get("aggregator_name")),
                "merchant_code": self._safe_id(first.get("merchant_code")),
            }
            # JioMart's site posts hosted payments to the option's payment_meta, else its aggregator flow.
            meta = first.get("payment_meta") if isinstance(first.get("payment_meta"), Mapping) else None
            aggregator = first.get("aggregator_name")
            flow = meta if meta and meta.get("api_link") else flows.get(aggregator) if isinstance(aggregator, str) else None
            link = self._payment_link(flow.get("api_link") if isinstance(flow, Mapping) else None)
            if link:
                mode["api_link"] = link
            if mode["name"] and mode["aggregator"] and (mode["code"] or mode["merchant_code"]):
                modes.append(mode)
        # Option labels and payment host only; they identify no customer.
        log.info("payment_modes offered=%s", ",".join(
            f"{m['name']}:{m['code']}:{m['aggregator']}" + (f"@{urlparse(m['api_link']).hostname}" if m.get("api_link") else "")
            for m in modes) or "none")
        return modes

    _PAYMENT_HOSTS = ("jio.com", "jiopay.com", "jiopay.in", "jiomart.com", "jiomartjcp.com", "jiopaymentsbank.com")

    @classmethod
    def _payment_link(cls, value: object) -> str | None:
        """Accept only an HTTPS payment endpoint on a Jio-owned domain."""
        if not isinstance(value, str) or len(value) > 500:
            return None
        try:
            parsed = urlparse(value)
            port = parsed.port
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        allowed = any(host == suffix or host.endswith("." + suffix) for suffix in cls._PAYMENT_HOSTS)
        if (parsed.scheme != "https" or port is not None or parsed.netloc.lower() != host
                or not re.fullmatch(r"[a-z0-9.-]{3,253}", host) or not allowed):
            if host:
                log.warning("payment_link_host_rejected host=%s", re.sub(r"[^a-z0-9.-]", "", host)[:80])
            return None
        return value

    @staticmethod
    def online_payment(modes: list[Mapping[str, str]]) -> dict[str, str] | None:
        """JioPay's hosted page ("Pay now" on the website), only with a vetted endpoint."""
        for mode in modes:
            if mode.get("name") in {"JIOPP", "JIOPPLINK"} and mode.get("api_link"):
                return dict(mode)
        return None

    @staticmethod
    def cash_on_delivery(modes: list[Mapping[str, str]]) -> dict[str, str] | None:
        """Only a direct COD option creates an order without a payment page."""
        for mode in modes:
            if mode.get("name") in {"COD", "JIOPP_COD"} and mode.get("code") == "COD":
                return dict(mode)
        return None

    async def checkout(
        self,
        session: dict[str, Any],
        location: Mapping[str, Any],
        *,
        cart_id: str,
        address_id: str,
        amount: float,
        method: str = "cod",
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        """Submit one confirmed order for the exact reviewed total.

        ``cod`` creates the order directly. ``online`` creates it awaiting payment and
        returns JioPay's hosted-page form, exactly as JioMart's website receives it.
        Address and total are re-checked first; any difference raises ``changed``
        before submission. Callers must persist an attempt before calling this
        and must never retry it blindly.
        """
        if method not in {"cod", "online"}:
            raise ValueError("unsupported payment method")
        if not self._enable_checkout:
            raise CommerceError("unavailable", "Checkout is not enabled.", stage="checkout", diagnostic_code="checkout_disabled")
        safe_cart, safe_address = self._safe_id(cart_id), self._safe_id(address_id)
        if not safe_cart or not safe_address:
            raise ValueError("cart and address are required")
        try:
            # Nothing is submitted before the checkout POST, so any failure here is definite.
            await self.select_address(session, location, safe_cart, safe_address)
            cart = await self.get_cart(session, location)
            total = next((e["amount"] for e in cart["price_breakup"]["raw"] if e["name"] == "total"), None)
            if cart["cart_id"] != safe_cart or total is None or abs(total - amount) > 0.005:
                raise CommerceError("changed", "The cart changed before checkout.", stage="checkout_precheck",
                                    diagnostic_code="total_changed")
            modes = await self.payment_modes(session, location, safe_cart, amount)
        except CommerceError as exc:
            if exc.code in {"auth_expired", "changed"}:
                raise
            raise CommerceError("rejected", "Checkout could not start.", stage="checkout_precheck",
                                http_status=exc.http_status, diagnostic_code="precheck_failed") from exc
        cod = self.cash_on_delivery(modes) if method == "cod" else self.online_payment(modes)
        if cod is None:
            raise CommerceError(
                "rejected", "That payment method is unavailable.", stage="checkout_precheck",
                diagnostic_code="cod_unavailable" if method == "cod" else "online_unavailable",
            )
        identifier = cod["code"] or cod["merchant_code"]
        body = {
            "id": safe_cart,
            "address_id": safe_address,
            "billing_address_id": safe_address,
            "aggregator": cod["aggregator"],
            "merchant_code": cod["merchant_code"],
            "payment_mode": cod["code"],
            "payment_identifier": identifier,
            "callback_url": callback_url if method == "online" and callback_url else self._CHECKOUT_CALLBACK,
            "payment_methods": [{
                "mode": cod["code"], "name": cod["code"], "payment": "required",
                "payment_identifier": identifier,
                "payment_meta": {"merchant_code": cod["merchant_code"], "payment_gateway": cod["aggregator"],
                                 "payment_identifier": identifier},
            }],
        }
        async with self._client(session.get("cookies")) as client:
            await self._verify_cart_session(client, session, "checkout_session")
            with contextlib.suppress(CommerceError):
                # Attribution only, as JioMart's own WhatsApp hand-off does.
                await self._checkout_step(
                    client, "PUT", self._CART_META_PATH, "checkout_meta", location,
                    params={"id": safe_cart}, json={"custom_cart_meta": {"channel_id": "whatsapp"}},
                )
            response = await self._account_request(
                client, "POST", self._api_url(self._CHECKOUT_PATH), stage="checkout",
                headers=self._catalog_location_headers(location), error_code="rejected", json=body,
            )
            payload = self._json(response, "checkout")
            self._save_cookies(session, client.cookies)
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        order_id = self._safe_id(payload.get("order_id") or data.get("order_id"))
        if payload.get("success") is False:
            # An order number means JioMart may already have created it; never call that "nothing placed".
            raise CommerceError(
                "unknown" if order_id else "rejected", "JioMart did not confirm the order.", stage="checkout",
                http_status=response.status_code, diagnostic_code="checkout_unconfirmed" if order_id else "checkout_rejected",
                upstream=self._first_text(payload, "message", "error_code"),
            )
        result: dict[str, Any] = {"order_id": order_id or None, "payment": None}
        if method == "online":
            # JioMart's website POSTs the checkout response's data fields to the payment link
            # (``base64_html`` for JioPay, merchant order/transaction IDs for JioOnePay).
            fields = {
                str(key): str(value) for key, value in data.items()
                if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(key))
                and isinstance(value, (str, int, float)) and not isinstance(value, bool)
                and 0 < len(str(value)) <= 300_000
            }
            if fields and len(fields) <= 40:
                result["payment"] = {"api_link": cod["api_link"], "fields": fields}
                log.info("checkout_payment_form fields=%s", ",".join(sorted(fields)))
            else:
                log.warning("checkout_payment_form_missing data_keys=%s", ",".join(sorted(map(str, data))[:30]))
        return result

    async def _checkout_step(
        self, client: httpx.AsyncClient, method: str, path: str, stage: str,
        location: Mapping[str, Any], **kwargs: Any,
    ) -> dict[str, Any]:
        response = await self._account_request(
            client, method, self._api_url(path), stage=stage,
            headers=self._catalog_location_headers(location), error_code="rejected", **kwargs,
        )
        payload = self._json(response, stage)
        if payload.get("success") is False:
            raise CommerceError(
                "rejected", "JioMart did not accept that step.", stage=stage,
                http_status=response.status_code, diagnostic_code=f"{stage}_rejected",
                upstream=self._first_text(payload, "message", "error_code"),
            )
        return payload

    async def _account_request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Account request that follows one JioMart-owned 307/308 redirect with the same method and body.

        JioMart routes some storefront operations to extensions on its website host. The
        session cookie is sent only to the two JioMart hosts in ``_ALLOWED_HOSTS``.
        """
        stage = kwargs.get("stage", "request")
        response = await self._request(client, method, url, **kwargs)
        if response.status_code not in (307, 308):
            return response
        target = self._account_redirect(response, stage)
        headers = dict(kwargs.pop("headers", {}) or {})
        kwargs.pop("params", None)  # The Location already carries the query string.
        if urlparse(target).hostname != urlparse(url).hostname and client.cookies:
            headers["cookie"] = "; ".join(f"{cookie.name}={cookie.value}" for cookie in client.cookies.jar)
        response = await self._request(client, method, target, headers=headers, **kwargs)
        if response.status_code in (301, 302, 303, 307, 308):
            raise CommerceError("unavailable", "Unable to complete the request.", stage=stage,
                                http_status=response.status_code, diagnostic_code="redirect_loop")
        return response

    async def _storefront_order_get(
        self, client: httpx.AsyncClient, path: str, stage: str, params: Mapping[str, str] | None
    ) -> httpx.Response | None:
        """GET an order route through www.jiomart.com/api with the session cookie; None on any failure."""
        headers = {"cookie": "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)} if client.cookies else {}
        try:
            return await self._account_request(
                client, "GET", self._storefront_url("/api" + path), stage=f"{stage}_storefront",
                headers=headers, params=dict(params or {}),
            )
        except CommerceError:
            return None

    def _account_redirect(self, response: httpx.Response, stage: str) -> str:
        location = response.headers.get("location") or ""
        target = urljoin(str(response.url), location)
        parsed = urlparse(target)
        if parsed.hostname == "api.jiomartjcp.com" and parsed.path.startswith("/ext/"):
            # JioMart extensions answer only on the storefront host (observed for search and select_address).
            target = parsed._replace(netloc="www.jiomart.com").geturl()
            parsed = urlparse(target)
        safe_path = re.sub(r"[^A-Za-z0-9/._-]", "", parsed.path)[:120]
        log.info("redirect stage=%s to=%s%s", stage, parsed.hostname or "none", safe_path)
        if (not location or parsed.scheme != "https" or parsed.hostname not in self._ALLOWED_HOSTS
                or parsed.username or parsed.password
                or not parsed.path.startswith(("/service/application/", "/api/service/application/", "/ext/"))):
            raise CommerceError("unavailable", "Unable to complete the request.", stage=stage,
                                http_status=response.status_code, diagnostic_code="unsafe_redirect")
        return target

    _JIOMART_PAYMENT_HOSTS = ("jiomart.com", "jiomartjcp.com")

    async def open_payment_session(
        self, session: dict[str, Any], api_link: str, fields: Mapping[str, str]
    ) -> dict[str, Any]:
        """Start JioMart's payment session server-side with the account's own sign-in.

        JioMart's site POSTs the checkout's merchant IDs to ``api_link`` from the signed-in
        browser. A customer's browser opened from WhatsApp has no JioMart session, so the bot
        makes that call and returns where the browser should go next: a payment-provider
        ``redirect`` URL or a provider ``form`` (action + fields). Only names/hosts are logged.
        """
        if not self._payment_link(api_link):
            raise ValueError("payment link is not a vetted JioMart endpoint")
        fields = {str(k): str(v) for k, v in fields.items()}
        async with self._client(session.get("cookies")) as client:
            cookie = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
            headers = {**self._app_headers(), "cookie": cookie,
                       "accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
            method, url, body = "POST", api_link, fields
            for hop in range(6):
                try:
                    response = await client.request(method, url, headers=headers, data=body, follow_redirects=False)
                except httpx.HTTPError as exc:
                    raise CommerceError("unavailable", "JioMart is unavailable.", stage="payment_session",
                                        diagnostic_code="transport_error") from exc
                host = (urlparse(str(response.url)).hostname or "").lower()
                log.info("payment_session hop=%s status=%s host=%s type=%s", hop, response.status_code, host,
                         response.headers.get("content-type", "").split(";")[0][:40])
                if response.status_code in (301, 302, 303, 307, 308):
                    target = urljoin(str(response.url), response.headers.get("location") or "")
                    parsed = urlparse(target)
                    if parsed.scheme != "https" or not parsed.hostname:
                        break
                    next_host = parsed.hostname.lower()
                    # Dot-boundary match: "evil-jiomart.com" must never receive the account cookie.
                    if not any(next_host == h or next_host.endswith("." + h) for h in self._JIOMART_PAYMENT_HOSTS):
                        # Left JioMart: this is the payment provider's own page.
                        return {"redirect": target}
                    if response.status_code in (301, 302, 303):
                        method, body = "GET", None
                    url = target
                    continue
                break
            self._save_cookies(session, client.cookies)
        if response.status_code == 401 or response.status_code == 403:
            raise CommerceError("auth_expired", "Sign-in has expired.", stage="payment_session",
                                http_status=response.status_code, diagnostic_code=f"http_{response.status_code}")
        content_type = response.headers.get("content-type", "")
        payload: Any = {}
        if response.status_code == 200 and "html" in content_type:
            form = self._payment_form(response.text[:300_000], str(response.url))
            if form:
                return {"form": form}
        if response.status_code == 200 and "json" in content_type:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            url = self._first_payment_url(payload)
            if url:
                return {"redirect": url}
        raise CommerceError("unavailable", "JioMart did not open a payment page.", stage="payment_session",
                            http_status=response.status_code, diagnostic_code="no_payment_page",
                            upstream=self._first_text(payload, "message") if isinstance(payload, Mapping) else "")

    @staticmethod
    def _payment_form(document: str, base: str) -> dict[str, Any] | None:
        """Extract the first form (action, method and named inputs) from a provider hand-off page."""
        from html.parser import HTMLParser

        class FormParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.form, self.done = None, False

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if self.done:
                    return
                if tag == "form" and self.form is None:
                    self.form = {"action": attrs.get("action") or "", "method": (attrs.get("method") or "post").lower(), "fields": {}}
                elif tag == "input" and self.form is not None and attrs.get("name"):
                    self.form["fields"][attrs["name"]] = attrs.get("value") or ""

            def handle_endtag(self, tag):
                if tag == "form" and self.form is not None:
                    self.done = True

        parser = FormParser()
        parser.feed(document)
        form = parser.form
        if not form or not form["action"]:
            return None
        action = urljoin(base, form["action"])
        if urlparse(action).scheme != "https":
            return None
        log.info("payment_session form host=%s fields=%s", urlparse(action).hostname,
                 ",".join(sorted(form["fields"]))[:200])
        return {"action": action, "method": "get" if form["method"] == "get" else "post", "fields": form["fields"]}

    @classmethod
    def _first_payment_url(cls, payload: object) -> str | None:
        for key in ("url", "redirect_url", "payment_url", "payment_link", "link"):
            value = cls._find(payload, key)
            if isinstance(value, str) and value.startswith("https://"):
                return value
        nxt = cls._find(payload, "next")
        if isinstance(nxt, list) and nxt and isinstance(nxt[0], Mapping):
            value = nxt[0].get("url")
            if isinstance(value, str) and value.startswith("https://"):
                return value
        return None

    def _require_writes(self, stage: str) -> None:
        if not self._enable_cart_writes:
            raise CommerceError("unavailable", "Cart changes are not enabled.", stage=stage, diagnostic_code="writes_disabled")

    async def _verify_cart_session(
        self, client: httpx.AsyncClient, session: Mapping[str, Any], stage: str
    ) -> None:
        """Require the authenticated account before exposing or changing a cart."""
        expected_account = self._safe_id(session.get("account_reference"))
        expected_application = session.get("application_id")
        if not expected_account or (
            expected_application is not None and expected_application != self.application_id
        ):
            raise CommerceError(
                "auth_expired",
                "Sign-in has expired. Please try again.",
                stage=stage,
                diagnostic_code="identity_context",
            )
        response = await self._request(
            client, "GET", self._api_url(self._SESSION_PATH), stage=stage
        )
        payload = self._json(response, stage)
        user = self._user_response(payload)
        account = self._safe_id(self._find(user, "user_id", "userId", "uid", "_id", "id"))
        application = self._find(user, "application_id", "applicationId")
        if account != expected_account or (
            application is not None and str(application) != self.application_id
        ):
            raise CommerceError(
                "auth_expired",
                "Sign-in has expired. Please try again.",
                stage=stage,
                http_status=response.status_code,
                diagnostic_code="identity_mismatch",
            )
        if isinstance(session, dict) and (
            expected_application is not None
            or self.application_id == self._LEGACY_CONSUMER_APPLICATION_ID
        ):
            session["application_id"] = self.application_id

    async def _catalog_get(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        stage: str,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        response = await self._request(
            client,
            "GET",
            self._storefront_url(path),
            stage=stage,
            headers=dict(headers),
            params=params,
            follow_redirects=False,
        )
        if response.is_redirect:
            response = await self._request(
                client,
                "GET",
                self._safe_redirect(response),
                stage=f"{stage}_redirect",
                headers=dict(headers),
                follow_redirects=False,
            )
        return response

    def _catalog_location_headers(self, location: Mapping[str, Any]) -> dict[str, str]:
        return {
            "x-location-detail": json.dumps(
                self._location_detail(location), separators=(",", ":")
            ),
            "x-currency": "INR",
        }

    @classmethod
    def _normalise_product_size(cls, value: object) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        size = cls._safe_id(value.get("value"))
        display = cls._bounded_text(value.get("display"), 120)
        quantity = value.get("quantity")
        available = value.get("is_available")
        if (
            not size
            or not display
            or type(quantity) is not int
            or quantity < 0
            or (available is not None and type(available) is not bool)
        ):
            return None
        identifiers = value.get("seller_identifiers")
        seller_identifiers = (
            [identifier for item in identifiers[:20] if (identifier := cls._safe_id(item))]
            if isinstance(identifiers, list)
            else []
        )
        return {
            "size": size,
            "display": display,
            "quantity": quantity,
            "is_available": available,
            "seller_identifiers": seller_identifiers,
        }

    @staticmethod
    def _normalise_moq(value: object) -> dict[str, int]:
        if not isinstance(value, Mapping):
            return {}
        return {
            key: value[key]
            for key in ("increment_unit", "minimum")
            if type(value.get(key)) is int and 1 <= value[key] <= 999
        }

    @classmethod
    def _normalise_product_seller(cls, value: object, size: str) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        article_id = cls._safe_id(value.get("article_id"))
        quantity = value.get("quantity")
        price = value.get("price")
        if not article_id or type(quantity) is not int or quantity < 0 or not isinstance(price, Mapping):
            return None
        effective = price.get("effective")
        marked = price.get("marked")
        if not isinstance(effective, (int, float)) or isinstance(effective, bool):
            return None
        output: dict[str, Any] = {
            "article_id": article_id,
            "size": size,
            "quantity": quantity,
            "price": {"effective": effective},
        }
        if isinstance(marked, (int, float)) and not isinstance(marked, bool):
            output["price"]["marked"] = marked
        if currency := cls._bounded_text(price.get("currency_code"), 8):
            output["price"]["currency_code"] = currency
        for source_key, target_key in (("seller", "seller_uid"), ("store", "store_uid")):
            source = value.get(source_key)
            if isinstance(source, Mapping) and (identifier := cls._safe_id(source.get("uid"))):
                output[target_key] = identifier
        if sku := cls._safe_id(cls._first_value(value, "item_id", "sku_code", "uid")):
            output["sku"] = sku
        return output

    @classmethod
    def _order_list_payload(
        cls, payload: Mapping[str, Any], http_status: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if cls._failure_envelope(payload):
            raise cls._contract_error("history", http_status)
        data = payload.get("data")
        raw_items = payload.get("items")
        if raw_items is None and isinstance(data, Mapping):
            raw_items = data.get("items")
        items = cls._list_from(payload, "items")
        page = cls._mapping_from(payload, "page")
        if page is None or not isinstance(raw_items, list):
            raise cls._contract_error("history", http_status)
        current = page.get("current")
        size = page.get("size")
        has_next = page.get("has_next")
        if type(current) is not int or current != 1 or type(size) is not int or not 1 <= size <= 20 or type(has_next) is not bool:
            raise cls._contract_error("history", http_status)
        return items, {"has_next": has_next}

    @classmethod
    def _order_detail_payload(cls, payload: Mapping[str, Any], http_status: int) -> dict[str, Any]:
        if cls._failure_envelope(payload):
            raise cls._contract_error("history_detail", http_status)
        order = cls._mapping_from(payload, "order")
        if order is None and isinstance(payload.get("data"), Mapping):
            data = payload["data"]
            order = dict(data) if "order_id" in data else None
        if order is None:
            order = dict(payload) if "order_id" in payload else None
        if order is None or not isinstance(order.get("shipments"), list):
            raise cls._contract_error("history_detail", http_status)
        return order

    @classmethod
    def _delivered_order_items(cls, order: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        order_reference = cls._bounded_text(order.get("order_id"), 128)
        order_date = cls._bounded_text(
            cls._first_value(order, "order_date", "order_created_ts"), 64
        )
        purchased_at = cls._epoch(order_date)
        incomplete = not order_reference or purchased_at is None
        evidence: list[dict[str, Any]] = []
        shipments = order.get("shipments")
        if not isinstance(shipments, list):
            return evidence, True
        if len(shipments) > 20:
            incomplete = True
        for shipment in shipments[:20]:
            if not isinstance(shipment, Mapping):
                incomplete = True
                continue
            status = cls._shipment_status(shipment)
            if status is None:
                incomplete = True
                continue
            if status != "delivered":
                continue
            bags = shipment.get("bags")
            if not isinstance(bags, list):
                incomplete = True
                continue
            if len(bags) > 50:
                incomplete = True
            for bag in bags[:50]:
                item = cls._history_item(order_reference, order_date, purchased_at, bag)
                if item is None:
                    incomplete = True
                else:
                    evidence.append(item)
                    if len(evidence) >= 50:
                        return evidence, True
        return evidence, incomplete

    @classmethod
    def _history_item(
        cls, order_reference: str, order_date: str, purchased_at: int | None, bag: object
    ) -> dict[str, Any] | None:
        if not order_reference or not isinstance(bag, Mapping):
            return None
        nested = bag.get("item") if isinstance(bag.get("item"), Mapping) else {}
        name = cls._bounded_text(cls._first_value(bag, "item_name", "product_name", "name"), 200) or cls._bounded_text(cls._first_value(nested, "name", "item_name", "product_name"), 200)
        product_uid = (
            cls._product_uid(cls._first_value(bag, "product_uid", "productUid"))
            or cls._product_uid(cls._first_value(nested, "product_uid", "productUid", "id"))
        )
        brand_value = bag.get("brand") if isinstance(bag.get("brand"), Mapping) else nested.get("brand")
        brand = cls._bounded_text(brand_value.get("name") if isinstance(brand_value, Mapping) else brand_value, 120)
        pack = cls._bounded_text(cls._first_value(bag, "size", "pack"), 80) or cls._bounded_text(cls._first_value(nested, "size", "pack"), 80)
        category = cls._bounded_text(cls._first_value(bag, "category"), 120) or cls._bounded_text(cls._first_value(nested, "category"), 120)
        quantity = bag.get("quantity")
        if not name or not product_uid or not isinstance(quantity, int) or not 1 <= quantity <= 999:
            return None
        recent = purchased_at is not None and purchased_at >= int((datetime.now(UTC) - timedelta(days=180)).timestamp())
        return {
            "id": product_uid,
            "name": name,
            "product_uid": product_uid,
            "brand": brand,
            "pack": pack,
            "category": category,
            "quantity": quantity,
            "order_reference": order_reference,
            "order_date": order_date,
            "purchased_at": purchased_at,
            "eligible_for_personalization": recent,
        }

    _DELIVERED = {"delivered", "delivery_done"}
    _SHIPPED = {"shipped", "out_for_delivery", "in_transit", "dp_assigned", "bag_picked", "handed_over_to_customer"}
    _PROCESSING = {"processing", "placed", "bag_confirmed", "bag_packed", "bag_invoiced", "ready_for_dp_assignment", "pending"}

    @classmethod
    def _shipment_status(cls, shipment: Mapping[str, Any]) -> str | None:
        """Coarse state from OMS ``value`` (or a title/legacy field); unknown stays None."""
        value = shipment.get("shipment_status")
        candidates = (
            [value.get("current_shipment_status"), value.get("value"), value.get("title")]
            if isinstance(value, Mapping) else [value]
        )
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            normalized = candidate.strip().lower().replace(" ", "_")
            if normalized in cls._DELIVERED:
                return "delivered"
            if normalized.startswith(("cancelled", "rto", "return")):
                return "cancelled"
            if normalized in cls._SHIPPED:
                return "shipped"
            if normalized in cls._PROCESSING:
                return "processing"
        return None

    @classmethod
    def _cart_payload(cls, payload: Mapping[str, Any], http_status: int) -> dict[str, Any]:
        cart = cls._mapping_from(payload, "cart")
        if cart is not None:
            if cls._failure_envelope(payload) or not cls._cart_snapshot_shape(cart):
                raise cls._contract_error("cart", http_status)
            return cart

        # The consumer endpoint uses a direct root object for an invalid cart and
        # may report ``success: false`` while still returning a complete snapshot.
        # Accept only that evidenced shape; generic failures continue to fail.
        if cls._invalid_cart_snapshot(payload):
            return dict(payload)
        if cls._failure_envelope(payload) or not cls._cart_snapshot_shape(payload):
            raise cls._contract_error("cart", http_status)
        return dict(payload)

    @classmethod
    def _cart_snapshot_shape(cls, value: Mapping[str, Any]) -> bool:
        identifier = next(
            (cls._safe_id(value.get(key)) for key in ("cart_id", "id", "uid") if cls._safe_id(value.get(key))),
            "",
        )
        return (
            bool(identifier)
            and isinstance(value.get("items"), list)
            and isinstance(value.get("last_modified"), str)
            and type(value.get("is_valid")) is bool
        )

    @staticmethod
    def _invalid_cart_snapshot(value: Mapping[str, Any]) -> bool:
        return (
            value.get("success") is False
            and (
                value.get("error_code") is None
                or isinstance(value.get("error_code"), str)
            )
            and not value.get("error")
            and not value.get("errors")
            and value.get("status") not in {"error", "failed", "failure"}
            and all(isinstance(value.get(key), str) for key in ("cart_id", "uid", "id"))
            and isinstance(value.get("items"), list)
            and isinstance(value.get("last_modified"), str)
            and value.get("is_valid") is False
            and isinstance(value.get("breakup_values"), Mapping)
            and isinstance(value.get("currency"), Mapping)
        )

    @classmethod
    def _normalise_cart(cls, cart: Mapping[str, Any]) -> dict[str, Any]:
        cart_id = next(
            (cls._safe_id(cart.get(key)) for key in ("cart_id", "id", "uid") if cls._safe_id(cart.get(key))),
            "",
        )
        last_modified = cls._bounded_text(cart.get("last_modified"), 64)
        is_valid = cart.get("is_valid") if type(cart.get("is_valid")) is bool else False
        items: list[dict[str, Any]] = []
        raw_items = cart.get("items")
        assert isinstance(raw_items, list)
        for item in raw_items[:50]:
            normalized = cls._cart_item(item)
            if normalized:
                items.append(normalized)
        coverage = (
            "complete"
            if len(raw_items) <= 50 and len(items) == len(raw_items)
            else "partial"
        )
        has_address = bool(cls._first_value(cart, "address_id", "delivery_address"))
        has_payment_methods = isinstance(cart.get("payment_methods"), list) and bool(cart["payment_methods"])
        missing = []
        if not cart_id:
            missing.append("cart_id")
        if not is_valid:
            missing.append("valid_cart")
        if not has_address:
            missing.append("delivery_address")
        if not has_payment_methods:
            missing.append("payment_method")
        return {
            "cart_id": cart_id,
            "last_modified": last_modified,
            "items": items,
            "price_breakup": cls._price_breakup(cart.get("breakup_values")),
            "currency": cls._currency_code(cart.get("currency")),
            "is_valid": is_valid,
            "coverage": coverage,
            "raw_item_count": len(raw_items),
            "checkout_context": {"enabled": False, "missing": missing},
        }

    @classmethod
    def _cart_item(cls, item: object) -> dict[str, Any] | None:
        if not isinstance(item, Mapping):
            return None
        identifiers = {
            key: value
            for key in ("article_id", "item_id", "seller_id", "store_id", "seller_identifier")
            if (value := cls._safe_id(item.get(key)))
        }
        nested_identifiers = item.get("identifiers")
        if isinstance(nested_identifiers, Mapping) and (
            identifier := cls._safe_id(nested_identifiers.get("identifier"))
        ):
            identifiers["identifier"] = identifier
        product = item.get("product")
        if isinstance(product, Mapping) and (
            product_uid := cls._safe_id(product.get("uid"))
        ):
            identifiers["product_uid"] = product_uid
        article = item.get("article")
        if isinstance(article, Mapping):
            if article_uid := cls._safe_id(article.get("uid")):
                identifiers["article_id"] = article_uid
            for source_key, target_key in (("seller", "seller_id"), ("store", "store_id")):
                source = article.get(source_key)
                if isinstance(source, Mapping) and (
                    nested_uid := cls._safe_id(source.get("uid"))
                ):
                    identifiers[target_key] = nested_uid
        quantity = item.get("quantity")
        if not identifiers or type(quantity) is not int or not 1 <= quantity <= 999:
            return None
        result = {"identifiers": identifiers, "quantity": quantity}
        size_value = item.get("item_size")
        if not size_value and isinstance(article, Mapping):
            size_value = article.get("size")
        if size := cls._bounded_text(size_value, 80):
            result["item_size"] = size
        # Display and update context: the customer reviews JioMart's own line data.
        index = item.get("res_item_index", item.get("item_index"))
        if type(index) is int and 0 <= index < 1000:
            result["item_index"] = index
        if isinstance(product, Mapping) and (name := cls._bounded_text(product.get("name"), 120)):
            result["name"] = name
        images = product.get("images") if isinstance(product, Mapping) else None
        for image in images[:3] if isinstance(images, list) else []:
            url = (image.get("secure_url") or image.get("url")) if isinstance(image, Mapping) else None
            if isinstance(url, str) and url.startswith("https://cdn1.jiomartjcp.com/") and len(url) <= 2048:
                result["image_url"] = url
                break
        line_total = cls._effective_price(item.get("price"))
        if line_total is not None:
            result["line_total"] = line_total
        availability = item.get("availability")
        if isinstance(availability, Mapping):
            flags = [
                availability.get("is_valid"),
                availability.get("deliverable"),
                None if availability.get("out_of_stock") is None else not availability.get("out_of_stock"),
            ]
            if any(flag is False for flag in flags):
                result["available"] = False
            elif all(flag is True for flag in flags):
                result["available"] = True
        return result

    @staticmethod
    def _effective_price(value: object) -> float | None:
        if not isinstance(value, Mapping):
            return None
        for source in ("converted", "base"):
            price = value.get(source)
            amount = price.get("effective") if isinstance(price, Mapping) else None
            if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount >= 0:
                return amount
        return None

    @classmethod
    def _price_breakup(cls, value: object) -> dict[str, Any]:
        raw_source = value.get("raw") if isinstance(value, Mapping) and isinstance(value.get("raw"), Mapping) else value
        display_source = value.get("display") if isinstance(value, Mapping) and isinstance(value.get("display"), Mapping) else {}
        raw = cls._breakup_entries(raw_source, numeric_only=True)
        display = cls._breakup_entries(display_source, numeric_only=False)
        return {"status": "available" if raw else "unavailable", "raw": raw, "display": display}

    @classmethod
    def _breakup_entries(cls, value: object, *, numeric_only: bool) -> list[dict[str, Any]]:
        source = value.items() if isinstance(value, Mapping) else []
        if isinstance(value, list):
            source = ((entry.get("name"), entry.get("value")) for entry in value if isinstance(entry, Mapping))
        result: list[dict[str, Any]] = []
        for name, amount in source:
            label = cls._bounded_text(name, 80)
            valid_amount = isinstance(amount, (int, float)) and not isinstance(amount, bool)
            if label and (valid_amount or (not numeric_only and isinstance(amount, str))):
                result.append({"name": label, "amount": amount})
        return result[:30]

    @staticmethod
    def _currency_code(value: object) -> str:
        if isinstance(value, Mapping):
            return JioMartClient._bounded_text(value.get("code"), 8)
        return ""

    @classmethod
    def _add_items(cls, items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return cls._typed_cart_items(items, require_existing=False)

    @classmethod
    def _update_items(cls, items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return cls._typed_cart_items(items, require_existing=True)

    @classmethod
    def _typed_cart_items(
        cls, items: list[Mapping[str, Any]], *, require_existing: bool, removal: bool = False
    ) -> list[dict[str, Any]]:
        """Build only the fields JioMart's own web cart sends.

        Adds carry the listing's item, size and seller (the server assigns the
        store). Updates/removals carry JioMart's line identifier and index from a
        fresh cart read; removals use quantity 0 like the website.
        """
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValueError("cart items must contain 1 to 20 entries")
        clean = []
        for item in items:
            low = 0 if removal else 1
            if not isinstance(item, Mapping) or type(item.get("quantity")) is not int or not low <= item["quantity"] <= 99:
                raise ValueError("cart quantity must be between 1 and 99")
            item_id = cls._safe_id(item.get("item_id") or item.get("product_uid"))
            size = item.get("item_size")
            size = size.strip() if isinstance(size, str) and re.fullmatch(r"[A-Za-z0-9 ._/-]{1,40}", size.strip()) else ""
            if not item_id or not size:
                raise ValueError("cart item has no supported identifier")
            vertical = item.get("vertical_code")
            vertical = vertical if isinstance(vertical, str) and re.fullmatch(r"[A-Za-z_]{1,40}", vertical) else "default"
            entry: dict[str, Any] = {
                "item_id": int(item_id) if item_id.isdigit() else item_id,
                "item_size": size,
                "quantity": 0 if removal else item["quantity"],
                "meta": {"vertical_code": vertical, "compute_delivery_fee": True},
            }
            if require_existing:
                identifier = cls._safe_id(item.get("identifier"))
                index = item.get("item_index")
                if not identifier or type(index) is not int or not 0 <= index < 1000:
                    raise ValueError("cart line identifier is required")
                entry["identifiers"] = {"identifier": identifier}
                entry["item_index"] = index
                if article := cls._safe_id(item.get("article_id")):
                    entry["article_id"] = article
            else:
                seller = cls._safe_id(item.get("seller_id"))
                if not seller:
                    raise ValueError("cart item needs its listing seller")
                entry["seller_id"] = int(seller) if seller.isdigit() else seller
            clean.append(entry)
        return clean

    @staticmethod
    def _safe_id(value: object) -> str:
        value = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
        return value if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) else ""

    @staticmethod
    def _product_uid(value: object) -> str:
        return JioMartClient._safe_id(value)

    @staticmethod
    def _bounded_text(value: object, limit: int) -> str:
        return value.strip()[:limit] if isinstance(value, str) else ""

    @staticmethod
    def _first_value(data: Mapping[str, Any], *keys: str) -> Any:
        return next((data[key] for key in keys if data.get(key) is not None), None)

    @staticmethod
    def _epoch(value: str) -> int | None:
        try:
            if value.isdigit():
                parsed = int(value)
                parsed = parsed // 1000 if parsed > 10_000_000_000 else parsed
            else:
                parsed = int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError, OverflowError):
            return None
        # 2000-01-01 .. 2100-01-01; anything else is not a usable order date.
        return parsed if 946684800 <= parsed <= 4102444800 else None

    @classmethod
    def _mapping_from(cls, payload: Mapping[str, Any], key: str) -> dict[str, Any] | None:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
        data = payload.get("data")
        if isinstance(data, Mapping) and isinstance(data.get(key), Mapping):
            return dict(data[key])
        return None

    @staticmethod
    def _contract_error(stage: str, http_status: int) -> CommerceError:
        return CommerceError("unavailable", "Unable to complete the request.", stage=stage, http_status=http_status, diagnostic_code="response_contract")

    @asynccontextmanager
    async def _client(self, cookie_records: object = None):
        cookies = self._load_cookies(cookie_records)
        async with httpx.AsyncClient(
            timeout=self._TIMEOUT,
            follow_redirects=False,
            transport=self._transport,
            cookies=cookies,
        ) as client:
            yield client

    async def _request(
        self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        await self._ensure_application_token(client)
        stage = kwargs.pop("stage", "request")
        error_code = kwargs.pop("error_code", "unavailable")
        headers = self._app_headers()
        headers.update(kwargs.pop("headers", {}))
        refreshable = not stage.startswith(self._NO_REFRESH_STAGES) and client.cookies
        # At most one refresh attempt per request: a proactive renewal here must not be
        # followed by a second attempt from the after-401 fallback below.
        refreshed = False
        if refreshable and self._access_token_expiring(client.cookies):
            refreshed = True
            await self._refresh_session(client)
        try:
            response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise CommerceError(
                "unavailable",
                "JioMart is unavailable. Please try again.",
                stage=stage,
                diagnostic_code="transport_error",
            ) from exc
        if response.status_code in (401, 403) and refreshable and not refreshed:
            # JioMart access tokens are short-lived. A 401/403 means the request was not processed,
            # so refreshing once and retrying once is safe even for cart writes and checkout.
            if await self._refresh_session(client):
                try:
                    response = await client.request(method, url, headers=headers, **kwargs)
                except httpx.HTTPError as exc:
                    raise CommerceError(
                        "unavailable", "JioMart is unavailable. Please try again.",
                        stage=stage, diagnostic_code="transport_error",
                    ) from exc
        if response.status_code in (401, 403):
            raise CommerceError(
                "auth_expired",
                "Sign-in has expired. Please try again.",
                stage=stage,
                http_status=response.status_code,
                diagnostic_code=f"http_{response.status_code}",
            )
        if response.status_code == 429:
            raise CommerceError(
                "rate_limited",
                "Too many requests. Please try again shortly.",
                stage=stage,
                http_status=response.status_code,
                diagnostic_code="http_429",
            )
        if response.status_code >= 500:
            raise CommerceError(
                "unavailable",
                "JioMart is unavailable. Please try again.",
                stage=stage,
                http_status=response.status_code,
                diagnostic_code="http_5xx",
            )
        if response.status_code >= 400:
            code = error_code if response.status_code == 400 else "unavailable"
            message = (
                "The delivery location is invalid."
                if code == "invalid_location"
                else "Unable to complete the request."
            )
            raise CommerceError(
                code,
                message,
                stage=stage,
                http_status=response.status_code,
                diagnostic_code=f"http_{response.status_code}",
                upstream=self._upstream_hint(response),
            )
        return response

    @staticmethod
    def _upstream_hint(response: httpx.Response) -> str:
        """Extract only an error code/message pair; the rest of the body is discarded."""
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return ""
        if not isinstance(payload, Mapping):
            return ""
        parts = []
        for key in ("error_code", "code", "message", "error"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                value = value.get("message") or value.get("code")
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) not in parts:
                parts.append(str(value))
        return ": ".join(parts[:2])

    @staticmethod
    def _access_token_cookie(cookies: httpx.Cookies):
        """Find the JWT access-token cookie by shape; JioMart's cookie name isn't documented."""
        return next((c for c in cookies.jar if c.value.startswith("eyJ") and c.value.count(".") == 2), None)

    @staticmethod
    def _jwt_expiry(token: str) -> int | None:
        """Read the unverified ``exp`` claim (epoch seconds) from a JWT. Never logs the token."""
        parts = token.split(".")
        if len(parts) != 3:
            return None
        segment = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            payload = json.loads(urlsafe_b64decode(segment))
        except (ValueError, UnicodeDecodeError):
            return None
        exp = payload.get("exp") if isinstance(payload, Mapping) else None
        return exp if isinstance(exp, int) and not isinstance(exp, bool) else None

    @classmethod
    def _access_token_expiring(cls, cookies: httpx.Cookies) -> bool:
        """True once the access token is within the proactive refresh window.

        An unreadable ``exp`` is not treated as expiring here: the caller still has an
        actual request to send, so the existing after-401 fallback stays the safety net.
        """
        cookie = cls._access_token_cookie(cookies)
        if cookie is None:
            return False
        exp = cls._jwt_expiry(cookie.value)
        if exp is None:
            return False
        return exp - datetime.now(UTC).timestamp() <= cls._PROACTIVE_REFRESH_WINDOW

    async def _refresh_session(self, client: httpx.AsyncClient) -> bool:
        """Renew the short-lived (about one hour) access JWT, proactively or after a 401.

        JioMart's documented contract carries the current access token in the body; its own
        storefront instead refreshes with the cookie alone. A 400 on the documented body is
        retried once, cookie-only, the same way the website does. Token values are never
        logged, and at most one refresh is attempted per call.
        """
        jwt_cookie = self._access_token_cookie(client.cookies)
        if jwt_cookie is None:
            # Cookie names only (never values), to learn which one carries the session.
            log.warning("session_refresh_failed reason=no_access_token cookies=%s",
                        ",".join(sorted({re.sub(r"[^A-Za-z0-9._-]", "", c.name)[:40] for c in client.cookies.jar})))
            return False
        for variant, body in (("body", {"access_token": jwt_cookie.value}), ("cookie", None)):
            try:
                response = await client.post(
                    self._api_url(self._REFRESH_PATH), headers=self._app_headers(), json=body
                )
            except httpx.HTTPError:
                log.warning("session_refresh_failed reason=transport variant=%s", variant)
                return False
            if response.status_code == 200:
                try:
                    renewed = response.json().get("access_token")
                except (ValueError, AttributeError):
                    renewed = None
                if isinstance(renewed, str) and renewed.startswith("eyJ") and not any(
                    c.name == jwt_cookie.name and c.value == renewed for c in client.cookies.jar
                ):
                    # Keep the client's cookie in step even if JioMart didn't send a Set-Cookie.
                    client.cookies.set(jwt_cookie.name, renewed, domain=jwt_cookie.domain, path=jwt_cookie.path)
                log.info("session_refreshed variant=%s", variant)
                return True
            if response.status_code != 400 or variant == "cookie":
                log.warning("session_refresh_failed status=%s variant=%s", response.status_code, variant)
                return False
            # The documented body shape returned 400; retry once, cookie-only, like the website.
        return False

    async def keep_alive(self, session: dict[str, Any], *, within: int = 900) -> bool:
        """Proactively renew this session's access token well before it expires.

        Meant for a background keep-alive loop rather than a customer-facing request: it
        refreshes only when the token is unreadable or expires within ``within`` seconds,
        and persists any renewed cookie back onto ``session``. Returns whether the session
        is still usable, so a caller can stop polling a session whose refresh failed.
        """
        async with self._client(session.get("cookies")) as client:
            cookie = self._access_token_cookie(client.cookies)
            if cookie is None:
                return False
            exp = self._jwt_expiry(cookie.value)
            if exp is not None and exp - datetime.now(UTC).timestamp() > within:
                return True
            await self._ensure_application_token(client)
            ok = await self._refresh_session(client)
            self._save_cookies(session, client.cookies)
            return ok

    async def _ensure_application_token(self, client: httpx.AsyncClient) -> None:
        """Read the public storefront bootstrap only when no token was configured.

        This is intentionally best-effort and keeps the discovered value in memory
        only.  The consumer API can change its public configuration at any time.
        """
        if self._application_token:
            return
        try:
            response = await client.get(self.STOREFRONT_ORIGIN, headers={"accept": "text/html"})
        except httpx.HTTPError as exc:
            raise CommerceError("unavailable", "JioMart is unavailable. Please try again.") from exc
        if response.status_code >= 500:
            raise CommerceError("unavailable", "JioMart is unavailable. Please try again.")
        token = self._extract_application_token(response.text)
        if not token:
            raise CommerceError("unavailable", "Unable to connect to JioMart.")
        self._application_token = token

    @staticmethod
    def _extract_application_token(html: str) -> str:
        # Public runtime configs have used both JSON and JavaScript key spelling.
        match = re.search(
            r'(?:["\']?(?:application[_-]?token|applicationToken)["\']?)\s*[:=]\s*["\']([^"\']+)["\']',
            html,
            flags=re.IGNORECASE,
        )
        return match.group(1) if match else ""

    def _app_headers(self) -> dict[str, str]:
        headers = {"x-application-id": self.application_id, "accept": "application/json"}
        if self._application_token:
            pair = f"{self.application_id}:{self._application_token}".encode()
            headers["authorization"] = f"Bearer {b64encode(pair).decode()}"
        return headers

    def _api_url(self, path: str) -> str:
        return f"{self.API_ORIGIN}{path}"

    def _storefront_url(self, path: str) -> str:
        return f"{self.STOREFRONT_ORIGIN}{path}"

    def _safe_redirect(self, response: httpx.Response) -> str:
        location = response.headers.get("location")
        if not location:
            raise CommerceError("unavailable", "Unable to complete the request.")
        target = urljoin(str(response.url), location)
        parsed = urlparse(target)
        if parsed.scheme != "https" or parsed.hostname not in self._ALLOWED_HOSTS:
            raise CommerceError("unavailable", "Unable to complete the request.")
        if parsed.hostname != "www.jiomart.com" or not parsed.path.startswith("/ext/vertex/"):
            raise CommerceError("unavailable", "Unable to complete the request.")
        return target

    @staticmethod
    def _json(response: httpx.Response, stage: str = "response") -> dict[str, Any]:
        try:
            value = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise CommerceError(
                "unavailable",
                "Unable to complete the request.",
                stage=stage,
                http_status=response.status_code,
                diagnostic_code="invalid_json",
            ) from exc
        if not isinstance(value, dict):
            raise CommerceError(
                "unavailable",
                "Unable to complete the request.",
                stage=stage,
                http_status=response.status_code,
                diagnostic_code="invalid_shape",
            )
        return value

    @staticmethod
    def _success(payload: Mapping[str, Any]) -> bool:
        return payload.get("success") is True or payload.get("status") == "success"

    @staticmethod
    def _failure_envelope(payload: Mapping[str, Any]) -> bool:
        """Recognise explicit API failures without exposing their contents."""
        return (
            payload.get("success") is False
            or payload.get("status") in {"error", "failed", "failure"}
            or bool(payload.get("error"))
            or bool(payload.get("errors"))
        )

    @classmethod
    def _user_response(cls, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if cls._failure_envelope(payload):
            return None
        user = cls._find(payload, "user", "userdata")
        return user if isinstance(user, Mapping) else None

    @classmethod
    def _verified_login_response(cls, payload: Mapping[str, Any]) -> bool:
        """Validate the documented OTP-login response, whose success flag is optional."""
        return (
            not cls._failure_envelope(payload)
            and cls._find(payload, "user_exists", "userExists") is True
            and cls._user_response(payload) is not None
        )

    @classmethod
    def _find(cls, data: object, *keys: str) -> Any:
        if isinstance(data, Mapping):
            for key, value in data.items():
                if key in keys:
                    return value
            for value in data.values():
                found = cls._find(value, *keys)
                if found is not None:
                    return found
        elif isinstance(data, list):
            for value in data:
                found = cls._find(value, *keys)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _list_from(payload: Mapping[str, Any], *keys: str) -> list[dict[str, Any]]:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, Mapping)]
        data = payload.get("data")
        if isinstance(data, Mapping):
            return JioMartClient._list_from(data, *keys)
        return []

    @staticmethod
    def _normalise_mobile(value: str) -> str:
        digits = "".join(re.findall(r"\d", value))
        return digits[2:] if len(digits) == 12 and digits.startswith("91") else digits

    @classmethod
    def _has_matching_mobile(cls, payload: object, mobile: str) -> bool:
        """Match only a returned verified phone record to the requested account."""
        if isinstance(payload, Mapping):
            if payload.get("verified") is False:
                return False
            for key in (
                "phone",
                "mobile",
                "mobile_number",
                "mobileNumber",
                "phone_number",
                "phoneNumber",
            ):
                value = payload.get(key)
                if isinstance(value, str) and cls._normalise_mobile(value) == mobile:
                    return True
            phones = payload.get("phone_numbers")
            if isinstance(phones, list):
                for phone in phones:
                    if not isinstance(phone, Mapping) or phone.get("verified") is False:
                        continue
                    number = phone.get("phone") or phone.get("mobile")
                    if isinstance(number, str):
                        country = str(phone.get("country_code", "")).strip("+")
                        candidates = (number, f"{country}{number}" if country else number)
                        if any(
                            cls._normalise_mobile(candidate) == mobile for candidate in candidates
                        ):
                            return True
            return any(cls._has_matching_mobile(value, mobile) for value in payload.values())
        if isinstance(payload, list):
            return any(cls._has_matching_mobile(value, mobile) for value in payload)
        return False

    @classmethod
    def _session_tokens(cls, *payloads: Mapping[str, Any]) -> dict[str, str]:
        tokens = {}
        for payload in payloads:
            for key in ("access_token", "refresh_token"):
                value = cls._find(payload, key)
                if isinstance(value, str) and value:
                    tokens[key] = value
        return tokens

    @staticmethod
    def _normalise_address(address: Mapping[str, Any]) -> dict[str, Any]:
        def first(*keys: str) -> Any:
            for key in keys:
                if address.get(key) is not None:
                    return address[key]
            return ""

        address_id = JioMartClient._safe_id(first("id", "address_id", "uid"))
        return {
            # The saved-address ID is what checkout needs; free-text fields stay out.
            **({"id": address_id} if address_id else {}),
            "label": first("label", "address_type", "addressType", "name"),
            "pincode": str(first("pincode", "pin_code", "postal_code", "area_code")),
            "city": first("city"),
            "state": first("state"),
            "country": first("country") or "India",
            "country_iso_code": first("country_iso_code", "countryIsoCode", "country_code") or "IN",
            "is_default": bool(
                first("is_default_address", "is_default", "isDefault", "default_address", "default")
            ),
        }

    @staticmethod
    def _location_detail(location: Mapping[str, Any]) -> dict[str, str]:
        detail = {
            name: str(location.get(name, "")).strip()
            for name in ("country", "country_iso_code", "pincode")
        }
        if (
            not detail["country"]
            or not detail["country_iso_code"]
            or not re.fullmatch(r"[1-9]\d{5}", detail["pincode"])
        ):
            raise CommerceError("invalid_location", "The delivery location is invalid.")
        for name in ("city", "state"):
            value = str(location.get(name, "")).strip()
            if value:
                detail[name] = value
        return detail

    @staticmethod
    def _dump_cookies(cookies: httpx.Cookies) -> list[dict[str, str]]:
        return [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
            }
            for cookie in cookies.jar
        ]

    @staticmethod
    def _load_cookies(records: object) -> httpx.Cookies:
        cookies = httpx.Cookies()
        if not isinstance(records, Iterable) or isinstance(records, (str, bytes, Mapping)):
            return cookies
        for item in records:
            if isinstance(item, Mapping) and all(
                isinstance(item.get(key), str) for key in ("name", "value")
            ):
                cookies.set(
                    item["name"],
                    item["value"],
                    domain=str(item.get("domain") or ""),
                    path=str(item.get("path") or "/"),
                )
        return cookies

    @classmethod
    def _save_cookies(cls, session: dict[str, Any], cookies: httpx.Cookies) -> None:
        session["cookies"] = cls._dump_cookies(cookies)
