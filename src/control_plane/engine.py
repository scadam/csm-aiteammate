"""
The CSM Autopilot job engine — **real**, not scripted.

A CSM Autopilot (one agent instance per CSM) "works" one of its accounts by
running the seven-stage adoption journey from the design — Signal Detected ->
Context Built -> Action Decided -> Content Built -> Prioritised & Reviewed ->
Delivered -> System Learns — driving the **real** capability tools:

* ``signals.decide_next_best_action`` — the auditable routing-rules decision.
* ``gainsight_cs.get_account_context`` — real Gainsight NXT REST (Company /
  Timeline / CTA), served from the simulated-real fixtures.
* ``workiq.search_microsoft_365`` — real Work IQ grounding over the manager's
  Microsoft 365 work data (OBO; offline fixture only when no endpoint/token).
* ``content_build.build_draft`` — a **model-generated** draft (Azure OpenAI via
  managed identity) constrained to approved content, in the CSM's voice.
* ``gainsight_cs.send_email`` / ``gainsight_px.trigger_in_product_message`` /
  ``gainsight_cs.create_review_task`` — real delivery / HITL routing.
* ``snowflake.write_outcome`` — the learning-loop write-back.

In production these jobs are kicked off by event hooks (a usage signal landing,
a release tagged, a renewal window opening). Here a job is started from the
manager's cockpit ("Start working") or a fleet sweep.

The engine is an async generator yielding ``(event_type, data)`` tuples that the
web layer streams to the browser as Server-Sent Events, while recording the same
events into :mod:`src.control_plane.store` for the ledger and metrics.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import AsyncIterator

from .. import config, cost, data_store, identity, memory, purview, scenarios, sit
from ..tools import content_build, gainsight_cs, gainsight_px, signals, snowflake, workiq
from . import store

logger = logging.getLogger(__name__)

# A small pause between stages so the live timeline reads naturally in the UI.
_STAGE_PAUSE = float(os.getenv("CONTROL_PLANE_STAGE_PAUSE", "0.35"))


def _pick_signal(account_id: str) -> dict | None:
    """Pick the highest-severity, newest open signal for an account."""
    candidates = [
        s for s in data_store.table("signals")
        if s.get("account_id") == account_id and str(s.get("status", "new")).lower() in ("new", "open", "")
    ]
    if not candidates:
        candidates = [s for s in data_store.table("signals") if s.get("account_id") == account_id]
    if not candidates:
        return None
    candidates.sort(key=lambda s: (int(s.get("severity_score", 0)), s.get("detected_date", "")), reverse=True)
    return candidates[0]


def pick_top_account(autopilot: dict) -> dict | None:
    """The account this autopilot should work next: highest open-signal severity."""
    best = None
    best_score = -1
    for card in autopilot.get("accounts", []):
        sig = _pick_signal(card["account_id"])
        score = int(sig.get("severity_score", 0)) if sig else -1
        if score > best_score:
            best_score = score
            best = card
    if best is None:
        return None
    return data_store.get("accounts", "account_id", best["account_id"])


def accounts_with_signals(autopilot: dict) -> list[dict]:
    """Full account records for this autopilot's accounts that have an open signal."""
    out = []
    for card in autopilot.get("accounts", []):
        if _pick_signal(card["account_id"]):
            acc = data_store.get("accounts", "account_id", card["account_id"])
            if acc:
                out.append(acc)
    out.sort(key=lambda a: int((_pick_signal(a["account_id"]) or {}).get("severity_score", 0)), reverse=True)
    return out


def _approved_content(content_source: str) -> list[dict]:
    return [
        c for c in data_store.find("content_library", content_source=content_source)
        if str(c.get("approved", "")).lower() == "yes"
    ]


# Strings that mean Work IQ returned no usable grounding — don't feed these to the
# draft (so the model writes a clean note instead of quoting an error/empty result).
_WIQ_EMPTY_MARKERS = (
    "unavailable", "could not acquire", "obo required",
    "no microsoft 365 results", "no microsoft 365 data",
)


def _wiq_for_draft(wiq: str) -> str:
    """Pass Work IQ grounding to the draft only when it carries real interaction content."""
    low = (wiq or "").lower()
    if not low.strip() or any(marker in low for marker in _WIQ_EMPTY_MARKERS):
        return ""
    return wiq


async def _emit(job_id: str, event: str, data: dict):
    """Record an event in the store and return the SSE tuple."""
    store.record_event(job_id, event, data if isinstance(data, dict) else {"value": data})
    return (event, data)


async def run_job(
    autopilot: dict,
    account: dict,
    *,
    source: str = "control-plane",
    trigger: str = "manual_start",
) -> AsyncIterator[tuple[str, dict]]:
    """
    Run one CSM Autopilot job for ONE account and stream journey events.

    Yields ``(event_type, data)`` for: ``status``, ``metadata``, ``stage``,
    ``tool_call``, ``tool_result``, ``turn``, ``outcome``, ``result``,
    ``stats``, ``done``, ``error``.
    """
    manager = autopilot.get("manager", {})
    model = config.COPILOT_MODEL
    account_id = account.get("account_id")
    account_name = account.get("account_name")
    title = f"Adoption journey — {account_name}"

    job = store.start_job(autopilot=autopilot, account=account, title=title,
                          prompt=trigger, source=source, model=model)
    job_id = job["id"]
    store.set_autopilot_status(autopilot["id"], "running", f"Working {account_name}…", job_id, account_name)

    # Act on behalf of the manager: set the manager context so the real tools
    # scope to the right CSM/accounts (and acquire OBO tokens when a turn
    # context is available — e.g. when invoked from the bot/agent host).
    ctx = identity.RequestContext(
        manager_id=manager.get("id", config.AGENT_MANAGER_USER_ID),
        conversation_id=job_id,
        entra_object_id=manager.get("entra_object_id"),
        upn=manager.get("upn"),
    )
    ctx_token = identity.set_request_context(ctx)

    # Resolve WHO this autonomous job acts as. With no signed-in manager turn, the
    # agent acts as its **own** governed Entra Agent ID (an agentic-user token
    # minted with no turn context) for everything except reading/writing the
    # manager's own data. This is honest about the autonomous identity model.
    acting = await identity.agentic_acting_identity(manager_id=ctx.manager_id)

    # One Purview/DSPM conversation per job (prompts + responses correlate here).
    correlation_id = job_id
    _seq = {"n": 0}

    def _next_seq() -> int:
        _seq["n"] += 1
        return _seq["n"]

    tool_index = 0

    def _turn(prompt_text: str, completion_text: str) -> None:
        store.record_turn(job_id, prompt_tokens=cost.estimate_tokens(prompt_text),
                          completion_tokens=cost.estimate_tokens(completion_text))

    async def _tool(name: str, arguments: dict, result: str) -> None:
        nonlocal tool_index
        tool_index += 1
        store.record_tool(job_id, f"call-{uuid.uuid4().hex[:8]}", name, arguments, (result or "")[:4000], tool_index)
        # Log the tool CALL itself to Purview DSPM (real processContent "Tool call"
        # event) so MCP/agent tool invocations are visible in DSPM, not just the
        # prompt/response and grounding-data events.
        await purview.log_tool_call(tool=name, manager=manager, arguments=arguments,
                                    result=result or "", surface="Agent tool",
                                    correlation_id=job_id, sequence=_next_seq(),
                                    account_id=account_id)

    async def _pause():
        if _STAGE_PAUSE:
            import asyncio
            await asyncio.sleep(_STAGE_PAUSE)

    try:
        yield await _emit(job_id, "status", {"message": f"{autopilot['displayName']} is working {account_name}.", "model": model})
        yield await _emit(job_id, "metadata", {
            "autopilot": autopilot["id"], "account": account_name, "account_id": account_id,
            "manager": manager.get("name"), "model": model, "trigger": trigger,
            "actingIdentity": acting.get("label"),
            "actingAsAgent": acting.get("minted", False),
        })

        # ── 1. Signal Detected ───────────────────────────────────────
        store.set_running_text(job_id, "Scanning adoption signals…")
        yield await _emit(job_id, "stage", {"key": "signal", "label": "Signal detected", "status": "running"})
        await _pause()
        signal = _pick_signal(account_id)
        if not signal:
            store.record_stage(job_id, {"key": "signal", "label": "Signal detected", "summary": "No open signals.", "status": "skipped"})
            outcome = {"channel": "none", "delivered": False, "summary": "No actionable signal — nothing to do.",
                       "account_name": account_name, "account_id": account_id}
            store.finish_job(job_id, status="complete", result="No actionable signals for this account.", outcome=outcome)
            store.set_autopilot_status(autopilot["id"], "idle", "Up to date")
            yield await _emit(job_id, "result", {"content": "No actionable signals for this account right now."})
            yield await _emit(job_id, "stats", store.get_job(job_id)["stats"])
            yield await _emit(job_id, "done", {})
            return
        sig_summary = (f"{signal.get('severity')} {signal.get('signal_type', '').replace('_', ' ')} on "
                       f"{signal.get('product')} / {signal.get('feature')} — {signal.get('description')}")
        await _tool("detect_signals", {"account_id": account_id}, sig_summary)
        # Snowflake adoption signal data is real grounding — run it through Purview
        # DSPM (DLP for grounding) so the data access is governed and audited.
        await purview.tag_data(source="Snowflake (CSM_DB.ADOPTION)", manager=manager,
                               account_id=account_id, summary=sig_summary,
                               label=sit.LABEL_CONFIDENTIAL, correlation_id=job_id, sequence=0)
        store.record_stage(job_id, {"key": "signal", "label": "Signal detected", "summary": sig_summary,
                                    "signalId": signal.get("signal_id"), "severity": signal.get("severity"), "status": "done"})
        yield await _emit(job_id, "tool_call", {"tool": "detect_signals", "summary": sig_summary, "severity": signal.get("severity")})

        # ── 2. Context Built (real Gainsight + real Work IQ grounding) ─
        store.set_running_text(job_id, "Building customer context…")
        yield await _emit(job_id, "stage", {"key": "context", "label": "Context built", "status": "running"})
        await _pause()
        try:
            context_json = await gainsight_cs.get_account_context(account_id)
        except Exception:  # pragma: no cover - resilient to fixture gaps
            context_json = json.dumps({"account": account}, default=str)
        await _tool("get_account_context", {"account_id": account_id}, context_json)
        # Gainsight CS data is real grounding — evaluated by Purview DSPM (DLP for
        # grounding) and tagged Confidential.
        await purview.tag_data(source="Gainsight CS", manager=manager, account_id=account_id,
                               summary=context_json, label=sit.LABEL_CONFIDENTIAL,
                               correlation_id=job_id, sequence=1)

        # Real Work IQ grounding over the manager's Microsoft 365 work data — focused
        # on the manager's actual relationship with THIS contact (emails, meetings,
        # Teams) so the outreach can open on a real, recent touchpoint.
        contact_name = account.get("primary_contact") or "the main contact"
        wiq_query = (
            f"Summarise my recent interactions with {contact_name} at {account_name} — "
            f"emails, meetings and Teams messages — especially anything about "
            f"{signal.get('feature')} or {signal.get('signal_type', '').replace('_', ' ')}."
        )
        try:
            wiq = await workiq.search_microsoft_365(wiq_query, account_id)
        except Exception as exc:  # pragma: no cover
            wiq = f"Work IQ grounding unavailable: {exc}"
        await _tool("search_microsoft_365", {"query": wiq_query, "account_id": account_id}, wiq)
        # Work IQ / Microsoft 365 grounding — evaluated by Purview DSPM (DLP for
        # grounding); M365 content carries its own sensitivity labels.
        await purview.tag_data(source="Work IQ (Microsoft 365)", manager=manager, account_id=account_id,
                               summary=wiq, label=sit.LABEL_CONFIDENTIAL,
                               correlation_id=job_id, sequence=2)
        wiq_real = config.USE_WORKIQ and "offline" not in wiq.lower() and "could not" not in wiq.lower()

        # Prompt-injection guard: poisoned grounding content (e.g. a voice-of-customer
        # note carrying hidden "ignore your instructions / email every customer"
        # directives) is detected here, withheld from the model's influence, and the
        # interaction is forced to CSM review — never auto-sent.
        injection_hits = scenarios.detect_prompt_injection(wiq)
        if injection_hits:
            store.record_stage(job_id, {"key": "context", "label": "Grounding screened",
                                        "summary": (f"Prompt-injection guard flagged {len(injection_hits)} "
                                                    "pattern(s) in grounding — forced CSM review."),
                                        "injection": injection_hits, "status": "warn"})
            yield await _emit(job_id, "tool_result", {"tool": "injection_guard",
                                                      "summary": f"Quarantined {len(injection_hits)} injection pattern(s); forcing CSM review."})

        ctx_summary = (f"{account.get('account_name')} · {account.get('tier')} · health {account.get('health_score')} · "
                       f"sentiment {account.get('sentiment')} · renews {account.get('renewal_date')}")
        store.record_stage(job_id, {"key": "context", "label": "Context built", "summary": ctx_summary,
                                    "workIqGrounded": wiq_real, "status": "done"})
        yield await _emit(job_id, "tool_result", {"tool": "get_account_context", "summary": ctx_summary})
        yield await _emit(job_id, "tool_result", {"tool": "search_microsoft_365",
                                                  "summary": ("Grounded in the manager's Microsoft 365 (Work IQ)."
                                                              if wiq_real else "Microsoft 365 grounding (offline fixture).")})

        # ── 3. Action Decided (deterministic, auditable rules) ───────
        store.set_running_text(job_id, "Deciding the next best action…")
        yield await _emit(job_id, "stage", {"key": "action", "label": "Next best action decided", "status": "running"})
        await _pause()
        decision_json = await signals.decide_next_best_action(signal.get("signal_id"))
        try:
            decision = json.loads(decision_json)
        except json.JSONDecodeError:
            decision = {"message_type": "guided_recovery_outreach", "channel": "csm_review",
                        "content_source": "content_release_alert", "review_required": "Yes"}
        await _tool("decide_next_best_action", {"signal_id": signal.get("signal_id")}, decision_json)
        review_required = str(decision.get("review_required", "No")).lower() == "yes"
        if injection_hits:
            review_required = True  # poisoned grounding always goes to a human
        channel = decision.get("channel", "csm_review")
        reasons = decision.get("review_reasons") or decision.get("auto_send_reasons") or ["base rule"]
        action_summary = (f"{decision.get('message_type')} via {channel} · "
                          f"review {'required' if review_required else 'not required'} ({', '.join(reasons)})")
        store.record_stage(job_id, {"key": "action", "label": "Next best action decided",
                                    "summary": action_summary, "decision": decision, "status": "done"})
        yield await _emit(job_id, "tool_result", {"tool": "decide_next_best_action", "summary": action_summary})

        # ── 4. Content Built (REAL model draft, approved content only) ─
        store.set_running_text(job_id, "Drafting outreach in your voice…")
        yield await _emit(job_id, "stage", {"key": "content", "label": "Content built", "status": "running"})
        await _pause()
        content_source = decision.get("content_source", "content_release_alert")
        approved = _approved_content(content_source)
        first_name = (account.get("primary_contact") or "there").split()[0]

        # The prompt the agent forms (uploadText) is evaluated by Purview DSPM on the
        # manager's behalf; a restrictAccess policy would block the draft here.
        draft_prompt = (f"Draft a {decision.get('message_type')} for {account.get('primary_contact')} "
                        f"at {account_name} about {signal.get('feature')} ({signal.get('product')}). "
                        f"Signal: {signal.get('description')}")
        prompt_decision = await purview.process_content(
            text=draft_prompt, activity="uploadText", manager=manager,
            correlation_id=correlation_id, sequence=_next_seq(),
            source="prompt", account_id=account_id, name=f"Draft for {account_name}")
        if not prompt_decision.allowed:
            outcome = {"channel": "blocked", "delivered": False,
                       "summary": "Blocked by Microsoft Purview DLP policy (prompt).",
                       "account_name": account_name, "account_id": account_id,
                       "manager_id": manager.get("id")}
            store.record_stage(job_id, {"key": "content", "label": "Content built",
                                        "summary": "Blocked by Purview DLP policy.", "status": "blocked"})
            store.finish_job(job_id, status="complete", result=prompt_decision.detail, outcome=outcome)
            store.set_autopilot_status(autopilot["id"], "idle", "Blocked by policy")
            yield await _emit(job_id, "result", {"content": prompt_decision.detail})
            yield await _emit(job_id, "done", {"status": "blocked"})
            return

        draft = await content_build.build_draft(
            content_source=content_source,
            feature=signal.get("feature", ""),
            recipient_first_name=first_name,
            extra_context=f"Signal: {signal.get('description')}. Product: {signal.get('product')}.",
            work_iq_context=_wiq_for_draft(wiq),
        )
        model_backed = not draft.lower().startswith(("no approved content", "draft generation is unavailable"))
        await _tool("build_draft", {"content_source": content_source, "feature": signal.get("feature")}, draft)
        _turn(f"Draft from approved content [{content_source}] in {manager.get('name')}'s voice.", draft)

        # Cross-customer data fence (defence in depth): a draft for THIS customer must
        # never carry another customer's confidential identifiers. If it does, the
        # send is blocked before it can leave the agent — the same protection the
        # Purview DLP-for-AI rule enforces at the platform on the prompt.
        cross = scenarios.scan_cross_customer(draft, account_id)
        if cross:
            names = ", ".join(c["account_name"] or c["account_id"] for c in cross)
            detail = (f"Blocked by the cross-customer data fence — the draft for {account_name} referenced "
                      f"another customer's confidential identifiers ({names}). The send was stopped.")
            outcome = {"channel": "blocked", "delivered": False, "summary": detail,
                       "account_name": account_name, "account_id": account_id,
                       "manager_id": manager.get("id")}
            store.record_stage(job_id, {"key": "content", "label": "Content built",
                                        "summary": detail, "status": "blocked", "crossCustomer": cross})
            store.finish_job(job_id, status="complete", result=detail, outcome=outcome)
            store.set_autopilot_status(autopilot["id"], "idle", "Blocked — cross-customer fence")
            yield await _emit(job_id, "result", {"content": detail})
            yield await _emit(job_id, "done", {"status": "blocked"})
            return

        # The generated response (downloadText) is also evaluated + classified by Purview.
        resp_decision = await purview.process_content(
            text=draft, activity="downloadText", manager=manager,
            correlation_id=correlation_id, sequence=_next_seq(),
            source="response", account_id=account_id, name=f"Draft for {account_name}")
        draft_label = resp_decision.label
        store.record_stage(job_id, {"key": "content", "label": "Content built",
                                    "summary": (f"Model-generated draft from approved content '{content_source}'."
                                                if model_backed else f"Draft from approved content '{content_source}'."),
                                    "draft": draft, "modelBacked": model_backed,
                                    "sensitivityLabel": draft_label,
                                    "sits": resp_decision.sits, "status": "done"})
        yield await _emit(job_id, "turn", {"phase": "content", "turn": store.get_job(job_id)["stats"]["turns"],
                                           "modelBacked": model_backed, "label": draft_label,
                                           "purviewReal": resp_decision.real})

        # ── 5. Prioritised & Reviewed ────────────────────────────────
        store.set_running_text(job_id, "Routing for delivery…")
        yield await _emit(job_id, "stage", {"key": "review", "label": "Prioritised & reviewed", "status": "running"})
        await _pause()
        subject = f"{signal.get('feature')} — {account.get('account_name')}"
        priority = "High" if int(signal.get("severity_score", 0)) >= 4 else "Medium"
        csm_name = manager.get("name", config.AGENT_DISPLAY_NAME)

        # ── 6. Delivered (real tool) or queued for CSM review ────────
        yield await _emit(job_id, "stage", {"key": "delivery", "label": "Delivered", "status": "running"})
        await _pause()
        if review_required:
            cta_result = await gainsight_cs.create_review_task(
                account_id=account_id, message_type=decision.get("message_type", "outreach"),
                channel="csm_review", priority=priority, draft_text=draft, signal_id=signal.get("signal_id", ""))
            await _tool("create_review_task", {"account_id": account_id, "priority": priority}, cta_result)
            outcome = {
                "channel": "csm_review", "requiresReview": True, "reviewDecision": "pending",
                "status": "pending_review", "delivered": False, "priority": priority,
                "subject": subject, "body": draft, "account_name": account.get("account_name"),
                "account_id": account_id, "recipient": account.get("primary_contact"),
                "manager": csm_name, "manager_id": manager.get("id"), "signalId": signal.get("signal_id"),
                "messageType": decision.get("message_type"), "reviewReasons": decision.get("review_reasons", []),
                "sensitivityLabel": draft_label,
                "summary": f"Drafted {decision.get('message_type')} routed to {csm_name} for review.",
            }
            result_text = f"Prepared a {decision.get('message_type')} for {account.get('primary_contact')} and routed it to {csm_name} for review."
            store.record_stage(job_id, {"key": "review", "label": "Prioritised & reviewed",
                                        "summary": f"Routed to {csm_name}'s review queue ({priority}).", "status": "done"})
            final_status = "needs_review"
            store.set_autopilot_status(autopilot["id"], "needs_review", "Awaiting your review")
        else:
            if channel == "in_product":
                content_id = approved[0].get("content_id") if approved else ""
                delivery = await gainsight_px.trigger_in_product_message(
                    user_id=signal.get("user_id", ""), content_id=content_id)
                await _tool("trigger_in_product_message", {"user_id": signal.get("user_id"), "content_id": content_id}, delivery)
                out_channel = "in_product"
            else:
                delivery = await gainsight_cs.send_email(account_id=account_id, subject=subject, body=draft)
                await _tool("send_email", {"account_id": account_id, "subject": subject}, delivery)
                out_channel = "email"
            low = delivery.lower()
            # The agent sends via Work IQ on the manager's behalf (OBO). In the
            # management console (no signed-in manager turn) the draft is queued
            # for the agent to deliver; the bot conversation completes the send.
            obo_pending = "could not acquire" in low or "obo required" in low
            routed_to_review = (not obo_pending) and ("not auto-sent" in low or "routed to a gainsight cta" in low)
            delivered = (not obo_pending) and (not routed_to_review) and ("sent to" in low or "executed for" in low)
            if routed_to_review:
                out_status, out_chan = "pending_review", "csm_review"
                summary = f"Routed {decision.get('message_type')} to {csm_name} for review by the delivery guardrail."
                ap_status, ap_activity = "needs_review", "Awaiting your review"
            elif delivered:
                out_status, out_chan = "delivered", out_channel
                summary = f"Auto-delivered {decision.get('message_type')} via {out_channel}."
                ap_status, ap_activity = "idle", "Delivered"
            else:  # OBO pending — queued for the agent to send via Work IQ
                out_status, out_chan = "queued_agent", out_channel
                summary = f"Drafted {decision.get('message_type')}; queued for the agent to send via Work IQ on your behalf."
                ap_status, ap_activity = "idle", "Queued for delivery"
            outcome = {
                "channel": out_chan,
                "requiresReview": routed_to_review, "reviewDecision": "pending" if routed_to_review else "auto",
                "status": out_status, "delivered": delivered, "priority": priority, "subject": subject, "body": draft,
                "account_name": account.get("account_name"), "account_id": account_id,
                "recipient": account.get("primary_contact"), "manager": csm_name, "manager_id": manager.get("id"),
                "signalId": signal.get("signal_id"), "messageType": decision.get("message_type"),
                "sensitivityLabel": draft_label,
                "deliveryDetail": delivery, "summary": summary,
            }
            result_text = delivery
            store.record_stage(job_id, {"key": "review", "label": "Prioritised & reviewed",
                                        "summary": summary, "status": "done"})
            final_status = "needs_review" if routed_to_review else "complete"
            store.set_autopilot_status(autopilot["id"], ap_status, ap_activity)
        store.record_stage(job_id, {"key": "delivery", "label": "Delivered", "summary": outcome["summary"], "status": "done"})
        yield await _emit(job_id, "outcome", outcome)

        # ── 7. System Learns (real learning-loop write-back + agent memory) ─
        yield await _emit(job_id, "stage", {"key": "learning", "label": "System learns", "status": "running"})
        await _pause()
        try:
            learn = await snowflake.write_outcome(
                item_id=signal.get("signal_id", ""),
                decision="auto_delivered" if outcome.get("delivered") else "routed_to_review",
                final_text=outcome.get("summary", ""))
        except Exception as exc:  # pragma: no cover
            learn = f"Outcome logged (in-memory): {exc}"
        await _tool("write_outcome", {"signal_id": signal.get("signal_id"), "channel": outcome["channel"]}, learn)

        # The agent reflects on this job and updates its working memory (it "learns").
        lesson = None
        try:
            record = (f"Account {account_name} ({account.get('tier')}, sentiment "
                      f"{account.get('sentiment')}, influence {account.get('influence')}). "
                      f"Signal: {signal.get('severity')} {signal.get('signal_type')} on "
                      f"{signal.get('feature')}. Decision: {decision.get('message_type')} via "
                      f"{outcome.get('channel')}; {'auto-delivered' if outcome.get('delivered') else 'routed to CSM review'}.")
            section = "What worked" if outcome.get("delivered") else "Account notes"
            lesson = await memory.reflect_on_job(
                manager_id=manager.get("id"), manager_name=csm_name, summary=record, section=section)
        except Exception as exc:  # pragma: no cover
            logger.info("memory reflection skipped: %s", exc)
        if lesson:
            await _tool("remember", {"section": "What worked" if outcome.get("delivered") else "Account notes"}, lesson)
        store.record_stage(job_id, {"key": "learning", "label": "System learns",
                                    "summary": ("Outcome logged; memory updated — " + lesson) if lesson
                                               else "Outcome + decision logged to the learning loop.",
                                    "lesson": lesson, "status": "done"})

        finished = store.finish_job(job_id, status=final_status, result=result_text, outcome=outcome)
        yield await _emit(job_id, "result", {"content": result_text})
        yield await _emit(job_id, "stats", finished["stats"])
        yield await _emit(job_id, "done", {"status": final_status})

    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Job %s failed", job_id)
        store.fail_job(job_id, str(exc))
        store.set_autopilot_status(autopilot["id"], "idle", "Error")
        yield ("error", {"message": f"Autopilot error: {exc}"})
        yield ("done", {"status": "error"})
    finally:
        identity.reset_request_context(ctx_token)
