"""
In-memory business store for the CSM Autopilot control plane.

Holds the **fleet** of CSM Autopilots — **one agent instance per human CSM
(manager)**, working across the accounts assigned to that CSM and acting on the
CSM's behalf — the **job ledger** (run history), the **outcomes** produced
(emails, in-product prompts, CSM review tasks), and the **review queue**.

It aggregates two scopes of metrics:

* **Manager scope** — what a single CSM sees about *their own* CSM Autopilot:
  their accounts, their signals, their review queue. "Managing a team of one."
* **Sponsor scope** — what the owner of the whole programme sees across *every*
  CSM Autopilot: cost, performance, per-agent HITL queue length, response times.

Everything is process-memory only (read-mostly fixtures + in-memory job state),
so nothing is persisted across restarts. The fleet is seeded from the simulated
``accounts``/``managers``/``owners`` fixtures via :mod:`src.data_store`.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .. import cost, data_store, directory, agent_instances, config
from . import cost_store

# Newest-first job ledger, capped.
_JOB_LIMIT = 120

_LOCK = threading.RLock()
_jobs: dict[str, dict] = {}
_job_order: list[str] = []
_outcomes: list[dict] = []
_fleet: dict[str, dict] = {}

# Durable-backed cost points (one per finished job). Loaded from the cost ledger
# on startup so the cost/token history survives container recycles, and appended
# to as jobs finish. The time-bucketed cost chart is built from this list.
_cost_points: list[dict] = []
_COST_POINTS_CAP = 20000


# ── helpers ────────────────────────────────────────────────────────
def now_ms() -> int:
    return int(time.time() * 1000)


def _initial(name: str) -> str:
    parts = [p for p in str(name).split() if p]
    return (parts[0][0] + parts[-1][0]).upper() if len(parts) >= 2 else (name[:2].upper() or "AP")


def _account_card(acc: dict, idx: dict | None = None) -> dict:
    """The customer/product-focused view of an account the manager cares about."""
    sig = (idx or {}).get(acc.get("account_id"), {})
    return {
        "account_id": acc.get("account_id"),
        "account_name": acc.get("account_name"),
        "tier": acc.get("tier"),
        "industry": acc.get("industry"),
        "region": acc.get("region"),
        "products": acc.get("products", []),
        "arr_gbp": acc.get("arr_gbp", 0),
        "health_score": acc.get("health_score"),
        "influence": acc.get("influence"),
        "sentiment": acc.get("sentiment"),
        "renewal_date": acc.get("renewal_date"),
        "strategic": acc.get("strategic"),
        "primary_contact": acc.get("primary_contact"),
        "primary_contact_title": acc.get("primary_contact_title"),
        "onboarding_stage": acc.get("onboarding_stage"),
        "openSignals": sig.get("open", 0),
        "topSeverityScore": sig.get("topScore", 0),
        "topSeverity": sig.get("topSeverity", ""),
        "avatar": _initial(acc.get("account_name", "AC")),
    }


_OPEN_STATUSES = ("new", "open", "")


def _open_signal_index() -> dict[str, dict]:
    """One-pass index of OPEN signals per account: count + top severity.

    At scale (thousands of signals across hundreds of accounts) the cockpit and
    sponsor views need per-account signal load without re-scanning the signals
    table once per account. This computes it once.
    """
    idx: dict[str, dict] = {}
    for s in data_store.table("signals"):
        if str(s.get("status", "new")).lower() not in _OPEN_STATUSES:
            continue
        acc_id = s.get("account_id")
        if not acc_id:
            continue
        score = int(s.get("severity_score", 0) or 0)
        entry = idx.get(acc_id)
        if entry is None:
            idx[acc_id] = {"open": 1, "topScore": score, "topSeverity": s.get("severity", "")}
        else:
            entry["open"] += 1
            if score > entry["topScore"]:
                entry["topScore"] = score
                entry["topSeverity"] = s.get("severity", "")
    return idx


# ── fleet (one autopilot per manager) ──────────────────────────────
def _autopilot_id(manager_id: str) -> str:
    return f"ap-{manager_id}"


def accounts_for_manager(manager_id: str) -> list[dict]:
    return [a for a in data_store.table("accounts") if a.get("csm_manager_id") == manager_id]


def build_fleet() -> dict[str, dict]:
    """Seed (once) one CSM Autopilot per CSM, covering that CSM's book of business."""
    with _LOCK:
        if _fleet:
            return _fleet
        owners = {o.get("owner_id"): o for o in data_store.table("owners")}
        sig_idx = _open_signal_index()
        for manager in data_store.table("managers"):
            mgr_id = manager.get("manager_id")
            accounts = accounts_for_manager(mgr_id)
            if not accounts:
                continue
            ap_id = _autopilot_id(mgr_id)
            owner = owners.get(manager.get("owner_id"), {})
            cards = [_account_card(a, sig_idx) for a in accounts]
            mgr_rr = directory.cached_role_region(manager.get("entra_object_id"))
            owner_rr = directory.cached_role_region(owner.get("entra_object_id"))
            _fleet[ap_id] = {
                "id": ap_id,
                "displayName": f"CSM Autopilot · {manager.get('display_name')}",
                "manager": {
                    "id": mgr_id,
                    "name": manager.get("display_name"),
                    "role": mgr_rr.get("role"),
                    "upn": manager.get("upn"),
                    "region": mgr_rr.get("region"),
                    "entra_object_id": manager.get("entra_object_id"),
                },
                "owner": {
                    "id": owner.get("owner_id"),
                    "name": owner.get("display_name"),
                    "role": owner_rr.get("role"),
                },
                "accounts": cards,
                "accountCount": len(cards),
                "arr_gbp": sum(c.get("arr_gbp", 0) for c in cards),
                "avgHealth": round(sum(c.get("health_score") or 0 for c in cards) / len(cards)) if cards else 0,
                "strategicCount": sum(1 for c in cards if str(c.get("strategic", "")).lower() == "yes"),
                "openSignals": sum(c.get("openSignals", 0) for c in cards),
                "avatar": _initial(manager.get("display_name", "AP")),
                "status": "idle",  # idle | running | needs_review
                "activity": "",
                "currentJobId": None,
                "currentAccount": None,
                "lastRunAt": None,
            }
        return _fleet


def fleet(manager_id: str | None = None, include_all: bool = False) -> list[dict]:
    """The CSM Autopilots.

    By default this reflects **real Agent 365 instances**: when instance discovery
    has run (Graph readable), only CSMs who actually have an instance are shown,
    and each carries its real instance metadata under ``realInstance``. When
    discovery hasn't run (offline/dev/tests), all simulated autopilots are shown
    so nothing silently disappears. Pass ``include_all=True`` to bypass the gate.
    """
    fl = list(build_fleet().values())
    instances = agent_instances.cached_csm_instances()
    active = agent_instances.active_manager_ids()  # None = unknown → don't gate
    for a in fl:
        a["realInstance"] = (instances or {}).get(a["manager"]["id"])
    if active is not None and not include_all:
        fl = [a for a in fl if a["manager"]["id"] in active]
    if manager_id:
        return [a for a in fl if a["manager"]["id"] == manager_id]
    return fl



def get_autopilot(ap_id: str) -> dict | None:
    return build_fleet().get(ap_id)


def autopilot_for_manager(manager_id: str) -> dict | None:
    ap = build_fleet().get(_autopilot_id(manager_id))
    if ap is not None:
        instances = agent_instances.cached_csm_instances()
        ap["realInstance"] = (instances or {}).get(manager_id)
    return ap



def set_autopilot_status(
    ap_id: str, status: str, activity: str = "", job_id: str | None = None, account: str | None = None
) -> None:
    with _LOCK:
        ap = build_fleet().get(ap_id)
        if not ap:
            return
        ap["status"] = status
        ap["activity"] = activity
        if job_id is not None:
            ap["currentJobId"] = job_id
        if account is not None:
            ap["currentAccount"] = account
        if status != "running":
            ap["lastRunAt"] = now_ms()
            ap["currentAccount"] = None


# ── jobs ───────────────────────────────────────────────────────────
def start_job(*, autopilot: dict, account: dict, title: str, prompt: str, source: str, model: str) -> dict:
    """Create and register a new job record (status=running) for one account."""
    with _LOCK:
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        job = {
            "id": job_id,
            "title": title,
            "status": "running",  # running | complete | needs_review | error
            "source": source,
            "autopilot": {
                "id": autopilot["id"],
                "name": autopilot["displayName"],
            },
            "manager": autopilot.get("manager", {}),
            "owner": autopilot.get("owner", {}),
            "account": {
                "account_id": account.get("account_id"),
                "account_name": account.get("account_name"),
                "tier": account.get("tier"),
                "products": account.get("products", []),
            },
            "prompt": prompt,
            "startedAt": now_ms(),
            "updatedAt": now_ms(),
            "completedAt": None,
            "stages": [],        # journey stages, ordered
            "toolData": {},      # callId -> {tool, arguments, result, index}
            "agentEvents": [],   # newest-first telemetry events
            "outcome": None,     # the delivered/queued outcome
            "result": "",
            "runningText": "Starting…",
            "stats": {
                "model": model,
                "turns": 0,
                "tool_calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "priced": True,
                "duration": 0.0,
            },
            "error": "",
        }
        _jobs[job_id] = job
        _job_order.insert(0, job_id)
        for stale in _job_order[_JOB_LIMIT:]:
            _jobs.pop(stale, None)
        del _job_order[_JOB_LIMIT:]
        return job


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def jobs(limit: int | None = None, manager_id: str | None = None) -> list[dict]:
    with _LOCK:
        out = [_jobs[j] for j in _job_order if j in _jobs]
    if manager_id:
        out = [j for j in out if (j.get("manager") or {}).get("id") == manager_id]
    return out[:limit] if limit else out


def record_event(job_id: str, event: str, attributes: dict) -> None:
    with _LOCK:
        job = _jobs.get(job_id)
        if not job:
            return
        job["updatedAt"] = now_ms()
        job["agentEvents"].insert(0, {"event": event, "timestamp": time.time(), "attributes": attributes})
        del job["agentEvents"][40:]


def record_stage(job_id: str, stage: dict) -> None:
    with _LOCK:
        job = _jobs.get(job_id)
        if job:
            job["stages"].append(stage)
            job["updatedAt"] = now_ms()


def record_tool(job_id: str, call_id: str, tool: str, arguments: dict, result: str, index: int) -> None:
    with _LOCK:
        job = _jobs.get(job_id)
        if job:
            job["toolData"][call_id] = {
                "tool": tool,
                "arguments": arguments,
                "result": result,
                "index": index,
            }
            job["stats"]["tool_calls"] = len(job["toolData"])
            job["updatedAt"] = now_ms()


def record_turn(job_id: str, *, prompt_tokens: int, completion_tokens: int) -> None:
    """Account for one LLM turn; recompute token-based Azure OpenAI cost."""
    with _LOCK:
        job = _jobs.get(job_id)
        if not job:
            return
        s = job["stats"]
        s["turns"] += 1
        s["prompt_tokens"] += prompt_tokens
        s["completion_tokens"] += completion_tokens
        s["total_tokens"] += prompt_tokens + completion_tokens
        breakdown = cost.cost_for_tokens(s["model"], s["prompt_tokens"], s["completion_tokens"])
        s["priced"] = breakdown.priced
        s["cost_usd"] = round(breakdown.cost_usd, 6) if breakdown.cost_usd is not None else None
        s["input_price_per_m"] = breakdown.input_price_per_m
        s["output_price_per_m"] = breakdown.output_price_per_m
        job["updatedAt"] = now_ms()


def set_running_text(job_id: str, text: str) -> None:
    with _LOCK:
        job = _jobs.get(job_id)
        if job:
            job["runningText"] = text
            job["updatedAt"] = now_ms()


def finish_job(job_id: str, *, status: str, result: str, outcome: dict | None) -> dict | None:
    with _LOCK:
        job = _jobs.get(job_id)
        if not job:
            return None
        job["status"] = status
        job["result"] = result
        job["outcome"] = outcome
        job["completedAt"] = now_ms()
        job["stats"]["duration"] = round((job["completedAt"] - job["startedAt"]) / 1000, 2)
        job["updatedAt"] = now_ms()
        # Record a durable cost point so the cost/token history survives recycles.
        _record_cost_point(job)
        if outcome:
            outcome.setdefault("id", f"out-{uuid.uuid4().hex[:10]}")
            outcome.setdefault("createdAt", now_ms())
            outcome["jobId"] = job_id
            outcome.setdefault("manager_id", (job.get("manager") or {}).get("id"))
            _outcomes.insert(0, outcome)
            del _outcomes[300:]
        return job


def fail_job(job_id: str, error: str) -> None:
    with _LOCK:
        job = _jobs.get(job_id)
        if job:
            job["status"] = "error"
            job["error"] = error
            job["completedAt"] = now_ms()
            job["updatedAt"] = now_ms()


# ── outcomes + review queue ────────────────────────────────────────
def outcomes(limit: int | None = None, manager_id: str | None = None) -> list[dict]:
    with _LOCK:
        out = list(_outcomes)
    if manager_id:
        out = [o for o in out if o.get("manager_id") == manager_id]
    return out[:limit] if limit else out


def get_outcome(outcome_id: str) -> dict | None:
    with _LOCK:
        return next((o for o in _outcomes if o.get("id") == outcome_id), None)


def review_queue(manager_id: str | None = None) -> list[dict]:
    """Outcomes awaiting CSM review (in-memory live + the seeded fixture queue).

    A live outcome is the authoritative, *decidable* review item. The simulated
    Gainsight CTA-create mirrors each review into the ``review_queue`` fixture, so
    we de-duplicate by (account, signal): a live item suppresses the matching
    seeded CTA / fixture row, and seeded rows de-dupe among themselves.
    """
    with _LOCK:
        live = [o for o in _outcomes if o.get("requiresReview") and o.get("reviewDecision") in (None, "pending")]
    seen: set = {(o.get("account_id"), o.get("signalId")) for o in live if o.get("signalId")}
    seeded = []
    # The seeded fixture queue is a demo aid only and is OFF by default, so the
    # cockpit never shows review items that didn't really happen. Real outcomes
    # the agent produces (``live``) are always shown.
    if config.SEED_REVIEW_QUEUE:
        for item in data_store.table("review_queue"):
            if str(item.get("status", "")).lower() not in ("", "pending", "new", "awaiting_review"):
                continue
            key = (item.get("account_id"), item.get("signal_id") or item.get("item_id"))
            if key in seen:
                continue
            seen.add(key)
            acc = data_store.get("accounts", "account_id", item.get("account_id")) or {}
            seeded.append({
                "id": item.get("item_id"),
                "channel": "csm_review",
                "requiresReview": True,
                "reviewDecision": "pending",
                "seeded": True,
                "manager_id": acc.get("csm_manager_id"),
                "manager": acc.get("csm_name"),
                "account_name": acc.get("account_name"),
                "account_id": item.get("account_id"),
                "signalId": item.get("signal_id"),
                "subject": item.get("message_type", "Review item"),
                "body": item.get("draft_text", ""),
                "priority": item.get("priority", "Medium"),
                "createdAt": None,
            })
    items = live + seeded
    if manager_id:
        items = [i for i in items if i.get("manager_id") == manager_id]
    return items


def decide_review(outcome_id: str, decision: str, final_text: str = "") -> dict | None:
    """Apply a CSM Accept/Draft/Discard decision to a queued outcome (the learning loop).

    This records the decision only. For accept the actual email send (and for
    ``draft`` the real Outlook-draft creation) happens in the web handler (async)
    via :mod:`src.mail`, and the real result is written back with
    :func:`mark_outcome_sent` / :func:`mark_outcome_drafted` — so ``delivered`` /
    ``drafted`` reflect a real action, not a presumption. Any non-pending decision
    removes the item from the review queue.
    """
    with _LOCK:
        outcome = next((o for o in _outcomes if o.get("id") == outcome_id), None)
        if outcome is None:
            return None
        outcome["reviewDecision"] = decision
        outcome["decidedAt"] = now_ms()
        if final_text:
            outcome["body"] = final_text
        if decision in ("accept", "edit"):
            # Provisional — the handler sends and calls mark_outcome_sent with the truth.
            outcome["status"] = "approved"
            outcome["delivered"] = False
        elif decision == "draft":
            # Provisional — the handler creates the real draft and calls mark_outcome_drafted.
            outcome["status"] = "drafting"
            outcome["delivered"] = False
        elif decision == "discard":
            outcome["status"] = "discarded"
            outcome["delivered"] = False
        return outcome


def mark_outcome_drafted(outcome_id: str, result: dict) -> dict | None:
    """Record the real save-to-drafts result on an outcome (honest drafted state)."""
    with _LOCK:
        outcome = next((o for o in _outcomes if o.get("id") == outcome_id), None)
        if outcome is None:
            return None
        saved = bool(result.get("saved"))
        outcome["delivered"] = False
        outcome["status"] = "drafted" if saved else "draft_failed"
        outcome["deliveryDetail"] = result.get("detail")
        outcome["sentTo"] = result.get("to")
        outcome["sentAs"] = result.get("as")
        outcome["updatedAt"] = now_ms()
        return outcome



def mark_outcome_sent(outcome_id: str, result: dict) -> dict | None:
    """Record the real email-send result on an outcome (honest delivered state)."""
    with _LOCK:
        outcome = next((o for o in _outcomes if o.get("id") == outcome_id), None)
        if outcome is None:
            return None
        sent = bool(result.get("sent"))
        outcome["delivered"] = sent
        outcome["status"] = "delivered" if sent else "send_failed"
        outcome["deliveryDetail"] = result.get("detail")
        outcome["sentTo"] = result.get("to")
        outcome["sentAs"] = result.get("as")
        outcome["updatedAt"] = now_ms()
        return outcome


# ── metrics ────────────────────────────────────────────────────────
def _signals_for(manager_id: str | None) -> list[dict]:
    sigs = data_store.table("signals")
    if not manager_id:
        return sigs
    account_ids = {a["account_id"] for a in accounts_for_manager(manager_id)}
    return [s for s in sigs if s.get("account_id") in account_ids]


# ── durable cost points + adaptive time-series ─────────────────────
def _record_cost_point(job: dict) -> None:
    """Append a cost point for a finished job (in-memory cache + durable ledger)."""
    s = job.get("stats", {})
    tokens = int(s.get("total_tokens") or 0)
    if tokens <= 0:
        return  # nothing metered for this job
    point = {
        "jobId": job.get("id"),
        "managerId": (job.get("manager") or {}).get("id") or "all",
        "t": job.get("completedAt") or now_ms(),
        "cost": float(s.get("cost_usd") or 0.0),
        "tokens": tokens,
        "inputTokens": int(s.get("prompt_tokens") or 0),
        "outputTokens": int(s.get("completion_tokens") or 0),
        "priced": s.get("priced") is not False,
        "model": s.get("model") or "",
        "account": (job.get("account") or {}).get("account_name") or "",
    }
    _cost_points.append(point)
    del _cost_points[:-_COST_POINTS_CAP]
    cost_store.persist_point(point)


def warm_cost_points() -> None:
    """Load the durable cost ledger into memory on startup (best-effort)."""
    try:
        loaded = cost_store.load_points()
    except Exception:  # pragma: no cover - best effort
        loaded = []
    if not loaded:
        return
    with _LOCK:
        have = {p.get("jobId") for p in _cost_points}
        for p in loaded:
            if p.get("jobId") not in have:
                _cost_points.append(p)
        _cost_points.sort(key=lambda p: p.get("t") or 0)
        del _cost_points[:-_COST_POINTS_CAP]


def _bucket_label(ms: int, gran: str) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if gran == "minute":
        return dt.strftime("%H:%M")
    if gran == "hour":
        return dt.strftime("%m-%d %H:00")
    return dt.strftime("%m-%d")


def cost_timeseries(manager_id: str | None = None) -> dict:
    """Inference cost/tokens bucketed by an **adaptive** time grain.

    The x-axis grain widens with the data span — per **minute** (< 1h of history),
    then per **hour** (< 2 days), then per **day** (up to the last 7 days) — and
    only uses a finer grain when there's enough history to fill it. Buckets are a
    continuous run up to "now" (zero-filled where idle), so the chart looks good at
    any volume and isn't a per-run plot where cost and tokens trivially coincide.
    """
    with _LOCK:
        pts = [p for p in _cost_points if not manager_id or p.get("managerId") == manager_id]
    pts = sorted(pts, key=lambda p: p.get("t") or 0)
    now = now_ms()
    MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000
    if not pts:
        return {"granularity": "minute", "bucketMs": MIN, "points": [],
                "totalCost": 0.0, "totalTokens": 0, "priced": True, "runs": 0}

    first = pts[0]["t"]
    span = max(0, now - first)
    if span < HOUR:
        size, gran, max_b, min_b = MIN, "minute", 60, 12
    elif span < 2 * DAY:
        size, gran, max_b, min_b = HOUR, "hour", 48, 8
    else:
        size, gran, max_b, min_b = DAY, "day", 7, 7

    sums: dict[int, list] = {}
    for p in pts:
        b = (int(p["t"]) // size) * size
        c = sums.setdefault(b, [0.0, 0, 0])
        c[0] += float(p.get("cost") or 0.0)
        c[1] += int(p.get("tokens") or 0)
        c[2] += 1

    end = (now // size) * size
    start_data = (first // size) * size
    n = int((end - start_data) // size) + 1
    n = min(max(n, min_b), max_b)
    start = end - (n - 1) * size

    points = []
    for i in range(n):
        b = start + i * size
        c = sums.get(b, [0.0, 0, 0])
        points.append({"t": b, "label": _bucket_label(b, gran),
                       "cost": round(c[0], 6), "tokens": c[1], "runs": c[2]})
    return {
        "granularity": gran,
        "bucketMs": size,
        "points": points,
        "totalCost": round(sum(float(p.get("cost") or 0.0) for p in pts), 6),
        "totalTokens": sum(int(p.get("tokens") or 0) for p in pts),
        "priced": not any(p.get("priced") is False for p in pts),
        "runs": len(pts),
    }


def metrics(manager_id: str | None = None) -> dict:
    """Aggregate the business KPIs + chart series. Scoped to one CSM if given."""
    with _LOCK:
        all_jobs = [_jobs[j] for j in _job_order if j in _jobs]
    if manager_id:
        all_jobs = [j for j in all_jobs if (j.get("manager") or {}).get("id") == manager_id]
    all_outcomes = outcomes(manager_id=manager_id)
    fl = fleet(manager_id)

    total_jobs = len(all_jobs)
    running = sum(1 for j in all_jobs if j["status"] == "running")
    completed = sum(1 for j in all_jobs if j["status"] in ("complete", "needs_review"))
    total_tokens = sum(j["stats"]["total_tokens"] for j in all_jobs)
    input_tokens = sum(j["stats"].get("prompt_tokens", 0) for j in all_jobs)
    output_tokens = sum(j["stats"].get("completion_tokens", 0) for j in all_jobs)
    total_cost = round(sum((j["stats"].get("cost_usd") or 0.0) for j in all_jobs), 6)
    any_unpriced = any(j["stats"].get("priced") is False for j in all_jobs)

    durations = [j["stats"]["duration"] for j in all_jobs if j["status"] != "running" and j["stats"]["duration"]]
    avg_response = round(sum(durations) / len(durations), 1) if durations else None

    by_channel: dict[str, int] = defaultdict(int)
    for o in all_outcomes:
        by_channel[o.get("channel", "other")] += 1

    by_severity: dict[str, int] = defaultdict(int)
    for sig in _signals_for(manager_id):
        by_severity[str(sig.get("severity", "Unknown"))] += 1

    decided = [o for o in all_outcomes if o.get("reviewDecision") in ("accept", "edit", "discard")]
    accepted = sum(1 for o in decided if o.get("reviewDecision") in ("accept", "edit"))
    acceptance_rate = round(100 * accepted / len(decided)) if decided else None

    auto_sent = sum(1 for o in all_outcomes if not o.get("requiresReview") and o.get("delivered"))
    pending_review = len(review_queue(manager_id))

    cost_series = [
        {"jobId": j["id"], "t": j["completedAt"] or j["startedAt"], "cost": (j["stats"].get("cost_usd") or 0.0),
         "tokens": j["stats"]["total_tokens"], "account": (j.get("account") or {}).get("account_name")}
        for j in sorted(all_jobs, key=lambda x: x["startedAt"])
        if j["status"] != "running"
    ]

    arr_covered = sum(ap.get("arr_gbp", 0) for ap in fl)
    accounts_covered = sum(ap.get("accountCount", 0) for ap in fl)

    return {
        "scope": manager_id or "all",
        "kpis": {
            "fleetSize": len(fl),
            "activeNow": running,
            "jobsTotal": total_jobs,
            "jobsCompleted": completed,
            "signalsWatched": sum(by_severity.values()),
            "outcomesDelivered": sum(1 for o in all_outcomes if o.get("delivered")),
            "autoSent": auto_sent,
            "pendingReview": pending_review,
            "accountsCovered": accounts_covered,
            "arrCovered": arr_covered,
            "tokensTotal": total_tokens,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "costUsd": total_cost,
            "costPriced": not any_unpriced,
            "acceptanceRate": acceptance_rate,
            "avgResponseSec": avg_response,
        },
        "charts": {
            "outcomesByChannel": dict(by_channel),
            "signalsBySeverity": dict(by_severity),
            "costSeries": cost_series,
            "costTimeseries": cost_timeseries(manager_id),
            "fleetStatus": {
                "idle": sum(1 for a in fl if a["status"] == "idle"),
                "running": sum(1 for a in fl if a["status"] == "running"),
                "needs_review": sum(1 for a in fl if a["status"] == "needs_review"),
            },
        },
        "generatedAt": now_ms(),
    }


def sponsor_overview() -> dict:
    """The owner/sponsor view: one row per CSM Autopilot across the whole programme."""
    with _LOCK:
        all_jobs = [_jobs[j] for j in _job_order if j in _jobs]
    owners = data_store.table("owners")
    owner = owners[0] if owners else {}

    rows = []
    for ap in fleet():
        mgr_id = ap["manager"]["id"]
        mjobs = [j for j in all_jobs if (j.get("manager") or {}).get("id") == mgr_id]
        mout = outcomes(manager_id=mgr_id)
        done = [j for j in mjobs if j["status"] != "running"]
        durations = [j["stats"]["duration"] for j in done if j["stats"]["duration"]]
        decided = [o for o in mout if o.get("reviewDecision") in ("accept", "edit", "discard")]
        accepted = sum(1 for o in decided if o.get("reviewDecision") in ("accept", "edit"))
        rows.append({
            "autopilotId": ap["id"],
            "manager": ap["manager"],
            "realInstance": ap.get("realInstance"),
            "avatar": ap["avatar"],
            "status": ap["status"],
            "activity": ap["activity"],
            "currentAccount": ap.get("currentAccount"),
            "accountsCovered": ap["accountCount"],
            "strategicCount": ap.get("strategicCount", 0),
            "arrCovered": ap["arr_gbp"],
            "avgHealth": ap.get("avgHealth"),
            "openSignals": ap.get("openSignals", 0),
            "jobs": len(mjobs),
            "jobsRunning": sum(1 for j in mjobs if j["status"] == "running"),
            "outcomes": len(mout),
            "delivered": sum(1 for o in mout if o.get("delivered")),
            "autoSent": sum(1 for o in mout if not o.get("requiresReview") and o.get("delivered")),
            "queueLength": len(review_queue(mgr_id)),
            "costUsd": round(sum((j["stats"].get("cost_usd") or 0.0) for j in mjobs), 6),
            "costPriced": not any(j["stats"].get("priced") is False for j in mjobs),
            "tokens": sum(j["stats"]["total_tokens"] for j in mjobs),
            "avgResponseSec": round(sum(durations) / len(durations), 1) if durations else None,
            "acceptanceRate": round(100 * accepted / len(decided)) if decided else None,
            "lastRunAt": ap.get("lastRunAt"),
        })

    totals = {
        "autopilots": len(rows),
        "managers": len(rows),
        "accountsCovered": sum(r["accountsCovered"] for r in rows),
        "arrCovered": sum(r["arrCovered"] for r in rows),
        "jobs": sum(r["jobs"] for r in rows),
        "running": sum(r["jobsRunning"] for r in rows),
        "outcomes": sum(r["outcomes"] for r in rows),
        "delivered": sum(r["delivered"] for r in rows),
        "queueLength": sum(r["queueLength"] for r in rows),
        "costUsd": round(sum(r["costUsd"] for r in rows), 6),
        "costPriced": all(r.get("costPriced", True) for r in rows),
        "tokens": sum(r["tokens"] for r in rows),
    }
    resp_all = [r["avgResponseSec"] for r in rows if r["avgResponseSec"] is not None]
    totals["avgResponseSec"] = round(sum(resp_all) / len(resp_all), 1) if resp_all else None
    acc_all = [r["acceptanceRate"] for r in rows if r["acceptanceRate"] is not None]
    totals["acceptanceRate"] = round(sum(acc_all) / len(acc_all)) if acc_all else None

    return {
        "owner": {
            "id": owner.get("owner_id"),
            "name": owner.get("display_name"),
            "role": directory.cached_role_region(owner.get("entra_object_id")).get("role"),
            "scope": directory.cached_role_region(owner.get("entra_object_id")).get("region"),
        },
        "rows": rows,
        "totals": totals,
        "generatedAt": now_ms(),
    }


def reset() -> None:
    """Clear all job/outcome state (used by tests)."""
    with _LOCK:
        _jobs.clear()
        _job_order.clear()
        _outcomes.clear()
        _fleet.clear()
