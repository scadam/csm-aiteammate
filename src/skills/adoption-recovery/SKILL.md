---
name: adoption-recovery
description: Re-engage an account whose product usage has dropped or stalled, recovering adoption before it becomes churn risk.
when_to_use: Use for adoption_gap signals, declining logins, or a licensed feature with low seat usage.
allowed-tools: [detect_signals, decide_next_best_action, get_account_context, search_knowledge_base, get_engagement_history, query_csm_database, build_draft, trigger_in_product_message, create_review_task, search_microsoft_365, ask, remember]
---

# Adoption recovery

A guided-recovery play for accounts where usage has dipped or a feature was never
adopted. The goal is to restore momentum with a low-friction, value-first nudge —
not to sell.

## How to think about it
1. **Anchor on the value, not the feature.** Lead with the outcome the customer
   cares about (time saved, risk reduced), then name the feature as the means.
2. **Make the next step tiny.** Offer a 20-minute walkthrough or a single concrete
   tip the user can try today. Never a multi-step ask.
3. **Use what they've already told you.** If VOC or prior notes mention a goal or a
   frustration, reference it so the outreach feels personal, not templated.
4. **Respect prior contact.** Check engagement history; do not repeat content the
   user has already been shown.

## Guardrails
- High-influence, frustrated, or strategic accounts → route to CSM review; do not
  auto-send.
- Ground every product claim in approved content only.
- Keep it to a few sentences, in the CSM's voice.

## Signals this fits
- `adoption_gap`, declining logins, licensed-but-unused features, onboarding stalls.
