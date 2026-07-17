# Reusable Architecture

## Stable Layers

1. **Ingress**: Microsoft 365 Agents SDK activities, ASGI APIs, MCP, scheduled events, and queues.
2. **Identity**: verified principal, assigned manager, fleet roles, manager OBO, agentic user, and downstream
   credential selection.
3. **Orchestration**: spec-driven workflows, deterministic conditions, idempotency, and review.
4. **Capabilities**: one typed registry for skills, data, MCP, fixed OpenAPI operations, rules,
   generation, and actions across all surfaces.
5. **Adapters**: MCP, HTTP, database, event bus, model, state store, and directory protocols.
6. **Experience**: manager/fleet APIs and Teams tabs, MCP Apps resources, and AG-UI state/events
  generated from terminology, workflow state, and UI metadata.
7. **Governance**: policy evaluation, audit, cost, traces, and operational health.

## Runtime Shape

The generated solution has three processes and two public security boundaries:

- Microsoft 365 Agents SDK aiohttp host: `POST /api/messages`
- FastAPI business control plane: manager/fleet APIs and pages
- JWT-protected FastMCP streamable-HTTP facade: `/mcp`

The Agent SDK gateway and FastAPI control-plane sidecar deploy in one Container App and share one
public FQDN. Only `/api/messages` passes through the Agents SDK JWT middleware; fixed tab/API/AG-UI
routes proxy to FastAPI, where Teams SSO is validated. This shared host is mandatory because Teams
ETS requires `webApplicationInfo.resource` and the blueprint identifier URI to use the tab domain.
MCP remains a separate public Container App behind the Agent 365 Tooling Gateway.

The Agent host constructs `MemoryStorage`, `MsalConnectionManager`, `CloudAdapter`,
`Authorization`, and `AgentApplication` once at import time. A365 Observability is configured
before importing that module. Each turn uses `InvokeAgentScope`; every tool uses
`ExecuteToolScope`; responses/errors are recorded and spans are flushed in `finally`.

## Control Plane

The generated FastAPI application exposes:

- `/health` and `/api/spec`
- `/api/me`
- `/api/manager/summary`
- `/api/fleet/summary`
- `/api/workflows`
- `POST /api/workflows/{workflow_id}/runs`
- `/api/runs/{run_id}`
- `/api/reviews` and `POST /api/reviews/{review_id}`
- `/api/ui/resources`, `/api/ui/{resource_id}/query`, and `POST /api/ag-ui`
- `/manager` and `/fleet`

The pages render generated dashboard/HITL definitions through AG-UI. MCP-visible definitions are
also published as MCP Apps resources linked by `_meta.ui.resourceUri`. App-only approval commands
use `_meta.ui.visibility: ["app"]`; every decision is still manager-scoped and digest-validated.

## Production Rules

- Do not create one runtime deployment per manager. Manager/agent instances are identity and
  governance boundaries. The generated deployment represents one Agent ID assigned to one manager;
  fleet reads may aggregate shared state, but cannot mutate another manager's instance.
- Do not horizontally scale process-local sessions, runs, reviews, or files. Replace the generated
  SQLite store with a shared durable implementation first.
- Use queues or Container Apps Jobs for portfolio fan-out. A request handler must not create an
  unbounded number of in-process tasks.
- Side effects require an idempotency key, durable status, retry policy, and dead-letter handling.
- Human review approves an exact capability/input digest; edited payloads are revalidated and
  replace the proposed effect. Fleet governance roles are read-only.
- Manager OBO is allowed only for delegated manager-owned resources and requires a live signed-in
  turn. Agentic-user federation is used for autonomous actions authored by the teammate. Neither
  identity may silently fall back to the other.
- Production control-plane and MCP ingress validate signed tokens. Direct raw MCP access must not
  bypass Agent 365 Tooling Gateway governance.
- The Teams package is a fresh tabs-only catalog app, never the Agent 365 app ID. It points SSO at
  the A365 blueprint, calls `app.initialize()`/`notifySuccess()` early, uses ETS/getAuthToken, and
  requires the domain-qualified blueprint URI plus preauthorized Teams clients configured by the
  A365 endpoint/permission flow. Production has no unsigned browser-session fallback.
- OpenAPI tools are fixed at generation time. Never expose a caller-controlled URL, host, method,
  auth header, or raw query string.
- Cache MCP discovery, schemas, and tokens within their safe lifetimes. Bound every network call
  with a timeout and retry only transient failures.
- Store secrets in the deployment platform's secret store or Key Vault and access Azure resources
  with managed identity. Never generate shared keys or SAS tokens.
- Keep deterministic policy out of model prompts. The model may summarize or draft after policy
  and approved grounding are resolved.
- Capture actual model usage from provider responses; do not estimate production cost from text
  length alone.

## Generated Versus Environment-Provisioned

The scaffold implements the Agent SDK host, dual identity adapters, A365 observability, generated
skills/tools, remote MCP client, fixed-operation OpenAPI client, governed FastMCP facade, dynamic
control planes, workflows/review/idempotency, Agent 365 package, and deployment assets. The A365
CLI must still provision tenant-specific blueprint/instance identities, permissions, and consent;
the generator deliberately leaves those IDs blank. Scenario integrations remain offline until
their endpoint and identity environment settings are supplied. Preview Agent 365 and MCP contracts
must be revalidated against pinned packages and current official samples whenever versions change.
