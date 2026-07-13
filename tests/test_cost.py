"""Tests for the token-based Azure OpenAI cost model."""

from src import cost


def test_estimate_tokens():
    assert cost.estimate_tokens("") == 0
    assert cost.estimate_tokens(None) == 0
    assert cost.estimate_tokens("a" * 40) == 10


def test_priced_model_token_cost():
    # gpt-4o is seeded from the Azure Retail Prices API ($2.50 in / $10 out per 1M).
    b = cost.cost_for_tokens("gpt-4o", input_tokens=1_000_000, output_tokens=1_000_000)
    assert b.priced is True
    assert b.input_price_per_m == 2.50
    assert b.output_price_per_m == 10.00
    assert b.cost_usd == 12.50  # 2.50 + 10.00
    assert b.total_tokens == 2_000_000


def test_priced_partial_tokens():
    b = cost.cost_for_tokens("gpt-4o", input_tokens=500_000, output_tokens=250_000)
    # 0.5*2.50 + 0.25*10.00 = 1.25 + 2.50 = 3.75
    assert round(b.cost_usd, 6) == 3.75


def test_deployment_name_prefix_resolves_price():
    # A dated/suffixed deployment name resolves to the base model price.
    assert cost.model_price("gpt-4o-2024-08-06") == {"input": 2.50, "output": 10.00}


def test_unpriced_model_reports_none_not_fabricated():
    # An unknown/unconfigured model must NOT invent a price.
    b = cost.cost_for_tokens("gpt-5.4-1", input_tokens=1000, output_tokens=500)
    assert b.priced is False
    assert b.cost_usd is None
    assert b.total_tokens == 1500
    assert b.as_dict()["costUsd"] is None


def test_env_override_sets_real_price(monkeypatch):
    import importlib

    monkeypatch.setenv("AOAI_MODEL_PRICES", '{"gpt-5.4":{"input":1.0,"output":4.0}}')
    importlib.reload(cost)
    try:
        b = cost.cost_for_tokens("gpt-5.4-1", input_tokens=1_000_000, output_tokens=1_000_000)
        assert b.priced is True
        assert b.cost_usd == 5.0  # 1.0 + 4.0
    finally:
        monkeypatch.delenv("AOAI_MODEL_PRICES", raising=False)
        importlib.reload(cost)
