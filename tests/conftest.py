"""Pytest configuration: force the SQLite simulation so tests need no Snowflake/Azure."""

import os

# Ensure the in-memory SQLite simulation is used (no external dependencies).
os.environ["SNOWFLAKE_ACCOUNT"] = ""
