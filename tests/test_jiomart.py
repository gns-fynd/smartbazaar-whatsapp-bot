import json
import sys
import time
from base64 import b64encode, urlsafe_b64encode
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from smartbazaar.jiomart import CommerceError, JioMartClient  # noqa: I001


APP_ID = "685945f46c8c7aee3f3af605"
ADDRESS_PATH = "/service/application/cart/v1.0/address"
REFRESH_PATH = "/service/application/user/authentication/v1.0/token/refresh"


def _fake_jwt(exp: int) -> str:
    """A syntactically valid JWT (header.payload.sig) carrying only an ``exp`` claim."""

    def segment(payload: dict) -> str:
        return urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()

    return f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment({'exp': exp})}.sig"


def _session_with_token(token: str) -> dict:
    return {"cookies": [{"name": "access_token", "value": token, "domain": "", "path": "/"}]}


def test_unverified_phone_cannot_establish_account_identity():
    assert not JioMartClient._has_matching_mobile(
        {"user": {"phone_numbers": [{"phone": "9000000001", "verified": False}]}},
        "9000000001",
    )


def client(handler: httpx.MockTransport | object) -> JioMartClient:
    transport = (
        handler if isinstance(handler, httpx.MockTransport) else httpx.MockTransport(handler)
    )
    return JioMartClient(APP_ID, application_token="public-token", transport=transport)


@pytest.mark.asyncio
async def test_login_challenge_cookies_are_not_shared_between_customers():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login/otp"):
            body = json.loads(request.content)
            mobile = body["mobile"]
            assert body == {"mobile": mobile, "country_code": "91", "action": "login"}
            assert request.url.params["platform"] == APP_ID
            assert (
                request.headers["authorization"]
                == "Bearer " + b64encode(f"{APP_ID}:public-token".encode()).decode()
            )
            return httpx.Response(
                200,
                json={"success": True, "request_id": f"request-{mobile}"},
                headers={"set-cookie": f"challenge={mobile}; Path=/; Secure"},
            )
        assert request.headers.get("cookie") in {"challenge=9999999991", "challenge=9999999992"}
        return httpx.Response(200, json={"success": True, "items": []})

    api = client(handler)
    first = await api.begin_login("+91 99999 99991")
    second = await api.begin_login("+91 99999 99992")
    assert first["cookies"] != second["cookies"]

    await api.get_addresses({"cookies": first["cookies"]})
    await api.get_addresses({"cookies": second["cookies"]})


@pytest.mark.asyncio
async def test_empty_config_discovers_public_runtime_token_only_in_memory():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(
                200, text='<script>applicationToken: "runtime-public-token"</script>'
            )
        expected = b64encode(f"{APP_ID}:runtime-public-token".encode()).decode()
        assert request.headers["authorization"] == f"Bearer {expected}"
        return httpx.Response(200, json={"success": True, "request_id": "request-1"})

    api = JioMartClient(APP_ID, transport=httpx.MockTransport(handler))
    challenge = await api.begin_login("919999999991")
    assert challenge["request_id"] == "request-1"
    assert api._application_token == "runtime-public-token"


@pytest.mark.asyncio
async def test_verify_login_rejects_a_different_session_identity():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/verify"):
            body = json.loads(request.content)
            assert body["action"] == "login"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "user_exists": True,
                    "user": {"phone_numbers": [{"phone": "9999999991", "verified": True}]},
                },
            )
        if request.url.path.endswith("/session"):
            return httpx.Response(
                200, json={"success": True, "user": {"mobile": "919999999999", "id": "u1"}}
            )
        raise AssertionError(request.url)

    with pytest.raises(CommerceError, match="existing account") as failure:
        await client(handler).verify_login(
            {"request_id": "request-1", "mobile": "919999999991", "cookies": []}, "123456"
        )
    assert failure.value.code == "invalid_otp"


@pytest.mark.asyncio
async def test_verify_login_accepts_documented_flagless_response_contracts():
    paths = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/verify"):
            assert json.loads(request.content) == {
                "action": "login",
                "otp": "123456",
                "request_id": "request-1",
            }
            return httpx.Response(
                200,
                json={
                    "user_exists": True,
                    "user": {"phone_numbers": [{"phone": "9000000001", "verified": True}]},
                },
                headers={"set-cookie": "authenticated=yes; Path=/; Secure"},
            )
        if request.url.path.endswith("/session"):
            assert request.headers["cookie"] == "authenticated=yes"
            return httpx.Response(
                200,
                json={
                    "user": {
                        "phone_numbers": [{"phone": "9000000001", "verified": True}],
                        "application_id": APP_ID,
                        "user_id": "user-1",
                    }
                },
            )
        raise AssertionError(request.url)

    session = await client(handler).verify_login(
        {"request_id": "request-1", "mobile": "9000000001", "cookies": []}, "123456"
    )

    assert session["mobile"] == "9000000001"
    assert session["account_reference"] == "user-1"
    assert "tokens" not in session
    assert session["cookies"][0]["name"] == "authenticated"
    assert paths == [
        "/service/application/user/authentication/v1.0/otp/mobile/verify",
        "/service/application/user/authentication/v1.0/session",
    ]


@pytest.mark.asyncio
async def test_verify_login_rejects_explicit_failure_even_with_success_shaped_fields():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/verify")
        return httpx.Response(
            200,
            json={
                "success": False,
                "user_exists": True,
                "user": {"phone_numbers": [{"phone": "9000000001", "verified": True}]},
                "error": "OTP invalid",
            },
        )

    with pytest.raises(CommerceError) as failure:
        await client(handler).verify_login(
            {"request_id": "request-1", "mobile": "9000000001", "cookies": []}, "123456"
        )
    assert failure.value.code == "invalid_otp"
    assert "OTP invalid" not in str(failure.value)


@pytest.mark.asyncio
async def test_verify_login_maps_an_invalid_otp_response_to_a_safe_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/verify")
        return httpx.Response(400, json={"message": "OTP invalid for 9000000001"})

    with pytest.raises(CommerceError) as failure:
        await client(handler).verify_login(
            {"request_id": "request-1", "mobile": "9000000001", "cookies": []}, "123456"
        )
    assert failure.value.code == "invalid_otp"
    assert "9000000001" not in str(failure.value)


@pytest.mark.asyncio
async def test_auth_diagnostic_logs_only_bounded_stage_status_and_code(caplog):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "message": "private provider body",
                "phone": "9000000001",
                "otp": "123456",
                "access_token": "secret-token",
            },
        )

    with caplog.at_level("WARNING", logger="smartbazaar"):
        with pytest.raises(CommerceError) as failure:
            await client(handler).verify_login(
                {"request_id": "request-1", "mobile": "9000000001", "cookies": []},
                "123456",
            )

    assert failure.value.code == "unavailable"
    assert failure.value.stage == "verify_login"
    assert failure.value.http_status == 422
    assert failure.value.diagnostic_code == "http_422"
    assert caplog.messages == ["commerce_failed stage=verify_login status=422 code=http_422"]
    logged = "\n".join(caplog.messages)
    assert "private provider body" not in logged
    assert "9000000001" not in logged
    assert "123456" not in logged
    assert "secret-token" not in logged


@pytest.mark.asyncio
async def test_verify_login_requires_the_established_session_to_match_requested_mobile():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/verify"):
            return httpx.Response(
                200,
                json={"success": True, "user_exists": True, "user": {"phone": "9000000001"}},
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "user": {"phone_numbers": [{"phone": "9000000002", "verified": True}]},
            },
        )

    with pytest.raises(CommerceError) as failure:
        await client(handler).verify_login(
            {"request_id": "request-1", "mobile": "9000000001", "cookies": []}, "123456"
        )
    assert failure.value.code == "invalid_otp"


def test_manual_pincode_location_is_valid_and_addresses_keep_default_metadata():
    assert JioMartClient._location_detail(
        {"pincode": "400053", "country": "India", "country_iso_code": "IN"}
    ) == {
        "pincode": "400053",
        "country": "India",
        "country_iso_code": "IN",
    }
    assert (
        JioMartClient._normalise_address({"pincode": "400053", "is_default": True})["is_default"]
        is True
    )


@pytest.mark.asyncio
async def test_live_address_envelope_preserves_search_area_without_customer_pii():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/address")
        return httpx.Response(
            200,
            json={
                "address": [
                    {
                        "area_code": "400053",
                        "is_default_address": True,
                        "address_type": "home",
                        "city": "Mumbai",
                        "state": "Maharashtra",
                        "country": "India",
                        "country_iso_code": "IN",
                        "phone": "9000000001",
                        "address": "Private street detail",
                        "user_id": "private-customer-id",
                    }
                ],
                "pii_masking": False,
            },
        )

    addresses = await client(handler).get_addresses({"cookies": []})
    assert addresses == [
        {
            "label": "home",
            "pincode": "400053",
            "city": "Mumbai",
            "state": "Maharashtra",
            "country": "India",
            "country_iso_code": "IN",
            "is_default": True,
        }
    ]


@pytest.mark.asyncio
async def test_auth_expiry_has_safe_error_and_refreshes_mutable_session_cookies():
    async def expired(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='{"message":"customer secret must not leak"}')

    with pytest.raises(CommerceError) as failure:
        await client(expired).get_addresses({"cookies": []})
    assert failure.value.code == "auth_expired"
    assert "secret" not in str(failure.value)

    async def refresh_cookie(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": True, "items": []},
            headers={"set-cookie": "refresh=rotated; Path=/; Secure"},
        )

    session = {"cookies": []}
    await client(refresh_cookie).get_addresses(session)
    assert session["cookies"][0]["name"] == "refresh"
    assert session["cookies"][0]["value"] == "rotated"


@pytest.mark.asyncio
async def test_search_uses_verified_gateway_location_and_partial_coverage():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.startswith("/api/"):
            return httpx.Response(
                302,
                headers={"location": "/ext/vertex/application/api/v1.0/products?q=rice"},
            )
        return httpx.Response(200, json={"items": [{"uid": "sku-1"}], "page": {"next_id": "n1"}})

    result = await client(handler).search(
        "rice",
        {
            "country": "India",
            "country_iso_code": "IN",
            "pincode": "400053",
            "city": "Mumbai",
            "state": "MH",
        },
        {"cookies": []},
    )
    assert result == {"items": [{"uid": "sku-1"}], "page": {"next_id": "n1"}, "coverage": "partial"}
    assert json.loads(requests[0].headers["x-location-detail"])["pincode"] == "400053"
    assert requests[0].headers["x-currency"] == "INR"
    assert requests[1].url.host == "www.jiomart.com"


@pytest.mark.asyncio
async def test_search_rejects_untrusted_redirect_and_response_failures():
    async def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://attacker.example/products"})

    location = {
        "country": "India",
        "country_iso_code": "IN",
        "pincode": "400053",
        "city": "Mumbai",
        "state": "MH",
    }
    with pytest.raises(CommerceError) as failure:
        await client(redirect).search("rice", location, {"cookies": []})
    assert failure.value.code == "unavailable"

    async def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="private gateway response")

    with pytest.raises(CommerceError) as failure:
        await client(rate_limited).get_addresses({"cookies": []})
    assert failure.value.code == "rate_limited"


@pytest.mark.asyncio
async def test_proactive_refresh_renews_the_token_before_it_expires():
    now = int(time.time())
    old_token, new_token = _fake_jwt(now + 60), _fake_jwt(now + 3600)
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/token/refresh"):
            assert json.loads(request.content) == {"access_token": old_token}
            return httpx.Response(200, json={"access_token": new_token})
        return httpx.Response(200, json={"success": True, "items": []})

    session = _session_with_token(old_token)
    await client(handler).get_addresses(session)

    assert calls == [REFRESH_PATH, ADDRESS_PATH]
    assert session["cookies"][0]["value"] == new_token


@pytest.mark.asyncio
async def test_refresh_falls_back_to_cookie_only_body_after_a_400_on_the_documented_body(caplog):
    now = int(time.time())
    old_token, new_token = _fake_jwt(now + 60), _fake_jwt(now + 3600)
    bodies = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token/refresh"):
            bodies.append(request.content)
            if request.content:
                return httpx.Response(400, json={"message": "bad request"})
            return httpx.Response(200, json={"access_token": new_token})
        return httpx.Response(200, json={"success": True, "items": []})

    session = _session_with_token(old_token)
    with caplog.at_level("INFO", logger="smartbazaar"):
        await client(handler).get_addresses(session)

    assert len(bodies) == 2
    assert json.loads(bodies[0]) == {"access_token": old_token}
    assert bodies[1] == b""
    assert session["cookies"][0]["value"] == new_token
    assert "session_refreshed variant=cookie" in caplog.text


@pytest.mark.asyncio
async def test_no_proactive_refresh_when_the_token_is_far_from_expiry():
    now = int(time.time())
    token = _fake_jwt(now + 3600)
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"success": True, "items": []})

    session = _session_with_token(token)
    await client(handler).get_addresses(session)

    assert calls == [ADDRESS_PATH]
    assert session["cookies"][0]["value"] == token


@pytest.mark.asyncio
async def test_after_401_refresh_fallback_still_works_even_with_a_healthy_looking_token():
    now = int(time.time())
    old_token, new_token = _fake_jwt(now + 3600), _fake_jwt(now + 7200)
    calls = []
    address_attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal address_attempts
        calls.append(request.url.path)
        if request.url.path.endswith("/token/refresh"):
            return httpx.Response(200, json={"access_token": new_token})
        address_attempts += 1
        if address_attempts == 1:
            return httpx.Response(401, text="expired")
        return httpx.Response(200, json={"success": True, "items": []})

    session = _session_with_token(old_token)
    await client(handler).get_addresses(session)

    assert calls == [ADDRESS_PATH, REFRESH_PATH, ADDRESS_PATH]
    assert session["cookies"][0]["value"] == new_token


@pytest.mark.asyncio
async def test_keep_alive_skips_a_token_that_is_not_near_expiry():
    now = int(time.time())
    healthy_token = _fake_jwt(now + 3600)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no call expected when the token is healthy")

    session = _session_with_token(healthy_token)
    assert await client(handler).keep_alive(session, within=900) is True
    assert session["cookies"][0]["value"] == healthy_token


@pytest.mark.asyncio
async def test_keep_alive_refreshes_and_saves_a_token_near_expiry():
    now = int(time.time())
    old_token, new_token = _fake_jwt(now + 60), _fake_jwt(now + 3600)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/token/refresh")
        return httpx.Response(200, json={"access_token": new_token})

    session = _session_with_token(old_token)
    assert await client(handler).keep_alive(session, within=900) is True
    assert session["cookies"][0]["value"] == new_token


@pytest.mark.asyncio
async def test_keep_alive_reports_an_unusable_session_without_an_access_token():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no call expected without an access token cookie")

    assert await client(handler).keep_alive({"cookies": []}) is False
