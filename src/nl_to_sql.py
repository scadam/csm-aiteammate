"""
NL-to-SQL engine — translate natural-language questions into read-only SQL.

Mirrors the approach used in the ``lseg-snowflake`` repo
(``server/nl_to_sql.py``): a system prompt that embeds the schema context plus
rules and recipes, strict SELECT-only validation, and a retry path that feeds
execution errors back to the model. Two differences for this repository:

* SQL is generated with the **standard OpenAI client** (``OPENAI_API_KEY``),
  not Azure OpenAI managed identity, and **never** Snowflake Cortex.
* The target dialect is **SQLite** (the simulated Snowflake), so the generated
  SQL uses plain table names and SQLite-compatible functions.
"""

from __future__ import annotations

import asyncio
import logging
import re

from . import config
from . import openai_client
from .schema import get_schema_context

logger = logging.getLogger(__name__)


def _system_prompt() -> str:
    dialect = "Snowflake SQL" if config.USE_SNOWFLAKE else "SQLite"
    return f"""
You are an expert SQL generator for the Customer Success (CSM) database.

{get_schema_context()}

Return ONLY the SQL query for the user's question — no explanation, no markdown
fences, no comments. Generate exactly one read-only SELECT (or WITH ... SELECT)
statement that is valid {dialect}.
""".strip()

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|MERGE|EXEC|EXECUTE|GRANT|REVOKE|ATTACH|PRAGMA)\b",
    re.IGNORECASE,
)


def _strip_fences(text: str) -> str:
    text = re.sub(r"^```(?:sql)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _validate_sql(sql: str) -> None:
    """Raise ValueError unless ``sql`` is a single read-only SELECT/WITH statement."""
    cleaned = sql.strip().rstrip(";").strip()
    if not re.match(r"^(SELECT|WITH)\s", cleaned, re.IGNORECASE):
        raise ValueError(f"Only SELECT statements are allowed. Got: {cleaned[:80]}")
    if _FORBIDDEN.search(cleaned):
        raise ValueError(f"Forbidden SQL keyword detected in: {cleaned[:80]}")
    if ";" in cleaned:
        raise ValueError("Multiple statements are not allowed.")


def _complete(messages: list[dict]) -> str:
    response = openai_client.chat_completion(
        model=config.OPENAI_SQL_MODEL,
        messages=messages,
        temperature=0,
        max_tokens=800,
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("OpenAI returned an empty response.")
    return _strip_fences(content)


async def nl_to_sql(question: str) -> str:
    """Translate ``question`` into a validated, read-only SELECT statement."""
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": question},
    ]
    sql = await asyncio.to_thread(_complete, messages)
    _validate_sql(sql)
    logger.info("Generated SQL for %r: %s", question[:60], sql[:200])
    return sql


async def retry_nl_to_sql(question: str, failed_sql: str, error: str) -> str:
    """Re-generate SQL after an execution error, feeding the error back to the model."""
    retry_msg = (
        "The following SQL failed to execute.\n\n"
        f"Question: {question}\n\n"
        f"Failed SQL:\n{failed_sql}\n\n"
        f"Error: {error}\n\n"
        "Return ONLY a corrected, read-only SELECT query."
    )
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": retry_msg},
    ]
    sql = await asyncio.to_thread(_complete, messages)
    _validate_sql(sql)
    logger.info("Retry SQL for %r: %s", question[:60], sql[:200])
    return sql
