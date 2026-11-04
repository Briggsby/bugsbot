"""Authentication for the web UI and API.

- GitHub OAuth (see oauth.py) is the only interactive sign-in. It yields a signed,
  HttpOnly session cookie.
- An optional API token (stored hashed) allows `Authorization: Bearer <token>` access.
- Single-use, short-lived sign-in links, printed to the server's terminal, let the
  operator in before OAuth is configured or if it ever breaks.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from bugsbot.store import Store

COOKIE = "bugsbot_session"
SESSION_TTL = 24 * 3600
LOGIN_LINK_TTL = 600
LINK_USER = "operator"  # session identity for terminal sign-in links


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Auth:
    def __init__(self, store: Store):
        self.store = store
        if not store.get_setting("session_secret"):
            store.update_settings({"session_secret": secrets.token_hex(32)})

    # -- sessions --------------------------------------------------------------

    def _sign(self, payload: str) -> str:
        key = self.store.get_setting("session_secret").encode()
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()

    def new_session(self, user: str, ttl: int = SESSION_TTL) -> str:
        """`user` is 'gh:<login>' for GitHub sign-in or LINK_USER for a terminal sign-in link."""
        payload = f"{int(time.time()) + ttl}.{secrets.token_hex(8)}.{user}"
        return f"{payload}.{self._sign(payload)}"

    def session_user(self, cookie: str | None) -> str | None:
        if not cookie or cookie.count(".") != 3:
            return None
        payload, sig = cookie.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sign(payload)):
            return None
        expiry, _, user = payload.split(".")
        return user if expiry.isdigit() and int(expiry) > time.time() else None

    def valid_session(self, cookie: str | None) -> bool:
        return self.session_user(cookie) is not None

    def sign_out_everyone(self) -> None:
        self.store.update_settings({"session_secret": secrets.token_hex(32)})

    # -- API token ---------------------------------------------------------------

    def regenerate_api_token(self) -> str:
        token = "bb_" + secrets.token_urlsafe(32)
        self.store.update_settings({"api_token_hash": _sha256(token)})
        return token

    def revoke_api_token(self) -> None:
        self.store.update_settings({"api_token_hash": ""})

    def valid_bearer(self, header: str | None) -> bool:
        stored = self.store.get_setting("api_token_hash")
        if not stored or not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(_sha256(header[7:].strip()), stored)

    # -- single-use sign-in links -------------------------------------------------

    def mint_login_link(self) -> str:
        token = secrets.token_urlsafe(24)
        self.store.add_login_link(_sha256(token), time.time() + LOGIN_LINK_TTL)
        return token

    def redeem_login_link(self, token: str) -> bool:
        return bool(token) and self.store.consume_login_link(_sha256(token))
