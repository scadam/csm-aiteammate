"""
Simulated Snowflake capabilities (helpers).

* ``query_csm_database`` — natural-language query: translate to read-only SQL
  (managed-identity Azure OpenAI) and run it against the simulated Snowflake.
* ``get_schema`` — return the relational schema for the NL-to-SQL surface.
* ``write_outcome`` — the "Snowflake write" capability: record a CSM decision /
  outcome back for the learning loop (in-memory). Manager-scoped, so it acquires
  an On-Behalf-Of token first.

These are plain async helpers. The Copilot ``@define_tool`` wrappers (see
``src/tools/__init__.py``) and the MCP tools both call them, so there is a single
source of truth per capability.
"""

from __future__ import annotations

import json
import logging

from .. import data_store, identity, nl_to_sql, sql_engine
from ..observability import execute_tool_scope
from ..schema import get_schema_markdown

logger = logging.getLogger(__name__)

_MAX_ROWS_RENDERED = 50


def _render(result: dict) -> str:
    rows = result.get("rows", [])
    if not rows:
        return f"SQL: {result.get('sql')}\n\n(0 rows)"
    shown = rows[:_MAX_ROWS_RENDERED]
    body = json.dumps(shown, indent=2, default=str)
    suffix = "" if len(rows) <= _MAX_ROWS_RENDERED else f"\n… {len(rows) - _MAX_ROWS_RENDERED} more row(s)"
    return f"SQL: {result.get('sql')}\nrows: {result.get('row_count')}\n{body}{suffix}"


async def query_csm_database(question: str) -> str:
    """Answer a natural-language question by generating and running read-only SQL."""
    with execute_tool_scope("snowflake.query_csm_database", {"question": question}):
        sql = await nl_to_sql.nl_to_sql(question)
        try:
            result = sql_engine.execute_sql(sql)
        except Exception as first_error:
            logger.info("SQL failed, retrying: %s", first_error)
            try:
                sql = await nl_to_sql.retry_nl_to_sql(question, sql, str(first_error))
                result = sql_engine.execute_sql(sql)
            except Exception as second_error:
                return f"Could not answer that from the database. Error: {second_error}"
        rendered = _render(result)
        # Snowflake CSM data is real grounding — run it through Purview DSPM (DLP for
        # grounding) so the data access is governed and audited; tag Confidential.
        try:
            from .. import purview, sit
            manager = identity.resolve_manager() or {}
            await purview.tag_data(source="Snowflake (CSM_DB)",
                                   manager={"id": manager.get("manager_id"), "name": manager.get("display_name"),
                                            "entra_object_id": manager.get("entra_object_id"), "upn": manager.get("upn")},
                                   account_id="", summary=rendered, label=sit.LABEL_CONFIDENTIAL)
        except Exception:  # pragma: no cover - tagging must never break the query
            pass
        return rendered


async def get_schema() -> str:
    """Return the relational schema available to natural-language queries."""
    with execute_tool_scope("snowflake.get_schema", {}):
        return get_schema_markdown()


async def write_outcome(item_id: str, decision: str, final_text: str = "") -> str:
    """
    Record a CSM decision/outcome back for the learning loop (in-memory).

    ``decision`` is one of accept | edit | discard. Accepted/edited drafts are
    appended to the CSM voice archive (knowledge base 4), which "grows from
    accepted drafts". Manager-scoped: an OBO token is acquired first.
    """
    with execute_tool_scope("snowflake.write_outcome", {"item_id": item_id, "decision": decision}):
        decision_norm = decision.strip().lower()
        if decision_norm not in {"accept", "edit", "discard"}:
            return "decision must be one of: accept, edit, discard."

        item = data_store.get("review_queue", "item_id", item_id)
        if item is None:
            return f"Review item '{item_id}' not found."
        if not identity.manager_owns_account(item.get("account_id", "")):
            return "This review item belongs to a different manager; not permitted."

        # Acting on behalf of the manager — acquire an OBO token before writing.
        await identity.exchange_obo_token(["https://gainsight.example/.default"])

        status_map = {"accept": "accepted", "edit": "edited", "discard": "discarded"}
        data_store.update(
            "review_queue", "item_id", item_id,
            {"status": status_map[decision_norm], "draft_text": final_text or item.get("draft_text", "")},
        )

        if decision_norm in {"accept", "edit"} and (final_text or item.get("draft_text")):
            manager = identity.resolve_manager() or {}
            data_store.append(
                "csm_voice",
                {
                    "voice_id": f"CV-{item_id}",
                    "csm_manager_id": manager.get("manager_id", ""),
                    "csm_name": manager.get("display_name", ""),
                    "channel": item.get("channel", ""),
                    "message_type": item.get("message_type", ""),
                    "text": final_text or item.get("draft_text", ""),
                    "accepted_date": "2026-06-05",
                },
            )
        sql_engine.refresh()
        return f"Recorded decision '{decision_norm}' for {item_id} and updated the learning loop."
