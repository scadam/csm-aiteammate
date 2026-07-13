"""
Generate **scale** sample data for the CSM Autopilot POC.

The hand-written fixtures in ``data/`` tell the story with a handful of records.
Real CSMs, though, each carry **many** accounts and face a **flood** of signals.
This script pumps the volume up dramatically so the manager cockpit and the
sponsor dashboard show the true scale of the problem — while keeping every
back end *simulated* (static JSON), exactly as the architecture requires.

Design:

* **Anchors are sacred.** The original, story-bearing records (``ACC-1001`` …
  ``ACC-1009``, ``SIG-5001`` … ``SIG-5011``, the three real CSMs, etc.) are read
  back from disk and preserved **verbatim**, so the narrative and the test
  invariants that pin them keep holding.
* **Deterministic + idempotent.** Synthetic records are generated with a seeded
  RNG into **non-overlapping id ranges** (``ACC-1100+``, ``SIG-6000+`` …). The
  script keeps only the anchor ids from any existing file and regenerates the
  rest, so re-running it reproduces the same data instead of doubling it.
* **Referential integrity.** Every account points at a real CSM (or
  ``unassigned``); every signal/voc/px/review row points at a real account; the
  ``managers[].accounts`` arrays are rebuilt to match ``accounts.json``.

Run:  python -m scripts.generate_sample_data
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SEED = 20260611
TODAY = date(2026, 6, 11)

# ── anchor ids preserved verbatim (read back from the existing fixtures) ──
ANCHORS: dict[str, tuple[str, set[str]]] = {
    # fixture -> (id field, anchor id values)
    "accounts": ("account_id", {f"ACC-{1000 + i}" for i in range(1, 10)}),          # 1001..1009
    "signals": ("signal_id", {f"SIG-{5000 + i}" for i in range(1, 12)}),            # 5001..5011
    "voc": ("voc_id", {f"VOC-{3000 + i}" for i in range(1, 6)}),                    # 3001..3005
    "csm_voice": ("voice_id", {f"CV-{2000 + i}" for i in range(1, 8)}),             # 2001..2007
    "px_engagement": ("engagement_id", {f"PX-{4000 + i}" for i in range(1, 5)}),    # 4001..4004
    "review_queue": ("item_id", {f"RQ-{6000 + i}" for i in range(1, 4)}),           # 6001..6003
    "enhancements": ("enhancement_id", {f"ENH-{7000 + i}" for i in range(1, 5)}),   # 7001..7004
    "content_library": ("content_id", {f"CNT-{9000 + i}" for i in range(1, 7)}),    # 9001..9006
}

# CSMs that take a managed book of business (NOT 'unassigned'). Distribution of
# the NEW accounts across the three real CSMs + a long-tail 'unassigned' pool.
NEW_ACCOUNTS_BY_OWNER = {
    "csm-svasireddy": 67,
    "csm-cora-thomas": 62,
    "csm-mario-rogers": 58,
    "unassigned": 53,
}

# ── content pools ──────────────────────────────────────────────────
NAME_PREFIX = [
    "Aldgate", "Ashford", "Aurora", "Belgrave", "Blackwater", "Bridgepoint", "Calderwood",
    "Carrington", "Cedarbrook", "Clearwater", "Cobalt", "Concord", "Cornerstone", "Crestline",
    "Dovetail", "Eastgate", "Evergreen", "Fairhaven", "Falcon", "Greystone", "Halcyon",
    "Harborview", "Highgate", "Ironbridge", "Kestrel", "Kingsway", "Lakeshore", "Larkspur",
    "Lattice", "Lindenhall", "Marblehead", "Maplewood", "Northwind", "Oakridge", "Obsidian",
    "Pinnacle", "Quantum", "Redwood", "Ridgeway", "Saffron", "Sentinel", "Silverlake",
    "Solstice", "Stonegate", "Summit", "Thornbury", "Tidewater", "Vanguard", "Westbrook",
    "Whitfield", "Windermere", "Wycombe", "Zephyr", "Anchorpoint", "Beaumont", "Cromwell",
    "Drakeford", "Emberton", "Fenwick", "Glenmorgan", "Hartwell", "Inglewood", "Jericho",
    "Kelmscott", "Langford", "Merrivale", "Norwood", "Penrose", "Ravenscourt",
]
NAME_CORE = [
    "Capital", "Asset", "Wealth", "Financial", "Investment", "Markets", "Securities",
    "Advisory", "Partners", "Holdings", "Trust", "Credit", "Equity", "Treasury",
    "Compliance", "Risk", "Global", "Sovereign", "Mutual", "Heritage",
]
NAME_SUFFIX = [
    "Capital Partners", "Asset Management", "Investment Group", "Advisory", "Bank",
    "Securities", "Holdings", "Wealth Management", "Financial Group", "Trust",
    "Partners", "Markets", "Capital", "Group", "Re", "Insurance", "Pension Fund",
    "Brokerage", "Family Office", "Treasury Services",
]
INDUSTRIES = [
    "Asset Management", "Banking", "Insurance", "Reinsurance", "Pensions", "Commodities",
    "Advisory", "Hedge Fund", "Private Equity", "Wealth Management", "Brokerage", "Fintech",
    "Sovereign Wealth", "Custody", "Exchange", "Clearing House", "Credit Union",
    "Building Society", "Family Office", "Corporate Treasury",
]
REGIONS = [
    "UK", "US", "Nordics", "APAC", "LATAM", "Germany", "France", "Benelux", "MEA",
    "Canada", "Japan", "Australia", "Singapore", "Switzerland", "Italy", "Spain",
    "Ireland", "India", "Hong Kong", "UAE",
]
PRODUCT_SETS = [
    ["FlowDesk"],
    ["CheckMate"],
    ["FlowDesk", "CheckMate"],
]
TIERS = ["Strategic", "Growth", "LongTail"]
TIER_WEIGHTS = [0.18, 0.42, 0.40]
SENTIMENTS = ["Frustrated", "Neutral", "Positive"]
SENTIMENT_WEIGHTS = [0.28, 0.44, 0.28]
INFLUENCE = ["High", "Medium", "Low"]
INFLUENCE_WEIGHTS = [0.25, 0.45, 0.30]
ONBOARDING = ["Onboarding", "Adopting", "Established"]

TITLES = [
    "Head of Compliance", "Chief Risk Officer", "Director of Research", "Head of Operations",
    "Head of Financial Crime Compliance", "Portfolio Manager", "Head of Investment Operations",
    "Head of Markets", "Director of Analytics", "Head of KYC", "Compliance Analyst",
    "Head of Onboarding", "VP Technology", "Head of Data", "Investment Risk Lead",
    "Managing Partner", "Operations Manager", "Head of Trading", "Head of Surveillance",
]

# Real, mail-enabled, human mailboxes in the demo tenant (M365CPI81302533) that are
# NOT CSMs. Customer-outreach emails are sent to ``primary_contact_email``, so every
# generated account draws its contact from THIS pool — that way an approved/auto-sent
# email actually delivers to a real inbox in the tenant. The same recipient is reused
# across many accounts on purpose (there are only a few dozen real mailboxes, but
# hundreds of simulated accounts) — that is expected and fine for the POC.
#
# Curated from a live Microsoft Graph /users read: conference rooms, bots/agents,
# "AI" service identities, the CSMs/owner, and any user without a real ``mail`` were
# excluded. The first nine match the hand-written anchor accounts verbatim.
REAL_RECIPIENTS = [
    {"name": "Kai Carter", "email": "KaiC@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Lisa Taylor", "email": "LisaT@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Omar Bennett", "email": "OmarB@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Peyton Davis", "email": "PeytonD@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Quinn Campbell", "email": "QuinnC@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Dakota Sanchez", "email": "DakotaS@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Amber Rodriguez", "email": "AmberR@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Corey Gray", "email": "CoreyG@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Monica Thompson", "email": "MonicaT@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Adil Eli", "email": "AdilE@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Billie Vester", "email": "BillieV@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Daichi Maruyama", "email": "DaichiM@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Eka Siahaan", "email": "EkaS@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Hadar Caspit", "email": "HadarC@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Robin Kline", "email": "RobinK@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Ronak Gupta", "email": "RonakG@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Sasha Ouellet", "email": "SashaO@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Sonia Rees", "email": "SoniaR@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Sydney Mattos", "email": "SydneyM@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Teresa Sac", "email": "TeresaS@M365CPI81302533.OnMicrosoft.com"},
    {"name": "Vance DeLeon", "email": "VanceD@M365CPI81302533.OnMicrosoft.com"},
]

FEATURES = {
    "FlowDesk": [
        "Advanced Charting", "AI Research Summaries", "Watchlists", "News Monitoring",
        "Excel Add-in", "Portfolio Analytics", "Equity Screener", "Quote Monitor",
        "Real-Time Data", "Company Filings", "Economic Indicators", "Eikon Messenger",
    ],
    "CheckMate": [
        "Bulk Screening API", "Perpetual KYC Monitoring", "Case Management",
        "Watchlist Refresh", "Ongoing Monitoring", "Adverse Media Screening",
        "Risk Scoring", "Onboarding Screening", "Audit Trail", "Delta Alerts",
    ],
}

SEVERITIES = [("Critical", 5), ("High", 4), ("Medium", 3), ("Low", 2)]
SEVERITY_WEIGHTS = [0.08, 0.22, 0.40, 0.30]
SIGNAL_TYPES = ["adoption_gap", "release_relevant", "risk"]
SIGNAL_TYPE_WEIGHTS = [0.45, 0.35, 0.20]

DESCRIPTIONS = {
    "adoption_gap": [
        "{feature} licensed but used by only {a} of {b} seats.",
        "Logins down {pct}% over 30 days; {feature} never adopted after onboarding.",
        "{feature} adoption stalled — no active usage in {d} days.",
        "Team reverted to manual workflows instead of {feature}.",
        "Onboarding nudge: {feature} not configured {d} days after provisioning.",
    ],
    "release_relevant": [
        "New {feature} release matches a capability this customer asked about.",
        "{feature} now generally available; account is a heavy {product} user.",
        "Self-service {feature} just shipped and fits this team's workflow.",
        "{feature} update relevant to this account's stated priorities.",
    ],
    "risk": [
        "Repeated {feature} failures and an open support escalation {d} days before renewal.",
        "{feature} errors raised {a} tickets in {b} days; compliance team concerned.",
        "Intermittent {feature} failures putting the renewal at risk.",
        "Escalation on {feature} unresolved; sentiment deteriorating.",
    ],
}

MESSAGE_TYPES = {
    "adoption_gap": "guided_recovery_outreach",
    "release_relevant": "release_alert",
    "risk": "risk_intervention_brief",
}


def _load(name: str) -> list[dict]:
    path = DATA_DIR / f"{name}.json"
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write(name: str, rows: list[dict] | dict) -> None:
    path = DATA_DIR / f"{name}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _anchors(name: str) -> list[dict]:
    """Return only the preserved anchor rows from an existing fixture."""
    id_field, ids = ANCHORS[name]
    return [r for r in _load(name) if r.get(id_field) in ids]


def _weighted(rng: random.Random, options, weights):
    return rng.choices(options, weights=weights, k=1)[0]


def _date_within(rng: random.Random, start_days_ago: int, end_days_ago: int) -> str:
    days = rng.randint(end_days_ago, start_days_ago)
    return (TODAY - timedelta(days=days)).isoformat()


def _future_date(rng: random.Random, min_days: int, max_days: int) -> str:
    return (TODAY + timedelta(days=rng.randint(min_days, max_days))).isoformat()


def _user_id(name: str) -> str:
    parts = name.split()
    first, last = parts[0], parts[-1]
    return f"USR-{first.lower()}.{last.lower()}"


def generate() -> None:
    rng = random.Random(SEED)

    # ── accounts ────────────────────────────────────────────────────
    accounts = _anchors("accounts")
    used_names = {a["account_name"] for a in accounts}
    acc_seq = 1100

    owner_plan: list[str] = []
    for owner_id, count in NEW_ACCOUNTS_BY_OWNER.items():
        owner_plan.extend([owner_id] * count)
    rng.shuffle(owner_plan)

    csm_names = {
        "csm-svasireddy": "Siva Vasireddy",
        "csm-cora-thomas": "Cora Thomas",
        "csm-mario-rogers": "Mario Rogers",
        "unassigned": "",
    }

    def _new_name() -> str:
        for _ in range(200):
            style = rng.random()
            if style < 0.6:
                nm = f"{rng.choice(NAME_PREFIX)} {rng.choice(NAME_SUFFIX)}"
            else:
                nm = f"{rng.choice(NAME_PREFIX)} {rng.choice(NAME_CORE)} {rng.choice(NAME_SUFFIX)}"
            if nm not in used_names:
                used_names.add(nm)
                return nm
        nm = f"{rng.choice(NAME_PREFIX)} {rng.choice(NAME_CORE)} {acc_seq}"
        used_names.add(nm)
        return nm

    for owner_id in owner_plan:
        account_id = f"ACC-{acc_seq}"
        acc_seq += 1
        tier = "LongTail" if owner_id == "unassigned" else _weighted(rng, TIERS, TIER_WEIGHTS)
        strategic = "Yes" if tier == "Strategic" else "No"
        if tier == "Strategic":
            arr = rng.randint(1000, 2500) * 1000
        elif tier == "Growth":
            arr = rng.randint(300, 900) * 1000
        else:
            arr = rng.randint(20, 120) * 1000
        sentiment = _weighted(rng, SENTIMENTS, SENTIMENT_WEIGHTS)
        # Health correlates loosely with sentiment.
        if sentiment == "Frustrated":
            health = rng.randint(30, 58)
        elif sentiment == "Neutral":
            health = rng.randint(52, 78)
        else:
            health = rng.randint(72, 96)
        contact = rng.choice(REAL_RECIPIENTS)
        accounts.append({
            "account_id": account_id,
            "account_name": _new_name(),
            "tier": tier,
            "industry": rng.choice(INDUSTRIES),
            "region": rng.choice(REGIONS),
            "csm_manager_id": owner_id,
            "csm_name": csm_names[owner_id],
            "products": rng.choice(PRODUCT_SETS),
            "arr_gbp": arr,
            "health_score": health,
            "influence": "High" if strategic == "Yes" else _weighted(rng, INFLUENCE, INFLUENCE_WEIGHTS),
            "sentiment": sentiment,
            "renewal_date": _future_date(rng, 40, 560),
            "strategic": strategic,
            "primary_contact": contact["name"],
            "primary_contact_email": contact["email"],
            "primary_contact_title": rng.choice(TITLES),
            "onboarding_stage": rng.choice(ONBOARDING),
        })

    # ── signals (vast: multiple per account) ────────────────────────
    signals = _anchors("signals")
    anchor_signal_accounts = {s["account_id"] for s in signals}
    sig_seq = 6000

    def _user_for(account: dict) -> tuple[str, str]:
        return _user_id(account["primary_contact"]), account["primary_contact"]

    for account in accounts:
        # Anchor accounts already have their hand-written signals; still add a few
        # more so even the story accounts feel busy at scale.
        base = 0 if account["account_id"] in anchor_signal_accounts else 0
        # More signals for unhealthy / strategic / frustrated accounts.
        lo, hi = 2, 7
        if account["tier"] == "Strategic":
            lo, hi = 4, 12
        elif account["tier"] == "Growth":
            lo, hi = 3, 9
        if account["sentiment"] == "Frustrated":
            hi += 3
        n = rng.randint(lo, hi) + base
        # Signal actors are real demo-tenant people (the primary contact plus a
        # couple more), so every name in the data maps to a real user.
        users = [(_user_id(account["primary_contact"]), account["primary_contact"])]
        for _ in range(rng.randint(1, 3)):
            r = rng.choice(REAL_RECIPIENTS)
            users.append((_user_id(r["name"]), r["name"]))

        for _ in range(n):
            stype = _weighted(rng, SIGNAL_TYPES, SIGNAL_TYPE_WEIGHTS)
            severity, score = _weighted(rng, SEVERITIES, SEVERITY_WEIGHTS)
            product = rng.choice(account["products"])
            feature = rng.choice(FEATURES[product])
            user_id, user_name = rng.choice(users)
            tmpl = rng.choice(DESCRIPTIONS[stype])
            desc = tmpl.format(
                feature=feature, product=product, pct=rng.randint(35, 75),
                a=rng.randint(1, 3), b=rng.randint(4, 12), d=rng.randint(10, 90),
            )
            signal_id = f"SIG-{sig_seq}"
            sig_seq += 1
            signals.append({
                "signal_id": signal_id,
                "account_id": account["account_id"],
                "user_id": user_id,
                "user_name": user_name,
                "signal_type": stype,
                "product": product,
                "feature": feature,
                "severity": severity,
                "severity_score": score,
                "description": desc,
                "detected_date": _date_within(rng, 75, 1),
                "metric_value": rng.randint(0, 12),
                "threshold": rng.randint(1, 40),
                "status": _weighted(rng, ["new", "open"], [0.7, 0.3]),
            })

    # ── voc (customer feedback) ─────────────────────────────────────
    voc = _anchors("voc")
    voc_seq = 4000
    voc_sources = ["call_summary", "survey", "health_note", "support_ticket", "qbr_note"]
    voc_sentiments = ["Frustrated", "Negative", "Neutral", "Positive"]
    voc_texts = [
        "{name} asked whether {feature} could be tailored to their workflow.",
        "{name} raised concerns about {feature} reliability during the last review.",
        "{name} requested better onboarding material for {feature}.",
        "Team feedback: {feature} is valued but discovery is hard.",
        "{name} would expand usage of {feature} if it saved more manual effort.",
        "Survey: satisfaction dipped after a {feature} incident; recovery needed.",
    ]
    managed_accounts = [a for a in accounts if a["csm_manager_id"] != "unassigned"]
    for account in managed_accounts:
        for _ in range(rng.randint(0, 3)):
            product = rng.choice(account["products"])
            feature = rng.choice(FEATURES[product])
            user_id, user_name = _user_for(account)
            voc.append({
                "voc_id": f"VOC-{voc_seq}",
                "account_id": account["account_id"],
                "user_id": user_id,
                "source": rng.choice(voc_sources),
                "date": _date_within(rng, 150, 5),
                "sentiment": rng.choice(voc_sentiments),
                "text": rng.choice(voc_texts).format(name=user_name.split()[0], feature=feature),
                "feature_requested": feature,
            })
            voc_seq += 1

    # ── px engagement (what was already shown) ──────────────────────
    px = _anchors("px_engagement")
    px_seq = 5000
    px_actions = ["viewed", "clicked", "dismissed", "completed", "ignored"]
    for account in accounts:
        for _ in range(rng.randint(0, 2)):
            product = rng.choice(account["products"])
            feature = rng.choice(FEATURES[product])
            user_id, _ = _user_for(account)
            px.append({
                "engagement_id": f"PX-{px_seq}",
                "account_id": account["account_id"],
                "user_id": user_id,
                "content_id": f"CNT-{rng.randint(9001, 9006)}",
                "content_title": f"{feature} — in-product guide",
                "shown_date": _date_within(rng, 160, 3),
                "action": rng.choice(px_actions),
            })
            px_seq += 1

    # ── review queue (seeded HITL backlog under volume) ─────────────
    review = _anchors("review_queue")
    rq_seq = 7000
    # Build a quick index of an open, review-worthy signal per managed account.
    sig_by_account: dict[str, list[dict]] = {}
    for s in signals:
        sig_by_account.setdefault(s["account_id"], []).append(s)
    # Weight toward Siva (largest book) so her cockpit shows a deep queue.
    review_targets = [a for a in managed_accounts]
    rng.shuffle(review_targets)
    review_count = 0
    for account in review_targets:
        if review_count >= 130:
            break
        cand = [s for s in sig_by_account.get(account["account_id"], [])
                if int(s.get("severity_score", 0)) >= 3]
        if not cand:
            continue
        # 1-2 review items per account, only some accounts.
        if rng.random() < 0.55:
            continue
        for s in rng.sample(cand, k=min(len(cand), rng.randint(1, 2))):
            stype = s["signal_type"]
            priority = "High" if int(s["severity_score"]) >= 4 else "Medium"
            review.append({
                "item_id": f"RQ-{rq_seq}",
                "account_id": account["account_id"],
                "csm_manager_id": account["csm_manager_id"],
                "priority": priority,
                "status": "pending",
                "message_type": MESSAGE_TYPES[stype],
                "channel": "csm_review" if stype != "risk" else "csm_brief",
                "draft_text": "",
                "created_date": _date_within(rng, 12, 0),
                "signal_id": s["signal_id"],
            })
            rq_seq += 1
            review_count += 1

    # ── enhancements (release catalogue) ────────────────────────────
    enhancements = _anchors("enhancements")
    enh_seq = 7100
    release_types = ["GA", "Update", "Preview", "Beta"]
    audiences = ["Compliance", "Research", "Operations", "Trading", "Risk", "Onboarding"]
    complexities = ["Low", "Medium", "High"]
    for product, feats in FEATURES.items():
        for feature in feats:
            if rng.random() < 0.5:
                continue
            enhancements.append({
                "enhancement_id": f"ENH-{enh_seq}",
                "product": product,
                "feature_area": feature,
                "release_type": rng.choice(release_types),
                "audience": rng.choice(audiences),
                "complexity": rng.choice(complexities),
                "self_service": _weighted(rng, ["Yes", "No"], [0.55, 0.45]),
                "title": f"{feature} Enhancements",
                "description": f"Improvements to {feature} in {product}, including usability and reliability updates.",
                "release_date": _date_within(rng, 120, 2),
                "matches_request_tag": _weighted(rng, ["Yes", "No"], [0.25, 0.75]),
            })
            enh_seq += 1

    # ── content library (approved blocks per source) ───────────────
    content = _anchors("content_library")
    cnt_seq = 9100
    sources = {
        "content_feature_tip": "feature_tip",
        "content_release_alert": "release_alert",
        "content_onboarding_nudge": "onboarding_nudge",
        "playbook_adoption_recovery": "guided_recovery_outreach",
        "playbook_renewal_risk": "risk_intervention_brief",
    }
    for source, mtype in sources.items():
        for product, feats in FEATURES.items():
            for feature in rng.sample(feats, k=2):
                content.append({
                    "content_id": f"CNT-{cnt_seq}",
                    "content_source": source,
                    "product": product,
                    "feature": feature,
                    "message_type": mtype,
                    "title": f"{feature} — {mtype.replace('_', ' ').title()}",
                    "body": (
                        f"Approved talking points for {feature} ({product}): "
                        "acknowledge the customer's context, offer a tailored next step, "
                        "and reference only generally-available capabilities. Keep it concise and human."
                    ),
                    "approved": "Yes",
                    "last_reviewed": _date_within(rng, 90, 5),
                })
                cnt_seq += 1

    # ── csm voice (style anchors per CSM, all message types) ────────
    csm_voice = _anchors("csm_voice")
    cv_seq = 3000
    voice_extra = {
        "onboarding_nudge": "Hi {first_name}, welcome aboard — just a nudge that getting {feature} set up early really pays off. Want me to send a 2-minute starter? Best, {csm}",
        "risk_intervention_brief": "Hi {first_name}, I'd like to grab 30 minutes to walk through {feature} and make sure we're on the same page ahead of renewal. No slides — just a practical conversation. {csm}",
    }
    for mid, cname in (("csm-svasireddy", "Siva Vasireddy"),
                       ("csm-cora-thomas", "Cora Thomas"),
                       ("csm-mario-rogers", "Mario Rogers")):
        for mtype, text in voice_extra.items():
            csm_voice.append({
                "voice_id": f"CV-{cv_seq}",
                "csm_manager_id": mid,
                "csm_name": cname,
                "channel": "email",
                "message_type": mtype,
                "text": text.replace("{csm}", cname.split()[0]),
                "accepted_date": _date_within(rng, 120, 10),
            })
            cv_seq += 1

    # ── signal_action_map: ensure coverage for all type/severity combos ─
    sam = _load("signal_action_map")
    have = {(r["signal_type"], r["severity"]) for r in sam}
    extra_map = [
        ("adoption_gap", "High", "guided_recovery_outreach", "csm_review", "playbook_adoption_recovery", "Yes",
         "High adoption gap needs CSM judgment."),
        ("risk", "Critical", "risk_intervention_brief", "csm_brief", "playbook_renewal_risk", "Yes",
         "Critical risk always escalates to a CSM-led conversation."),
        ("risk", "Medium", "risk_intervention_brief", "csm_review", "playbook_renewal_risk", "Yes",
         "Medium risk is reviewed before any outreach."),
        ("risk", "Low", "feature_tip", "email", "content_feature_tip", "No",
         "Low risk handled with a light-touch tip."),
        ("release_relevant", "Low", "release_alert", "in_product", "content_release_alert", "No",
         "Low-relevance release surfaced in-product."),
        ("release_relevant", "Critical", "release_alert", "csm_review", "content_release_alert", "Yes",
         "Critical release relevance routed to the CSM."),
    ]
    map_seq = 10
    for stype, sev, mtype, channel, source, review_req, note in extra_map:
        if (stype, sev) in have:
            continue
        sam.append({
            "map_id": f"MAP-{map_seq:02d}",
            "signal_type": stype,
            "severity": sev,
            "message_type": mtype,
            "channel": channel,
            "content_source": source,
            "review_required": review_req,
            "notes": note,
        })
        map_seq += 1

    # ── rebuild managers[].accounts to match accounts.json ──────────
    managers = _load("managers")
    for m in managers:
        m["accounts"] = [a["account_id"] for a in accounts if a["csm_manager_id"] == m["manager_id"]]

    # ── Work IQ (Microsoft 365) OFFLINE grounding ───────────────────
    # The manager's real-feeling relationship history with each account's contact —
    # recent emails, meetings and Teams messages. This is the *offline-dev fallback*
    # (data/workiq.json) that powers personalised drafts when no live Work IQ MCP
    # endpoint is configured; the deployed agent uses the REAL Work IQ server on the
    # manager's OBO token. Keyed to the real contacts + the assigned CSM so the draft
    # can open on a genuine touchpoint ("following up on our call about …").
    mgr_by_id = {m["manager_id"]: m for m in managers}
    top_signal: dict[str, dict] = {}
    for s in signals:
        cur = top_signal.get(s["account_id"])
        if cur is None or int(s.get("severity_score", 0)) > int(cur.get("severity_score", 0)):
            top_signal[s["account_id"]] = s

    email_templates = [
        ("{feature} — quick question from the team",
         "Hi {csm_first}, we hit a snag with {feature} this week and the team is asking — could we grab 20 minutes? It's getting urgent ahead of our planning cycle."),
        ("Re: {feature} rollout",
         "Thanks for the steer on {feature}. We tried it with a couple of users and have a few questions before we roll it out more widely."),
        ("Following up after our call",
         "Good to speak earlier. As promised I'm noting where we got to on {feature} — keen to keep the momentum going on our side."),
    ]
    meeting_templates = [
        ("{account} — {feature} working session",
         "Walked through {feature}; agreed next steps and a follow-up. The team is keen but wants some hand-holding to embed it."),
        ("{account} quarterly check-in",
         "Covered {feature} adoption, open risks and the renewal timeline. Overall sentiment {sentiment}."),
    ]
    teams_templates = [
        "Hi {csm_first}, did the {feature} fix land? The team keeps asking me.",
        "Thanks {csm_first} — that {feature} tip really helped, we'll try it today.",
        "Quick one: could you send over the {feature} guide you mentioned?",
        "Morning {csm_first}, are we still on for the {feature} session this week?",
    ]

    wiq_emails: list[dict] = []
    wiq_meetings: list[dict] = []
    wiq_teams: list[dict] = []
    wiq_people: list[dict] = []
    eml_seq = mtg_seq = tm_seq = 1
    for account in accounts:
        mid = account["csm_manager_id"]
        if mid == "unassigned":
            continue  # long-tail accounts have no dedicated CSM relationship
        mgr = mgr_by_id.get(mid, {})
        csm_first = (mgr.get("display_name", "your CSM").split() or ["your CSM"])[0]
        csm_email = mgr.get("upn", "customer.success@example.com")
        sig = top_signal.get(account["account_id"], {})
        feature = sig.get("feature") or "your product tools"
        contact = account["primary_contact"]
        contact_first = contact.split()[0]
        contact_email = account["primary_contact_email"]
        acc_name = account["account_name"]

        wiq_people.append({
            "id": f"PPL-{account['account_id']}",
            "name": contact, "title": account.get("primary_contact_title", ""),
            "account_id": account["account_id"], "email": contact_email,
        })
        for _ in range(rng.randint(1, 2)):
            subj, body = rng.choice(email_templates)
            wiq_emails.append({
                "id": f"EML-{eml_seq:04d}",
                "from": contact_email, "to": csm_email,
                "subject": subj.format(feature=feature, first=contact_first),
                "received": _date_within(rng, 30, 1) + "T09:15:00Z",
                "snippet": body.format(csm_first=csm_first, feature=feature),
                "account_id": account["account_id"],
            })
            eml_seq += 1
        if rng.random() < 0.7:
            subj, notes = rng.choice(meeting_templates)
            wiq_meetings.append({
                "id": f"MTG-{mtg_seq:04d}",
                "subject": subj.format(account=acc_name, feature=feature),
                "start": _date_within(rng, 25, 2) + "T10:00:00Z",
                "attendees": [csm_email, contact_email],
                "account_id": account["account_id"],
                "notes": notes.format(feature=feature, sentiment=str(account.get("sentiment", "neutral")).lower()),
            })
            mtg_seq += 1
        if rng.random() < 0.6:
            wiq_teams.append({
                "id": f"TM-{tm_seq:04d}",
                "from": contact, "to": mgr.get("display_name", "CSM"),
                "sent": _date_within(rng, 14, 0) + "T13:30:00Z",
                "text": rng.choice(teams_templates).format(csm_first=csm_first, feature=feature),
                "account_id": account["account_id"],
            })
            tm_seq += 1

    workiq = {"emails": wiq_emails, "meetings": wiq_meetings, "teams": wiq_teams, "people": wiq_people}

    # ── write everything ────────────────────────────────────────────
    _write("accounts", accounts)
    _write("signals", signals)
    _write("voc", voc)
    _write("px_engagement", px)
    _write("review_queue", review)
    _write("enhancements", enhancements)
    _write("content_library", content)
    _write("csm_voice", csm_voice)
    _write("signal_action_map", sam)
    _write("managers", managers)
    _write("workiq", workiq)

    # ── summary ──────────────────────────────────────────────────────
    by_owner: dict[str, int] = {}
    for a in accounts:
        by_owner[a["csm_manager_id"]] = by_owner.get(a["csm_manager_id"], 0) + 1
    print("Generated scale sample data:")
    print(f"  accounts        : {len(accounts)}  {by_owner}")
    print(f"  signals         : {len(signals)}")
    print(f"  voc             : {len(voc)}")
    print(f"  px_engagement   : {len(px)}")
    print(f"  review_queue    : {len(review)} (pending HITL)")
    print(f"  enhancements    : {len(enhancements)}")
    print(f"  content_library : {len(content)}")
    print(f"  csm_voice       : {len(csm_voice)}")
    print(f"  signal_action_map: {len(sam)}")
    print(f"  workiq (M365)   : {len(wiq_emails)} emails, {len(wiq_meetings)} meetings, "
          f"{len(wiq_teams)} teams, {len(wiq_people)} people")


if __name__ == "__main__":
    generate()
