"""Tests for the data repository layer."""

from src import data_store


def test_all_tables_load():
    for name in data_store.TABLES:
        rows = data_store.table(name)
        assert isinstance(rows, list)
        assert rows, f"fixture {name} is empty"


def test_find_case_insensitive():
    rows = data_store.find("accounts", sentiment="frustrated")
    assert any(r["account_id"] == "ACC-1001" for r in rows)


def test_get_by_id():
    account = data_store.get("accounts", "account_id", "ACC-1001")
    assert account is not None
    assert account["account_name"] == "Meridian Capital Partners"


def test_append_and_update_in_memory():
    data_store.append("review_queue", {"item_id": "RQ-TEST", "status": "pending", "account_id": "ACC-1001"})
    assert data_store.get("review_queue", "item_id", "RQ-TEST") is not None
    data_store.update("review_queue", "item_id", "RQ-TEST", {"status": "accepted"})
    assert data_store.get("review_queue", "item_id", "RQ-TEST")["status"] == "accepted"
