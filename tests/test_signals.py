"""Tests for deterministic Signal Detection and Next Best Action."""

import json

import pytest

from src.tools import signals


@pytest.mark.asyncio
async def test_detect_signals_respects_threshold():
    out = await signals.detect_signals(min_severity_score=5)
    # Only the critical signal (severity_score 5) should qualify.
    assert "SIG-5001" in out
    assert "SIG-5005" not in out  # severity_score 2


@pytest.mark.asyncio
async def test_next_best_action_strategic_requires_review():
    # SIG-5001 is on a Strategic + Frustrated account -> must require review.
    out = await signals.decide_next_best_action("SIG-5001")
    decision = json.loads(out)
    assert decision["review_required"] == "Yes"
    assert any("RR-0" in reason for reason in decision["review_reasons"])


@pytest.mark.asyncio
async def test_next_best_action_unknown_signal():
    out = await signals.decide_next_best_action("SIG-DOES-NOT-EXIST")
    assert "not found" in out.lower()
