"""
Gainsight NXT REST client (router + handlers).

Separated from the projections/where-eval helpers in :mod:`src.gainsight.rest`
for readability. :class:`GainsightRestClient.request` is the single entry point
that behaves like the real Gainsight REST surface.
"""

from __future__ import annotations

import logging
import uuid

from .. import config, data_store, identity
from .rest import (
    GainsightApiError,
    _company_record,
    _cta_record,
    _envelope,
    _evaluate_where,
    _gsid,
    _person_record,
    _project,
)

logger = logging.getLogger(__name__)


def _visible_account_ids() -> set[str] | None:
    """Account ids the acting user may see, or ``None`` for unrestricted.

    This is the simulation of Gainsight's per-user RBAC / sharing: when a
    signed-in user context is present (the dashboards / bot act **on behalf of**
    a CSM), Company and Person reads are trimmed to that CSM's book of business —
    exactly what a user-delegated Gainsight token would enforce. With no user
    context (the data loader / admin path) visibility is unrestricted.
    """
    ctx = identity.current_context()
    if ctx is None:
        return None
    mid = ctx.manager_id
    manager = data_store.get("managers", "manager_id", mid)
    if not manager:
        return None  # unknown principal → don't over-restrict the simulation
    return {a["account_id"] for a in data_store.table("accounts") if a.get("csm_manager_id") == mid}


class GainsightRestClient:
    """In-process simulation of the Gainsight NXT REST API (CS + PX)."""

    def __init__(self, access_key: str | None = None, domain: str | None = None):
        # Mirrors real auth: an access key passed as the `accesskey` header. In
        # production this is a **user-delegated** token (per acting CSM); here it
        # carries the acting user so the simulation can enforce per-user RBAC.
        self.access_key = access_key or config.GAINSIGHT_ACCESS_KEY
        self.domain = domain or config.GAINSIGHT_DOMAIN

    # -- public entry point ------------------------------------------------

    def request(self, method: str, path: str, body: dict | None = None,
                params: dict | None = None) -> dict:
        """Route an HTTP-style request to the matching Gainsight handler."""
        method = method.upper()
        path = path.split("?")[0].rstrip("/") or "/"
        params = params or {}
        body = body or {}
        try:
            # Company API
            if method == "POST" and path == "/v1/data/objects/query/Company":
                return self._query_company(body)
            if method == "POST" and path == "/v1/data/objects/Company":
                return self._insert_company(body)
            if method == "PUT" and path == "/v1/data/objects/Company":
                return self._update_company(body, params)
            if method == "DELETE" and path.startswith("/v1/data/objects/Company/"):
                return self._delete_company(path.rsplit("/", 1)[-1])
            # Person API
            if method == "POST" and path == "/v1/data/objects/query/Person":
                return self._query_person(body)
            # Cockpit / CTA API
            if method == "POST" and path == "/v2/cockpit/cta":
                return self._create_cta(body)
            if method == "PUT" and path == "/v2/cockpit/cta":
                return self._update_cta(body)
            if method == "POST" and path == "/v2/cockpit/cta/list":
                return self._fetch_cta(body)
            if method == "GET" and path == "/v2/cockpit/admin/picklist/lite":
                return self._cta_config(params)
            # Timeline API
            if method == "POST" and path == "/v1/timeline/activities":
                return self._create_timeline_activity(body)
            if method == "POST" and path == "/v1/timeline/activities/query":
                return self._query_timeline(body)
            # Gainsight PX (Aptrinsic)
            if method == "GET" and path == "/v1/engagements":
                return self._px_list_engagements(params)
            if method == "POST" and path.startswith("/v1/engagements/") and path.endswith("/execute"):
                return self._px_execute_engagement(path.split("/")[3])
            if method == "GET" and path.startswith("/v1/accounts/") and path.endswith("/feature-activity"):
                return self._px_feature_activity(path.split("/")[3])
        except GainsightApiError as exc:
            return _envelope(None, result=False, error_code=exc.error_code, error_desc=exc.error_desc)

        return _envelope(None, result=False, error_code="GS_4040",
                         error_desc=f"No Gainsight endpoint for {method} {path}")

    # -- Company API -------------------------------------------------------

    def _query_company(self, body: dict) -> dict:
        select = body.get("select")
        if not select:
            raise GainsightApiError("GSOBJ_1001", "Please add fields in the select clause")
        where = body.get("where")
        limit = int(body.get("limit", 5000))
        offset = int(body.get("offset", 0))
        visible = _visible_account_ids()
        accounts = [a for a in data_store.table("accounts")
                    if visible is None or a.get("account_id") in visible]
        rows = [_company_record(a) for a in accounts]
        matched = [r for r in rows if _evaluate_where(r, where)]
        page = matched[offset: offset + limit]
        return _envelope([_project(r, select) for r in page])

    def _insert_company(self, body: dict) -> dict:
        records = body.get("records", [])
        out = []
        for rec in records:
            account_id = rec.get("AccountId__gc") or f"ACC-{uuid.uuid4().hex[:6]}"
            new = {
                "account_id": account_id,
                "account_name": rec.get("Name"),
                "industry": rec.get("Industry"),
                "arr_gbp": rec.get("Arr"),
                "onboarding_stage": rec.get("Stage"),
                "renewal_date": rec.get("RenewalDate"),
                "tier": rec.get("Tier__gc", "Growth"),
                "csm_manager_id": config.AGENT_MANAGER_USER_ID,
                "csm_name": rec.get("CSMFirstName", ""),
                "health_score": rec.get("Scorecard_Overall__gc", 70),
                "sentiment": "Neutral", "influence": "Medium", "strategic": "No",
                "products": [], "region": rec.get("Region__gc", ""),
            }
            data_store.append("accounts", new)
            out.append(_company_record(new))
        return _envelope({"records": out})

    def _update_company(self, body: dict, params: dict) -> dict:
        keys = (params.get("keys") or "Name").split(",")
        records = body.get("records", [])
        updated = []
        for rec in records:
            match = None
            for a in data_store.table("accounts"):
                comp = _company_record(a)
                if all(str(comp.get(k)) == str(rec.get(k)) for k in keys if k in rec):
                    match = a
                    break
            if match is None:
                continue
            field_map = {"Industry": "industry", "Arr": "arr_gbp", "Stage": "onboarding_stage",
                         "RenewalDate": "renewal_date", "Sentiment__gc": "sentiment",
                         "Scorecard_Overall__gc": "health_score"}
            for gs_field, local in field_map.items():
                if gs_field in rec:
                    match[local] = rec[gs_field]
            updated.append(_company_record(match))
        return _envelope({"count": len(updated), "errors": None, "records": updated})

    def _delete_company(self, gsid: str) -> dict:
        for a in data_store.table("accounts"):
            if _gsid("1P02", a["account_id"]) == gsid:
                return _envelope(f"Record with GSID: {gsid} successfully deleted.")
        raise GainsightApiError("GSOBJ_1004", "No data found for given criteria")

    # -- Person API --------------------------------------------------------

    def _query_person(self, body: dict) -> dict:
        select = body.get("select") or ["Name", "CompanyId__gr.Gsid"]
        where = body.get("where")
        visible = _visible_account_ids()
        people = []
        seen = set()
        for v in data_store.table("voc"):
            if visible is not None and v.get("account_id") and v.get("account_id") not in visible:
                continue
            key = v.get("user_id")
            if key and key not in seen:
                seen.add(key)
                people.append(_person_record(v))
        matched = [p for p in people if _evaluate_where(p, where)]
        return _envelope([_project(p, select) for p in matched])

    # -- Cockpit / CTA API -------------------------------------------------

    def _create_cta(self, body: dict) -> dict:
        requests = body.get("requests", [])
        success, failure = [], []
        for req in requests:
            record = req.get("record", {})
            ref = record.get("referenceId", str(uuid.uuid4()))
            account_id = record.get("AccountId__gc") or record.get("SFDCID")
            if not account_id and record.get("CompanyId"):
                for a in data_store.table("accounts"):
                    if _gsid("1P02", a["account_id"]) == record["CompanyId"]:
                        account_id = a["account_id"]
                        break
            if not account_id:
                failure.append({ref: "COCKPIT_9700: Missing mandatory fields for create - CompanyId"})
                continue
            item_id = f"RQ-{len(data_store.table('review_queue')) + 6001}"
            # The CTA is owned by the account's assigned CSM (not a global default),
            # so review items route to the correct manager's queue.
            acct = data_store.get("accounts", "account_id", account_id) or {}
            owner_mgr = acct.get("csm_manager_id") or config.AGENT_MANAGER_USER_ID
            data_store.append("review_queue", {
                "item_id": item_id,
                "account_id": account_id,
                "csm_manager_id": owner_mgr,
                "priority": record.get("priority", "Medium"),
                "status": "pending",
                "message_type": record.get("type", "Lifecycle"),
                "channel": record.get("reason", "csm_review"),
                "draft_text": record.get("Comments", ""),
                "created_date": record.get("DueDate", "2026-06-05"),
                "signal_id": record.get("SignalId__gc", ""),
            })
            success.append({ref: _gsid("1S01", item_id)})
        return _envelope({"success": success, "failure": failure})

    def _update_cta(self, body: dict) -> dict:
        requests = body.get("requests", [])
        success, failure = [], []
        status_map = {"Closed Success": "accepted", "In Progress": "edited",
                      "Closed Invalid": "discarded", "New": "pending"}
        for req in requests:
            record = req.get("record", {})
            ref = record.get("referenceId", str(uuid.uuid4()))
            gsid = record.get("Gsid")
            target = None
            for item in data_store.table("review_queue"):
                if _gsid("1S01", item["item_id"]) == gsid:
                    target = item
                    break
            if target is None:
                failure.append({ref: "COCKPIT_6007: Invalid GSID of CTA"})
                continue
            if "status" in record:
                target["status"] = status_map.get(record["status"], target.get("status"))
            if "Comments" in record:
                target["draft_text"] = record["Comments"]
            success.append({ref: gsid})
        return _envelope({"success": success, "failure": failure})

    def _fetch_cta(self, body: dict) -> dict:
        select = body.get("select")
        if not select:
            raise GainsightApiError("COCKPIT_5101", "Please add fields in the select clause")
        where = body.get("where")
        page_size = int(body.get("pageSize", 100))
        page_number = int(body.get("pageNumber", 1))
        rows = [_cta_record(i) for i in data_store.table("review_queue")]
        matched = [r for r in rows if _evaluate_where(r, where)]
        start = (page_number - 1) * page_size
        return _envelope(matched[start: start + page_size])

    def _cta_config(self, params: dict) -> dict:
        categories = (params.get("category") or "CTA_STATUS,CTA_PRIORITY,CTA_TYPE").split(",")
        catalog = {
            "CTA_STATUS": ["New", "In Progress", "Closed Success", "Closed Invalid"],
            "CTA_PRIORITY": ["High", "Medium", "Low"],
            "CTA_TYPE": ["Risk", "Opportunity", "Lifecycle"],
            "CTA_REASON": ["Usage Drop", "Onboarding", "Renewal Risk", "Adoption Gap"],
        }
        data = {}
        for cat in categories:
            cat = cat.strip()
            data[cat] = [
                {"name": name, "gsid": _gsid("1I00", f"{cat}:{name}"), "active": True,
                 "entityType": "GLOBAL", "picklistCategory": cat, "typeName": "ALL_CTA_TYPE"}
                for name in catalog.get(cat, [])
            ]
        return _envelope(data)

    # -- Timeline API ------------------------------------------------------

    def _create_timeline_activity(self, body: dict) -> dict:
        activity_id = _gsid("1T01", str(uuid.uuid4()))
        return _envelope({"id": activity_id, "type": body.get("activityType", "UPDATE"),
                          "subject": body.get("subject", ""), "createdDate": "2026-06-05"})

    def _query_timeline(self, body: dict) -> dict:
        account_id = (body.get("contextId") or body.get("companyId") or "")
        out = []
        for v in data_store.table("voc"):
            if account_id and v.get("account_id") != account_id:
                continue
            out.append({
                "id": _gsid("1T01", v["voc_id"]),
                "activityType": {"call_summary": "CALL", "health_note": "UPDATE",
                                 "survey": "EMAIL"}.get(v.get("source"), "UPDATE"),
                "subject": v.get("feature_requested", "Customer note"),
                "notes": v.get("text"),
                "activityDate": v.get("date"),
                "companyId": _gsid("1P02", v.get("account_id", "")),
                "sentiment": v.get("sentiment"),
            })
        return _envelope(out)

    # -- Gainsight PX (Aptrinsic) ------------------------------------------

    def _px_list_engagements(self, params: dict) -> dict:
        # PX returns a bare list under `engagements`.
        content = []
        for c in data_store.table("content_library"):
            content.append({
                "id": _gsid("PXEN", c["content_id"]),
                "name": c.get("title"),
                "state": "ACTIVE" if str(c.get("approved", "")).lower() == "yes" else "DRAFT",
                "type": "DIALOG" if c.get("message_type") == "onboarding_nudge" else "GUIDE",
                "contentId": c["content_id"],
            })
        return {"engagements": content, "totalElements": len(content)}

    def _px_execute_engagement(self, engagement_gsid: str) -> dict:
        return {"result": "QUEUED", "engagementId": engagement_gsid}

    def _px_feature_activity(self, account_gsid: str) -> dict:
        account_id = None
        for a in data_store.table("accounts"):
            if _gsid("1P02", a["account_id"]) == account_gsid:
                account_id = a["account_id"]
                break
        rows = [e for e in data_store.table("px_engagement")
                if not account_id or e.get("account_id") == account_id]
        return {"accountId": account_gsid, "featureActivity": rows}


_client: GainsightRestClient | None = None


def get_client() -> GainsightRestClient:
    """Return a cached Gainsight REST client."""
    global _client
    if _client is None:
        _client = GainsightRestClient()
    return _client
