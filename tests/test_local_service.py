import importlib.util
import plistlib
import subprocess
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "local_service.py"
SPEC = importlib.util.spec_from_file_location("local_service", MODULE_PATH)
assert SPEC and SPEC.loader
local_service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local_service)


def make_root(tmp_path):
    root = tmp_path / "staged runtime"
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    tunnel_runner = root / "scripts" / "tunnel_runner.py"
    tunnel_runner.parent.mkdir()
    tunnel_runner.write_text("")
    return root


def test_plists_use_staged_runtime_and_dynamic_tunnel_runner(tmp_path):
    root = make_root(tmp_path)
    ngrok = tmp_path / "ngrok"
    ngrok.write_text("")
    plists = local_service.service_plists(root, None, ngrok, use_caffeinate=True)

    app = plists[local_service.APP_LABEL]
    tunnel = plists[local_service.TUNNEL_LABEL]
    assert app["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-i"]
    assert app["ProgramArguments"][2] == str(root / ".venv/bin/python")
    assert app["WorkingDirectory"] == str(root)
    assert app["StandardOutPath"].startswith(str(root))
    assert app["KeepAlive"] is True
    assert app["RunAtLoad"] is True
    assert app["ThrottleInterval"] == 10
    assert app["EnvironmentVariables"] == {"PATH": local_service.MINIMAL_PATH}
    assert tunnel["ProgramArguments"] == [
        str(root / ".venv/bin/python"),
        str(root / "scripts/tunnel_runner.py"),
        "--ngrok-path",
        str(ngrok),
    ]
    assert ".env" not in plistlib.dumps(app).decode()


def test_fixed_public_url_is_forwarded_only_when_explicit(tmp_path):
    root = make_root(tmp_path)
    ngrok = tmp_path / "ngrok"
    ngrok.write_text("")
    tunnel = local_service.service_plists(
        root, "https://fixed.ngrok.app", ngrok, use_caffeinate=False
    )[local_service.TUNNEL_LABEL]
    assert tunnel["ProgramArguments"][-2:] == ["--public-url", "https://fixed.ngrok.app"]


def test_write_refuses_unknown_plist(tmp_path):
    path = tmp_path / "Library" / "LaunchAgents" / f"{local_service.APP_LABEL}.plist"
    path.parent.mkdir(parents=True)
    with path.open("wb") as handle:
        plistlib.dump({"Label": local_service.APP_LABEL}, handle)

    with pytest.raises(local_service.ServiceError, match="unrecognized"):
        local_service.write_plist_safely(path, local_service.APP_LABEL, {"Label": local_service.APP_LABEL})


def test_write_managed_plist_is_private_and_replaceable(tmp_path):
    path = tmp_path / "Library" / "LaunchAgents" / f"{local_service.APP_LABEL}.plist"
    payload = {"Label": local_service.APP_LABEL, local_service.MANAGED_KEY: True}
    local_service.write_plist_safely(path, local_service.APP_LABEL, payload)
    assert local_service.managed_plist(path, local_service.APP_LABEL)
    assert path.stat().st_mode & 0o777 == 0o600
    local_service.write_plist_safely(path, local_service.APP_LABEL, payload)


def test_status_requires_running_state(tmp_path, capsys):
    for label in (local_service.APP_LABEL, local_service.TUNNEL_LABEL):
        local_service.write_plist_safely(
            local_service.plist_path(label, tmp_path),
            label,
            {"Label": label, local_service.MANAGED_KEY: True},
        )

    def runner(command, **_kwargs):
        label = command[-1]
        state = "running" if label.endswith(local_service.APP_LABEL) else "exited"
        return subprocess.CompletedProcess(command, 0, f"state = {state}\n", "")

    assert local_service.status(tmp_path, runner) == 1
    assert "com.smartbazaar.local-tunnel: exited" in capsys.readouterr().out


@pytest.mark.parametrize(
    "url",
    ["http://example.test", "https://example.test/", "https://example.test/x", "https://example.test?a=b"],
)
def test_public_url_must_be_an_exact_https_origin(url):
    with pytest.raises(local_service.ServiceError):
        local_service.validate_public_url(url)
