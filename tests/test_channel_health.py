from datetime import UTC, datetime, timedelta

import httpx
import pytest

from smartbazaar.channel_health import (
    ChannelHealthProbe,
    ChannelHealthReason,
    ChannelHealthState,
)

NOW = datetime(2026, 10, 18, 10, tzinfo=UTC)


def debug(*, valid=True, app_id="app-1", scopes=None, expires=0, data_access_expires=0):
    return {
        "data": {
            "is_valid": valid,
            "app_id": app_id,
            "scopes": scopes if scopes is not None else ["whatsapp_business_messaging"],
            "expires_at": expires,
            "data_access_expires_at": data_access_expires,
        }
    }


def probe(handler, **kwargs):
    return ChannelHealthProbe(
        "private-token",
        "app-1",
        "private-secret",
        "phone-1",
        "v25.0",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_available_token_probes_configured_phone_without_exposing_identifiers():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("debug_token"):
            return httpx.Response(200, json=debug(expires=int((NOW + timedelta(days=2)).timestamp())))
        return httpx.Response(200, json={"id": "phone-1"})

    client = probe(handler)
    try:
        status = await client.check()
    finally:
        await client.close()

    assert status.status is ChannelHealthState.AVAILABLE
    assert status.reason is ChannelHealthReason.OK
    assert status.expires_at == NOW + timedelta(days=2)
    assert [request.url.path for request in requests] == ["/v25.0/debug_token", "/v25.0/phone-1"]
    assert requests[0].url.params.get("input_token") == "private-token"
    assert requests[0].headers["Authorization"] == "Bearer app-1|private-secret"
    assert requests[1].url.params.get("fields") == "id"


@pytest.mark.asyncio
async def test_expired_token_is_auth_rejected_before_phone_probe():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=debug(expires=int((NOW - timedelta(seconds=1)).timestamp())))

    client = probe(handler)
    try:
        status = await client.check()
    finally:
        await client.close()

    assert (status.status, status.reason, calls) == (
        ChannelHealthState.AUTH_REJECTED,
        ChannelHealthReason.EXPIRED,
        1,
    )


@pytest.mark.asyncio
async def test_unknown_or_malformed_debug_response_is_unavailable():
    client = probe(lambda _: httpx.Response(200, json={"data": {"is_valid": True}}))
    try:
        status = await client.check()
    finally:
        await client.close()
    assert (status.status, status.reason) == (
        ChannelHealthState.UNAVAILABLE,
        ChannelHealthReason.MALFORMED_RESPONSE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry", [True, -1, 10**30])
async def test_invalid_epoch_is_rejected_as_malformed(expiry):
    client = probe(lambda _: httpx.Response(200, json=debug(expires=expiry)))
    try:
        status = await client.check()
    finally:
        await client.close()
    assert status.reason is ChannelHealthReason.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_app_mismatch_and_missing_scope_are_unavailable():
    for payload, reason in (
        (debug(app_id="other"), ChannelHealthReason.APP_MISMATCH),
        (debug(scopes=[]), ChannelHealthReason.MISSING_SCOPE),
    ):
        client = probe(lambda _, payload=payload: httpx.Response(200, json=payload))
        try:
            status = await client.check()
        finally:
            await client.close()
        assert (status.status, status.reason) == (ChannelHealthState.UNAVAILABLE, reason)


@pytest.mark.asyncio
async def test_no_expiry_is_available_but_not_a_forever_validity_claim():
    def handler(request):
        return httpx.Response(200, json=debug() if request.url.path.endswith("debug_token") else {"id": "phone-1"})

    client = probe(handler)
    try:
        status = await client.check()
    finally:
        await client.close()
    assert status.status is ChannelHealthState.AVAILABLE
    assert status.expires_at is None


@pytest.mark.asyncio
async def test_http_401_and_network_failure_have_safe_statuses():
    auth = probe(lambda _: httpx.Response(401))
    network = probe(lambda _: (_ for _ in ()).throw(httpx.ConnectError("private-token")))
    try:
        auth_status = await auth.check()
        network_status = await network.check()
    finally:
        await auth.close()
        await network.close()
    assert (auth_status.status, auth_status.reason) == (
        ChannelHealthState.AUTH_REJECTED,
        ChannelHealthReason.HTTP_AUTH,
    )
    assert (network_status.status, network_status.reason) == (
        ChannelHealthState.UNAVAILABLE,
        ChannelHealthReason.TRANSPORT_ERROR,
    )


@pytest.mark.asyncio
async def test_expiring_token_warns_within_24_hours():
    def handler(request):
        expiry = int((NOW + timedelta(hours=23)).timestamp())
        return httpx.Response(200, json=debug(expires=expiry) if request.url.path.endswith("debug_token") else {"id": "phone-1"})

    client = probe(handler)
    try:
        status = await client.check()
    finally:
        await client.close()
    assert (status.status, status.reason) == (ChannelHealthState.EXPIRING, ChannelHealthReason.EXPIRING_SOON)


@pytest.mark.asyncio
async def test_accessible_phone_but_denied_configured_account_is_unavailable():
    def handler(request):
        if request.url.path.endswith("debug_token"):
            return httpx.Response(200, json=debug())
        if request.url.path.endswith("phone-1"):
            return httpx.Response(200, json={"id": "phone-1"})
        return httpx.Response(400, json={"error": {"code": 10}})

    client = probe(handler, account_id="waba-1")
    try:
        status = await client.check()
    finally:
        await client.close()
    assert (status.status, status.reason) == (
        ChannelHealthState.UNAVAILABLE,
        ChannelHealthReason.ACCOUNT_ACCESS,
    )


@pytest.mark.asyncio
async def test_matching_configured_account_is_required_for_available_status():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("debug_token"):
            return httpx.Response(200, json=debug())
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})

    client = probe(handler, account_id="waba-1")
    try:
        status = await client.check()
    finally:
        await client.close()
    assert status.status is ChannelHealthState.AVAILABLE
    assert [request.url.path for request in requests] == [
        "/v25.0/debug_token",
        "/v25.0/phone-1",
        "/v25.0/waba-1",
    ]
