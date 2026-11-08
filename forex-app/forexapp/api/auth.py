"""Login check for the REST API and the WebSocket.

* ``none``: development only, everyone is "default".
* ``token``: one shared API token (``FOREX_API_TOKEN``), sent as
  ``Authorization: Bearer <token>`` or ``?token=`` on the WebSocket.
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


def http_user(cfg):
    def dep(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token")
        try:
            return _user_from_token(cfg, token)
        except PermissionError as e:
            raise HTTPException(status_code=401, detail=str(e))
    return dep


def ws_user(cfg, ws: WebSocket) -> str:
    return _user_from_token(cfg, ws.query_params.get("token"))
