import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from smartbazaar.jiomart import CommerceError, JioMartClient  # noqa: I001


APP_ID = "app-1"
LOCATION = {"country": "India", "country_iso_code": "IN", "pincode": "400053"}


def client(handler, *, writes=False, checkout=False):
    return JioMartClient(
        APP_ID,
        application_token="test-token",
        transport=httpx.MockTransport(handler),
        enable_cart_writes=writes,
        enable_checkout=checkout,
    )


@pytest.mark.asyncio
async def test_history_extracts_only_delivered_nested_bags_and_hydrates_when_needed():
    calls = []
    recent = datetime.now(UTC).isoformat()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/orders"):
            # Same parameters as JioMart's My Orders page; status=1 is required there.
            assert request.url.params == httpx.QueryParams({"page_no": "1", "page_size": "12", "status": "1"})
            return httpx.Response(
                200,
                json={
                    "items": [{"order_id": "order-1"}],
                    "page": {"current": 1, "size": 20, "has_next": False},
                },
            )
        assert request.url.path.endswith("/orders/order-1")
        return httpx.Response(
            200,
            json={
                "order": {
                    "order_id": "order-1",
                    "order_date": recent,
                    "shipments": [
                        {
                            "shipment_status": {"current_shipment_status": "delivered"},
                            "bags": [
                                {
                                    "item": {"name": "Milk", "product_uid": "milk-500", "brand": "Dairy"},
                                    "quantity": 2,
                                    "size": "500 ml",
                                }
                            ],
                        },
                        {"shipment_status": {"current_shipment_status": "unknown-status"}, "bags": []},
                    ],
                }
            },
        )

    result = await client(handler).get_purchase_history({"cookies": []})

    assert result["status"] == "partial"
    assert result["items"] == [
        {
            "id": "milk-500",
            "name": "Milk",
            "product_uid": "milk-500",
            "brand": "Dairy",
            "pack": "500 ml",
            "category": "",
            "quantity": 2,
            "order_reference": "order-1",
            "order_date": recent,
            "purchased_at": int(datetime.fromisoformat(recent).timestamp()),
            "eligible_for_personalization": True,
        }
    ]
    assert calls == [
        "/service/application/order/v1.0/orders",
        "/service/application/order/v1.0/orders/order-1",
    ]


@pytest.mark.asyncio
async def test_empty_history_is_unresolved_not_no_purchase_history():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [], "page": {"current": 1, "size": 20, "has_next": False}}
        )

    assert await client(handler).get_purchase_history({"cookies": []}) == {
        "status": "unresolved",
        "items": [],
    }


@pytest.mark.asyncio
async def test_history_auth_failure_is_unavailable_without_invalidating_the_session():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "private detail"})

    session = {"cookies": [{"name": "session", "value": "existing"}], "account_reference": "user-1"}
    assert await client(handler).get_purchase_history(session) == {"status": "unavailable", "items": []}
    assert session["account_reference"] == "user-1"


@pytest.mark.asyncio
async def test_history_detail_hydration_is_limited_to_three_orders():
    detail_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal detail_calls
        if request.url.path.endswith("/orders"):
            return httpx.Response(
                200,
                json={
                    "items": [{"order_id": f"order-{index}"} for index in range(4)],
                    "page": {"current": 1, "size": 20, "has_next": False},
                },
            )
        detail_calls += 1
        return httpx.Response(200, json={"order": {"order_id": "order", "shipments": []}})

    result = await client(handler).get_purchase_history({"cookies": []})

    assert detail_calls == 3
    assert result == {"status": "partial", "items": []}


@pytest.mark.asyncio
async def test_history_rejects_malformed_pagination_without_leaking_response():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "page": {"current": 2, "size": 20}})

    with pytest.raises(CommerceError) as failure:
        await client(handler).get_purchase_history({"cookies": []})
    assert failure.value.code == "unavailable"
    assert failure.value.diagnostic_code == "response_contract"


@pytest.mark.asyncio
async def test_cart_read_is_sanitized_and_includes_authoritative_breakup_context():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/v1.0/session"):
            return httpx.Response(200, json={"user": {"user_id": "user-1"}})
        assert request.method == "GET"
        assert request.url.path.endswith("/cart/v1.0/detail")
        assert request.url.params["area_code"] == "400053"
        return httpx.Response(
            200,
            json={
                "success": True,
                "cart_id": "cart-1",
                "last_modified": "2026-10-18T10:00:00Z",
                "is_valid": True,
                "address_id": "private-address-id",
                "payment_methods": [{"name": "private payment instrument"}],
                "items": [
                    {
                        "article_id": "article-1",
                        "seller_id": "seller-1",
                        "quantity": 2,
                        "item_size": "500 ml",
                        "customer_note": "do not leak",
                    },
                ],
                "breakup_values": {
                    "raw": {"total": 75.5, "delivery_charge": 0},
                    "display": {"total": "₹75.50"},
                },
                "currency": {"code": "INR", "symbol": "₹"},
            },
        )

    result = await client(handler).get_cart(
        {"cookies": [], "account_reference": "user-1", "application_id": APP_ID}, LOCATION
    )

    assert result == {
        "cart_id": "cart-1",
        "last_modified": "2026-10-18T10:00:00Z",
        "items": [{"identifiers": {"article_id": "article-1", "seller_id": "seller-1"}, "quantity": 2, "item_size": "500 ml"}],
        "price_breakup": {
            "status": "available",
            "raw": [{"name": "total", "amount": 75.5}, {"name": "delivery_charge", "amount": 0}],
            "display": [{"name": "total", "amount": "₹75.50"}],
        },
        "currency": "INR",
        "is_valid": True,
        "coverage": "complete",
        "raw_item_count": 1,
        "checkout_context": {"enabled": False, "missing": []},
    }


@pytest.mark.asyncio
async def test_cart_read_accepts_only_the_complete_empty_consumer_snapshot():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/v1.0/session"):
            return httpx.Response(200, json={"user": {"user_id": "user-1"}})
        return httpx.Response(
            200,
            json={
                "success": False,
                "error_code": None,
                "cart_id": "",
                "uid": "",
                "id": "",
                "items": [],
                "last_modified": "2026-10-18T10:00:00Z",
                "is_valid": False,
                "breakup_values": {"raw": {}, "display": {}},
                "currency": {"code": "INR"},
            },
        )

    result = await client(handler).get_cart(
        {"cookies": [], "account_reference": "user-1", "application_id": APP_ID}, LOCATION
    )

    assert result == {
        "cart_id": "",
        "last_modified": "2026-10-18T10:00:00Z",
        "items": [],
        "price_breakup": {"status": "unavailable", "raw": [], "display": []},
        "currency": "INR",
        "is_valid": False,
        "coverage": "complete",
        "raw_item_count": 0,
        "checkout_context": {
            "enabled": False,
            "missing": ["cart_id", "valid_cart", "delivery_address", "payment_method"],
        },
    }


@pytest.mark.asyncio
async def test_cart_read_rejects_malformed_empty_failure_envelope():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/v1.0/session"):
            return httpx.Response(200, json={"user": {"user_id": "user-1"}})
        return httpx.Response(
            200,
            json={
                "success": False,
                "error_code": None,
                "cart_id": "",
                "items": [],
                "last_modified": "2026-10-18T10:00:00Z",
                "is_valid": False,
            },
        )

    with pytest.raises(CommerceError) as failure:
        await client(handler).get_cart(
            {"cookies": [], "account_reference": "user-1", "application_id": APP_ID},
            LOCATION,
        )
    assert failure.value.diagnostic_code == "response_contract"


@pytest.mark.asyncio
async def test_cart_read_sanitizes_a_complete_invalid_consumer_snapshot():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/v1.0/session"):
            return httpx.Response(200, json={"user": {"user_id": "user-1"}})
        return httpx.Response(
            200,
            json={
                "success": False,
                "error_code": "provider-cart-state",
                "message": "private provider detail",
                "cart_id": "cart-1",
                "uid": "cart-uid",
                "id": "cart-id",
                "items": [
                    {
                        "identifiers": {"identifier": "sku-1"},
                        "product": {"uid": 123},
                        "article": {"uid": "article-1", "size": "500 ml"},
                        "quantity": 2,
                    }
                ],
                "last_modified": "2026-10-18T10:00:00Z",
                "is_valid": False,
                "breakup_values": {"raw": {}, "display": []},
                "currency": {"code": "INR"},
            },
        )

    result = await client(handler).get_cart(
        {"cookies": [], "account_reference": "user-1", "application_id": APP_ID}, LOCATION
    )

    assert result["is_valid"] is False
    assert result["coverage"] == "complete"
    assert result["raw_item_count"] == 1
    assert result["items"] == [
        {
            "identifiers": {
                "identifier": "sku-1",
                "product_uid": "123",
                "article_id": "article-1",
            },
            "quantity": 2,
            "item_size": "500 ml",
        }
    ]


@pytest.mark.asyncio
async def test_cart_snapshot_marks_unparseable_items_partial_and_keeps_numeric_ids():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/v1.0/session"):
            return httpx.Response(200, json={"user": {"user_id": "user-1"}})
        return httpx.Response(
            200,
            json={
                "success": True,
                "cart_id": 123,
                "items": [
                    {"article_id": 456, "seller_id": 789, "quantity": 2},
                    {"article_id": 999, "quantity": True},
                ],
                "last_modified": "2026-10-18T10:00:00Z",
                "is_valid": True,
            },
        )

    result = await client(handler).get_cart(
        {"cookies": [], "account_reference": "user-1", "application_id": APP_ID}, LOCATION
    )

    assert result["cart_id"] == "123"
    assert result["items"] == [
        {
            "identifiers": {"article_id": "456", "seller_id": "789"},
            "quantity": 2,
        }
    ]
    assert result["coverage"] == "partial"
    assert result["raw_item_count"] == 2


@pytest.mark.asyncio
async def test_cart_write_gate_prevents_network_and_rejects_arbitrary_payloads():
    called = False

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    with pytest.raises(CommerceError) as failure:
        await client(handler).add_cart_items(
            {"cookies": []}, LOCATION, [{"article_id": "article-1", "quantity": 1, "ignored": "value"}]
        )
    assert failure.value.diagnostic_code == "writes_disabled"
    assert not called

    with pytest.raises(ValueError, match="quantity"):
        await client(handler, writes=True).add_cart_items(
            {"cookies": []}, LOCATION, [{"article_id": "article-1", "quantity": 100}]
        )
    assert not called


@pytest.mark.asyncio
async def test_cart_read_rejects_a_session_that_does_not_match_the_account():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/authentication/v1.0/session")
        return httpx.Response(
            200, json={"user": {"user_id": "other-user", "application_id": APP_ID}}
        )

    with pytest.raises(CommerceError) as failure:
        await client(handler).get_cart(
            {"cookies": [], "account_reference": "user-1", "application_id": APP_ID}, LOCATION
        )
    assert failure.value.code == "auth_expired"
    assert failure.value.diagnostic_code == "identity_mismatch"


@pytest.mark.asyncio
async def test_cart_read_backfills_legacy_session_application_only_after_identity_check():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/v1.0/session"):
            return httpx.Response(200, json={"user": {"user_id": "user-1"}})
        return httpx.Response(
            200,
            json={
                "cart_id": "cart-1",
                "items": [],
                "last_modified": "2026-10-18T10:00:00Z",
                "is_valid": True,
            },
        )

    session = {"cookies": [], "account_reference": "user-1"}
    consumer = JioMartClient(
        "685945f46c8c7aee3f3af605",
        application_token="test-token",
        transport=httpx.MockTransport(handler),
    )
    await consumer.get_cart(session, LOCATION)
    assert session["application_id"] == "685945f46c8c7aee3f3af605"


@pytest.mark.asyncio
async def test_product_sizes_and_sellers_are_location_scoped_and_sanitized():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-location-detail"] == (
            '{"country":"India","country_iso_code":"IN","pincode":"400053"}'
        )
        if request.url.path.endswith("/products/cola/sizes"):
            return httpx.Response(
                200,
                json={
                    "sellable": True,
                    "sizes": [
                        {
                            "display": "500 ml",
                            "value": "500ml",
                            "quantity": 4,
                            "seller_identifiers": ["seller-1", "unsafe/value"],
                            "is_available": True,
                        }
                    ],
                    "moq": {"minimum": 1, "increment_unit": 1},
                },
            )
        assert request.url.path.endswith("/products/cola/sizes/500ml/sellers")
        assert request.url.host == "www.jiomart.com"
        assert request.url.params == httpx.QueryParams(
            {"pincode": "400053", "strategy": "optimal", "page_no": "1", "page_size": "12"}
        )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "article_id": "article-1",
                        "item_id": "sku-1",
                        "quantity": 3,
                        "price": {"effective": 42, "marked": 50, "currency_code": "INR"},
                        "seller": {"uid": "seller-1", "name": "omit"},
                        "store": {"uid": "store-1", "name": "omit"},
                    }
                ],
                "page": {"has_next": False},
            },
        )

    api = client(handler)
    sizes = await api.get_product_sizes("cola", {"cookies": []}, LOCATION)
    sellers = await api.get_product_sellers("cola", "500ml", {"cookies": []}, LOCATION)

    assert sizes == {
        "sellable": True,
        "sizes": [{"size": "500ml", "display": "500 ml", "quantity": 4, "is_available": True, "seller_identifiers": ["seller-1"]}],
        "moq": {"increment_unit": 1, "minimum": 1},
        "coverage": "available",
    }
    assert sellers == {
        "items": [{"article_id": "article-1", "size": "500ml", "quantity": 3, "price": {"effective": 42, "marked": 50, "currency_code": "INR"}, "seller_uid": "seller-1", "store_uid": "store-1", "sku": "sku-1"}],
        "coverage": "available",
    }


@pytest.mark.asyncio
async def test_product_read_rejects_malformed_schema_and_invalid_identifiers():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sellable": True, "sizes": "not-a-list"})

    with pytest.raises(ValueError, match="slug"):
        await client(handler).get_product_sizes("bad/path", {"cookies": []}, LOCATION)
    with pytest.raises(CommerceError) as failure:
        await client(handler).get_product_sizes("cola", {"cookies": []}, LOCATION)
    assert failure.value.diagnostic_code == "response_contract"


@pytest.mark.asyncio
async def test_product_sizes_preserve_unknown_availability():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sizes": [
                    {
                        "display": "500 ml",
                        "value": "500ml",
                        "quantity": 0,
                        "seller_identifiers": [],
                    }
                ]
            },
        )

    result = await client(handler).get_product_sizes("cola", {"cookies": []}, LOCATION)

    assert result["sellable"] is None
    assert result["sizes"][0]["is_available"] is None


@pytest.mark.asyncio
async def test_product_seller_http_error_fails_closed_without_fallback_requests():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params == httpx.QueryParams(
            {"pincode": "400053", "strategy": "optimal", "page_no": "1", "page_size": "12"}
        )
        return httpx.Response(400, json={"message": "private provider detail"})

    with pytest.raises(CommerceError) as failure:
        await client(handler).get_product_sellers("cola", "500ml", {"cookies": []}, LOCATION)

    assert calls == 1
    assert failure.value.stage == "product_sellers"
    assert failure.value.http_status == 400
    assert failure.value.diagnostic_code == "http_400"


SESSION = {"cookies": [], "account_reference": "user-1", "application_id": APP_ID}
CHECKOUT_LOCATION = {**LOCATION, "id": "addr-1"}


def checkout_handler(calls, *, total=72, modes=None, checkout_status=200):
    modes = modes if modes is not None else [
        {"name": "JIOPP", "list": [{"code": "JIOPP", "aggregator_name": "Jio", "merchant_code": "JIOPP"}]},
        {"name": "COD", "list": [{"code": "COD", "aggregator_name": "Fynd", "merchant_code": "COD"}]},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, path, dict(request.url.params), body))
        if path.endswith("/authentication/v1.0/session"):
            return httpx.Response(200, json={"user": {"user_id": "user-1"}})
        if path.endswith("/cart/v1.0/select-address"):
            return httpx.Response(200, json={"success": True, "is_valid": True})
        if path.endswith("/cart/v1.0/detail"):
            return httpx.Response(200, json={
                "success": True, "cart_id": "cart-1", "last_modified": "t", "is_valid": True, "items": [],
                "breakup_values": {"raw": {"total": total}}, "currency": {"code": "INR"},
            })
        if path.endswith("/payment/v1.0/payment/options"):
            return httpx.Response(200, json={"success": True, "payment_options": {"payment_option": modes}})
        if path.endswith("/cart/v1.0/meta"):
            return httpx.Response(200, json={"success": True})
        if path.endswith("/cart/v2.0/checkout"):
            if checkout_status != 200:
                return httpx.Response(checkout_status, json={"success": False, "message": "Cart is invalid"})
            return httpx.Response(200, json={"success": True, "order_id": "FY-ORDER-1", "callback_url": "x",
                                             "data": {"merchant_order_id": "MO1", "merchant_transaction_id": "MT1"}})
        raise AssertionError(f"unexpected {path}")

    return handler


@pytest.mark.asyncio
async def test_checkout_rechecks_total_and_submits_a_cod_order_like_the_website():
    calls = []
    result = await client(checkout_handler(calls), writes=True, checkout=True).checkout(
        dict(SESSION), CHECKOUT_LOCATION, cart_id="cart-1", address_id="addr-1", amount=72
    )

    assert result == {"order_id": "FY-ORDER-1", "payment": None}
    steps = [(method, path.rsplit("/", 2)[-2] + "/" + path.rsplit("/", 1)[-1]) for method, path, _, _ in calls
             if not path.endswith("/session")]
    assert steps == [
        ("POST", "v1.0/select-address"), ("GET", "v1.0/detail"), ("GET", "payment/options"),
        ("PUT", "v1.0/meta"), ("POST", "v2.0/checkout"),
    ]
    select = next(c for c in calls if c[1].endswith("select-address"))
    assert select[2] == {"cart_id": "cart-1"}
    assert select[3] == {"cart_id": "cart-1", "id": "addr-1", "billing_address_id": "addr-1"}
    options = next(c for c in calls if c[1].endswith("payment/options"))
    assert options[2] == {"amount": "7200", "cart_id": "cart-1", "checkout_mode": "self"}
    meta = next(c for c in calls if c[1].endswith("/meta"))
    assert meta[3] == {"custom_cart_meta": {"channel_id": "whatsapp"}}
    body = calls[-1][3]
    assert body["id"] == "cart-1" and body["address_id"] == "addr-1"
    assert body["payment_mode"] == "COD" and body["aggregator"] == "Fynd"
    assert body["payment_methods"] == [{
        "mode": "COD", "name": "COD", "payment": "required", "payment_identifier": "COD",
        "payment_meta": {"merchant_code": "COD", "payment_gateway": "Fynd", "payment_identifier": "COD"},
    }]


@pytest.mark.asyncio
async def test_checkout_stops_before_submission_when_total_changed_or_cod_missing():
    calls = []
    with pytest.raises(CommerceError) as changed:
        await client(checkout_handler(calls, total=80), writes=True, checkout=True).checkout(
            dict(SESSION), CHECKOUT_LOCATION, cart_id="cart-1", address_id="addr-1", amount=72
        )
    assert changed.value.code == "changed"
    assert not any(c[1].endswith("/checkout") for c in calls)

    calls = []
    online_only = [{"name": "JIOPP", "list": [{"code": "JIOPP", "aggregator_name": "Jio", "merchant_code": "JIOPP"}]}]
    with pytest.raises(CommerceError) as no_cod:
        await client(checkout_handler(calls, modes=online_only), writes=True, checkout=True).checkout(
            dict(SESSION), CHECKOUT_LOCATION, cart_id="cart-1", address_id="addr-1", amount=72
        )
    assert no_cod.value.code == "rejected"
    assert no_cod.value.diagnostic_code == "cod_unavailable"
    assert not any(c[1].endswith("/checkout") for c in calls)


@pytest.mark.asyncio
async def test_checkout_requires_both_flags_and_reports_rejection_with_a_bounded_hint(caplog):
    calls = []
    with pytest.raises(CommerceError) as disabled:
        await client(checkout_handler(calls), writes=True).checkout(
            dict(SESSION), CHECKOUT_LOCATION, cart_id="cart-1", address_id="addr-1", amount=72
        )
    assert disabled.value.diagnostic_code == "checkout_disabled"
    assert calls == []

    with caplog.at_level("WARNING", logger="smartbazaar"):
        with pytest.raises(CommerceError) as rejected:
            await client(checkout_handler(calls, checkout_status=400), writes=True, checkout=True).checkout(
                dict(SESSION), CHECKOUT_LOCATION, cart_id="cart-1", address_id="addr-1", amount=72
            )
    assert rejected.value.code == "rejected" and rejected.value.http_status == 400
    assert "upstream=Cart is invalid" in caplog.messages[-1]


@pytest.mark.asyncio
async def test_cart_add_sends_the_website_reorder_shape_without_store_or_article():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/v1.0/session"):
            return httpx.Response(200, json={"user": {"user_id": "user-1"}})
        calls.append((request.method, dict(request.url.params), json.loads(request.content)))
        return httpx.Response(200, json={"success": True, "partial": False, "cart": {}})

    result = await client(handler, writes=True).add_cart_items(
        dict(SESSION), LOCATION,
        [{"item_id": "7519959", "item_size": "OS", "seller_id": "1", "store_id": "10495",
          "vertical_code": "GROCERIES", "quantity": 2}],
        "cart-1",
    )

    assert result == {"accepted": True, "partial": False}
    method, params, body = calls[0]
    assert method == "POST"
    assert params == {"i": "true", "b": "true", "area_code": "400053", "id": "cart-1"}
    assert body == {"items": [{
        "item_id": 7519959, "item_size": "OS", "quantity": 2, "seller_id": 1,
        "meta": {"vertical_code": "GROCERIES", "compute_delivery_fee": True},
    }]}


@pytest.mark.asyncio
async def test_expired_access_cookie_is_refreshed_once_and_the_request_retried():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("cookie", "")))
        if request.url.path.endswith("/token/refresh"):
            assert json.loads(request.content) == {"access_token": "eyJold.payload.sig"}
            return httpx.Response(200, json={"access_token": "eyJnew.payload.sig", "exp": 3600})
        if "f.session=eyJnew.payload.sig" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"message": "expired"})
        return httpx.Response(200, json={"items": [], "page": {"current": 1, "size": 12, "has_next": False}})

    session = {**SESSION, "cookies": [{"name": "f.session", "value": "eyJold.payload.sig", "domain": "api.jiomartjcp.com", "path": "/"}]}
    orders = await client(handler).list_orders(session)

    assert orders == []
    # An empty list also tries the storefront gateway, which JioMart's own My Orders page uses.
    assert [(path.split("/")[1], path.rsplit("/", 1)[-1]) for _, path, _ in calls] == [
        ("service", "orders"), ("service", "refresh"), ("service", "orders"), ("api", "orders"),
    ]
    assert any(c["value"] == "eyJnew.payload.sig" for c in session["cookies"])


@pytest.mark.asyncio
async def test_failed_refresh_still_reports_expired_sign_in_and_login_never_refreshes():
    paths = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(401, json={"message": "expired"})

    session = {**SESSION, "cookies": [{"name": "f.session", "value": "eyJold.payload.sig", "domain": "api.jiomartjcp.com", "path": "/"}]}
    with pytest.raises(CommerceError) as expired:
        await client(handler).list_orders(session)
    assert expired.value.code == "auth_expired" and paths == ["orders", "refresh"]

    paths.clear()
    with pytest.raises(CommerceError):
        await client(handler).verify_login({"request_id": "r", "mobile": "9000000001", "cookies": session["cookies"]}, "123456")
    assert "refresh" not in paths


@pytest.mark.asyncio
async def test_select_address_follows_one_jiomart_redirect_with_body_and_session():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/v1.0/session"):
            return httpx.Response(200, json={"user": {"user_id": "user-1"}})
        seen.append((request.method, request.url.host, request.url.path, request.headers.get("cookie", ""),
                     json.loads(request.content) if request.content else None))
        if request.url.host == "api.jiomartjcp.com":
            return httpx.Response(307, headers={"location": "https://www.jiomart.com/ext/cart/select-address?cart_id=cart-1"})
        return httpx.Response(200, json={"success": True})

    session = {**SESSION, "cookies": [{"name": "f.session", "value": "s1", "domain": "api.jiomartjcp.com", "path": "/"}]}
    await client(handler, writes=True).select_address(session, LOCATION, "cart-1", "addr-1")

    assert [(m, host, path) for m, host, path, _, _ in seen] == [
        ("POST", "api.jiomartjcp.com", "/service/application/cart/v1.0/select-address"),
        ("POST", "www.jiomart.com", "/ext/cart/select-address"),
    ]
    assert "f.session=s1" in seen[1][3]
    assert seen[1][4] == {"cart_id": "cart-1", "id": "addr-1", "billing_address_id": "addr-1"}


@pytest.mark.asyncio
async def test_redirect_to_a_foreign_host_is_refused():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/v1.0/session"):
            return httpx.Response(200, json={"user": {"user_id": "user-1"}})
        return httpx.Response(307, headers={"location": "https://evil.example/ext/steal"})

    with pytest.raises(CommerceError) as refused:
        await client(handler, writes=True).select_address(dict(SESSION), LOCATION, "cart-1", "addr-1")
    assert refused.value.diagnostic_code == "unsafe_redirect"


@pytest.mark.asyncio
async def test_relative_extension_redirect_is_sent_to_the_storefront_host():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/v1.0/session"):
            return httpx.Response(200, json={"user": {"user_id": "user-1"}})
        seen.append((request.url.host, request.url.path, request.headers.get("cookie", "")))
        if request.url.host == "api.jiomartjcp.com":
            return httpx.Response(307, headers={"location": "/ext/jmshipmentfee/cart/v1.0/select_address?cart_id=cart-1"})
        return httpx.Response(200, json={"success": True})

    session = {**SESSION, "cookies": [{"name": "f.session", "value": "s1", "domain": "api.jiomartjcp.com", "path": "/"}]}
    await client(handler, writes=True).select_address(session, LOCATION, "cart-1", "addr-1")

    assert seen[1][:2] == ("www.jiomart.com", "/ext/jmshipmentfee/cart/v1.0/select_address")
    assert "f.session=s1" in seen[1][2]


@pytest.mark.asyncio
async def test_online_checkout_returns_jiomarts_payment_fields_for_the_pay_page():
    calls = []
    online = [{"name": "JIOPP", "list": [{"code": "JIOPP", "aggregator_name": "JioOnePay", "merchant_code": "JIOPP"}]},
              {"name": "COD", "list": [{"code": "COD", "aggregator_name": "Fynd", "merchant_code": "COD"}]}]

    def with_flow(handler):
        async def wrapped(request):
            if request.url.path.endswith("/payment/v1.0/payment/options"):
                return httpx.Response(200, json={"success": True, "payment_options": {
                    "payment_option": online,
                    "payment_flows": {"JioOnePay": {"api_link": "https://www.jiomart.com/ext/jioonepay/pay", "type": "standard_extension"}},
                }})
            return await handler(request)
        return wrapped

    result = await client(with_flow(checkout_handler(calls)), writes=True, checkout=True).checkout(
        dict(SESSION), CHECKOUT_LOCATION, cart_id="cart-1", address_id="addr-1", amount=72,
        method="online", callback_url="https://bot.ngrok-free.app/paid/token",
    )

    assert result == {"order_id": "FY-ORDER-1", "payment": {
        "api_link": "https://www.jiomart.com/ext/jioonepay/pay",
        "fields": {"merchant_order_id": "MO1", "merchant_transaction_id": "MT1"},
    }}
    body = calls[-1][3]
    assert body["payment_mode"] == "JIOPP" and body["aggregator"] == "JioOnePay"
    assert body["callback_url"] == "https://bot.ngrok-free.app/paid/token"


PAY_LINK = "https://www.jiomart.com/api/service/webhooks/payment/v1.0/payment_session"


@pytest.mark.asyncio
async def test_payment_session_is_opened_with_the_account_session_and_returns_the_provider_page():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.host, request.headers.get("cookie", ""), request.content.decode()))
        if request.url.host == "www.jiomart.com":
            return httpx.Response(307, headers={"location": "https://store1.jiomartjcp.com/service/webhooks/payment/v1.0/payment_session"})
        return httpx.Response(302, headers={"location": "https://pay.jiopay.com/checkout?token=t1"})

    session = {**SESSION, "cookies": [{"name": "f.session", "value": "s1", "domain": "api.jiomartjcp.com", "path": "/"}]}
    opened = await client(handler).open_payment_session(session, PAY_LINK, {"merchant_order_id": "MO1", "merchant_transaction_id": "MT1"})

    assert opened == {"redirect": "https://pay.jiopay.com/checkout?token=t1"}
    assert [(m, h) for m, h, _, _ in seen] == [("POST", "www.jiomart.com"), ("POST", "store1.jiomartjcp.com")]
    assert all("f.session=s1" in cookie for _, _, cookie, _ in seen)
    assert "merchant_order_id=MO1" in seen[1][3]


@pytest.mark.asyncio
async def test_payment_session_html_hand_off_is_turned_into_a_form_and_failures_are_explicit():
    async def form_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            "<html><body><form method='post' action='https://pg.jio.com/pay'>"
            "<input type='hidden' name='token' value='abc'><input name='mid' value='m1'></form></body></html>"))

    opened = await client(form_handler).open_payment_session(dict(SESSION), PAY_LINK, {"merchant_order_id": "MO1"})
    assert opened == {"form": {"action": "https://pg.jio.com/pay", "method": "post", "fields": {"token": "abc", "mid": "m1"}}}

    async def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"success": False, "message": "Invalid session or session expired."})

    with pytest.raises(CommerceError) as expired:
        await client(unauthorized).open_payment_session(dict(SESSION), PAY_LINK, {"merchant_order_id": "MO1"})
    assert expired.value.code == "auth_expired"

    with pytest.raises(ValueError):
        await client(form_handler).open_payment_session(dict(SESSION), "https://evil.example/pay", {})


@pytest.mark.asyncio
async def test_payment_session_never_follows_a_lookalike_host_with_the_account_cookie():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil-jiomart.com/steal"})

    session = {**SESSION, "cookies": [{"name": "f.session", "value": "s1", "domain": "api.jiomartjcp.com", "path": "/"}]}
    opened = await client(handler).open_payment_session(session, PAY_LINK, {"merchant_order_id": "MO1"})

    # The lookalike is treated as a foreign page for the browser, never requested by the bot.
    assert opened == {"redirect": "https://evil-jiomart.com/steal"}
    assert seen == ["www.jiomart.com"]
