"""
Signal Detection + Next Best Action (helpers).

* ``detect_signals`` — the Signal Detection Agent: pure detection logic (no AI).
  Returns signals at or above a severity threshold, scoped to the manager's
  accounts, with account context attached.
* ``decide_next_best_action`` — the Next Best Action Agent: a deterministic,
  fully-auditable lookup in the ``signal_action_map`` and ``routing_rules``
  tables (no AI generation). Returns the message type, channel, content source,
  and whether CSM review is required — with the rule ids that drove the decision.
"""

from __future__ import annotations

import json
import logging

from .. import config, data_store, identity
from ..observability import execute_tool_scope

logger = logging.getLogger(__name__)

_COMPLEX_MESSAGE_TYPES = {"risk_intervention_brief", "guided_recovery_outreach"}


def _find_enhancement(signal: dict) -> dict | None:
    feature = str(signal.get("feature", "")).lower()
    for enh in data_store.find("enhancements", product=signal.get("product", "")):
        haystack = f"{enh.get('title','')} {enh.get('feature_area','')}".lower()
        if feature and (feature in haystack or haystack in feature):
            return enh
    matches = data_store.find("enhancements", product=signal.get("product", ""))
    return matches[0] if matches else None


async def detect_signals(min_severity_score: int | None = None) -> str:
    """Return signals at/above the severity threshold for the manager's accounts."""
    with execute_tool_scope("signals.detect_signals", {"min_severity_score": min_severity_score}):
        threshold = config.SIGNAL_SEVERITY_THRESHOLD if min_severity_score is None else min_severity_score
        manager_id = identity.current_manager_id()
        owned = {
            a.get("account_id")
            for a in data_store.find("accounts", csm_manager_id=manager_id)
        }
        detected = []
        for sig in data_store.table("signals"):
            if int(sig.get("severity_score", 0)) < threshold:
                continue
            if owned and sig.get("account_id") not in owned:
                continue
            account = data_store.get("accounts", "account_id", sig.get("account_id")) or {}
            detected.append(
                {
                    "signal_id": sig.get("signal_id"),
                    "account_id": sig.get("account_id"),
                    "account_name": account.get("account_name"),
                    "signal_type": sig.get("signal_type"),
                    "product": sig.get("product"),
                    "feature": sig.get("feature"),
                    "severity": sig.get("severity"),
                    "severity_score": sig.get("severity_score"),
                    "description": sig.get("description"),
                }
            )
        detected.sort(key=lambda s: s.get("severity_score", 0), reverse=True)
        if not detected:
            return f"No signals at or above severity score {threshold} for your accounts."
        return f"{len(detected)} signal(s) >= severity {threshold}:\n" + json.dumps(detected, indent=2, default=str)


async def decide_next_best_action(signal_id: str) -> str:
    """Deterministically decide the next best action for a signal (auditable rules lookup)."""
    with execute_tool_scope("signals.decide_next_best_action", {"signal_id": signal_id}):
        signal = data_store.get("signals", "signal_id", signal_id)
        if signal is None:
            return f"Signal '{signal_id}' not found."
        account = data_store.get("accounts", "account_id", signal.get("account_id")) or {}
        enhancement = _find_enhancement(signal) or {}

        # 1) Base signal-to-action mapping (deterministic).
        mapping_rows = data_store.find(
            "signal_action_map",
            signal_type=signal.get("signal_type", ""),
            severity=signal.get("severity", ""),
        )
        if not mapping_rows:
            mapping_rows = data_store.find("signal_action_map", signal_type=signal.get("signal_type", ""))
        if not mapping_rows:
            return f"No signal-to-action mapping found for {signal.get('signal_type')}."
        mapping = mapping_rows[0]
        message_type = mapping.get("message_type", "")
        channel = mapping.get("channel", "")
        content_source = mapping.get("content_source", "")

        # 2) Apply routing rules to determine whether CSM review is required.
        review_reasons: list[str] = []
        if str(account.get("influence", "")).lower() == "high" or str(account.get("sentiment", "")).lower() == "frustrated":
            review_reasons.append("RR-01 high_influence_or_frustrated")
        if signal.get("signal_type") == "release_relevant" and str(enhancement.get("matches_request_tag", "")).lower() == "yes":
            review_reasons.append("RR-02 enhancement_matches_request")
        if str(account.get("strategic", "")).lower() == "yes":
            review_reasons.append("RR-03 strategic_account")
        if message_type in _COMPLEX_MESSAGE_TYPES:
            review_reasons.append("RR-05 complex_topic")

        base_review = str(mapping.get("review_required", "")).lower() == "yes"
        review_required = base_review or bool(review_reasons)

        auto_reasons: list[str] = []
        if not review_required:
            if str(account.get("tier", "")).lower() == "longtail" and account.get("csm_manager_id") == "unassigned":
                auto_reasons.append("RR-09 long_tail_no_csm")
            if message_type == "onboarding_nudge":
                auto_reasons.append("RR-06 onboarding_nudge")
            if message_type == "release_alert" and str(enhancement.get("self_service", "")).lower() == "yes":
                auto_reasons.append("RR-08 self_service_release")

        decision = {
            "signal_id": signal_id,
            "account_id": signal.get("account_id"),
            "message_type": message_type,
            "channel": "csm_review" if review_required and channel in ("email", "in_product") else channel,
            "content_source": content_source,
            "review_required": "Yes" if review_required else "No",
            "review_reasons": review_reasons or (["base mapping"] if base_review else []),
            "auto_send_reasons": auto_reasons,
            "mapping_id": mapping.get("map_id"),
            "enhancement_id": enhancement.get("enhancement_id"),
        }
        return json.dumps(decision, indent=2, default=str)
