"""Signed Meta webhook, bounded in-memory inbox and encrypted durable outbox."""

import asyncio
import contextlib
import hmac
import html
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from .agent import ShoppingAgent
from .channel_health import ChannelHealthProbe
from .checkout import CheckoutFlow
from .config import Settings
from .conversation import Conversation
from .jiomart import CommerceError, JioMartClient
from .media import MediaError, MediaProcessor
from .openwa import OpenWA
from .order_watch import OrderWatcher
from .privacy import router as privacy_router
from .session_keepalive import keepalive_loop
from .storage import Store
from .whatsapp import DeliveryError, WhatsApp, inbound_media_reference, valid_signature

log = logging.getLogger("smartbazaar")
# HTTP client loggers may include URLs; the app emits safe event codes only.
for name in ("httpx", "httpcore", "openai"):
    logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging():
    """Send the app's own safe event codes to stderr; uvicorn configures only its loggers."""
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)


def records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def mapping(value):
    return value if isinstance(value, dict) else {}


def delivery_error_code(status):
    """Extract one bounded diagnostic code without retaining provider error text."""
    for error in records(mapping(status).get("errors")):
        code = error.get("code")
        if type(code) is int and 0 <= code <= 2_147_483_647:
            return code
        if isinstance(code, str) and code.isascii() and code.isdecimal():
            parsed = int(code)
            if parsed <= 2_147_483_647:
                return parsed
    return None


def outbound_transport_status(error_code: str) -> str | None:
    """Keep provider failures visible even when the HTTP process is alive."""
    return "auth_rejected" if error_code in {"http_401", "http_403"} else "unavailable"


def openwa_phone(chat_id):
    """Only a canonical direct-chat JID proves a phone identity; never infer from @lid."""
    match = re.fullmatch(r"([1-9]\d{5,14})@c\.us", chat_id)
    return match.group(1) if match else None


def create_app(settings=None, commerce=None, agent=None, transport=None, media=None, channel_probe=None,
               tunnel_lookup=None):
    settings = settings or Settings()
    configure_logging()

    @asynccontextmanager
    async def lifespan(app):
        if settings.channel_provider not in {"meta", "openwa"}:
            raise RuntimeError("CHANNEL_PROVIDER must be meta or openwa")
        if not settings.session_encryption_key.get_secret_value():
            raise RuntimeError("SESSION_ENCRYPTION_KEY must be configured")
        app.state.store = Store(
            settings.database_path,
            settings.session_encryption_key.get_secret_value(),
            settings.session_ttl_seconds,
        )
        app.state.commerce = commerce or JioMartClient(
            settings.jiomart_application_id, settings.jiomart_application_token.get_secret_value(),
            enable_cart_writes=settings.jiomart_cart_writes,
            enable_checkout=settings.jiomart_checkout,
            enable_order_cancel=settings.jiomart_order_cancel,
        )
        app.state.agent = agent or ShoppingAgent(
            settings.openai_api_key.get_secret_value(), settings.openai_model,
            cart_writes=settings.jiomart_cart_writes, checkout=settings.jiomart_checkout,
        )
        if transport:
            app.state.transport = transport
        elif settings.channel_provider == "openwa":
            app.state.transport = OpenWA(
                settings.openwa_base_url,
                settings.openwa_api_key.get_secret_value(),
                settings.openwa_session_id,
            )
        else:
            app.state.transport = WhatsApp(
                settings.meta_access_token.get_secret_value(),
                settings.meta_phone_number_id,
                settings.meta_graph_version,
            )
        app.state.public_origin = public_origin(settings.public_base_url or settings.render_external_url)
        # Without a configured origin, ask the local tunnel itself; request headers are never trusted.
        app.state.tunnel_lookup = None if app.state.public_origin else (tunnel_lookup or ngrok_public_url)
        app.state.origin_checked = 0.0
        app.state.checkout = CheckoutFlow(
            app.state.store, app.state.commerce,
            cart_writes=settings.jiomart_cart_writes, checkout=settings.jiomart_checkout,
            max_total=settings.checkout_max_total, public_origin=lambda: app.state.public_origin,
            online_payment=settings.jiomart_online_payment,
        )
        app.state.watcher = OrderWatcher(app.state.store, app.state.commerce)
        app.state.watch_interval = settings.order_watch_interval_seconds
        app.state.conversation = Conversation(
            app.state.store, app.state.commerce, app.state.agent, app.state.checkout
        )
        app.state.media = media or MediaProcessor(
            settings.meta_access_token.get_secret_value(), settings.meta_graph_version, app.state.agent
        )
        app.state.queue = asyncio.Queue(maxsize=32)
        app.state.locks = {}
        app.state.last_message = {}
        # This is observation-only: health checks must not send provider probes.
        app.state.outbound_transport_status = "not_observed"
        app.state.channel_probe = channel_probe
        if channel_probe is None and transport is None and settings.channel_provider == "meta":
            app.state.channel_probe = ChannelHealthProbe(
                settings.meta_access_token.get_secret_value(), settings.meta_app_id,
                settings.meta_app_secret.get_secret_value(), settings.meta_phone_number_id,
                settings.meta_graph_version,
                account_id=settings.meta_waba_id,
            )
        app.state.channel_check_interval = settings.channel_check_interval_seconds
        app.state.channel_health = {"status": "not_monitored", "checked_at": None}
        app.state.last_delivery_failure = None
        app.state.last_delivery_success = None
        jobs = [asyncio.create_task(inbox_worker(app)) for _ in range(2)]
        jobs.append(asyncio.create_task(outbox_worker(app)))
        # Every linked customer stays signed in for the whole session lifetime, not just an hour.
        jobs.append(asyncio.create_task(keepalive_loop(
            app.state.store, app.state.commerce, app.state.locks,
            interval=settings.session_keepalive_interval_seconds,
            active_within=settings.session_ttl_seconds,
        )))
        if app.state.checkout.checkout_enabled:
            await refresh_public_origin(app)
            jobs.append(asyncio.create_task(order_watch_worker(app)))
        if app.state.channel_probe is not None:
            await check_channel(app)
            jobs.append(asyncio.create_task(channel_health_worker(app)))
        app.state.jobs = jobs
        try:
            yield
        finally:
            for job in jobs:
                job.cancel()
            for job in jobs:
                with contextlib.suppress(asyncio.CancelledError):
                    await job
            await app.state.commerce.close()
            await app.state.transport.close()
            await app.state.media.close()
            if app.state.channel_probe is not None:
                await app.state.channel_probe.close()
            close = getattr(app.state.agent, "close", None)
            if close:
                await close()
            app.state.store.close()

    app = FastAPI(
        title="SmartBazaar login and search POC", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.include_router(privacy_router)

    page_headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "origin",
        "X-Robots-Tag": "noindex",
    }

    def page(title: str, body: str, *, status_code: int = 200,
             csp: str = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'") -> HTMLResponse:
        document = (
            "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>body{{font-family:system-ui,sans-serif;max-width:28rem;margin:3rem auto;"
            "padding:0 1.25rem;line-height:1.5;color:#222}a.btn,button{display:block;width:100%;padding:.9rem;margin-top:1.25rem;"
            "border:0;border-radius:.6rem;background:#0c5273;color:#fff;font-size:1rem;text-align:center;text-decoration:none}</style>"
            f"</head><body>{body}</body></html>"
        )
        return HTMLResponse(document, status_code=status_code, headers={**page_headers, "Content-Security-Policy": csp})

    @app.get("/pay/{token}")
    async def pay(token: str):
        link = app.state.store.payment_link(token) if re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token) else None
        attempt = app.state.store.get_attempt(link[0]) if link else None
        if not link or not attempt or attempt[2] != "awaiting_payment":
            return page("Link expired", "<h2>This payment link is no longer active</h2><p>Go back to WhatsApp and send "
                        "<b>ORDER STATUS</b> or <b>CHECKOUT</b>.</p>", status_code=404)
        _, data = link
        action = html.escape(data["api_link"], quote=True)
        fields = data.get("fields") or {"base64_html": data.get("base64_html", "")}
        if "base64_html" not in fields:
            # JioMart's payment session needs the account's JioMart sign-in, which the customer's
            # browser (opened from WhatsApp) doesn't have. The bot opens the session with the
            # account's own server-side sign-in and sends the browser to the provider's page.
            owner = attempt[0]
            async with app.state.locks.setdefault(owner, asyncio.Lock()):
                state = app.state.store.get(owner)
                if not state.get("session"):
                    return page("Sign in again", "<h2>Your JioMart session has expired</h2><p>Go back to WhatsApp, "
                                "send <b>LOGIN</b>, then <b>ORDER STATUS</b> for a fresh payment link.</p>", status_code=409)
                try:
                    opened = await app.state.commerce.open_payment_session(state["session"], data["api_link"], fields)
                except (CommerceError, ValueError) as exc:
                    log.warning("pay_session_failed type=%s", type(exc).__name__)
                    opened = None
                finally:
                    app.state.store.save(owner, state, touch=False)
            if opened and opened.get("redirect"):
                return RedirectResponse(opened["redirect"], status_code=303,
                                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            form = opened.get("form") if opened else None
            if not form:
                return page("Payment unavailable", "<h2>JioMart didn’t open a payment page</h2><p>Nothing was charged. "
                            "Go back to WhatsApp: you can pay this order from My Orders in the JioMart app, "
                            "or order again with cash on delivery.</p>", status_code=502)
            inputs = "".join(
                f"<input type='hidden' name='{html.escape(str(n), quote=True)}' value='{html.escape(str(v), quote=True)}'>"
                for n, v in form["fields"].items()
            )
            body = (
                f"<h2>Opening payment…</h2><p>Order {html.escape(str(data.get('order_id', '')))} · "
                f"₹{html.escape(str(data.get('total', '')))}</p>"
                f"<form method='{form['method']}' action='{html.escape(form['action'], quote=True)}'>{inputs}"
                "<button type='submit'>Continue to payment</button></form><script>document.forms[0].submit()</script>"
            )
            return page("Pay", body, csp="default-src 'none'; style-src 'unsafe-inline'; "
                        "script-src 'unsafe-inline'; frame-ancestors 'none'")
        inputs = "".join(
            f"<input type='hidden' name='{html.escape(name, quote=True)}' value='{html.escape(value, quote=True)}'>"
            for name, value in fields.items()
            if isinstance(name, str) and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name)
        )
        # JioMart's website submits exactly these checkout fields to its payment page.
        body = (
            f"<h2>Opening JioMart payment…</h2><p>Order {html.escape(str(data.get('order_id', '')))} · "
            f"₹{html.escape(str(data.get('total', '')))}</p>"
            f"<form method='post' action='{action}'>{inputs}<button type='submit'>Continue to payment</button></form>"
            "<script>document.forms[0].submit()</script>"
        )
        # No form-action limit: JioPay may redirect onward to a bank or UPI page.
        return page("Pay with JioPay", body, csp="default-src 'none'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; frame-ancestors 'none'")

    @app.api_route("/paid/{token}", methods=["GET", "POST"])
    async def paid(token: str):
        # The browser's return is navigation only; JioMart's order record decides payment.
        link = app.state.store.payment_link(token) if re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token) else None
        if link:
            attempt_id = link[0]
            task = asyncio.create_task(check_orders(app, only=attempt_id))
            app.state.background = getattr(app.state, "background", set())
            app.state.background.add(task)
            task.add_done_callback(app.state.background.discard)
        number = re.sub(r"\D", "", settings.whatsapp_display_number)
        back = f"https://wa.me/{number}" if number else "whatsapp://"
        return page("Back to WhatsApp", "<h2>Thanks! Head back to WhatsApp</h2><p>I’m checking your payment with JioMart "
                    "and will confirm your order in the chat in a moment.</p>"
                    f"<a class='btn' href='{html.escape(back, quote=True)}'>Back to WhatsApp</a>")

    @app.get("/healthz")
    async def health():
        transport_status = app.state.outbound_transport_status
        channel = app.state.channel_health.copy()
        checked_at = channel.get("checked_at")
        if checked_at and time.time() - checked_at > settings.channel_check_interval_seconds * 2:
            channel["status"] = "stale"
        workers_ok = all(not job.done() for job in app.state.jobs)
        degraded = (
            transport_status in {"auth_rejected", "unavailable"}
            or channel["status"] not in {"available", "not_monitored"}
            or not workers_ok
            or app.state.last_delivery_failure is not None
        )
        return {
            "status": (
                "configuration_required"
                if settings.missing()
                else "degraded"
                if degraded
                else "ready"
            ),
            "outbound_transport": transport_status,
            "channel_credentials": channel,
            "workers_alive": workers_ok,
            "last_delivery_failure": app.state.last_delivery_failure,
            "last_delivery_success": app.state.last_delivery_success,
            "scope": "shopping_cart_checkout" if app.state.checkout.cart_writes else "shopping_draft_memory_media",
            "cart_writes_enabled": app.state.checkout.cart_writes,
            "checkout_enabled": app.state.checkout.checkout_enabled,
            "model": settings.openai_model,
            "missing": settings.missing(),
            **app.state.store.counts(),
        }

    @app.get("/readyz")
    async def ready():
        result = await health()
        return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)

    @app.get("/webhooks/whatsapp", response_class=PlainTextResponse)
    async def verify(request: Request):
        token = settings.webhook_verify_token.get_secret_value()
        if (
            token
            and request.query_params.get("hub.mode") == "subscribe"
            and hmac.compare_digest(request.query_params.get("hub.verify_token", ""), token)
        ):
            return request.query_params.get("hub.challenge", "")
        raise HTTPException(403, "Verification failed")

    @app.post("/webhooks/whatsapp")
    async def receive(request: Request):
        if settings.channel_provider == "openwa":
            raise HTTPException(404, "Not found")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 262144:
                raise HTTPException(413, "Payload too large")
        if not valid_signature(
            bytes(body),
            request.headers.get("x-hub-signature-256", ""),
            settings.meta_app_secret.get_secret_value(),
        ):
            raise HTTPException(403, "Signature verification failed")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(data, dict) or data.get("object") != "whatsapp_business_account":
            return {"ok": True}
        incoming = []
        for entry in records(data.get("entry")):
            if not isinstance(entry, dict) or str(entry.get("id")) != settings.meta_waba_id:
                continue
            for change in records(entry.get("changes")):
                value = mapping(change.get("value"))
                if (
                    str(mapping(value.get("metadata")).get("phone_number_id"))
                    != settings.meta_phone_number_id
                ):
                    continue
                for status in records(value.get("statuses")):
                    status_name = status.get("status")
                    error_code = delivery_error_code(status)
                    app.state.store.delivered(str(status.get("id", "")), status_name, error_code)
                    if status_name == "failed":
                        app.state.last_delivery_failure = {"at": time.time(), "code": error_code}
                        log.warning(
                            "delivery_failed code=%s",
                            error_code if error_code is not None else "unknown",
                        )
                    elif status_name in {"delivered", "read"}:
                        app.state.last_delivery_success = time.time()
                        app.state.last_delivery_failure = None
                for message in records(value.get("messages")):
                    sender, mid = str(message.get("from", "")), str(message.get("id", ""))
                    if sender not in settings.allowed_numbers or not mid:
                        continue
                    # Reject old/replayed captured events; duplicates are also indexed below.
                    try:
                        timestamp = int(message.get("timestamp", 0))
                    except (ValueError, TypeError):
                        continue
                    if abs(time.time() - timestamp) > 900:
                        continue
                    kind = message.get("type")
                    if kind == "text":
                        text = mapping(message.get("text")).get("body")
                    elif kind == "interactive":
                        interactive = mapping(message.get("interactive"))
                        reply_id = (mapping(interactive.get("button_reply")).get("id")
                                    or mapping(interactive.get("list_reply")).get("id"))
                        text = (
                            "/button:" + reply_id
                            if isinstance(reply_id, str) and 1 <= len(reply_id) <= 128
                            else None
                        )
                    else:
                        text = None
                    reference = inbound_media_reference(message)
                    incoming.append(
                        ("meta:" + mid, sender, sender, {"kind": "text", "text": text})
                        if isinstance(text, str)
                        else ("meta:" + mid, sender, sender, reference or {"kind": "unsupported"})
                    )
        if app.state.queue.qsize() + len(incoming) > 32:
            raise HTTPException(503, "Please retry")
        for mid, recipient, sender, payload in incoming:
            owner = app.state.store.digest(settings.meta_waba_id + ":" + sender)
            if app.state.store.claim(mid, owner):
                app.state.queue.put_nowait(
                    {"id": mid, "recipient": recipient, "owner": owner, "sender": sender, "input": payload}
                )
        return {"ok": True}

    @app.post("/webhooks/openwa")
    async def receive_openwa(request: Request):
        if settings.channel_provider != "openwa":
            raise HTTPException(404, "Not found")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 262144:
                raise HTTPException(413, "Payload too large")
        if not valid_signature(
            bytes(body),
            request.headers.get("x-openwa-signature", ""),
            settings.openwa_webhook_secret.get_secret_value(),
        ):
            raise HTTPException(401, "Signature verification failed")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(data, dict) or data.get("event") != "message.received":
            return {"ok": True}
        if request.headers.get("x-openwa-event") not in (None, "message.received"):
            return {"ok": True}
        if str(data.get("sessionId", "")) != settings.openwa_session_id:
            return {"ok": True}
        message = mapping(data.get("data"))
        chat_id = message.get("chatId")
        sender = message.get("author") or message.get("from")
        if (
            message.get("fromMe") is not False
            or message.get("direction") == "outgoing"
            or not isinstance(chat_id, str)
            or not isinstance(sender, str)
        ):
            return {"ok": True}
        phone = openwa_phone(chat_id)
        # Direct chats only: group authors and privacy @lid values are never accepted as customers.
        if (
            message.get("isGroup") is True
            or message.get("isStatusBroadcast") is True
            or not phone
            or sender != chat_id
            or phone not in settings.allowed_numbers
        ):
            return {"ok": True}
        # The delivery header is useful observability only. Deduplication must use the HMAC-signed body.
        event_id = data.get("idempotencyKey")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 512:
            return {"ok": True}
        header_event_id = request.headers.get("x-openwa-idempotency-key")
        if header_event_id is not None and not hmac.compare_digest(header_event_id, event_id):
            return {"ok": True}
        try:
            timestamp = int(message.get("timestamp", 0))
        except (ValueError, TypeError):
            return {"ok": True}
        if abs(time.time() - timestamp) > 900:
            return {"ok": True}
        text = message.get("body") if message.get("type") == "text" else None
        if not isinstance(text, str):
            text = "/unsupported_media"
        if app.state.queue.qsize() >= 32:
            raise HTTPException(503, "Please retry")
        claimed_id = f"openwa:{settings.openwa_session_id}:{event_id}"
        owner = app.state.store.digest(f"openwa:{settings.openwa_session_id}:{chat_id}")
        if app.state.store.claim(claimed_id, owner):
            app.state.queue.put_nowait(
                {"id": claimed_id, "recipient": chat_id, "owner": owner, "sender": phone, "input": {"kind": "text", "text": text}}
            )
        return {"ok": True}

    return app


async def check_channel(app):
    """Read provider readiness without sending customer messages or logging credentials."""
    previous = app.state.channel_health.get("status")
    try:
        result = asdict(await app.state.channel_probe.check())
        for key in ("checked_at", "expires_at"):
            if result.get(key) is not None:
                result[key] = result[key].timestamp()
    except Exception:
        result = {"status": "unavailable", "reason": "probe_failed", "checked_at": time.time(),
                  "expires_at": None}
    app.state.channel_health = result
    if previous != result["status"] and result["status"] != "available":
        log.warning("channel_health status=%s reason=%s", result["status"], result.get("reason"))


async def channel_health_worker(app):
    while True:
        await asyncio.sleep(app.state.channel_check_interval)
        await check_channel(app)


def chunk_replies(replies):
    """Keep long shopping lists complete within the WhatsApp text limit."""
    result = []
    for reply in replies:
        if not isinstance(reply, str):
            result.append(reply)
            continue
        while len(reply) > 3900:
            boundary = reply.rfind("\n", 0, 3900)
            boundary = boundary if boundary > 0 else 3900
            result.append(reply[:boundary])
            reply = reply[boundary:].lstrip("\n")
        if reply:
            result.append(reply)
    return result


_TUNNEL_HOST = re.compile(r"[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})*\.ngrok(?:-free)?\.(?:app|dev|io)")


def public_origin(value: str) -> str | None:
    """Accept a configured https origin, or a bare ngrok host reported by the local ngrok agent."""
    value = (value or "").strip().lower().rstrip("/")
    if value.startswith("https://"):
        host = value.removeprefix("https://")
        return value if re.fullmatch(r"[a-z0-9.-]{3,253}(?::\d{2,5})?", host) else None
    return f"https://{value}" if _TUNNEL_HOST.fullmatch(value) else None


async def ngrok_public_url() -> str | None:
    """Read this machine's ngrok agent API for the HTTPS tunnel that serves the app."""
    try:
        async with httpx.AsyncClient(timeout=2, follow_redirects=False) as client:
            response = await client.get("http://127.0.0.1:4040/api/tunnels")
        tunnels = response.json().get("tunnels", []) if response.status_code == 200 else []
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    for tunnel in tunnels if isinstance(tunnels, list) else []:
        address = str((tunnel.get("config") or {}).get("addr", "")) if isinstance(tunnel, dict) else ""
        url = tunnel.get("public_url") if isinstance(tunnel, dict) else None
        if isinstance(url, str) and url.startswith("https://") and address.rstrip("/").endswith(":8080"):
            return url
    return None


async def refresh_public_origin(app, *, every: float = 60):
    lookup = getattr(app.state, "tunnel_lookup", None)
    if lookup is None or time.time() - app.state.origin_checked < every:
        return
    app.state.origin_checked = time.time()
    found = public_origin((await lookup() or "").removeprefix("https://"))
    if found != app.state.public_origin:
        log.info("public_origin %s", "updated" if found else "unavailable")
    app.state.public_origin = found


async def check_orders(app, *, only=None):
    try:
        for event_id, recipient, messages in await app.state.watcher.check(app.state.locks, only=only):
            app.state.store.enqueue(event_id, recipient, messages)
    except Exception as exc:
        log.warning("order_watch_failed type=%s", type(exc).__name__)


async def order_watch_worker(app):
    """Push payment and delivery updates for orders placed from chat."""
    while True:
        await asyncio.sleep(app.state.watch_interval)
        await refresh_public_origin(app)
        await check_orders(app)


async def inbox_worker(app):
    while True:
        job = await app.state.queue.get()
        mid, recipient, owner, sender = job["id"], job["recipient"], job["owner"], job["sender"]
        payload = job["input"]
        acknowledge = getattr(app.state.transport, "acknowledge", None)
        if acknowledge and mid.startswith("meta:"):
            # Read receipt + "typing…" right away; replies can take several seconds.
            task = asyncio.create_task(acknowledge(mid.removeprefix("meta:")))
            app.state.background = getattr(app.state, "background", set())
            app.state.background.add(task)
            task.add_done_callback(app.state.background.discard)
        lock = app.state.locks.setdefault(owner, asyncio.Lock())
        async with lock:
            try:
                if payload.get("kind") == "unsupported":
                    replies = [
                        "Please send text, a voice note, or a product image."
                    ]
                else:
                    text = payload.get("text")
                    if payload.get("kind") in {"audio", "image"}:
                        state = app.state.store.get(owner)
                        if state.get("challenge"):
                            replies = ["Please type the OTP from JioMart (or CANCEL). Login codes can’t be sent by voice or photo."]
                        elif not state.get("session") and payload.get("kind") == "image":
                            replies = ["Connect your JioMart account before sending shopping photos."]
                        else:
                            try:
                                text = await app.state.media.resolve(payload)
                            except MediaError as exc:
                                if exc.code == "image_uncertain":
                                    replies = ["I couldn’t identify the product clearly. Please send a clearer image or type the product name."]
                                else:
                                    replies = ["I couldn’t read that media. Please send a product name or try another file."]
                            else:
                                replies = None
                    else:
                        replies = None
                    if replies is not None:
                        pass
                    elif not isinstance(text, str):
                        replies = ["Please send text, a voice note, or a product image."]
                    else:
                        now = time.time()
                        previous = app.state.last_message.get(owner, 0)
                        if now - previous < 1:
                            replies = ["Please wait a moment between requests."]
                        else:
                            app.state.last_message[owner] = now
                            handle = app.state.conversation.handle
                            # A photo can only supply shopping items; a voice note is the customer speaking.
                            if payload.get("kind") == "image":
                                call = handle(owner, sender, text, shopping_only=True)
                            elif payload.get("kind") == "audio":
                                call = handle(owner, sender, text, voice=True)
                            else:
                                call = handle(owner, sender, text)
                            replies = await asyncio.wait_for(
                                call, timeout=150
                            )
                app.state.store.enqueue(mid, recipient, chunk_replies(replies))
                app.state.store.complete(mid)
                log.info("inbound_completed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log repr/traceback: upstream exceptions can contain request content.
                log.warning("inbound_failed type=%s", type(exc).__name__)
                text = payload.get("text") if isinstance(payload, dict) else None
                if isinstance(text, str) and text.startswith("/button:sbco_"):
                    # An order submission keeps running and records its outcome; never invite a second tap.
                    recovery = ("Your order request is taking longer than usual. Please don’t tap again: "
                                "send ORDER STATUS in a minute to see whether JioMart created it.")
                elif app.state.store.get(owner).get("session"):
                    recovery = "That request was interrupted. Send CART to check your cart before retrying."
                else:
                    recovery = "That request was interrupted. Please send it again."
                app.state.store.enqueue(mid, recipient, [recovery])
                app.state.store.complete(mid)
            finally:
                payload = {}
                app.state.queue.task_done()


async def outbox_worker(app):
    last_prune = 0
    while True:
        if time.time() - last_prune > 3600:
            app.state.store.prune()
            last_prune = time.time()
        record = app.state.store.next_outbound()
        if not record:
            await asyncio.sleep(0.5)
            continue
        oid, payload, attempts = record
        try:
            message = payload.get("message", payload.get("text"))
            if isinstance(message, dict) and not getattr(app.state.transport, "supports_interactive", False):
                message = message.get("fallback")
            if not isinstance(message, (str, dict)):
                raise DeliveryError("invalid_payload")
            provider_id = await app.state.transport.send(payload["to"], message)
            app.state.store.sent(oid, provider_id)
            app.state.outbound_transport_status = "available"
            log.info("outbound_accepted")
        except DeliveryError as exc:
            transport_status = outbound_transport_status(exc.code)
            if transport_status:
                app.state.outbound_transport_status = transport_status
            if transport_status == "auth_rejected":
                app.state.store.defer_auth(oid)
                log.error("outbound_auth_rejected reply_held_for_recovery")
                continue
            if isinstance(payload.get("message"), dict) and isinstance(
                payload["message"].get("fallback"), str
            ):
                app.state.store.fallback_to_text(
                    oid, payload["to"], payload["message"]["fallback"]
                )
                log.warning("outbound_card_fallback code=%s", exc.code)
                continue
            app.state.store.failed(oid, attempts, exc.retry)
            log.warning("outbound_failed code=%s", exc.code)
        except Exception as exc:
            app.state.store.failed(oid, attempts, False)
            log.warning("outbound_failed type=%s", type(exc).__name__)
