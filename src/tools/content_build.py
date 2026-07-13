"""
Constrained draft generation (the Content Build Agent).

Generates a personalised draft **only** from approved content retrieved from the
content library and anchored to the assigned CSM's voice. Never invents product
or enhancement claims. Uses the shared Azure OpenAI managed-identity client.
"""

from __future__ import annotations

import asyncio
import logging

from .. import config, data_store, identity
from ..observability import execute_tool_scope
from .. import openai_client

logger = logging.getLogger(__name__)

DRAFT_SYSTEM_PROMPT = """
You are a Customer Success Manager (CSM) writing a short, warm, genuinely personal
outreach email to a customer contact. Write in the CSM's OWN voice, closely matching the tone,
warmth and sign-off of the provided style examples — it must read as if the CSM wrote it, not a
template.

Make it personal using the "Recent interactions" context (the CSM's real Microsoft 365 history
with this person — emails, meetings and Teams messages). Open by naturally referencing the most
relevant recent touchpoint ("Following up on our call last week…", "You mentioned in Teams that…",
"Good to see you at Tuesday's review…") so the reader immediately feels this is a 1:1 note from
someone who knows them. Only reference interactions that actually appear in that context — if
there are none, simply write a warm, relevant note without inventing any history.

Base every product/value claim ONLY on the approved talking points provided. Never invent product
capabilities, dates, commitments or quotes. Keep it concise (roughly 90–150 words), specific and
helpful, with no marketing fluff and no subject line. Address the recipient by first name and end
with a short, natural sign-off as the CSM. Reply with ONLY the message body text.
""".strip()


def _compose_prompt(
    approved_content: list[dict],
    voice_examples: list[dict],
    recipient_first_name: str,
    csm_name: str,
    feature: str,
    extra_context: str,
    work_iq_context: str = "",
) -> str:
    content_block = "\n\n".join(
        f"- {c.get('title')}: {c.get('body')}" for c in approved_content
    ) or "(none)"
    voice_block = "\n\n".join(v.get("text", "") for v in voice_examples) or "(none)"
    wiq_block = (work_iq_context or "").strip() or "(no recent interactions on record)"
    return (
        f"Approved talking points to base the message on:\n{content_block}\n\n"
        f"Examples of {csm_name or 'the CSM'}'s writing style to match:\n{voice_block}\n\n"
        f"Recent interactions between {csm_name or 'the CSM'} and this contact "
        f"(from Microsoft 365 via Work IQ — emails, meetings, Teams):\n{wiq_block}\n\n"
        f"Recipient first name: {recipient_first_name or '(unknown)'}\n"
        f"Sign off as: {csm_name}\n"
        f"Topic: {feature or '(general check-in)'}\n"
        f"Helpful context: {extra_context or '(none)'}\n\n"
        f"Please write the personalised outreach message."
    )


def _generate(prompt: str) -> str:
    response = openai_client.chat_completion(
        model=config.OPENAI_DRAFT_MODEL,
        messages=[
            {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=500,
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("Azure OpenAI returned an empty draft.")
    return content.strip()


async def build_draft(
    content_source: str,
    feature: str = "",
    recipient_first_name: str = "",
    extra_context: str = "",
    work_iq_context: str = "",
) -> str:
    """
    Build a draft from approved content in the assigned CSM's voice, personalised
    with the manager's recent Microsoft 365 activity with the contact (Work IQ).

    Returns the draft text, or a clear message if no approved content matches.
    """
    with execute_tool_scope("content_build.build_draft", {"content_source": content_source}):
        approved = [
            c
            for c in data_store.find("content_library", content_source=content_source)
            if str(c.get("approved", "")).lower() == "yes"
        ]
        if not approved:
            return (
                f"No approved content found for source '{content_source}'. "
                "A draft cannot be generated without approved content."
            )

        manager = identity.resolve_manager() or {}
        csm_name = manager.get("display_name", "Your CSM")
        voice = data_store.find(
            "csm_voice", csm_manager_id=manager.get("manager_id", "")
        )

        prompt = _compose_prompt(
            approved_content=approved,
            voice_examples=voice,
            recipient_first_name=recipient_first_name,
            csm_name=csm_name,
            feature=feature,
            extra_context=extra_context,
            work_iq_context=work_iq_context,
        )
        try:
            draft = await asyncio.to_thread(_generate, prompt)
        except Exception as exc:  # pragma: no cover - depends on live Azure OpenAI
            logger.warning("Draft generation failed: %s", exc)
            return (
                "Draft generation is unavailable right now. Approved content for "
                f"'{content_source}': " + approved[0].get("body", "")
            )
        return draft
