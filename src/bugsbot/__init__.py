"""bugsbot: an agent harness that works GitHub issues and PRs carrying chosen labels."""

from __future__ import annotations

import argparse
import getpass
import logging
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from .store import default_home


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1" if host in ("0.0.0.0", "") else host, port)) != 0


def _set_password(home: Path) -> None:
    from .auth import Auth
    from .store import Store

    password = getpass.getpass("New bugsbot password (min 10 chars): ")
    if password != getpass.getpass("Repeat: "):
        sys.exit("Passwords do not match.")
    try:
        store = Store(home)
        Auth(store).set_password(password)
        store.update_settings({"password_login_enabled": True})  # break-glass if GitHub sign-in breaks
    except ValueError as e:
        sys.exit(str(e))
    print("Password updated and password sign-in enabled; existing sessions have been signed out.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="bugsbot", description=__doc__)
    parser.add_argument("command", nargs="?", choices=["serve", "set-password"], default="serve")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default 127.0.0.1; use 0.0.0.0 to expose publicly)")
    parser.add_argument("--port", type=int, default=8765, help="port for the web UI (default 8765)")
    parser.add_argument("--home", type=Path, default=None,
                        help="state directory (default $BUGSBOT_HOME or ~/.bugsbot)")
    parser.add_argument("--public-url", default=None,
                        help="external URL when behind a proxy, used for the printed login link")
    parser.add_argument("--forwarded-allow-ips", default="127.0.0.1",
                        help="proxies trusted for X-Forwarded-* headers (default 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the web UI automatically")
    args = parser.parse_args()

    home = (args.home or default_home()).expanduser().resolve()
    if args.command == "set-password":
        _set_password(home)
        return

    port = args.port
    while not _port_free(args.host, port):
        port += 1

    import uvicorn  # imported late so --help is fast

    from .app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app(home, public_url=args.public_url)
    auth = app.state.auth
    base = (args.public_url or f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{port}").rstrip("/")
    generated = auth.ensure_password()
    link = f"{base}/auth/link?t={auth.mint_login_link()}"

    sys.stdout.reconfigure(line_buffering=True)
    print(f"bugsbot: state in {home}")
    print(f"bugsbot: web UI at {base}/  (listening on {args.host}:{port})")
    if generated:
        print(f"bugsbot: generated admin password: {generated}")
        print("bugsbot:   (shown once — change it in Settings or with `bugsbot set-password`)")
    print(f"bugsbot: one-time sign-in link (valid 10 minutes): {link}")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(link,)).start()
    uvicorn.run(app, host=args.host, port=port, log_level="warning",
                proxy_headers=True, forwarded_allow_ips=args.forwarded_allow_ips)
