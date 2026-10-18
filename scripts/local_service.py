#!/usr/bin/env python3
"""Install or inspect local launchd supervision for the SmartBazaar POC.

The script writes only its two marked LaunchAgent plists. It never searches for
or kills a process listening on port 8080. A runtime staged outside protected
folders can be selected with ``--runtime-root``.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

APP_LABEL = "com.smartbazaar.local-app"
TUNNEL_LABEL = "com.smartbazaar.local-tunnel"
MANAGED_KEY = "SmartBazaarLocalService"
LOG_DIRECTORY_NAME = "local-service-logs"
MINIMAL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


class ServiceError(RuntimeError):
    """An actionable setup error that must not change a running service."""


def repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def runtime_root(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else repository_root()


def launch_agents_directory(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents"


def plist_path(label: str, home: Path | None = None) -> Path:
    return launch_agents_directory(home) / f"{label}.plist"


def log_directory(root: Path) -> Path:
    return root / "work" / LOG_DIRECTORY_NAME


def ensure_log_directory(root: Path) -> Path:
    directory = log_directory(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    for name in ("app.stdout.log", "app.stderr.log", "tunnel.stdout.log", "tunnel.stderr.log"):
        path = directory / name
        path.touch(exist_ok=True)
        path.chmod(0o600)
    return directory


def find_python(root: Path) -> Path:
    python = root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise ServiceError(f"Expected virtualenv Python at {python}; stage the virtualenv first.")
    # Keep the virtualenv entry point: resolving this symlink loses pyvenv.cfg discovery.
    return python.absolute()


def find_tunnel_runner(root: Path) -> Path:
    runner = root / "scripts" / "tunnel_runner.py"
    if not runner.is_file():
        raise ServiceError(f"Expected the staged tunnel runner at {runner}.")
    return runner.absolute()


def find_ngrok(explicit_path: str | None = None) -> Path:
    candidate = Path(explicit_path).expanduser() if explicit_path else None
    if candidate is None:
        found = shutil.which("ngrok")
        candidate = Path(found) if found else None
    if candidate is None or not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ServiceError("ngrok was not found; install it or pass --ngrok-path with an executable path.")
    return candidate.absolute()


def validate_public_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ServiceError("The public URL must be an exact HTTPS origin, for example https://example.ngrok.app.")
    if value.rstrip("/") != value:
        raise ServiceError("The public URL must not have a trailing slash; preserve the callback origin exactly.")
    return value


def service_plists(
    root: Path,
    public_url: str | None,
    ngrok: Path,
    use_caffeinate: bool,
) -> dict[str, dict[str, Any]]:
    python = find_python(root)
    tunnel_runner = find_tunnel_runner(root)
    logs = log_directory(root)
    app_args = [
        str(python),
        "-m",
        "uvicorn",
        "smartbazaar.app:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        "8080",
        "--no-access-log",
    ]
    if use_caffeinate:
        app_args = ["/usr/bin/caffeinate", "-i", *app_args]
    tunnel_args = [str(python), str(tunnel_runner), "--ngrok-path", str(ngrok)]
    if public_url:
        tunnel_args.extend(["--public-url", validate_public_url(public_url)])
    base = {
        "KeepAlive": True,
        "RunAtLoad": True,
        "ThrottleInterval": 10,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PATH": MINIMAL_PATH},
        MANAGED_KEY: True,
    }
    app = {
        **base,
        "Label": APP_LABEL,
        "ProgramArguments": app_args,
        "StandardOutPath": str(logs / "app.stdout.log"),
        "StandardErrorPath": str(logs / "app.stderr.log"),
    }
    tunnel = {
        **base,
        "Label": TUNNEL_LABEL,
        "ProgramArguments": tunnel_args,
        "StandardOutPath": str(logs / "tunnel.stdout.log"),
        "StandardErrorPath": str(logs / "tunnel.stderr.log"),
    }
    return {APP_LABEL: app, TUNNEL_LABEL: tunnel}


def managed_plist(path: Path, label: str) -> bool:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return False
    return isinstance(payload, dict) and payload.get("Label") == label and payload.get(MANAGED_KEY) is True


def write_plist_safely(path: Path, label: str, payload: dict[str, Any]) -> None:
    if path.exists() and not managed_plist(path, label):
        raise ServiceError(f"Refusing to replace unrecognized LaunchAgent: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(".plist.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def launchctl(args: list[str], runner=subprocess.run) -> subprocess.CompletedProcess[str]:
    return runner(["/bin/launchctl", *args], text=True, capture_output=True, check=False)


def domain() -> str:
    return f"gui/{os.getuid()}"


def service_state(label: str, runner=subprocess.run) -> str | None:
    result = launchctl(["print", f"{domain()}/{label}"], runner)
    if result.returncode:
        return None
    for line in result.stdout.splitlines():
        key, separator, value = line.strip().partition(" = ")
        if separator and key == "state":
            return value.strip()
    return "loaded"


def is_loaded(label: str, runner=subprocess.run) -> bool:
    return service_state(label, runner) is not None


def install(
    root: Path,
    public_url: str | None,
    ngrok: Path,
    use_caffeinate: bool,
    home: Path | None = None,
    runner=subprocess.run,
) -> None:
    ensure_log_directory(root)
    plists = service_plists(root, public_url, ngrok, use_caffeinate)
    for label, payload in plists.items():
        path = plist_path(label, home)
        write_plist_safely(path, label, payload)
        # Do not kickstart an already-loaded agent: that would restart it.
        if not is_loaded(label, runner):
            result = launchctl(["bootstrap", domain(), str(path)], runner)
            if result.returncode:
                raise ServiceError(f"launchctl could not load {label}: {result.stderr.strip()}")


def status(home: Path | None = None, runner=subprocess.run) -> int:
    code = 0
    for label in (APP_LABEL, TUNNEL_LABEL):
        path = plist_path(label, home)
        if not path.exists():
            print(f"{label}: plist missing")
            code = 1
        elif not managed_plist(path, label):
            print(f"{label}: plist is unrecognized")
            code = 1
        else:
            state = service_state(label, runner)
            print(f"{label}: {state or 'not loaded'} ({path})")
            if state != "running":
                code = 1
    return code


def uninstall(home: Path | None = None, runner=subprocess.run) -> None:
    for label in (APP_LABEL, TUNNEL_LABEL):
        path = plist_path(label, home)
        if not path.exists():
            continue
        if not managed_plist(path, label):
            raise ServiceError(f"Refusing to remove unrecognized LaunchAgent: {path}")
        if is_loaded(label, runner):
            result = launchctl(["bootout", domain(), str(path)], runner)
            if result.returncode:
                raise ServiceError(f"launchctl could not unload {label}: {result.stderr.strip()}")
        path.unlink()


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    subcommands = command.add_subparsers(dest="command", required=True)
    for name in ("prepare", "install"):
        sub = subcommands.add_parser(name)
        sub.add_argument("--runtime-root", help="staged runtime root; defaults to this repository")
        sub.add_argument("--public-url", help="reserved ngrok HTTPS origin; omit for a free dynamic URL")
        sub.add_argument("--ngrok-path")
        sub.add_argument("--caffeinate", action="store_true")
    subcommands.add_parser("status")
    subcommands.add_parser("uninstall")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in {"prepare", "install"}:
            root = runtime_root(args.runtime_root)
            public_url = validate_public_url(args.public_url) if args.public_url else None
            ngrok = find_ngrok(args.ngrok_path)
            if args.command == "prepare":
                plists = service_plists(root, public_url, ngrok, args.caffeinate)
                for label, payload in plists.items():
                    print(f"{label}: {plist_path(label)}")
                    print(plistlib.dumps(payload, sort_keys=True).decode(), end="")
            else:
                install(root, public_url, ngrok, args.caffeinate)
        elif args.command == "status":
            return status()
        else:
            uninstall()
    except ServiceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
