"""bugsbot: an agent harness that works GitHub issues and PRs carrying chosen labels."""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from bugsbot.store import default_home


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1" if host in ("0.0.0.0", "") else host, port)) != 0


def _base_url(args: argparse.Namespace, port: int) -> str:
    host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    return (args.public_url or f"http://{host}:{port}").rstrip("/")


def _login_link(home: Path, base: str) -> str:
    from bugsbot.auth import Auth
    from bugsbot.store import Store

    return f"{base}/auth/link?t={Auth(Store(home)).mint_login_link()}"


def main() -> None:
    parser = argparse.ArgumentParser(prog="bugsbot", description=__doc__)
    parser.add_argument("command", nargs="?", choices=["serve", "login-link"], default="serve",
                        help="serve (default) runs the harness; login-link prints a single-use sign-in "
                             "link for an already-running server")
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
    if args.command == "login-link":
        print(_login_link(home, _base_url(args, args.port)))
        return

    port = args.port
    while not _port_free(args.host, port):
        port += 1

    import uvicorn  # imported late so --help is fast

    from bugsbot.app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app(home, public_url=args.public_url)
    base = _base_url(args, port)
    link = _login_link(home, base)

    sys.stdout.reconfigure(line_buffering=True)
    print(f"bugsbot: state in {home}")
    print(f"bugsbot: web UI at {base}/  (listening on {args.host}:{port})")
    print(f"bugsbot: single-use sign-in link (valid 10 minutes): {link}")
    print("bugsbot:   configure GitHub sign-in under Settings; `bugsbot login-link` mints another link")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(link,)).start()
    uvicorn.run(app, host=args.host, port=port, log_level="warning",
                proxy_headers=True, forwarded_allow_ips=args.forwarded_allow_ips)
