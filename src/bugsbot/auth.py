"""Authentication for the web UI and API.

- One admin password (scrypt-hashed) unlocks a signed, HttpOnly session cookie.
- An optional API token (stored hashed) allows `Authorization: Bearer <token>` access.
- A single-use, short-lived login link is minted at startup so the auto-opened
  browser tab is signed in without typing the password.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict

from .store import Store

COOKIE = "bugsbot_session"
SESSION_TTL = 14 * 24 * 3600
LOGIN_LINK_TTL = 600
MAX_FAILURES = 5
FAILURE_WINDOW = 300


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Auth:
    def __init__(self, store: Store):
        self.store = store
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._login_links: dict[str, float] = {}
        if not store.get_setting("session_secret"):
            store.update_settings({"session_secret": secrets.token_hex(32)})

    # -- password --------------------------------------------------------------

    @property
    def has_password(self) -> bool:
        return bool(self.store.get_setting("admin_password_hash"))

    def set_password(self, password: str) -> None:
        if len(password) < 10:
            raise ValueError("password must be at least 10 characters")
        # Rotating the secret signs out every existing session.
        self.store.update_settings({"admin_password_hash": hash_password(password),
                                    "session_secret": secrets.token_hex(32)})

    def ensure_password(self) -> str | None:
        """On first run, generate a password and return it so the CLI can print it once."""
        if self.has_password:
            return None
        password = secrets.token_urlsafe(15)
        self.set_password(password)
        return password

    def rate_limited(self, client: str) -> bool:
        now = time.time()
        self._failures[client] = [t for t in self._failures[client] if now - t < FAILURE_WINDOW]
        return len(self._failures[client]) >= MAX_FAILURES

    def check_password(self, client: str, password: str) -> bool:
        if self.rate_limited(client):
            return False
        if verify_password(password, self.store.get_setting("admin_password_hash")):
            self._failures.pop(client, None)
            return True
        self._failures[client].append(time.time())
        return False

    # -- sessions --------------------------------------------------------------

    def _sign(self, payload: str) -> str:
        key = self.store.get_setting("session_secret").encode()
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()

    def new_session(self) -> str:
        payload = f"{int(time.time()) + SESSION_TTL}.{secrets.token_hex(8)}"
        return f"{payload}.{self._sign(payload)}"

    def valid_session(self, cookie: str | None) -> bool:
        if not cookie or cookie.count(".") != 2:
            return False
        expiry, nonce, sig = cookie.split(".")
        if not hmac.compare_digest(sig, self._sign(f"{expiry}.{nonce}")):
            return False
        return expiry.isdigit() and int(expiry) > time.time()

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

    # -- one-time login links ----------------------------------------------------

    def mint_login_link(self) -> str:
        token = secrets.token_urlsafe(24)
        self._login_links[token] = time.time() + LOGIN_LINK_TTL
        return token

    def redeem_login_link(self, token: str) -> bool:
        expiry = self._login_links.pop(token, 0)
        return expiry > time.time()
