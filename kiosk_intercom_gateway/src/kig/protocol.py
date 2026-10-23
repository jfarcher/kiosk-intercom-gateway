"""The Kiosk Satellite intercom wire, as far as a peer needs it.

Derived from reading Kiosk Satellite's intercom_manager.dart, intercom_routes.dart
and remote/auth.dart (behaviour only; no code is copied). Summary:

* Every kiosk serves ``GET /api/intercom/identity`` on its remote admin port:
  ``{id, name, version, enabled, key, dnd, endpoint: {port, tls}}`` where ``key``
  is the first 8 hex digits of SHA-256(intercom key) and ``endpoint`` is the
  dedicated intercom listener. It answers 429 to more than one probe per
  second from the same client.
* Every other route carries a single-use bearer token valid for about 60 s::

      base64url(JSON {intercom: <call id>, from: <sender id>, n: <nonce>, exp: <ms>})
      + "." + base64url(HMAC-SHA256(key="intercom:" + <intercom key>, msg=<payload>))

  Both halves use padded URL-safe base64, as Dart's ``base64Url.encode`` does.
* Audio is raw PCM16 little-endian mono at 16 kHz in 80 ms binary frames.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

SAMPLE_RATE = 16_000
FRAME_MS = 80
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2  # 2560
TOKEN_TTL_S = 60
DEFAULT_ADMIN_PORT = 2324


def fingerprint(key: str) -> str:
    """What ``/api/intercom/identity`` reports as ``key``. Empty for no key."""
    if not key:
        return ""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(key: str, payload: str) -> str:
    secret = f"intercom:{key}".encode()
    return _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())


def issue_token(
    key: str,
    call_id: str,
    sender_id: str,
    ttl_s: int = TOKEN_TTL_S,
    now: float | None = None,
) -> str:
    """A fresh token for one request about ``call_id``. Never reuse one."""
    exp = int(((time.time() if now is None else now) + ttl_s) * 1000)
    claims = {
        "intercom": call_id,
        "from": sender_id,
        "n": secrets.token_hex(4),
        "exp": exp,
    }
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{payload}.{_sign(key, payload)}"


def token_claims(key: str, token: str, now: float | None = None) -> dict[str, Any] | None:
    """The claims of a correctly signed, unexpired token, else None."""
    if not key or not token:
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    if not hmac.compare_digest(parts[1], _sign(key, parts[0])):
        return None
    try:
        claims = json.loads(_unb64(parts[0]))
        exp = claims["exp"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(exp, int):
        return None
    now_ms = int((time.time() if now is None else now) * 1000)
    return claims if now_ms < exp else None


class TokenVerifier:
    """Verifies tokens from kiosks the way a kiosk does: right call, once."""

    def __init__(self, key: str) -> None:
        self.key = key
        self._seen: dict[str, int] = {}

    def verify(self, token: str, call_id: str, now: float | None = None) -> bool:
        claims = token_claims(self.key, token, now)
        if claims is None or str(claims.get("intercom")) != call_id:
            return False
        now_ms = int((time.time() if now is None else now) * 1000)
        self._seen = {t: e for t, e in self._seen.items() if e >= now_ms}
        if token in self._seen:
            return False
        self._seen[token] = claims["exp"]
        return True
