"""
Gainsight PX capabilities (simulated-real Gainsight PX / Aptrinsic REST).

Speaks the **real Gainsight PX (Aptrinsic) REST contracts** through
:class:`src.gainsight.client.GainsightRestClient` (the PX surface:
``GET /v1/engagements``, ``POST /v1/engagements/{id}/execute``,
``GET /v1/accounts/{id}/feature-activity``), served from the local fixtures.

* ``trigger_in_product_message`` — execute an approved in-product engagement to a
  user (records the engagement so the same content is not shown repeatedly).
* ``get_engagement_history`` — in-product engagement/feature activity for a user
  or account (knowledge base 3), used to avoid repetition.
"""

from __future__ import annotations

import json
import logging

from .. import data_store, identity
from ..gainsight.client import get_client as gainsight
from ..gainsight.rest import _gsid
from ..observability import execute_tool_scope

logger = logging.getLogger(__name__)


async def get_engagement_history(user_id: str = "", account_id: str = "") -> str:
    """Return in-product engagement history (PX feature-activity) for a user/account."""
    with execute_tool_scope("gainsight_px.get_engagement_history", {"user_id": user_id, "account_id": account_id}):
        if account_id:
            resolved = data_store.resolve_account(account_id)
            account_id = resolved["account_id"] if resolved else account_id
            gs = gainsight()
            result = gs.request("GET", f"/v1/accounts/{_gsid('1P02', account_id)}/feature-activity")
            rows = result.get("featureActivity", [])
        else:
            rows = data_store.table("px_engagement")
        out = [
            r for r in rows
            if (not user_id or r.get("user_id") == user_id)
            and (not account_id or r.get("account_id") in ("", account_id))
        ]
        if not out:
            return "No in-product engagement history found."
        return json.dumps(out, indent=2, default=str)


async def trigger_in_product_message(user_id: str, content_id: str) -> str:
    """Execute an approved Gainsight PX in-product engagement for a user."""
    with execute_tool_scope("gainsight_px.trigger_in_product_message", {"user_id": user_id, "content_id": content_id}):
        content = data_store.get("content_library", "content_id", content_id)
        if content is None:
            return f"Content '{content_id}' not found."
        if str(content.get("approved", "")).lower() != "yes":
            return f"Content '{content_id}' is not approved; cannot trigger."

        # Avoid repetition: skip if this user has already been shown this content.
        already = [
            e for e in data_store.find("px_engagement", user_id=user_id)
            if e.get("content_id") == content_id
        ]
        if already:
            return f"Skipped: user {user_id} was already shown '{content.get('title')}'."

        # Look up the PX engagement, then execute it (real PX contract).
        gs = gainsight()
        listing = gs.request("GET", "/v1/engagements")
        engagement = next(
            (e for e in listing.get("engagements", []) if e.get("contentId") == content_id), None
        )
        if engagement is None:
            return f"No Gainsight PX engagement found for content '{content_id}'."
        gs.request("POST", f"/v1/engagements/{engagement['id']}/execute")

        engagement_id = f"PX-{len(data_store.table('px_engagement')) + 4001}"
        data_store.append(
            "px_engagement",
            {
                "engagement_id": engagement_id,
                "account_id": "",
                "user_id": user_id,
                "content_id": content_id,
                "content_title": content.get("title", ""),
                "shown_date": "2026-06-05",
                "action": "shown",
            },
        )
        return f"Gainsight PX engagement '{content.get('title')}' executed for {user_id} ({engagement_id})."
