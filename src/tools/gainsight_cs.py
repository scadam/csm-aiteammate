"""
Gainsight CS capabilities (simulated-real Gainsight NXT REST).

These tools speak the **real Gainsight NXT REST contracts** — Company API,
Timeline API, and Cockpit / Call-To-Action (CTA) API — through
:class:`src.gainsight.client.GainsightRestClient` (real request payloads + real
``{result, errorCode, data, ...}`` response envelopes, served from the local
fixtures). ``send_email`` instead delivers a **real** email through Work IQ
(``do_action /me/sendMail``) on the manager's behalf.

* ``get_account_context`` — Company Read + Timeline + open CTAs for an account.
* ``create_review_task``  — create a Gainsight CTA (the CSM review item).
* ``send_email``          — send a real M365 email via Work IQ; high-influence /
  frustrated / strategic accounts are routed to a Gainsight CTA for CSM review
  instead of being auto-sent.

Manager-scoped actions verify the manager owns the account and acquire an OBO
token before taking real action.
"""

from __future__ import annotations

import json
import logging

from .. import config, data_store, identity, workiq_client
from ..gainsight.client import get_client as gainsight
from ..observability import execute_tool_scope

logger = logging.getLogger(__name__)


def _requires_review(account: dict) -> bool:
    return (
        str(account.get("influence", "")).lower() == "high"
        or str(account.get("sentiment", "")).lower() == "frustrated"
        or str(account.get("strategic", "")).lower() == "yes"
    )


async def get_account_context(account_id: str) -> str:
    """Return account context via the Gainsight Company, Timeline, and CTA APIs."""
    with execute_tool_scope("gainsight_cs.get_account_context", {"account_id": account_id}):
        account = data_store.resolve_account(account_id)
        if account is None:
            return f"Account '{account_id}' not found."
        account_id = account["account_id"]
        gs = gainsight()
        company = gs.request(
            "POST", "/v1/data/objects/query/Company",
            {
                "select": [
                    "Gsid", "Name", "Industry", "Arr", "Stage", "Status", "RenewalDate",
                    "Tier__gc", "Influence__gc", "Sentiment__gc", "Strategic__gc",
                    "Csm__gr.Name", "Scorecard_Overall__gc",
                ],
                "where": {"conditions": [
                    {"name": "AccountId__gc", "alias": "A", "value": [account_id], "operator": "EQ"}
                ], "expression": "A"},
            },
        )
        if not company.get("result") or not company.get("data"):
            return f"Account '{account_id}' not found in Gainsight."

        timeline = gs.request("POST", "/v1/timeline/activities/query", {"contextId": account_id})
        company_gsid = company["data"][0].get("Gsid")
        ctas = gs.request(
            "POST", "/v2/cockpit/cta/list",
            {
                "select": ["Name", "TypeId__gr.Name", "StatusId__gr.Name", "PriorityId__gr.Name", "Comments"],
                "where": {"conditions": [
                    {"name": "CompanyId", "alias": "A", "value": [company_gsid], "operator": "EQ"},
                    {"name": "IsClosed", "alias": "B", "value": ["false"], "operator": "EQ"},
                ], "expression": "A AND B"},
                "pageSize": 25, "pageNumber": 1,
            },
        )
        payload = {
            "company": company["data"][0],
            "recent_timeline": (timeline.get("data") or [])[-5:],
            "open_ctas": ctas.get("data") or [],
        }
        return json.dumps(payload, indent=2, default=str)


async def create_review_task(
    account_id: str,
    message_type: str,
    channel: str,
    priority: str = "Medium",
    draft_text: str = "",
    signal_id: str = "",
) -> str:
    """Create a Gainsight CTA (the CSM review item) via the real CTA Create API."""
    with execute_tool_scope("gainsight_cs.create_review_task", {"account_id": account_id}):
        account = data_store.resolve_account(account_id)
        if account is None:
            return f"Account '{account_id}' not found."
        account_id = account["account_id"]
        if not identity.manager_owns_account(account_id):
            return "This account belongs to a different manager; not permitted."

        manager = identity.resolve_manager() or {}
        gs = gainsight()
        ref = signal_id or f"REF-{account_id}"
        response = gs.request(
            "POST", "/v2/cockpit/cta",
            {
                "requests": [{"record": {
                    "referenceId": ref,
                    "Name": f"{message_type} — {account_id}",
                    "AccountId__gc": account_id,
                    "type": message_type,
                    "reason": channel,
                    "status": "New",
                    "priority": priority,
                    "Comments": draft_text,
                    "OwnerEmail": manager.get("upn", ""),
                    "SignalId__gc": signal_id,
                }}],
                "lookups": {"OwnerId": {
                    "fields": {"OwnerEmail": "Email"}, "lookupField": "Gsid",
                    "objectName": "gsuser", "multiMatchOption": "FIRSTMATCH", "onNoMatch": "ERROR",
                }},
            },
        )
        if not response.get("result"):
            return f"Gainsight CTA create failed: {response.get('errorCode')} {response.get('errorDesc')}"
        success = (response.get("data") or {}).get("success", [])
        cta_gsid = next(iter(success[0].values())) if success else "(unknown)"
        return (
            f"Created Gainsight CTA {cta_gsid} for {account_id} (priority {priority}), "
            f"routed to {manager.get('display_name', 'the CSM')}."
        )


async def send_email(account_id: str, subject: str, body: str) -> str:
    """
    Send a REAL email to the account contact via Work IQ (``do_action /me/sendMail``).

    Enforces the review guardrail: high-influence / frustrated / strategic
    accounts are routed to a Gainsight CTA for CSM review instead of auto-send.
    """
    with execute_tool_scope("gainsight_cs.send_email", {"account_id": account_id}):
        account = data_store.resolve_account(account_id)
        if account is None:
            return f"Account '{account_id}' not found."
        account_id = account["account_id"]
        if not identity.manager_owns_account(account_id):
            return "This account belongs to a different manager; not permitted."

        if _requires_review(account):
            await create_review_task(
                account_id=account_id, message_type="email", channel="csm_review",
                priority="High", draft_text=f"Subject: {subject}\n\n{body}",
            )
            return (
                f"Not auto-sent: {account.get('account_name')} requires CSM review "
                "(high-influence/frustrated/strategic). Routed to a Gainsight CTA instead."
            )

        # Real email on the manager's behalf — Work IQ (OBO) when a live turn is
        # present, else Graph app-only sendMail as the manager. Lands in the CSM's
        # Sent Items and is delivered to the account's real contact mailbox.
        from .. import mail

        result = await mail.deliver_email(account_id=account_id, subject=subject, body=body)
        if result.get("sent"):
            return f"Email sent to {result.get('to')} at {account.get('account_name')} — subject: '{subject}'."
        return (
            f"Could not send email to {account.get('account_name')} "
            f"({result.get('detail', 'delivery unavailable')})."
        )
