"""
Token-based cost model for the agentic loop.

The CSM Autopilot reasoning loop runs on **Azure OpenAI** (managed identity), so
cost is metered the way Azure OpenAI bills: **per token**, separately for input
(prompt) and output (completion) tokens, at a **per-model** rate. This replaces
the earlier GitHub Copilot *premium-request* model, which is GitHub's (now
legacy) request-based billing and does not reflect how this deployment is
charged.

Prices are **$ per 1,000,000 tokens** and are **never invented**:

* a small seed table holds only prices verified against the public **Azure Retail
  Prices API** (e.g. ``gpt-4o`` = $2.50 in / $10.00 out per 1M);
* operators supply exact contracted prices for their models via the
  ``AOAI_MODEL_PRICES`` environment variable (a JSON map), e.g.::

      AOAI_MODEL_PRICES={"gpt-5.4":{"input":<in>,"output":<out>}}

When a model has **no known price**, the cost is reported as ``None`` (unpriced)
and the UI shows token usage with a "price not configured" note — it never shows
a fabricated dollar figure. Token counts are always tracked.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

# Prices are $ per 1,000,000 tokens.
_PER_MILLION = 1_000_000

# Seed table: ONLY prices verified against the public Azure Retail Prices API
# (Global Standard, USD). Extend/override via AOAI_MODEL_PRICES. Do not add a
# model here unless its price is verified — unknown models stay unpriced.
_SEED_PRICES: dict[str, dict[str, float]] = {
    # gpt-4o-0806 Global Standard: input $2.50/1M (Azure Retail Prices API),
    # output $10.00/1M (published companion rate).
    "gpt-4o": {"input": 2.50, "output": 10.00},
}


def _load_prices() -> dict[str, dict[str, float]]:
    prices = {k: dict(v) for k, v in _SEED_PRICES.items()}
    override = os.getenv("AOAI_MODEL_PRICES", "")
    if override:
        try:
            data = json.loads(override)
            for model, p in data.items():
                key = str(model).strip().lower()
                entry = prices.setdefault(key, {})
                if "input" in p:
                    entry["input"] = float(p["input"])
                if "output" in p:
                    entry["output"] = float(p["output"])
        except (ValueError, TypeError):
            pass
    return prices


_PRICES = _load_prices()


def _normalise(model: str) -> str:
    """Map a deployment name to a price key (e.g. 'gpt-5.4-1' -> 'gpt-5.4')."""
    key = (model or "").strip().lower()
    if key in _PRICES:
        return key
    for name in sorted(_PRICES, key=len, reverse=True):
        if key.startswith(name):
            return name
    return key


def model_price(model: str) -> dict[str, float] | None:
    """Return ``{'input': $/1M, 'output': $/1M}`` for a model, or None if unpriced."""
    entry = _PRICES.get(_normalise(model))
    if entry and "input" in entry and "output" in entry:
        return dict(entry)
    return None


def estimate_tokens(text: str | None) -> int:
    """Rough token estimate (~4 characters per token) when exact counts are absent."""
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class CostBreakdown:
    """The token-priced cost of a job (or an aggregate of jobs)."""

    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_price_per_m: float | None
    output_price_per_m: float | None
    cost_usd: float | None     # None when the model is unpriced
    priced: bool

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.total_tokens,
            "inputPricePerM": self.input_price_per_m,
            "outputPricePerM": self.output_price_per_m,
            "costUsd": round(self.cost_usd, 6) if self.cost_usd is not None else None,
            "priced": self.priced,
            "basis": "Azure OpenAI token pricing ($/1M tokens)",
        }


def cost_for_tokens(model: str, input_tokens: int, output_tokens: int) -> CostBreakdown:
    """Token-based cost: input_tokens·price_in + output_tokens·price_out (per 1M)."""
    it = max(0, int(input_tokens))
    ot = max(0, int(output_tokens))
    price = model_price(model)
    if price is None:
        return CostBreakdown(model=model, input_tokens=it, output_tokens=ot,
                             total_tokens=it + ot, input_price_per_m=None,
                             output_price_per_m=None, cost_usd=None, priced=False)
    cost = (it / _PER_MILLION) * price["input"] + (ot / _PER_MILLION) * price["output"]
    return CostBreakdown(model=model, input_tokens=it, output_tokens=ot,
                         total_tokens=it + ot, input_price_per_m=price["input"],
                         output_price_per_m=price["output"], cost_usd=cost, priced=True)


def has_pricing(model: str) -> bool:
    return model_price(model) is not None
