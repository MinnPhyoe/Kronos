"""Login check for the REST API and the WebSocket.

* ``none``: development only, everyone is "default".
* ``token``: one shared password (``FOREX_PASSWORD``, or ``FOREX_API_TOKEN``),
  sent as ``Authorization: Bearer <token>`` or ``?token=`` on the WebSocket.
  The web app asks for it once and remembers it on that device. After 10
  wrong tries from one address in 10 minutes, that address waits.
* ``supabase``: users log in with Supabase Auth in the app; the API verifies
  the access token (HS256, ``SUPABASE_JWT_SECRET``) and uses its ``sub`` as
  the user id. (Firebase Auth works the same way with its own verifier.)

The data provider's API key never leaves the server: the app only talks to
this API.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import HTTPException, Request, WebSocket


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_hs256(token: str, secret: str, audience: str | None = "authenticated") -> dict:
    try:
        h, p, sig = token.split(".")
        header = json.loads(_b64d(h))
        payload = json.loads(_b64d(p))
    except (ValueError, TypeError):
        raise ValueError("malformed token")
    if header.get("alg") != "HS256":
        raise ValueError("unsupported token algorithm")
    want = hmac.new(secret.encode(), (h + "." + p).encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(want, _b64d(sig)):
        raise ValueError("bad signature")
    if payload.get("exp") is not None and time.time() > float(payload["exp"]):
        raise ValueError("token expired")
    aud = payload.get("aud")
    if audience and aud is not None and audience not in (aud if isinstance(aud, list) else [aud]):
        raise ValueError("wrong audience")
    return payload


def _user_from_token(cfg, token: str | None) -> str:
    mode = cfg.auth_mode
    if mode == "none":
        return "default"
    if not token:
        raise PermissionError("missing token")
    if mode == "token":
        if cfg.api_token and hmac.compare_digest(token, cfg.api_token):
            return "default"
        raise PermissionError("bad token")
    if mode == "supabase":
        if not cfg.supabase_jwt_secret:
            raise PermissionError("SUPABASE_JWT_SECRET is not set")
        try:
            return str(verify_hs256(token, cfg.supabase_jwt_secret).get("sub") or "default")
        except ValueError as e:
            raise PermissionError(str(e))
    raise PermissionError("unknown FOREX_AUTH_MODE %r" % mode)


class Lockout:
    """Slows down password guessing: 10 failures per address per 10 minutes."""

    def __init__(self, limit: int = 10, window: float = 600.0):
        self.limit, self.window = limit, window
        self.fails: dict[str, list] = {}

    def blocked(self, addr: str) -> bool:
        now = time.time()
        recent = [t for t in self.fails.get(addr, []) if now - t < self.window]
        self.fails[addr] = recent
        return len(recent) >= self.limit

    def fail(self, addr: str):
        self.fails.setdefault(addr, []).append(time.time())


def http_user(cfg, lockout: Lockout | None = None):
    lockout = lockout or Lockout()

    def dep(request: Request) -> str:
        addr = request.client.host if request.client else "?"
        if cfg.auth_mode != "none" and lockout.blocked(addr):
            raise HTTPException(status_code=429, detail="too many wrong passwords: wait 10 minutes")
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token")
        try:
            return _user_from_token(cfg, token)
        except PermissionError as e:
            if token:
                lockout.fail(addr)
            raise HTTPException(status_code=401, detail=str(e))
    dep.lockout = lockout
    return dep


def ws_user(cfg, ws: WebSocket, lockout: Lockout | None = None) -> str:
    addr = ws.client.host if ws.client else "?"
    if lockout and cfg.auth_mode != "none" and lockout.blocked(addr):
        raise PermissionError("too many wrong passwords")
    try:
        return _user_from_token(cfg, ws.query_params.get("token"))
    except PermissionError:
        if lockout and ws.query_params.get("token"):
            lockout.fail(addr)
        raise
