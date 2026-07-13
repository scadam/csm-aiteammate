"""
App-only Microsoft Graph access for the control plane (read-only directory facts).

The control plane runs server-side (no per-turn user context), so the
governance/technical view reads tenant facts — the agent **blueprint** service
principal, any deployed **agent instances**, and CSM **directory attributes**
(job title, office location) — with the host's **own** identity, using
``DefaultAzureCredential`` (the container's managed identity; ``az login`` in
dev). No client secrets, no keys.

Everything here is **best-effort and honest**: if no token can be acquired or a
call fails (e.g. the identity lacks ``Directory.Read.All`` /
``Application.Read.All``), helpers return ``None``/empty and the caller shows an
explicit "not available" state. Nothing is ever fabricated.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import config

logger = logging.getLogger(__name__)

_GRAPH_SCOPE = "https://graph.microsoft.com/.default"

_token_cache: dict[str, object] = {"token": None, "exp": 0.0}
_unavailable_reason: str | None = None


def unavailable_reason() -> str | None:
    """A short human-readable reason the last Graph call could not be made (or None)."""
    return _unavailable_reason


async def app_token() -> str | None:
    """Acquire an app-only Graph token via DefaultAzureCredential (cached). None on failure."""
    global _unavailable_reason
    now = time.time()
    if _token_cache["token"] and now < float(_token_cache["exp"]) - 60:
        return str(_token_cache["token"])

    def _acquire() -> tuple[str | None, float, str | None]:
        try:
            from azure.identity import DefaultAzureCredential
        except Exception as exc:  # pragma: no cover - dependency missing
            return None, 0.0, f"azure-identity unavailable: {exc}"
        try:
            # DefaultAzureCredential does not accept a tenant_id kwarg; the
            # container's managed identity already lives in the demo tenant, and
            # a tenant can be pinned via the AZURE_TENANT_ID environment variable
            # when needed. Exclude interactive/dev credentials for server use.
            cred = DefaultAzureCredential(
                exclude_interactive_browser_credential=True,
                exclude_shared_token_cache_credential=True,
                exclude_visual_studio_code_credential=True,
            )
            tok = cred.get_token(_GRAPH_SCOPE)
            return tok.token, float(tok.expires_on), None
        except Exception as exc:  # pragma: no cover - depends on live identity
            return None, 0.0, f"token acquisition failed: {exc}"

    token, exp, reason = await asyncio.to_thread(_acquire)
    if token:
        _token_cache["token"] = token
        _token_cache["exp"] = exp
        _unavailable_reason = None
        return token
    _unavailable_reason = reason
    logger.info("Graph app token unavailable: %s", reason)
    return None


_app_only_cache: dict[str, object] = {"token": None, "exp": 0.0}


async def app_only_token() -> str | None:
    """Acquire an **app-only** Graph token via the agent's client credentials (cached).

    Sending email *as a manager* through ``/users/{id}/sendMail`` needs an
    application identity holding the ``Mail.Send`` application permission — a
    delegated ``az login`` user token cannot send as another mailbox. We use the
    agent's own app registration (the ``CONNECTIONS__SERVICE_CONNECTION``
    client id/secret) for this. Returns ``None`` when no client secret is
    configured (e.g. the container uses its managed identity, which goes through
    :func:`app_token`) so the caller can fall back.
    """
    if not (config.SERVICE_CLIENT_ID and config.SERVICE_CLIENT_SECRET and config.SERVICE_TENANT_ID):
        return None
    now = time.time()
    if _app_only_cache["token"] and now < float(_app_only_cache["exp"]) - 60:
        return str(_app_only_cache["token"])

    def _acquire() -> tuple[str | None, float, str | None]:
        try:
            from azure.identity import ClientSecretCredential
        except Exception as exc:  # pragma: no cover - dependency missing
            return None, 0.0, f"azure-identity unavailable: {exc}"
        try:
            cred = ClientSecretCredential(
                tenant_id=config.SERVICE_TENANT_ID,
                client_id=config.SERVICE_CLIENT_ID,
                client_secret=config.SERVICE_CLIENT_SECRET,
            )
            tok = cred.get_token(_GRAPH_SCOPE)
            return tok.token, float(tok.expires_on), None
        except Exception as exc:  # pragma: no cover - depends on live identity
            return None, 0.0, f"client-credential token failed: {exc}"

    token, exp, reason = await asyncio.to_thread(_acquire)
    if token:
        _app_only_cache["token"] = token
        _app_only_cache["exp"] = exp
        return token
    logger.info("Graph app-only (client-credential) token unavailable: %s", reason)
    return None


async def graph_get(path: str, token: str, params: dict | None = None) -> dict | None:
    """GET a Graph resource. Returns the parsed JSON, or None on any error."""
    global _unavailable_reason
    import aiohttp

    url = path if path.startswith("http") else f"{config.GRAPH_BASE_URL}{path}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"Authorization": f"Bearer {token}"}, params=params) as r:
                if r.status >= 400:
                    body = (await r.text())[:300]
                    _unavailable_reason = f"Graph GET {path} -> {r.status}: {body}"
                    logger.info(_unavailable_reason)
                    return None
                return await r.json()
    except Exception as exc:  # pragma: no cover - depends on live Graph
        _unavailable_reason = f"Graph GET {path} failed: {exc}"
        logger.info(_unavailable_reason)
        return None


async def graph_post(path: str, token: str, json_body: dict) -> tuple[int, str]:
    """POST to a Graph resource. Returns ``(status, body_text)`` (no exception)."""
    import aiohttp

    url = path if path.startswith("http") else f"{config.GRAPH_BASE_URL}{path}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=json_body,
            ) as r:
                return r.status, (await r.text())
    except Exception as exc:  # pragma: no cover - depends on live Graph
        return 0, f"{type(exc).__name__}: {exc}"


async def send_mail_as_user(
    sender_upn: str, to_email: str, subject: str, body: str, *,
    save_to_sent: bool = True, html: bool = False,
) -> tuple[bool, str]:
    """Send an email **as** ``sender_upn`` via Graph app-only ``/users/{id}/sendMail``.

    Uses the host's app-only token (needs the ``Mail.Send`` application permission).
    The message is sent from the sender's mailbox (so it lands in their Sent Items)
    and delivered to ``to_email``. Set ``html=True`` to send a rich HTML body.
    Returns ``(sent, detail)``; never raises.
    """
    if not (sender_upn and to_email):
        return False, "missing sender or recipient"
    # Prefer an app-only token (client credentials) — it carries the Mail.Send
    # application permission needed to send as the manager. Fall back to the
    # host identity (managed identity in the container) when no client secret is set.
    token = await app_only_token() or await app_token()
    if not token:
        return False, f"no Graph token ({unavailable_reason() or 'unavailable'})"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML" if html else "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        },
        "saveToSentItems": save_to_sent,
    }
    import urllib.parse

    status, text = await graph_post(f"/users/{urllib.parse.quote(sender_upn)}/sendMail", token, payload)
    if status in (200, 202):
        return True, f"sent as {sender_upn} to {to_email}"
    return False, f"Graph sendMail HTTP {status}: {text[:300]}"


async def create_draft_as_user(
    sender_upn: str, to_email: str, subject: str, body: str, *, html: bool = False
) -> tuple[bool, str]:
    """Create a real **draft** message in ``sender_upn``'s mailbox (Drafts folder).

    Uses Graph app-only ``POST /users/{id}/messages`` (creating a message resource
    yields an unsent draft). Needs the ``Mail.ReadWrite`` application permission.
    The CSM can open it in Outlook, review, and send it themselves. Returns
    ``(saved, detail)``; never raises.
    """
    if not (sender_upn and to_email):
        return False, "missing sender or recipient"
    token = await app_only_token() or await app_token()
    if not token:
        return False, f"no Graph token ({unavailable_reason() or 'unavailable'})"
    payload = {
        "subject": subject,
        "body": {"contentType": "HTML" if html else "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
        "isDraft": True,
    }
    import urllib.parse

    status, text = await graph_post(f"/users/{urllib.parse.quote(sender_upn)}/messages", token, payload)
    if status in (200, 201):
        return True, f"draft created in {sender_upn}'s mailbox for {to_email}"
    return False, f"Graph create-draft HTTP {status}: {text[:300]}"

