"""
Knowledge-base search (helpers) over the four searchable stores:

1. Customer feedback / VOC          -> ``voc``
2. Approved content & playbooks      -> ``content_library``
3. PX engagement history             -> ``px_engagement``
4. CSM voice archive                 -> ``csm_voice``

Structured substring lookup over the JSON fixtures (no Snowflake Cortex / vector
search). For richer natural-language questions, callers can use
``snowflake.query_csm_database`` (OpenAI-generated read-only SQL).
"""

from __future__ import annotations

import json
import logging

from .. import data_store
from ..observability import execute_tool_scope

logger = logging.getLogger(__name__)

STORE_ALIASES = {
    "voc": "voc",
    "feedback": "voc",
    "content": "content_library",
    "content_library": "content_library",
    "playbooks": "content_library",
    "px": "px_engagement",
    "engagement": "px_engagement",
    "px_engagement": "px_engagement",
    "voice": "csm_voice",
    "csm_voice": "csm_voice",
}

# Text fields to match per store.
_TEXT_FIELDS = {
    "voc": ("text", "feature_requested", "sentiment", "source"),
    "content_library": ("title", "body", "feature", "message_type", "content_source"),
    "px_engagement": ("content_title", "action"),
    "csm_voice": ("text", "message_type", "channel"),
}


async def search_knowledge_base(
    store: str,
    query: str = "",
    account_id: str = "",
    user_id: str = "",
    limit: int = 10,
) -> str:
    """Search one of the four knowledge bases by substring + optional account/user filter."""
    with execute_tool_scope("knowledge_base.search", {"store": store, "query": query}):
        table_name = STORE_ALIASES.get(store.strip().lower())
        if table_name is None:
            return (
                "Unknown store. Use one of: voc, content_library, px_engagement, csm_voice."
            )

        rows = data_store.table(table_name)
        fields = _TEXT_FIELDS[table_name]
        q = query.strip().lower()
        matches: list[dict] = []
        for row in rows:
            if account_id and row.get("account_id") not in (account_id, None) and "account_id" in row:
                if row.get("account_id") != account_id:
                    continue
            if user_id and "user_id" in row and row.get("user_id") != user_id:
                continue
            if q:
                blob = " ".join(str(row.get(f, "")) for f in fields).lower()
                if q not in blob:
                    continue
            matches.append(row)

        matches = matches[: max(1, limit)]
        if not matches:
            return f"No matches in '{table_name}' for query '{query}'."
        return f"{len(matches)} match(es) in {table_name}:\n" + json.dumps(matches, indent=2, default=str)
