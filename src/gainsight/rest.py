"""
Simulated-real Gainsight NXT REST API.

This module implements the **real Gainsight NXT REST contracts** (paths, request
bodies, ``accesskey`` auth header, and the standard
``{result, errorCode, errorDesc, requestId, data, message}`` response envelope)
— but backed by the local JSON fixtures instead of a live Gainsight tenant. It is
a faithful in-process simulation so the demo behaves like a real Gainsight
integration without requiring a Gainsight instance.

Entry point: :meth:`GainsightRestClient.request` routes ``(method, path, body)``
exactly as the real REST surface would. The Gainsight MCP tools
(``src/tools/gainsight_cs.py`` and ``gainsight_px.py``) construct real Gainsight
request payloads and call this client, then format the real response envelopes.

Implemented surface (Gainsight NXT):
- Company API (Company & Relationship): ``POST /v1/data/objects/query/Company``,
  ``POST /v1/data/objects/Company``, ``PUT /v1/data/objects/Company``,
  ``DELETE /v1/data/objects/Company/{Gsid}``.
- Cockpit / Call To Action (CTA) API: ``POST /v2/cockpit/cta/`` (create),
  ``PUT /v2/cockpit/cta/`` (update), ``POST /v2/cockpit/cta/list`` (fetch),
  ``GET /v2/cockpit/admin/picklist/lite`` (config).
- Timeline API: ``POST /v1/timeline/activities`` (create),
  ``POST /v1/timeline/activities/query`` (fetch).
- Person API: ``POST /v1/data/objects/query/Person``.
- Gainsight PX (Aptrinsic): ``GET /v1/engagements``, ``POST /v1/engagements/{id}/execute``,
  ``GET /v1/accounts/{id}/feature-activity``.

References (public Gainsight docs):
- https://support.gainsight.com/gainsight_nxt/API_and_Developer_Docs/Company_and_Relationship_API/Company_API_Documentation
- https://support.gainsight.com/gainsight_nxt/API_and_Developer_Docs/Cockpit_API/Call_To_Action_(CTA)_API_Documentation
- https://support.gainsight.com/gainsight_nxt/API_and_Developer_Docs/Timeline_API/Timeline_APIs
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Any

from .. import config, data_store

logger = logging.getLogger(__name__)


class GainsightApiError(Exception):
    """Raised for malformed requests (mirrors a Gainsight error envelope)."""

    def __init__(self, error_code: str, error_desc: str):
        super().__init__(f"{error_code}: {error_desc}")
        self.error_code = error_code
        self.error_desc = error_desc


def _gsid(prefix: str, key: str) -> str:
    """Deterministic pseudo-GSID for a fixture key (mirrors Gainsight GSID look)."""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest().upper()
    return f"{prefix}{digest[:33]}"


def _envelope(data: Any, *, result: bool = True, error_code: str | None = None,
              error_desc: str | None = None, message: str | None = None) -> dict:
    """Build the standard Gainsight response envelope."""
    return {
        "result": result,
        "errorCode": error_code,
        "errorDesc": error_desc,
        "requestId": str(uuid.uuid4()),
        "data": data,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Object projections — map local fixtures to Gainsight object schemas
# ---------------------------------------------------------------------------

def _company_record(account: dict) -> dict:
    """Project an `accounts` fixture row into a Gainsight Company record."""
    return {
        "Gsid": _gsid("1P02", account["account_id"]),
        "AccountId__gc": account["account_id"],  # external key (custom field)
        "Name": account.get("account_name"),
        "Industry": account.get("industry"),
        "Arr": account.get("arr_gbp"),
        "CurrencyIsoCode": "GBP",
        "Stage": account.get("onboarding_stage"),
        "Status": "Active",
        "RenewalDate": account.get("renewal_date"),
        "Tier__gc": account.get("tier"),
        "Region__gc": account.get("region"),
        "Influence__gc": account.get("influence"),
        "Sentiment__gc": account.get("sentiment"),
        "Strategic__gc": account.get("strategic"),
        "Csm__gr.Name": account.get("csm_name"),
        "Csm__gr.Gsid": _gsid("1P01", account.get("csm_manager_id", "")),
        "Scorecard_Overall__gc": account.get("health_score"),
    }


def _cta_record(item: dict) -> dict:
    """Project a `review_queue` fixture row into a Gainsight CTA record."""
    account = data_store.get("accounts", "account_id", item.get("account_id")) or {}
    type_map = {
        "guided_recovery_outreach": "Risk",
        "risk_intervention_brief": "Risk",
        "release_alert": "Opportunity",
        "feature_tip": "Lifecycle",
        "onboarding_nudge": "Lifecycle",
        "email": "Lifecycle",
    }
    return {
        "Gsid": _gsid("1S01", item["item_id"]),
        "ReferenceId__gc": item["item_id"],
        "Name": f"{item.get('message_type', 'Outreach')} — {account.get('account_name', item.get('account_id'))}",
        "CompanyId": _gsid("1P02", item.get("account_id", "")),
        "CompanyId__gr.Name": account.get("account_name"),
        "TypeId__gr.Name": type_map.get(item.get("message_type", ""), "Lifecycle"),
        "StatusId__gr.Name": {"pending": "New", "accepted": "Closed Success",
                               "edited": "In Progress", "discarded": "Closed Invalid"}.get(
            item.get("status", "pending"), "New"),
        "PriorityId__gr.Name": item.get("priority", "Medium"),
        "ReasonId__gr.Name": item.get("channel", ""),
        "Comments": item.get("draft_text", ""),
        "OwnerId__gr.Gsid": _gsid("1P01", item.get("csm_manager_id", "")),
        "DueDate": item.get("created_date"),
        "EntityType": "COMPANY",
        "IsClosed": item.get("status") in ("accepted", "edited", "discarded"),
    }


def _person_record(voc_or_contact: dict) -> dict:
    return {
        "Gsid": _gsid("1P03", voc_or_contact.get("user_id", voc_or_contact.get("voc_id", ""))),
        "Name": voc_or_contact.get("user_name", ""),
        "CompanyId__gr.Gsid": _gsid("1P02", voc_or_contact.get("account_id", "")),
    }


# ---------------------------------------------------------------------------
# where-clause evaluation (Gainsight conditions + expression)
# ---------------------------------------------------------------------------

def _match_condition(record: dict, cond: dict) -> bool:
    field = cond.get("name") or cond.get("fieldName")
    op = (cond.get("operator") or "EQ").upper()
    values = cond.get("value", []) or []
    cell = record.get(field)
    if op in ("IS_NULL",):
        return cell in (None, "")
    if op in ("IS_NOT_NULL",):
        return cell not in (None, "")
    if op == "EQ":
        return any(str(cell).lower() == str(v).lower() for v in values)
    if op == "NE":
        return all(str(cell).lower() != str(v).lower() for v in values)
    if op == "IN":
        return any(str(cell).lower() == str(v).lower() for v in values)
    if op == "CONTAINS":
        return any(str(v).lower() in str(cell or "").lower() for v in values)
    if op == "STARTS_WITH":
        return any(str(cell or "").lower().startswith(str(v).lower()) for v in values)
    if op == "BTW" and len(values) == 2:
        return str(values[0]) <= str(cell) <= str(values[1])
    if op in ("GT", "GTE", "LT", "LTE"):
        try:
            c = float(cell)
            v = float(values[0])
        except (TypeError, ValueError):
            return False
        return {"GT": c > v, "GTE": c >= v, "LT": c < v, "LTE": c <= v}[op]
    return False


def _evaluate_where(record: dict, where: dict | None) -> bool:
    if not where:
        return True
    conditions = where.get("conditions", [])
    if not conditions:
        return True
    results = {c.get("alias", chr(65 + i)): _match_condition(record, c) for i, c in enumerate(conditions)}
    expression = where.get("expression")
    if not expression:
        return all(results.values())
    expr = expression
    for alias, val in results.items():
        expr = re.sub(rf"\b{re.escape(alias)}\b", "True" if val else "False", expr)
    expr = expr.replace(" AND ", " and ").replace(" OR ", " or ").replace(" NOT ", " not ")
    try:
        return bool(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 - sanitised booleans only
    except Exception:
        return all(results.values())


def _project(record: dict, select: list[str] | None) -> dict:
    if not select:
        return record
    return {f: record.get(f) for f in select}
