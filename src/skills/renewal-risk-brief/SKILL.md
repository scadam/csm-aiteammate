---
name: renewal-risk-brief
description: Prepare a concise risk brief for an at-risk account approaching renewal, so the CSM can intervene with the full picture.
when_to_use: Use for risk signals, low health scores, or any account within ~90 days of its renewal date.
allowed-tools: [get_account_context, query_csm_database, search_knowledge_base, get_engagement_history, search_microsoft_365, ask, build_draft, create_review_task, remember]
---

# Renewal risk brief

When an account is at risk near renewal, the agent prepares a **brief for the
CSM** (not a customer message). The CSM needs the picture fast — under a minute to
read — so they can decide how to intervene.

## What a good brief contains
1. **The headline risk** in one line (what's wrong, how urgent, days to renewal).
2. **Evidence** — the signal, the metric vs. threshold, and any open support
   escalations or failed jobs.
3. **Context** — health score, sentiment, influence, ARR, recent VOC themes.
4. **Recommended play** — the single next best action and why, with the approved
   content source to use.
5. **What's already been tried** — prior outreach/engagement so the CSM doesn't
   repeat it.

## Guardrails
- This is always a **CSM-review** output (a brief/task), never an auto-send.
- Be factual and specific; cite the signal id and account id.
- Prioritise: severity score ≥ 4 or strategic account → High priority.

## Signals this fits
- `risk`, failed batch jobs, open escalations, sentiment = Frustrated near renewal.
