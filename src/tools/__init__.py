"""
Capability layer for the CSM AI Teammate.

Each capability (§11.7 of the instructions) is implemented once as an async
helper in a submodule, then exposed two ways from a single source of truth:

* as a GitHub Copilot ``Tool`` via :data:`COPILOT_TOOLS` (Pydantic-typed
  ``@define_tool``), used by the reasoning loop; and
* as a plain ``(name, description, coroutine, ParamModel)`` entry in
  :data:`TOOL_SPECS`, used by the MCP server (``src/mcp/server.py``).

The six skills: Snowflake query + schema + write, knowledge-base search,
Gainsight CS (account context, review tasks, email), Gainsight PX (in-product
messages, engagement history), AI draft generation (Content Build), plus the
Signal Detection / Next Best Action helpers and Work IQ grounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

# The GitHub Copilot SDK is optional here: it is required by the agent's
# reasoning loop (Windows) but NOT by the MCP server (which only needs the plain
# ``TOOL_SPECS`` registry and runs in a Linux container where the Copilot SDK
# wheel is unavailable). Import it defensively so ``TOOL_SPECS`` is always usable.
try:  # pragma: no cover - import availability differs by host
    from copilot import Tool, define_tool

    _HAS_COPILOT = True
except Exception:  # noqa: BLE001 - any import failure means copilot is unavailable
    Tool = object  # type: ignore[assignment,misc]
    define_tool = None  # type: ignore[assignment]
    _HAS_COPILOT = False

from . import (
    agents,
    content_build,
    gainsight,
    gainsight_cs,
    gainsight_px,
    knowledge_base,
    learning,
    notify,
    signals,
    snowflake,
    workiq,
)


# --------------------------------------------------------------------------
# Pydantic parameter models
# --------------------------------------------------------------------------

class QueryDatabaseParams(BaseModel):
    question: str = Field(description="A natural-language question about the CSM data.")


class EmptyParams(BaseModel):
    pass


class WriteOutcomeParams(BaseModel):
    item_id: str = Field(description="Review-queue item id, e.g. 'RQ-6001'.")
    decision: str = Field(description="One of: accept, edit, discard.")
    final_text: str = Field(default="", description="Final message text (for accept/edit).")


class SearchKnowledgeBaseParams(BaseModel):
    store: str = Field(description="One of: voc, content_library, px_engagement, csm_voice.")
    query: str = Field(default="", description="Substring to search for.")
    account_id: str = Field(default="", description="Optional account filter, e.g. 'ACC-1001'.")
    user_id: str = Field(default="", description="Optional user filter.")
    limit: int = Field(default=10, description="Maximum rows to return.")


class AccountContextParams(BaseModel):
    account_id: str = Field(description="Account id or name, e.g. 'ACC-1001' or 'Nordia Bank'.")


class CreateReviewTaskParams(BaseModel):
    account_id: str = Field(description="Account id or name the task is for.")
    message_type: str = Field(description="Message type, e.g. 'guided_recovery_outreach'.")
    channel: str = Field(description="Channel, e.g. 'csm_review' or 'csm_brief'.")
    priority: str = Field(default="Medium", description="High | Medium | Low.")
    draft_text: str = Field(default="", description="The drafted message or brief.")
    signal_id: str = Field(default="", description="Originating signal id, if any.")


class SendEmailParams(BaseModel):
    account_id: str = Field(description="Account id or name to email.")
    subject: str = Field(description="Email subject.")
    body: str = Field(description="Email body (from approved content).")


class TriggerInProductParams(BaseModel):
    user_id: str = Field(description="User to show the in-product message to.")
    content_id: str = Field(description="Approved content id, e.g. 'CNT-9006'.")


class EngagementHistoryParams(BaseModel):
    user_id: str = Field(default="", description="Optional user filter.")
    account_id: str = Field(default="", description="Optional account filter (id or name).")


class BuildDraftParams(BaseModel):
    content_source: str = Field(description="Approved content source key, e.g. 'content_release_alert'.")
    feature: str = Field(default="", description="Feature/topic the message is about.")
    recipient_first_name: str = Field(default="", description="Recipient first name.")
    extra_context: str = Field(default="", description="Any extra grounding context.")


class DetectSignalsParams(BaseModel):
    min_severity_score: int | None = Field(
        default=None, description="Override the severity threshold (1-5)."
    )


class NextBestActionParams(BaseModel):
    signal_id: str = Field(description="Signal id, e.g. 'SIG-5001'.")


class WorkIQSearchParams(BaseModel):
    query: str = Field(description="What to look for in Microsoft 365 work data.")
    account_id: str = Field(default="", description="Optional account filter.")


class AskParams(BaseModel):
    question: str = Field(description="A natural-language question for Microsoft 365 Copilot.")
    account_id: str = Field(default="", description="Optional account filter.")


class GainsightRestParams(BaseModel):
    method: str = Field(description="HTTP method: GET | POST | PUT | DELETE.")
    path: str = Field(description="Gainsight REST path, e.g. /v1/data/objects/query/Company or /v2/cockpit/cta.")
    body: str = Field(default="", description="JSON string request body (for POST/PUT).")
    query: str = Field(default="", description="Optional URL query string, e.g. 'category=CTA_STATUS&et=COMPANY'.")


class GetSkillParams(BaseModel):
    name: str = Field(description="Skill name to load, e.g. 'adoption-recovery' or 'renewal-risk-brief'.")


class RememberParams(BaseModel):
    note: str = Field(description="A concise, durable learning to store in working memory.")
    section: str = Field(default="Insights",
                         description="One of: Insights, What worked, What to avoid, Account notes.")


class NotifyManagerParams(BaseModel):
    message: str = Field(description="The message to send to the manager in a 1:1 Teams chat.")
    title: str = Field(default="", description="Optional bold title/headline for the message.")


# --------------------------------------------------------------------------
# Tool spec registry (single source of truth)
# --------------------------------------------------------------------------

@dataclass
class ToolSpec:
    name: str
    description: str
    func: Callable[..., Awaitable[str]]
    params_model: type[BaseModel]


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "query_csm_database",
        "Answer a natural-language question about CSM data (signals, accounts, rules, content) "
        "by generating and running a read-only SQL query.",
        snowflake.query_csm_database,
        QueryDatabaseParams,
    ),
    ToolSpec("get_schema", "Return the relational schema available to natural-language queries.", snowflake.get_schema, EmptyParams),
    ToolSpec(
        "write_outcome",
        "Record a CSM decision (accept/edit/discard) on a review item for the learning loop.",
        snowflake.write_outcome,
        WriteOutcomeParams,
    ),
    ToolSpec(
        "search_knowledge_base",
        "Search a knowledge base: voc, content_library, px_engagement, or csm_voice.",
        knowledge_base.search_knowledge_base,
        SearchKnowledgeBaseParams,
    ),
    ToolSpec("get_account_context", "Get account profile, recent feedback, and open review items.", gainsight_cs.get_account_context, AccountContextParams),
    ToolSpec("create_review_task", "Route a draft or brief to the CSM review queue.", gainsight_cs.create_review_task, CreateReviewTaskParams),
    ToolSpec("send_email", "Send an email outreach (auto-routes to review when the spec requires it).", gainsight_cs.send_email, SendEmailParams),
    ToolSpec("trigger_in_product_message", "Trigger an approved in-product prompt for a user.", gainsight_px.trigger_in_product_message, TriggerInProductParams),
    ToolSpec("get_engagement_history", "Read in-product engagement history (what a user has been shown).", gainsight_px.get_engagement_history, EngagementHistoryParams),
    ToolSpec("build_draft", "Generate a draft from approved content in the CSM's voice (Content Build).", content_build.build_draft, BuildDraftParams),
    ToolSpec("detect_signals", "Detect signals at/above the severity threshold for the manager's accounts.", signals.detect_signals, DetectSignalsParams),
    ToolSpec("decide_next_best_action", "Deterministically decide the next best action for a signal.", signals.decide_next_best_action, NextBestActionParams),
    ToolSpec("search_microsoft_365", "Search Microsoft 365 work data (Work IQ) for the manager.", workiq.search_microsoft_365, WorkIQSearchParams),
    ToolSpec("ask", "Ask Microsoft 365 Copilot a natural-language question, grounded in work data.", agents.ask, AskParams),
    ToolSpec("list_agents", "List available Microsoft 365 Copilot agents.", agents.list_agents, EmptyParams),
    ToolSpec(
        "gainsight_rest",
        "Call a Gainsight NXT REST endpoint directly (Company, Person, Timeline, Cockpit/CTA, PX) "
        "with a real request body; returns the real Gainsight response envelope.",
        gainsight.gainsight_rest,
        GainsightRestParams,
    ),
    ToolSpec(
        "get_skill",
        "Load the full instructions of a named skill (packaged CSM know-how) when a task matches it. "
        "Skills: adoption-recovery, renewal-risk-brief, voice-matched-outreach, enhancement-match, escalation-triage.",
        learning.get_skill,
        GetSkillParams,
    ),
    ToolSpec(
        "remember",
        "Store a concise, durable learning in your working memory so you improve next time.",
        learning.remember,
        RememberParams,
    ),
    ToolSpec(
        "recall",
        "Read back your working memory for the current manager (what you've learned so far).",
        learning.recall,
        EmptyParams,
    ),
    ToolSpec(
        "notify_manager",
        "Send a proactive 1:1 Teams message to your manager to escalate a decision that needs "
        "their judgment (human-in-the-loop) or to share a prepared brief.",
        notify.notify_manager,
        NotifyManagerParams,
    ),
]


def _make_copilot_tool(spec: ToolSpec) -> "Tool":
    """Wrap a ToolSpec as a Copilot Tool, validating params via its Pydantic model."""

    async def handler(params, _invocation):  # define_tool passes (params, invocation)
        data = params.model_dump() if isinstance(params, BaseModel) else dict(params or {})
        return await spec.func(**data)

    return define_tool(
        spec.name,
        description=spec.description,
        handler=handler,
        params_type=spec.params_model,
    )


# Built only when the Copilot SDK is available (agent host); empty otherwise
# (e.g. the MCP server container), where only ``TOOL_SPECS`` is consumed.
COPILOT_TOOLS: list = [_make_copilot_tool(spec) for spec in TOOL_SPECS] if _HAS_COPILOT else []

__all__ = ["TOOL_SPECS", "COPILOT_TOOLS", "ToolSpec"]
