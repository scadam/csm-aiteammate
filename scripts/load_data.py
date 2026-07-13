"""
Create and populate the CSM Snowflake database from the JSON fixtures.

Mirrors the ``lseg-snowflake`` loader (``scripts/load_data.py``): connect with
key-pair auth, create the database/schema, and upload each table with
``write_pandas`` (auto-create + overwrite). Two roles are used:

* an **admin** role (``SNOWFLAKE_ADMIN_ROLE``, default ``SYSADMIN``) creates the
  database/schema, loads the tables, and grants access; and
* the **runtime** role (``SNOWFLAKE_ROLE``, default ``GIM_AGENT_ROLE``) is granted
  read-only access so the agent can query at runtime with least privilege.

JSON keys are upper-cased into Snowflake columns so unquoted SQL works, and
list/dict values are stored as JSON text (matched with ILIKE at query time).

Run:  python -m scripts.load_data
"""

from __future__ import annotations

import json
import logging

import pandas as pd
import snowflake.connector
from snowflake.connector.pandas_tools import write_pandas

from src import config, data_store
from src.db.snowflake_client import build_connect_kwargs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load_data")

# Fixture name -> Snowflake table name.
TABLES = {
    "accounts": "ACCOUNTS",
    "signals": "SIGNALS",
    "signal_action_map": "SIGNAL_ACTION_MAP",
    "routing_rules": "ROUTING_RULES",
    "enhancements": "ENHANCEMENTS",
    "content_library": "CONTENT_LIBRARY",
    "voc": "VOC",
    "csm_voice": "CSM_VOICE",
    "px_engagement": "PX_ENGAGEMENT",
    "review_queue": "REVIEW_QUEUE",
    "managers": "MANAGERS",
}


def _to_dataframe(rows: list[dict]) -> pd.DataFrame:
    """Build a DataFrame with UPPERCASE columns; list/dict cells become JSON text."""
    normalised: list[dict] = []
    for row in rows:
        out: dict = {}
        for key, value in row.items():
            out[key.upper()] = json.dumps(value) if isinstance(value, (list, dict)) else value
        normalised.append(out)
    df = pd.DataFrame(normalised)
    # Replace NaN with None so Snowflake stores NULLs.
    return df.where(pd.notna(df), None)


def main() -> None:
    if not config.USE_SNOWFLAKE:
        raise SystemExit("SNOWFLAKE_ACCOUNT is not configured; nothing to load.")

    database = config.SNOWFLAKE_DATABASE
    schema = config.SNOWFLAKE_SCHEMA
    admin_role = config.SNOWFLAKE_ADMIN_ROLE
    runtime_role = config.SNOWFLAKE_ROLE

    # Connect with the admin role for DDL + load + grants.
    conn = snowflake.connector.connect(**build_connect_kwargs(role=admin_role))
    cur = conn.cursor()
    try:
        logger.info("Creating database %s and schema %s.%s (role %s)", database, database, schema, admin_role)
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {database}")
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {database}.{schema}")
        cur.execute(f"USE SCHEMA {database}.{schema}")
    finally:
        cur.close()

    for fixture, table_name in TABLES.items():
        rows = data_store.table(fixture)
        df = _to_dataframe(rows)
        cur = conn.cursor()
        try:
            cur.execute(f"DROP TABLE IF EXISTS {database}.{schema}.{table_name}")
        finally:
            cur.close()
        success, num_chunks, num_rows, _ = write_pandas(
            conn, df, table_name, database=database, schema=schema,
            auto_create_table=True, overwrite=True,
        )
        logger.info("  %-18s -> %3d rows (success=%s)", table_name, num_rows, success)

    # Grant least-privilege read access to the runtime role.
    cur = conn.cursor()
    try:
        logger.info("Granting read access on %s.%s to role %s", database, schema, runtime_role)
        cur.execute(f"GRANT USAGE ON DATABASE {database} TO ROLE {runtime_role}")
        cur.execute(f"GRANT USAGE ON SCHEMA {database}.{schema} TO ROLE {runtime_role}")
        cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {database}.{schema} TO ROLE {runtime_role}")
        cur.execute(f"GRANT SELECT ON FUTURE TABLES IN SCHEMA {database}.{schema} TO ROLE {runtime_role}")
    finally:
        cur.close()

    conn.close()
    logger.info("Done. Loaded %d tables into %s.%s.", len(TABLES), database, schema)


if __name__ == "__main__":
    main()
