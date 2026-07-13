"""
Snowflake connection + read-only query execution.

Mirrors the ``lseg-snowflake`` reference (``server/db/snowflake_client.py``):
key-pair (RSA) auth preferred, password fallback, a cached connection, and a
structured result dict. Only read-only SELECT/WITH statements are permitted —
the same validation used by the SQLite simulation.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from .. import config

logger = logging.getLogger(__name__)

_connection = None  # type: ignore[var-annotated]

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|MERGE|EXEC|EXECUTE|GRANT|REVOKE|ATTACH|PUT|REMOVE|COPY)\b",
    re.IGNORECASE,
)


def _load_private_key_bytes() -> bytes | None:
    """Return the RSA private key as DER/PKCS8 bytes, or None if not configured."""
    pem: bytes | None = None
    if config.SNOWFLAKE_PRIVATE_KEY_PATH:
        with open(config.SNOWFLAKE_PRIVATE_KEY_PATH, "rb") as fh:
            pem = fh.read()
    elif config.SNOWFLAKE_PRIVATE_KEY and "PRIVATE KEY" in config.SNOWFLAKE_PRIVATE_KEY:
        pem = config.SNOWFLAKE_PRIVATE_KEY.encode("utf-8")
    if pem is None:
        return None

    from cryptography.hazmat.primitives import serialization

    passphrase = (
        config.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE.encode("utf-8")
        if config.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE
        else None
    )
    private_key = serialization.load_pem_private_key(pem, password=passphrase)
    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def build_connect_kwargs(role: str | None = None) -> dict[str, Any]:
    """Build snowflake.connector.connect kwargs from configuration."""
    kwargs: dict[str, Any] = {
        "account": config.SNOWFLAKE_ACCOUNT,
        "user": config.SNOWFLAKE_USER,
        "database": config.SNOWFLAKE_DATABASE,
        "schema": config.SNOWFLAKE_SCHEMA,
        "warehouse": config.SNOWFLAKE_WAREHOUSE,
        "role": role or config.SNOWFLAKE_ROLE,
    }
    private_key_bytes = _load_private_key_bytes()
    if private_key_bytes is not None:
        kwargs["private_key"] = private_key_bytes
    else:
        kwargs["password"] = config.SNOWFLAKE_PASSWORD
    return kwargs


def _get_connection():
    global _connection
    import snowflake.connector

    if _connection is None or _connection.is_closed():
        logger.info(
            "Connecting to Snowflake %s as role %s (%s.%s)",
            config.SNOWFLAKE_ACCOUNT,
            config.SNOWFLAKE_ROLE,
            config.SNOWFLAKE_DATABASE,
            config.SNOWFLAKE_SCHEMA,
        )
        _connection = snowflake.connector.connect(**build_connect_kwargs())
    return _connection


def execute_sql(sql: str) -> dict:
    """Execute a read-only SELECT/WITH statement against Snowflake."""
    cleaned = sql.strip().rstrip(";").strip()
    if not re.match(r"^(SELECT|WITH)\s", cleaned, re.IGNORECASE):
        raise ValueError("Only SELECT/WITH statements are allowed.")
    if _FORBIDDEN.search(cleaned):
        raise ValueError("Forbidden SQL keyword detected.")

    conn = _get_connection()
    cur = conn.cursor()
    try:
        t0 = time.monotonic()
        cur.execute(cleaned)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        result_rows = [dict(zip(columns, row)) for row in rows]
        return {
            "sql": cleaned,
            "columns": columns,
            "rows": result_rows,
            "row_count": len(result_rows),
            "query_id": getattr(cur, "sfqid", None),
            "execution_time_ms": elapsed_ms,
        }
    finally:
        cur.close()
