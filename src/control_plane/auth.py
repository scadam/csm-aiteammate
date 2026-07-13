"""
Identity resolution for the control-plane web app.

Resolves the **acting user** (who is signed in) from, in priority order:

1. A **Teams SSO bearer token** — the real path when the dashboard is embedded
   in Teams as a static tab. The Teams JS ``authentication.getAuthToken()`` call
   returns an Entra access token for the signed-in user; the page sends it as
   ``Authorization: Bearer …`` and we validate it here against the Entra JWKS
   (signature, issuer, audience, expiry) and read the ``oid``/``upn`` claims.
2. A **signed session cookie** set by the simulated sign-in picker (browser
   fallback for local/dev demos — "view as" any real tenant user).
3. The configured **default** manager (last resort).

The resolved Entra ``oid``/``upn`` is the join key: :func:`src.identity.resolve_user`
maps it to a CSM (manager) and/or the programme owner (sponsor), which decides
the dashboard and scopes the data — exactly the production pattern.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

from aiohttp import web

from .. import config, identity

logger = logging.getLogger(__name__)

# Cookie + signing for the simulated browser session (dev/demo fallback).
SESSION_COOKIE = "csm_session"
_SESSION_SECRET = os.getenv("CONTROL_PLANE_SESSION_SECRET", "") or hashlib.sha256(
    f"csm-autopilot::{config.AGENT_BLUEPRINT_ID}".encode()
).hexdigest()
_SESSION_TTL = int(os.getenv("CONTROL_PLANE_SESSION_TTL", "43200"))  # 12h

# Teams SSO audiences/issuers we accept (the bot/blueprint app + its App ID URI).
_TENANT = config.AGENT_TENANT_ID
_APP_ID = config.AGENT_BLUEPRINT_ID
_JWKS_URL = f"https://login.microsoftonline.com/{_TENANT}/discovery/v2.0/keys" if _TENANT else ""

try:  # PyJWT is optional; without it we fail closed to the session/default path.
    import jwt
    from jwt import PyJWKClient

    _jwks_client = PyJWKClient(_JWKS_URL) if _JWKS_URL else None
    _HAS_JWT = True
except Exception:  # pragma: no cover - depends on env
    jwt = None  # type: ignore[assignment]
    _jwks_client = None
    _HAS_JWT = False


# ── Teams SSO token validation ─────────────────────────────────────
def validate_teams_token(token: str) -> dict | None:
    """Validate a Teams SSO access token and return its claims, or None.

    Verifies the signature against the Entra JWKS, the issuer (tenant), and that
    our app id is the audience. Returns the decoded claims on success.
    """
    if not token or not _HAS_JWT or _jwks_client is None:
        return None
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token).key
        # Verify signature + expiry here; audience is checked manually below to
        # tolerate both the bare app-id and the api://…/<app-id> forms Teams uses.
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
    except Exception as exc:  # pragma: no cover - depends on live tokens
        logger.info("Teams SSO token validation failed: %s", exc)
        return None

    # Audience must reference our app id (raw guid or api://<domain>/<guid>).
    aud = claims.get("aud", "")
    aud_ok = aud == _APP_ID or (isinstance(aud, str) and aud.endswith(_APP_ID))
    if not aud_ok:
        logger.info("Teams SSO token audience mismatch: %s", aud)
        return None

    # Issuer must be our tenant (v2 or v1 issuer forms).
    iss = claims.get("iss", "")
    if _TENANT and _TENANT not in iss:
        logger.info("Teams SSO token issuer mismatch: %s", iss)
        return None
    return claims


# ── simulated signed session (browser fallback) ────────────────────
def _sign(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    sig = hmac.new(_SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def _unsign(value: str) -> dict | None:
    try:
        raw, sig = value.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def set_session(response: web.StreamResponse, *, manager_id: str | None,
                object_id: str | None = None, upn: str | None = None) -> None:
    payload = {"manager_id": manager_id, "oid": object_id, "upn": upn,
               "exp": int(time.time()) + _SESSION_TTL}
    response.set_cookie(SESSION_COOKIE, _sign(payload), max_age=_SESSION_TTL,
                        httponly=True, samesite="Lax", path="/")


def clear_session(response: web.StreamResponse) -> None:
    response.del_cookie(SESSION_COOKIE, path="/")


# ── resolve the acting user for a request ──────────────────────────
def _bearer(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Teams-Token") or None


def resolve_acting_user(request: web.Request, *, body_token: str | None = None) -> identity.UserPrincipal:
    """Resolve who is making this request (Teams SSO → session → default)."""
    # 1) Teams SSO bearer token (real).
    token = body_token or _bearer(request)
    if token:
        claims = validate_teams_token(token)
        if claims:
            return identity.resolve_user(
                object_id=claims.get("oid"),
                upn=claims.get("preferred_username") or claims.get("upn") or claims.get("unique_name"),
                display_name=claims.get("name"),
                source="teams_sso",
            )

    # 2) Signed session cookie (simulated picker).
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        payload = _unsign(cookie)
        if payload:
            if payload.get("oid") or payload.get("upn"):
                return identity.resolve_user(object_id=payload.get("oid"), upn=payload.get("upn"),
                                             source="session")
            if payload.get("manager_id"):
                return identity.resolve_user_by_manager_id(payload["manager_id"], source="session")

    # 3) Default (local dev / browser demo). Resolve to the configured manager,
    # or the first real CSM if that id is stale, so the demo "just works".
    from .. import data_store

    default_id = config.AGENT_MANAGER_USER_ID
    if not data_store.get("managers", "manager_id", default_id):
        managers = data_store.table("managers")
        default_id = managers[0]["manager_id"] if managers else default_id
    return identity.resolve_user_by_manager_id(default_id, source="default")


def auth_status() -> dict:
    """Diagnostic: how the control plane will validate Teams SSO tokens."""
    return {
        "jwtValidation": _HAS_JWT and bool(_JWKS_URL),
        "tenant": _TENANT or None,
        "appId": _APP_ID or None,
        "jwksUrl": _JWKS_URL or None,
    }
