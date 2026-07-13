---
name: escalation-triage
description: Decide what needs the human CSM and how urgently, routing the right items to review and letting routine ones flow at scale.
when_to_use: Use when deciding whether an action should auto-send or go to CSM review, and at what priority.
allowed-tools: [get_account_context, search_microsoft_365, ask, decide_next_best_action, create_review_task, write_outcome, remember]
---

# Escalation triage

The agent's value is handling the routine at scale **and** knowing what to escalate
to the human. This skill encodes that judgment, deferring to the data-driven
routing rules and adding good defaults.

## Route to CSM review when
- The account is **high-influence**, **frustrated**, or **strategic**.
- The enhancement **matches a prior request** (relationship-sensitive).
- It's the **first outreach to a new senior contact**.
- The topic is **complex** (risk intervention, guided recovery).
- Anything touching a **strategic account**.

## Let it flow automatically when
- Routine onboarding nudges.
- Low-complexity feature tips for already-active users.
- Self-service product-release alerts.
- Long-tail accounts with no dedicated CSM.
- Message types with consistently high unedited-acceptance rates.

## Priority
- Severity score ≥ 4, or strategic account, or open escalation → **High**.
- Otherwise **Medium**; onboarding/long-tail nudges → **Low**.

## Principle
Keep the decision **auditable**: prefer the `signal_action_map` and `routing_rules`
tables, and record the rule ids that drove the decision. Never auto-send where the
rules require review.
