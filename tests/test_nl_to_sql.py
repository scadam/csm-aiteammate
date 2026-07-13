"""Tests for the read-only SQL validator in the NL-to-SQL engine."""

import pytest

from src import nl_to_sql


def test_valid_select_passes():
    nl_to_sql._validate_sql("SELECT * FROM accounts")
    nl_to_sql._validate_sql("WITH x AS (SELECT 1) SELECT * FROM x")


@pytest.mark.parametrize(
    "bad",
    [
        "DELETE FROM accounts",
        "DROP TABLE accounts",
        "SELECT 1; DROP TABLE accounts",
        "UPDATE accounts SET tier='X'",
        "GRANT SELECT ON accounts TO role",
    ],
)
def test_invalid_sql_rejected(bad):
    with pytest.raises(ValueError):
        nl_to_sql._validate_sql(bad)


def test_strip_fences():
    assert nl_to_sql._strip_fences("```sql\nSELECT 1\n```") == "SELECT 1"
    assert nl_to_sql._strip_fences("SELECT 1") == "SELECT 1"
