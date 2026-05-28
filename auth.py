"""Minimal shared-secret + HTTP Basic auth for the SOT app.

This app has *no* multi-user identity model — it is a single-team admin tool.
For a public-internet deployment we just want to keep strangers off the
endpoints. Two modes are supported, controlled by env vars:

  SOT_AUTH_MODE=basic      → HTTP Basic auth, credentials from
                              SOT_AUTH_USER / SOT_AUTH_PASSWORD.
  SOT_AUTH_MODE=token      → require `X-Auth-Token: <SOT_AUTH_TOKEN>` header
                              OR `?token=<SOT_AUTH_TOKEN>` query param.
                              Suitable for API-only use.
  SOT_AUTH_MODE=none       → no auth (default for local dev).
                              Refuses to start unless host is loopback or
                              SOT_ALLOW_OPEN=1 is set.

Apply via `@require_auth` on every route, or globally with
`app.before_request(enforce_auth)`.
"""

from __future__ import annotations

import os
import secrets
from functools import wraps

from flask import Response, request, current_app


def _mode() -> str:
    return os.environ.get("SOT_AUTH_MODE", "none").strip().lower() or "none"


def _check_basic() -> bool:
    expected_user = os.environ.get("SOT_AUTH_USER", "")
    expected_pw = os.environ.get("SOT_AUTH_PASSWORD", "")
    if not expected_user or not expected_pw:
        return False
    auth = request.authorization
    if not auth or not auth.username or not auth.password:
        return False
    return (
        secrets.compare_digest(auth.username, expected_user)
        and secrets.compare_digest(auth.password, expected_pw)
    )


def _check_token() -> bool:
    expected = os.environ.get("SOT_AUTH_TOKEN", "")
    if not expected:
        return False
    given = request.headers.get("X-Auth-Token") or request.args.get("token") or ""
    return bool(given) and secrets.compare_digest(given, expected)


def is_authenticated() -> bool:
    mode = _mode()
    if mode == "none":
        return True
    if mode == "basic":
        return _check_basic()
    if mode == "token":
        return _check_token()
    return False


def _unauthorized() -> Response:
    if _mode() == "basic":
        return Response(
            "Authentication required",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Source of Truth"'},
        )
    return Response("Authentication required", status=401)


def require_auth(view):
    """Decorator: protect a single Flask view function."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            return _unauthorized()
        return view(*args, **kwargs)
    return wrapper


def enforce_auth():
    """`before_request` hook: protect every route in the app."""
    # Allow static files through unauthenticated (no static dir ships secrets).
    if request.endpoint == "static":
        return None
    if not is_authenticated():
        return _unauthorized()
    return None


def assert_safe_to_serve_open():
    """Refuse to start without auth on a non-loopback host.

    Called from app.py before app.run(). The check is a belt-and-braces guard
    against the most common mistake (deploying with the default config to a
    public host).
    """
    if _mode() != "none":
        return
    host = os.environ.get("SOT_HOST", "127.0.0.1")
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    if os.environ.get("SOT_ALLOW_OPEN", "").strip() == "1":
        return
    raise RuntimeError(
        "SOT_AUTH_MODE=none but SOT_HOST is not loopback. "
        "Either set SOT_AUTH_MODE=basic (with SOT_AUTH_USER/SOT_AUTH_PASSWORD) "
        "or SOT_AUTH_MODE=token (with SOT_AUTH_TOKEN), or set SOT_ALLOW_OPEN=1 "
        "if you really want to expose this app without authentication."
    )
