"""Tests for the SQLite simulation back end and read-only guard."""

import pytest

from src import data_store, sql_engine


def test_select_accounts():
    result = sql_engine.execute_sql("SELECT account_id, account_name FROM accounts")
    # Row count tracks the (scaled) accounts fixture rather than a fixed number.
    assert result["row_count"] == len(data_store.table("accounts"))
    assert result["row_count"] >= 9
    assert {"account_id", "account_name"} <= set(result["columns"])


def test_json_array_like_match():
    result = sql_engine.execute_sql(
        "SELECT account_name FROM accounts WHERE products LIKE '%CheckMate%'"
    )
    names = {r["account_name"] for r in result["rows"]}
    assert "Meridian Capital Partners" in names
    assert "Nordia Bank" in names


def test_join_signals_accounts():
    result = sql_engine.execute_sql(
        "SELECT s.signal_id, a.account_name FROM signals s "
        "JOIN accounts a ON s.account_id = a.account_id WHERE s.severity_score >= 4"
    )
    assert result["row_count"] >= 1


@pytest.mark.parametrize(
    "bad_sql",
    [
        "DROP TABLE accounts",
        "UPDATE accounts SET tier='X'",
        "DELETE FROM signals",
        "INSERT INTO accounts (account_id) VALUES ('x')",
    ],
)
def test_write_statements_blocked(bad_sql):
    with pytest.raises(ValueError):
        sql_engine.execute_sql(bad_sql)
