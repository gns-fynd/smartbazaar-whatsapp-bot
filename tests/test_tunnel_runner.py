import importlib.util
import io
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "tunnel_runner.py"
SPEC = importlib.util.spec_from_file_location("tunnel_runner", MODULE_PATH)
assert SPEC and SPEC.loader
tunnel_runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tunnel_runner
SPEC.loader.exec_module(tunnel_runner)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def response(payload):
    return Response(json.dumps(payload).encode())


def config():
    return tunnel_runner.GraphConfig("123", "app-secret", "verify-secret", "v25.0")


def subscription(callback="https://old.ngrok.app/webhooks/whatsapp"):
    return {
        "data": [
            {
                "object": "whatsapp_business_account",
                "callback_url": callback,
                "fields": [{"name": "messages"}, {"name": "message_template_status_update"}],
            }
        ]
    }


def test_dynamic_ngrok_has_no_reserved_url_and_disables_request_inspection():
    args = tunnel_runner.ngrok_arguments(Path("/opt/ngrok"), None)
    assert args[:3] == ["/opt/ngrok", "http", "8080"]
    assert "--url" not in args
    assert "--inspect=false" in args


def test_default_opener_rejects_redirect_without_visiting_target():
    visited_target = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/target")
                self.end_headers()
                return
            visited_target.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        request = tunnel_runner.urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/redirect",
            headers={"Authorization": "Bearer private-token"},
        )
        with pytest.raises(tunnel_runner.RunnerError, match="failed with HTTP 302") as failure:
            tunnel_runner._read_json(
                request, tunnel_runner.no_redirect_urlopen, "redirect regression check"
            )
        assert "private-token" not in str(failure.value)
        assert visited_target == []
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_inspector_selects_exactly_one_local_8080_tunnel():
    def opener(request, timeout):
        assert request.full_url == tunnel_runner.INSPECTOR_URL
        assert timeout == 3
        return response(
            {
                "tunnels": [
                    {
                        "public_url": "https://dynamic.ngrok-free.app",
                        "config": {"addr": "http://localhost:8080"},
                    },
                    {
                        "public_url": "https://other.ngrok-free.app",
                        "config": {"addr": "http://localhost:9000"},
                    },
                ]
            }
        )

    assert tunnel_runner.inspect_public_url(opener) == "https://dynamic.ngrok-free.app"


def test_reconcile_preserves_fields_and_verifies_callback_without_query_secrets():
    requests = []
    replies = iter(
        [
            subscription(),
            {"success": True},
            subscription("https://new.ngrok.app/webhooks/whatsapp"),
        ]
    )

    def opener(request, timeout):
        requests.append(request)
        return response(next(replies))

    tunnel_runner.reconcile_subscription(config(), "https://new.ngrok.app", opener)

    assert len(requests) == 3
    assert all("app-secret" not in request.full_url for request in requests)
    assert requests[0].get_header("Authorization") == "Bearer 123|app-secret"
    posted = urllib.parse.parse_qs(requests[1].data.decode())
    assert posted == {
        "object": ["whatsapp_business_account"],
        "callback_url": ["https://new.ngrok.app/webhooks/whatsapp"],
        "verify_token": ["verify-secret"],
        "fields": ["messages,message_template_status_update"],
    }


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"data": []}, "exactly one"),
        ({"data": [subscription()["data"][0], subscription()["data"][0]]}, "exactly one"),
        (
            {"data": [{"object": "user", "callback_url": "x", "fields": ["messages"]}]},
            "unexpected",
        ),
        (
            {
                "data": [
                    {
                        "object": "whatsapp_business_account",
                        "callback_url": "x",
                        "fields": ["account_update"],
                    }
                ]
            },
            "missing the messages",
        ),
    ],
)
def test_reconcile_fails_safe_on_missing_multiple_or_unexpected_subscription(payload, message):
    with pytest.raises(tunnel_runner.RunnerError, match=message):
        tunnel_runner.reconcile_subscription(
            config(), "https://new.ngrok.app", lambda *_a, **_k: response(payload)
        )


def test_wait_for_startup_stops_when_child_exits_without_extra_retries():
    class Child:
        def poll(self):
            return 17

    with pytest.raises(tunnel_runner.RunnerError, match="ngrok exited"):
        tunnel_runner.wait_for_startup(
            Child(),
            lambda: pytest.fail("check should not run"),
            "inspection",
            attempts=3,
            sleep=lambda _delay: pytest.fail("sleep should not run"),
        )


def test_run_waits_for_its_ngrok_child_after_reconciling():
    class Child:
        terminated = False

        def poll(self):
            return None

        def wait(self, timeout=None):
            assert timeout is None
            return 23

        def terminate(self):
            self.terminated = True

    child = Child()
    started = []
    replies = iter(
        [
            {
                "tunnels": [
                    {
                        "public_url": "https://dynamic.ngrok-free.app",
                        "config": {"addr": "http://localhost:8080"},
                    }
                ]
            },
            {"status": "ready", "workers_alive": True},
            subscription("https://dynamic.ngrok-free.app/webhooks/whatsapp"),
        ]
    )

    def popen(args):
        started.append(args)
        return child

    result = tunnel_runner.run(
        Path("/opt/ngrok"),
        None,
        config(),
        popen=popen,
        opener=lambda *_a, **_k: response(next(replies)),
    )

    assert result == 23
    assert started[0][:3] == ["/opt/ngrok", "http", "8080"]
    assert child.terminated is False


def test_run_reports_process_start_failure_without_raw_exception():
    def popen(_args):
        raise OSError("private path detail")

    with pytest.raises(tunnel_runner.RunnerError, match="ngrok could not be started") as failure:
        tunnel_runner.run(Path("/opt/ngrok"), None, config(), popen=popen)
    assert "private path detail" not in str(failure.value)
