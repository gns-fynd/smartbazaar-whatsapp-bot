import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from smartbazaar.jiomart import CommerceError, JioMartClient  # noqa: I001


APP_ID = "app-1"
SESSION = {"account_reference": "user-1", "application_id": APP_ID, "cookies": []}


def client(handler, *, order_cancel=True):
    return JioMartClient(
        APP_ID,
        application_token="test-token",
        transport=httpx.MockTransport(handler),
        enable_order_cancel=order_cancel,
    )


# --- cancellation_reasons --------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_reasons_disabled_flag_raises_before_any_request():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected when cancellation is disabled")

    with pytest.raises(CommerceError) as failure:
        await client(handler, order_cancel=False).cancellation_reasons(dict(SESSION), "SH1")
    assert failure.value.code == "unavailable"
    assert failure.value.diagnostic_code == "order_cancel_disabled"


@pytest.mark.asyncio
async def test_cancellation_reasons_requests_documented_route_and_sanitizes_the_list():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "reasons": [
                    {"reason_id": 1, "reason_text": "Ordered by mistake", "priority": 1},
                    {"reason_id": 2, "reason_text": "Found a better price"},
                    {"reason_id": "not-an-int", "reason_text": "Bad shape"},
                    {"reason_text": "Missing id"},
                    {"reason_id": 3, "reason_text": ""},
                ]
            },
        )

    reasons = await client(handler).cancellation_reasons(dict(SESSION), "SH1")

    assert seen == [("GET", "/service/application/order/v1.0/orders/shipments/SH1/reasons")]
    assert reasons == [
        {"id": 1, "text": "Ordered by mistake"},
        {"id": 2, "text": "Found a better price"},
    ]


@pytest.mark.asyncio
async def test_cancellation_reasons_rejects_an_invalid_shipment_id():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected for an invalid shipment id")

    with pytest.raises(ValueError):
        await client(handler).cancellation_reasons(dict(SESSION), "bad id/with slash")


# --- cancel_shipment --------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_shipment_disabled_flag_raises_before_any_request():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected when cancellation is disabled")

    with pytest.raises(CommerceError) as failure:
        await client(handler, order_cancel=False).cancel_shipment(
            dict(SESSION), "SH1", reason_id=1, reason_text="Ordered by mistake"
        )
    assert failure.value.code == "unavailable"
    assert failure.value.diagnostic_code == "order_cancel_disabled"


@pytest.mark.asyncio
async def test_cancel_shipment_sends_the_documented_update_shipment_status_body():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        import json

        body = json.loads(request.content)
        assert body == {
            "statuses": [{
                "shipments": [{
                    "identifier": "SH1",
                    "status": "cancelled_customer",
                    "reasons": {"entities": [{"data": {"reason_id": 7, "reason_text": "Ordered by mistake"}}]},
                }],
            }],
        }
        return httpx.Response(200, json={"statuses": [], "shipments": []})

    result = await client(handler).cancel_shipment(dict(SESSION), "SH1", reason_id=7, reason_text="Ordered by mistake")

    assert seen == [("PUT", "/service/application/order-manage/v1.0/orders/shipments/SH1/status")]
    assert result == {"shipment_id": "SH1", "status": "cancelled_customer"}


@pytest.mark.asyncio
async def test_cancel_shipment_definite_rejection_is_not_confused_with_an_unknown_outcome():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "message": "Shipment is not cancellable"})

    with pytest.raises(CommerceError) as failure:
        await client(handler).cancel_shipment(dict(SESSION), "SH1", reason_id=1, reason_text="Changed my mind")
    assert failure.value.code == "rejected"
    assert failure.value.diagnostic_code == "cancel_rejected"
    assert "not cancellable" not in str(failure.value)


@pytest.mark.asyncio
async def test_cancel_shipment_http_500_is_an_unknown_outcome_never_a_rejection():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    with pytest.raises(CommerceError) as failure:
        await client(handler).cancel_shipment(dict(SESSION), "SH1", reason_id=1, reason_text="Changed my mind")
    assert failure.value.code == "unknown"
    assert failure.value.diagnostic_code == "http_5xx"


@pytest.mark.asyncio
async def test_cancel_shipment_transport_failure_is_also_an_unknown_outcome():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    with pytest.raises(CommerceError) as failure:
        await client(handler).cancel_shipment(dict(SESSION), "SH1", reason_id=1, reason_text="Changed my mind")
    assert failure.value.code == "unknown"
    assert failure.value.diagnostic_code == "transport_error"


@pytest.mark.asyncio
async def test_cancel_shipment_requires_a_non_empty_reason():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected for a missing reason")

    with pytest.raises(ValueError):
        await client(handler).cancel_shipment(dict(SESSION), "SH1", reason_id=1, reason_text="   ")


# --- invoice_pdf -------------------------------------------------------------


INVOICE_URL = "https://api.jiomartjcp.com/files/invoice-SH1.pdf"


def invoice_meta_handler(pdf_response: httpx.Response, *, url: str = INVOICE_URL):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/invoice"):
            return httpx.Response(200, json={"success": True, "shipment_id": "SH1", "presigned_url": url})
        return pdf_response

    return handler


@pytest.mark.asyncio
async def test_invoice_pdf_downloads_the_presigned_document():
    pdf_bytes = b"%PDF-1.4 fake invoice content"
    handler = invoice_meta_handler(httpx.Response(200, content=pdf_bytes, headers={"content-type": "application/pdf"}))

    result = await client(handler, order_cancel=False).invoice_pdf(dict(SESSION), "SH1")

    assert result == {"filename": "invoice_SH1.pdf", "content": pdf_bytes}


@pytest.mark.asyncio
async def test_invoice_pdf_not_yet_generated_is_a_distinct_error_code():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "invoice not found"})

    with pytest.raises(CommerceError) as failure:
        await client(handler, order_cancel=False).invoice_pdf(dict(SESSION), "SH1")
    assert failure.value.diagnostic_code == "invoice_unavailable"
    assert failure.value.http_status == 404


@pytest.mark.asyncio
async def test_invoice_pdf_rejects_an_untrusted_host():
    handler = invoice_meta_handler(
        httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}),
        url="https://attacker.example/invoice.pdf",
    )

    with pytest.raises(CommerceError) as failure:
        await client(handler, order_cancel=False).invoice_pdf(dict(SESSION), "SH1")
    assert failure.value.diagnostic_code == "invoice_host_blocked"


@pytest.mark.asyncio
async def test_invoice_pdf_rejects_an_oversize_document_by_content_length():
    oversize = 5 * 1024 * 1024 + 1
    handler = invoice_meta_handler(
        httpx.Response(
            200, content=b"%PDF-1.4",
            headers={"content-type": "application/pdf", "content-length": str(oversize)},
        )
    )

    with pytest.raises(CommerceError) as failure:
        await client(handler, order_cancel=False).invoice_pdf(dict(SESSION), "SH1")
    assert failure.value.diagnostic_code == "invoice_too_large"


@pytest.mark.asyncio
async def test_invoice_pdf_rejects_a_document_that_is_not_actually_a_pdf():
    handler = invoice_meta_handler(
        httpx.Response(200, content=b"<html>not a pdf</html>", headers={"content-type": "text/html"})
    )

    with pytest.raises(CommerceError) as failure:
        await client(handler, order_cancel=False).invoice_pdf(dict(SESSION), "SH1")
    assert failure.value.diagnostic_code == "invoice_not_pdf"


@pytest.mark.asyncio
async def test_invoice_pdf_rejects_an_invalid_shipment_id():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected for an invalid shipment id")

    with pytest.raises(ValueError):
        await client(handler, order_cancel=False).invoice_pdf(dict(SESSION), "../etc/passwd")


# --- additive shipment/item summary fields ----------------------------------


def test_shipment_summary_exposes_tri_state_cancel_and_invoice_flags():
    with_flags = JioMartClient._shipment_summary({
        "shipment_id": "SH1",
        "shipment_status": {"title": "Placed", "value": "placed"},
        "can_cancel": True,
        "show_download_invoice": True,
        "bags": [],
    })
    assert with_flags["can_cancel"] is True
    assert with_flags["invoice_available"] is True

    without_flags = JioMartClient._shipment_summary({
        "shipment_id": "SH1",
        "shipment_status": {"title": "Placed", "value": "placed"},
        "bags": [],
    })
    assert without_flags["can_cancel"] is None
    assert without_flags["invoice_available"] is None

    url_only = JioMartClient._shipment_summary({
        "shipment_id": "SH1",
        "shipment_status": {"title": "Placed", "value": "placed"},
        "invoice": {"invoice_url": "https://www.jiomart.com/invoice/SH1"},
        "bags": [],
    })
    assert url_only["invoice_available"] is True


def test_order_item_exposes_bag_id_line_number_and_identifier_for_cancellation():
    item = JioMartClient._order_item({
        "quantity": 2,
        "line_number": 1,
        "identifier": "SKU-1",
        "item": {"name": "Amul Milk 500 ml", "id": 101, "code": "SKU-FALLBACK"},
        "prices": {"price_effective": 42, "id": "bag-999"},
    })
    assert item["bag_id"] == "bag-999"
    assert item["line_number"] == 1
    assert item["identifier"] == "SKU-1"

    fallback = JioMartClient._order_item({
        "quantity": 1,
        "item": {"name": "Amul Milk 500 ml", "id": 101, "code": "SKU-FALLBACK"},
        "prices": {"price_effective": 42},
    })
    assert fallback["bag_id"] is None
    assert fallback["line_number"] is None
    assert fallback["identifier"] == "SKU-FALLBACK"
