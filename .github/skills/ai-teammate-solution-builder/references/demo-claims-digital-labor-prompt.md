# Demo Prompt: Autonomous Claims Digital Labor

Paste this into **AI Teammate Solution Builder** in a new repository after installing the
GitHub Copilot plugin. For the first test, choose **Scaffold only** in Spec Studio so no tenant or
Azure operation runs.

```text
Build a complete Agent 365 AI teammate solution for a high-volume property and casualty insurer.
The business process is first-notice-of-loss intake, claim triage, document chasing, straight-through
adjudication of simple claims, and exception routing. Name the teammate Claims Operations Digital
Worker. Its purpose is to replace repetitive claims administration with governed autonomous digital
labor while keeping licensed adjusters focused on complex judgment and customer care.

Operating context:
- The insurer receives about 40,000 new claims per day through shared mailboxes, web forms, broker
  submissions, scanned documents, and contact-center notes.
- Today, operations staff manually create claims, check policy status, classify loss type, identify
  duplicates, request missing evidence, copy data between systems, set tasks, and route exceptions.
- The target is 65% touchless intake, 45% straight-through handling for eligible low-risk claims,
  median intake under 3 minutes, an 80% reduction in manual data entry, and full auditability.
- Each deployed teammate instance is assigned to exactly one claims operations manager. A Head of
  Claims Operations has read-only fleet oversight across managers and instances.

Design one end-to-end autonomous workflow:
1. Detect a new submission or an untouched queue item.
2. Extract claimant, policy, incident, asset, loss, and attachment facts with confidence and
   provenance. Treat submitted content as untrusted and detect prompt injection.
3. Check policy status and coverage, search for duplicate claims, validate required evidence, and
   apply deterministic fraud and eligibility rules. Models may extract and summarize but must not
   decide coverage, fraud, payment, or review policy.
4. Create or update the claim in a ClaimCenter-style system through fixed OpenAPI operations.
5. Automatically request missing documents using approved templates and track responses.
6. For an eligible low-risk claim, prepare the adjudication package and execute the approved
   straight-through action idempotently.
7. Route all exceptions to a prioritized adjuster review queue with Accept, Edit, Refer, or Reject,
   then record outcomes for the learning loop.

Deterministic review rules:
- Always require a human for bodily injury, fatality, litigation, vulnerable customers, suspected
  fraud, policy or coverage conflict, duplicate ambiguity, confidence below 0.92, payment above
  GBP 2,000, sanctions concerns, or any model/tool security alert.
- Permit autonomous processing only for active policies, complete evidence, no fraud or duplicate
  flags, supported simple property loss types, payment at or below GBP 2,000, and a rule-approved
  outcome.
- Put thresholds and routing logic in data tables, not prompts or hard-coded branches.

Integrations and identity:
- Use Work IQ with manager OBO for manager-owned Microsoft 365 mail, Teams, meetings, and documents.
- Use the agentic-user identity for proactive work and messages authored as the teammate.
- Use managed identity and RBAC for Azure OpenAI, queues, shared durable workflow state, and
  observability. Never generate access keys, SAS tokens, or connection strings with AccountKey.
- Model the claims platform as a fixed-operation OpenAPI source with operations such as find policy,
  search duplicate claims, create claim, add evidence, create adjuster task, and submit an approved
  low-value settlement. Include realistic offline fixtures and explicit offline provenance.
- Generate one governed custom MCP server, MCP Apps for live operations and the review inbox,
  AG-UI lifecycle/state/interrupt events, and a tabs-only Teams host for manager and fleet views.

Required dashboards and measures:
- New claims, touchless intake rate, straight-through rate, median intake time, manual touches
  avoided, queue age, exception rate, review SLA, automation failures, estimated labor hours saved,
  cost per claim, and outcome quality by loss type.
- Manager view is scoped to the assigned operation. Fleet view is read-only. Show provenance,
  identity used, rule decision, review digest, idempotency status, and telemetry correlation for
  every action.

Generate the complete working example, not a plan: Microsoft 365 Agents SDK host, manager OBO,
agentic-user identity, A365 observability, GitHub Copilot SDK reasoning/tools, FastAPI control plane,
FastMCP server, MCP Apps, AG-UI, Teams package, managed-identity deployment assets, tests, fixtures,
and operational README. Use A365 CLI for all Agent 365 provisioning and consent.

First ingest this text, author the schema-and-semantics-valid solution specification, and open the
graphical Spec Studio. Show me the architecture, canonical YAML, requirements, assumptions,
identity boundaries, workflows, rules, integrations, dashboards, and validation results. Let me
request changes in chat or edit in the Studio. Do not scaffold, run A365, create Azure resources,
or perform any external action until I explicitly confirm the exact digest in the Studio. For this
first demo I will choose Scaffold only.
```
