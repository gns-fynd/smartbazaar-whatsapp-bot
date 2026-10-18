import hashlib
import hmac
import json
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from smartbazaar.app import create_app
from smartbazaar.config import Settings
from smartbazaar.whatsapp import DeliveryError


class FakeCommerce:
    def __init__(self):
        self.begin_calls = []

    async def begin_login(self, mobile):
        self.begin_calls.append(mobile)
        return {"request_id": "challenge-1", "mobile": mobile, "cookies": []}

    async def close(self):
        return None


class FakeAgent:
    async def close(self):
        return None


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    async def send(self, recipient, text):
        self.sent.append((recipient, text))
        return f"provider-{len(self.sent)}"

    async def close(self):
        return None


class AuthFailingThenHealthyWhatsApp(FakeWhatsApp):
    def __init__(self):
        super().__init__()
        self.reject_auth = True

    async def send(self, recipient, text):
        if self.reject_auth:
            raise DeliveryError("http_401")
        return await super().send(recipient, text)


class FakeMedia:
    def __init__(self):
        self.calls = []

    async def resolve(self, payload):
        self.calls.append(payload)
        return "find milk"

    async def close(self):
        return None


def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "bot.sqlite3"),
        session_encryption_key=Fernet.generate_key().decode(),
        meta_app_secret="app-secret",
        meta_access_token="token",
        meta_waba_id="waba-1",
        meta_phone_number_id="phone-1",
        webhook_verify_token="verify-token",
        allowed_whatsapp_numbers="919000000001",
        openai_api_key="not-used",
    )


def wait_for(condition, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    assert condition()


def signed_payload(secret, *, waba="waba-1", phone="phone-1", sender="919000000001", mid="m1"):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": waba,
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone},
                            "messages": [
                                {
                                    "from": sender,
                                    "id": mid,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": "LOGIN 9000000001"},
                                }
                            ],
                        }
                    }
                ],
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"x-hub-signature-256": signature, "content-type": "application/json"}


def signed_media_payload(secret, kind, *, mid="media-1"):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba-1", "changes": [{"value": {"metadata": {"phone_number_id": "phone-1"}, "messages": [{"from": "919000000001", "id": mid, "timestamp": str(int(time.time())), "type": kind, kind: {"id": f"{kind}-id", "mime_type": "audio/ogg" if kind == "audio" else "image/jpeg"}}]}}]}],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return raw, {"x-hub-signature-256": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(), "content-type": "application/json"}


def test_signed_media_is_queued_as_a_reference_and_cannot_bypass_login_or_otp(tmp_path):
    config, media = settings(tmp_path), FakeMedia()
    app = create_app(config, commerce=FakeCommerce(), agent=FakeAgent(), transport=FakeWhatsApp(), media=media)
    with TestClient(app) as client:
        # A guest's photo can't shop, but a guest's voice note is just them talking.
        raw, headers = signed_media_payload("app-secret", "image", mid="guest-image")
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
        time.sleep(0.1)
        assert media.calls == []
        raw, headers = signed_media_payload("app-secret", "audio")
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
        time.sleep(0.1)
        assert [call["kind"] for call in media.calls] == ["audio"]
        time.sleep(1.1)  # Past the one-message-per-second limit.
        raw, headers = signed_payload("app-secret", mid="login-message")
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
        time.sleep(0.1)
        # While an OTP is pending, neither voice nor photos are read: the code must be typed.
        for kind in ("image", "audio"):
            raw, headers = signed_media_payload("app-secret", kind, mid=f"{kind}-during-otp")
            assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
            time.sleep(0.1)
        assert [call["kind"] for call in media.calls] == ["audio"]


def signed_status(secret, errors, provider_id="provider-1"):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-1"},
                            "statuses": [{"id": provider_id, "status": "failed", "errors": errors}],
                        }
                    }
                ],
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"x-hub-signature-256": signature, "content-type": "application/json"}


def openwa_settings(tmp_path):
    return Settings(
        _env_file=None,
        channel_provider="openwa",
        database_path=str(tmp_path / "openwa.sqlite3"),
        session_encryption_key=Fernet.generate_key().decode(),
        openwa_base_url="http://127.0.0.1:2785/api",
        openwa_api_key="openwa-key",
        openwa_session_id="shopping-poc",
        openwa_webhook_secret="openwa-secret",
        allowed_whatsapp_numbers="919000000001",
        openai_api_key="not-used",
    )


def signed_openwa_payload(
    secret,
    *,
    session="shopping-poc",
    chat_id="919000000001@c.us",
    sender="919000000001@c.us",
    event_id="event-1",
    from_me=False,
    direction="incoming",
):
    payload = {
        "event": "message.received",
        "sessionId": session,
        "idempotencyKey": event_id,
        "data": {
            "id": "message-1",
            "chatId": chat_id,
            "from": sender,
            "body": "LOGIN 9000000001",
            "type": "text",
            "fromMe": from_me,
            "direction": direction,
            "timestamp": int(time.time()),
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {
        "x-openwa-signature": signature,
        "x-openwa-event": "message.received",
        "x-openwa-idempotency-key": event_id,
        "content-type": "application/json",
    }


def test_webhook_verification_and_signed_allowlisted_delivery_runs_once(tmp_path, caplog):
    config, commerce, agent, transport = (
        settings(tmp_path),
        FakeCommerce(),
        FakeAgent(),
        FakeWhatsApp(),
    )
    app = create_app(config, commerce=commerce, agent=agent, transport=transport)
    with TestClient(app) as client:
        verified = client.get(
            "/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=verify-token&hub.challenge=ok"
        )
        assert verified.status_code == 200 and verified.text == "ok"
        assert client.get("/privacy").status_code == 200
        assert (
            client.get("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=wrong").status_code
            == 403
        )

        raw, headers = signed_payload("app-secret")
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
        time.sleep(0.1)
        assert commerce.begin_calls == ["9000000001"]

        for kwargs in ({"waba": "other"}, {"phone": "other"}, {"sender": "919000000002"}):
            blocked_raw, blocked_headers = signed_payload("app-secret", mid=str(kwargs), **kwargs)
            assert (
                client.post(
                    "/webhooks/whatsapp", content=blocked_raw, headers=blocked_headers
                ).status_code
                == 200
            )
        time.sleep(0.1)
        assert commerce.begin_calls == ["9000000001"]

        time.sleep(0.6)
        assert transport.sent
        status_raw, status_headers = signed_status(
            "app-secret", [{"code": 131026, "title": "private"}]
        )
        assert (
            client.post(
                "/webhooks/whatsapp", content=status_raw, headers=status_headers
            ).status_code
            == 200
        )
        assert client.get("/healthz").json()["delivery_errors"] == {"131026": 1}
        assert "delivery_failed code=131026" in caplog.text
        malformed_raw, malformed_headers = signed_status("app-secret", {"code": "not-a-list"})
        assert (
            client.post(
                "/webhooks/whatsapp", content=malformed_raw, headers=malformed_headers
            ).status_code
            == 200
        )

        assert (
            client.post(
                "/webhooks/whatsapp", content=raw, headers={"content-type": "application/json"}
            ).status_code
            == 403
        )


def test_health_degrades_after_outbound_auth_rejection_and_recovers_on_success(tmp_path):
    config, transport = settings(tmp_path), AuthFailingThenHealthyWhatsApp()
    app = create_app(config, commerce=FakeCommerce(), agent=FakeAgent(), transport=transport)
    with TestClient(app) as client:
        assert client.get("/healthz").json()["outbound_transport"] == "not_observed"

        raw, headers = signed_payload("app-secret", mid="auth-rejected")
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
        wait_for(lambda: client.get("/healthz").json()["status"] == "degraded")
        health = client.get("/healthz").json()
        assert health["outbound_transport"] == "auth_rejected"

        transport.reject_auth = False
        time.sleep(1)
        raw, headers = signed_payload("app-secret", mid="recovered")
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
        wait_for(lambda: client.get("/healthz").json()["status"] == "ready")
        assert client.get("/healthz").json()["outbound_transport"] == "available"


def test_openwa_webhook_accepts_only_signed_allowlisted_direct_inbound_messages(tmp_path):
    config, commerce, transport = openwa_settings(tmp_path), FakeCommerce(), FakeWhatsApp()
    app = create_app(config, commerce=commerce, agent=FakeAgent(), transport=transport)
    with TestClient(app) as client:
        assert client.post("/webhooks/whatsapp", content=b"{}").status_code == 404
        raw, headers = signed_openwa_payload("openwa-secret")
        assert client.post("/webhooks/openwa", content=raw, headers=headers).status_code == 200
        assert client.post("/webhooks/openwa", content=raw, headers=headers).status_code == 200
        changed_header = {**headers, "x-openwa-idempotency-key": "attacker-chosen-key"}
        assert client.post("/webhooks/openwa", content=raw, headers=changed_header).status_code == 200
        time.sleep(0.1)
        assert commerce.begin_calls == ["9000000001"]

        for kwargs in (
            {"session": "other"},
            {"chat_id": "919000000001@lid", "sender": "919000000001@lid"},
            {"chat_id": "123@g.us", "sender": "919000000001@c.us"},
            {"from_me": True},
            {"direction": "outgoing"},
            {"sender": "919000000002@c.us"},
        ):
            blocked_raw, blocked_headers = signed_openwa_payload(
                "openwa-secret", event_id=str(kwargs), **kwargs
            )
            assert client.post("/webhooks/openwa", content=blocked_raw, headers=blocked_headers).status_code == 200
        assert client.post("/webhooks/openwa", content=raw, headers={"content-type": "application/json"}).status_code == 401
        time.sleep(0.1)
        assert commerce.begin_calls == ["9000000001"]
        time.sleep(0.6)
        assert transport.sent and transport.sent[0][0] == "919000000001@c.us"


def test_readiness_checks_credentials_before_first_message_and_detects_stale_probe(tmp_path):
    from datetime import UTC, datetime

    from smartbazaar.channel_health import ChannelHealth

    class Probe:
        calls = 0
        closed = False

        async def check(self):
            self.calls += 1
            return ChannelHealth('expiring', datetime.now(UTC), datetime.now(UTC), 'expiring_soon')

        async def close(self):
            self.closed = True

    probe = Probe()
    app = create_app(settings(tmp_path), commerce=FakeCommerce(), agent=FakeAgent(),
                     transport=FakeWhatsApp(), channel_probe=probe)
    with TestClient(app) as client:
        result = client.get('/readyz')
        assert result.status_code == 503
        assert result.json()['channel_credentials']['status'] == 'expiring'
        assert probe.calls == 1
        client.get('/healthz')
        assert probe.calls == 1
        app.state.channel_health = {'status': 'available', 'checked_at': time.time() - 1000}
        assert client.get('/readyz').json()['channel_credentials']['status'] == 'stale'
        app.state.channel_health = {'status': 'available', 'checked_at': time.time()}
        assert client.get('/readyz').status_code == 200
    assert probe.closed


def test_meta_startup_wires_messaging_account_probe_with_real_transport(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from smartbazaar.channel_health import ChannelHealth, ChannelHealthProbe
    from smartbazaar.whatsapp import WhatsApp

    async def check(probe):
        assert probe._account_id == "waba-1"
        return ChannelHealth("unavailable", datetime.now(UTC), None, "account_access")

    monkeypatch.setattr(ChannelHealthProbe, "check", check)
    app = create_app(settings(tmp_path), commerce=FakeCommerce(), agent=FakeAgent(), media=FakeMedia())
    with TestClient(app) as client:
        assert isinstance(app.state.transport, WhatsApp)
        result = client.get("/readyz")
        assert result.status_code == 503
        assert result.json()["channel_credentials"]["reason"] == "account_access"


def test_signed_guest_greeting_delivers_ai_quick_reply_without_commerce_login(tmp_path):
    from smartbazaar.agent import Intent

    class ConversationalAgent(FakeAgent):
        async def interpret(self, text, context):
            assert text == "hi" and context["connected"] is False
            return Intent(action="help", reply="Hello from your grocery helper!", presentation="buttons", quick_actions=["connect"])

    transport = FakeWhatsApp()
    transport.supports_interactive = True
    commerce = FakeCommerce()
    app = create_app(settings(tmp_path), commerce=commerce, agent=ConversationalAgent(), transport=transport)
    raw, _ = signed_payload("app-secret", mid="greeting")
    payload = json.loads(raw)
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "hi"
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        result = client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": signature})
        assert result.status_code == 200
        wait_for(lambda: bool(transport.sent))
        assert not commerce.begin_calls
        card = transport.sent[0][1]
        assert card["type"] == "quick_reply" and card["body"] == "Hello from your grocery helper!"
        assert card["buttons"][0]["title"] == "Connect JioMart"


def test_render_external_url_is_the_public_origin_when_none_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://smartbazaar-whatsapp-bot.onrender.com")
    app = create_app(settings(tmp_path), commerce=FakeCommerce(), agent=FakeAgent(), transport=FakeWhatsApp(), media=FakeMedia())
    with TestClient(app):
        # Pay links use Render's own https address; the local ngrok agent is never consulted.
        assert app.state.public_origin == "https://smartbazaar-whatsapp-bot.onrender.com"
        assert app.state.tunnel_lookup is None
