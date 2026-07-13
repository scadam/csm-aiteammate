"""
Direct Gainsight NXT REST passthrough tool.

Exposes the **real Gainsight REST surface** as a single generic tool so the agent
can call any supported Gainsight endpoint (Company, Person, Timeline, Cockpit/CTA,
PX) with a real request body and receive the real
``{result, errorCode, data, ...}`` envelope. Backed by the in-process
simulation (:mod:`src.gainsight.client`); when ``GAINSIGHT__LIVE=true`` and a real
domain + access key are configured, this is the seam to call the live API.
"""

from __future__ import annotations

import json
import logging

from ..gainsight.client import get_client as gainsight
from ..observability import execute_tool_scope

logger = logging.getLogger(__name__)

_ALLOWED = {"GET", "POST", "PUT", "DELETE"}


async def gainsight_rest(method: str, path: str, body: str = "", query: str = "") -> str:
    """
    Call a Gainsight NXT REST endpoint.

    Args:
        method: GET | POST | PUT | DELETE.
        path:   e.g. /v1/data/objects/query/Company, /v2/cockpit/cta, /v1/engagements.
        body:   JSON string request body (for POST/PUT).
        query:  Optional URL query string, e.g. "category=CTA_STATUS&et=COMPANY".
    """
    with execute_tool_scope("gainsight.rest", {"method": method, "path": path}):
        method = method.strip().upper()
        if method not in _ALLOWED:
            return f"Unsupported method '{method}'. Use one of {sorted(_ALLOWED)}."
        parsed_body = {}
        if body:
            try:
                parsed_body = json.loads(body)
            except json.JSONDecodeError as exc:
                return f"Invalid JSON body: {exc}"
        params = {}
        for pair in query.split("&") if query else []:
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k] = v
        result = gainsight().request(method, path, parsed_body, params)
        return json.dumps(result, indent=2, default=str)
