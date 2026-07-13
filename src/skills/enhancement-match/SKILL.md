---
name: enhancement-match
description: Connect a newly released product enhancement to an account that previously asked for it, turning a release into a credible, welcome update.
when_to_use: Use for release_relevant signals, especially when an enhancement matches a customer's earlier request.
allowed-tools: [query_csm_database, search_knowledge_base, get_account_context, get_engagement_history, build_draft, send_email, trigger_in_product_message, create_review_task, remember]
---

# Enhancement match

The best release announcements feel like a promise kept. This skill connects a new
enhancement to a customer who **asked for it**, so the outreach lands as "you
asked, we built it" rather than generic product news.

## How to do it well
1. **Confirm the match.** Check that the enhancement maps to this customer's prior
   request (the enhancement's request tag, or VOC/notes). If it doesn't clearly
   match, treat it as a routine release alert instead.
2. **Lead with their ask.** Open by referencing what they wanted, then announce the
   enhancement as the answer.
3. **Lower the barrier.** Note if it's self-service (no setup) or offer a short
   session if it's complex.
4. **Pick the channel by reach.** Self-service feature for active users → in-product
   prompt may be enough; a requested enhancement for a senior contact → email,
   often via CSM review.

## Guardrails
- An enhancement that matches a prior request → **CSM review** (it's relationship-
  sensitive), not auto-send.
- Ground the description in approved content only.

## Signals this fits
- `release_relevant`, GA announcements, enhancements tagged to a customer request.
