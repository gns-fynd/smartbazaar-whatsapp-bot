#!/usr/bin/env python3
"""Run ngrok and keep the Meta app callback aligned with its current URL."""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

INSPECTOR_URL = "http://127.0.0.1:4040/api/tunnels"
HEALTH_URL = "http://127.0.0.1:8080/healthz"
TARGET_OBJECT = "whatsapp_business_account"
TARGET_FIELD = "messages"
CALLBACK_PATH = "/webhooks/whatsapp"
STARTUP_ATTEMPTS = 30
STARTUP_DELAY_SECONDS = 1.0


class RunnerError(RuntimeError):
    """A safe operational error that contains no secret or raw provider response."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so authorization and verification data stay on the intended origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirectHandler())


def no_redirect_urlopen(request: urllib.request.Request, timeout: float):
    return NO_REDIRECT_OPENER.open(request, timeout=timeout)


@dataclass(frozen=True)
class GraphConfig:
    app_id: str
    app_secret: str
    verify_token: str
    graph_version: str


def validate_public_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or value.rstrip("/") != value
    ):
        raise RunnerError("The public URL must be an exact HTTPS origin without a trailing slash.")
    return value


def load_graph_config(root: Path) -> GraphConfig:
    sys.path.insert(0, str(root))
    try:
        from smartbazaar.config import Settings

        settings = Settings(_env_file=root / ".env")
    except Exception:
        raise RunnerError("Could not load the staged runtime configuration.") from None
    config = GraphConfig(
        app_id=settings.meta_app_id.strip(),
        app_secret=settings.meta_app_secret.get_secret_value(),
        verify_token=settings.webhook_verify_token.get_secret_value(),
        graph_version=settings.meta_graph_version.strip(),
    )
    if not config.app_id or not config.app_secret or not config.verify_token:
        raise RunnerError("META_APP_ID, META_APP_SECRET, and WEBHOOK_VERIFY_TOKEN are required.")
    if not re.fullmatch(r"v\d+\.\d+", config.graph_version):
        raise RunnerError("META_GRAPH_VERSION must use the form v25.0.")
    return config


def ngrok_arguments(ngrok: Path, public_url: str | None) -> list[str]:
    args = [
        str(ngrok),
        "http",
        "8080",
        "--inspect=false",
        "--log",
        "stdout",
        "--log-format",
        "json",
    ]
    if public_url:
        args[2:2] = ["--url", validate_public_url(public_url)]
    return args


def _read_json(request: urllib.request.Request, opener, purpose: str) -> Any:
    try:
        with opener(request, timeout=3) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RunnerError(f"{purpose} failed with HTTP {error.code}.") from None
    except (OSError, ValueError, TypeError):
        raise RunnerError(f"{purpose} returned no usable response.") from None


def _local_target(address: Any) -> bool:
    if not isinstance(address, str):
        return False
    if address == "8080":
        return True
    parsed = urlparse(address if "://" in address else f"http://{address}")
    try:
        port = parsed.port
    except ValueError:
        return False
    return parsed.hostname in {"localhost", "127.0.0.1"} and port == 8080


def inspect_public_url(opener=no_redirect_urlopen) -> str:
    payload = _read_json(urllib.request.Request(INSPECTOR_URL), opener, "ngrok inspection")
    tunnels = payload.get("tunnels") if isinstance(payload, dict) else None
    if not isinstance(tunnels, list):
        raise RunnerError("ngrok inspection returned an unexpected tunnel list.")
    matches = [
        item
        for item in tunnels
        if isinstance(item, dict)
        and isinstance(item.get("config"), dict)
        and _local_target(item["config"].get("addr"))
    ]
    if len(matches) != 1:
        raise RunnerError("Expected exactly one ngrok tunnel for the local app on port 8080.")
    public_url = matches[0].get("public_url")
    if not isinstance(public_url, str):
        raise RunnerError("The ngrok tunnel did not provide a public URL.")
    return validate_public_url(public_url)


def probe_health(opener=no_redirect_urlopen) -> None:
    payload = _read_json(urllib.request.Request(HEALTH_URL), opener, "local app health check")
    if not isinstance(payload, dict):
        raise RunnerError("The local app health response was malformed.")
    if payload.get("status") == "configuration_required" or payload.get("workers_alive") is not True:
        raise RunnerError("The local app is not ready to receive webhook traffic.")


def wait_for_startup(
    child: subprocess.Popen[Any],
    check: Callable[[], Any],
    purpose: str,
    attempts: int = STARTUP_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    last_error: RunnerError | None = None
    for attempt in range(attempts):
        if child.poll() is not None:
            raise RunnerError(f"ngrok exited before {purpose} succeeded.")
        try:
            return check()
        except RunnerError as error:
            last_error = error
        if attempt + 1 < attempts:
            sleep(STARTUP_DELAY_SECONDS)
    raise RunnerError(f"Timed out waiting for {purpose}: {last_error}")


def _graph_request(
    config: GraphConfig,
    opener,
    data: dict[str, str] | None = None,
) -> Any:
    url = f"https://graph.facebook.com/{config.graph_version}/{config.app_id}/subscriptions"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {config.app_id}|{config.app_secret}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    return _read_json(request, opener, "Meta subscription request")


def _subscription(payload: Any) -> tuple[dict[str, Any], list[str]]:
    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list) or len(records) != 1:
        raise RunnerError("Expected exactly one Meta app webhook subscription.")
    record = records[0]
    if not isinstance(record, dict) or record.get("object") != TARGET_OBJECT:
        raise RunnerError("The Meta app has an unexpected webhook subscription.")
    raw_fields = record.get("fields")
    if not isinstance(raw_fields, list):
        raise RunnerError("The Meta app subscription fields were malformed.")
    fields: list[str] = []
    for field in raw_fields:
        name = field.get("name") if isinstance(field, dict) else field
        if not isinstance(name, str) or not name or "," in name or name in fields:
            raise RunnerError("The Meta app subscription fields were malformed.")
        fields.append(name)
    if TARGET_FIELD not in fields:
        raise RunnerError("The Meta app subscription is missing the messages field.")
    return record, fields


def reconcile_subscription(
    config: GraphConfig,
    public_url: str,
    opener=no_redirect_urlopen,
) -> None:
    callback_url = f"{validate_public_url(public_url)}{CALLBACK_PATH}"
    before, fields = _subscription(_graph_request(config, opener))
    if before.get("callback_url") == callback_url:
        return
    result = _graph_request(
        config,
        opener,
        {
            "object": TARGET_OBJECT,
            "callback_url": callback_url,
            "verify_token": config.verify_token,
            "fields": ",".join(fields),
        },
    )
    if not isinstance(result, dict) or result.get("success") is not True:
        raise RunnerError("Meta did not confirm the callback update.")
    after, verified_fields = _subscription(_graph_request(config, opener))
    if after.get("callback_url") != callback_url or verified_fields != fields:
        raise RunnerError("Meta callback verification did not match the requested subscription.")


def stop_child(child: subprocess.Popen[Any]) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def run(
    ngrok: Path,
    public_url: str | None,
    config: GraphConfig,
    popen=subprocess.Popen,
    opener=no_redirect_urlopen,
) -> int:
    try:
        child = popen(ngrok_arguments(ngrok, public_url))
    except OSError:
        raise RunnerError("ngrok could not be started.") from None
    previous_handlers: dict[signal.Signals, Any] = {}

    def terminate_child(_signum, _frame) -> None:
        if child.poll() is None:
            child.terminate()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, terminate_child)
    try:
        discovered_url = wait_for_startup(
            child, lambda: inspect_public_url(opener), "ngrok inspection"
        )
        if public_url and discovered_url != public_url:
            raise RunnerError("ngrok did not start the requested fixed public URL.")
        wait_for_startup(child, lambda: probe_health(opener), "local app health")
        reconcile_subscription(config, discovered_url, opener)
        print(f"tunnel ready: {discovered_url}", flush=True)
        return child.wait()
    except RunnerError:
        stop_child(child)
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--ngrok-path", required=True)
    command.add_argument("--public-url", help="reserved HTTPS origin; omit for a dynamic free URL")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        ngrok = Path(args.ngrok_path).expanduser().absolute()
        if not ngrok.is_file():
            raise RunnerError("The configured ngrok executable was not found.")
        root = Path(__file__).resolve().parent.parent
        config = load_graph_config(root)
        return run(ngrok, args.public_url, config)
    except RunnerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
