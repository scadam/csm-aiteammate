# __PROJECT_NAME__

__PROJECT_DESCRIPTION__

This project is a complete generated Agent 365 AI teammate example. The scenario, persona,
skills, typed tools, workflows, review policy, remote MCP servers, selected OpenAPI operations,
manager vocabulary, and fleet vocabulary come from `solution.yaml`.

## Runtime Surfaces

| Process | Command | Default URL |
|---|---|---|
| Microsoft 365 Agents SDK host | `python -m app.agent_main` | `http://127.0.0.1:3978/api/messages` |
| Business control plane | `uvicorn app.main:app --port 8000` | `http://127.0.0.1:8000/manager` |
| Governed FastMCP facade | `python -m app.mcp_server` | `http://127.0.0.1:8001/mcp` |

All three surfaces use the same capability registry. Agent tools, workflows, and MCP tools
therefore share Pydantic validation, authorization policy, A365 tool telemetry, review gates,
and durable idempotency.

The generated `appPackage/` is a fresh tabs-only Teams app. In Azure, the Agent SDK gateway and
FastAPI control plane run as sidecars behind one public FQDN; MCP remains separate. The gateway
proxies only fixed tab/API/AG-UI paths, preserving SSE, and applies Bot Framework JWT middleware
only to `/api/messages`. FastAPI validates Teams ETS tokens independently.

## Local Verification

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item env.TEMPLATE .env
pytest -q
```

The template starts in explicit offline development mode. Offline MCP/OpenAPI/model results
carry `offline:*` provenance and make no network calls. `/health` reports `offline`; the UI must
not be interpreted as a live Agent 365 deployment.

Run all processes locally with:

```powershell
docker compose up --build
```

## Agent 365 Setup

Do not create Entra applications, Agent IDs, permissions, or consent manually. Install/update
the A365 CLI and run the generated bootstrap immediately after scaffolding:

```powershell
python scripts/provision_agent365.py early
```

The runner executes requirements, blueprint creation (`--no-endpoint --m365`), MCP permissions,
and bot permissions in order. It writes safe IDs/status to `.a365/provisioning-state.json`, never
invokes `--show-secret`, and redacts secret-shaped output. If Agent ID Developer or Global
Administrator action is required, it records the CLI's admin-consent URL as a resumable checkpoint.

Verify blueprint permissions and inheritance before deployment:

```powershell
python scripts/provision_agent365.py verify
```

The generated Agent 365 package templates are under `manifest/`. They deliberately contain
`${A365_BLUEPRINT_APP_ID}` and `${A365_AGENTIC_TEMPLATE_ID}` rather than fabricated identities.
After `a365 setup blueprint`, set `A365_BLUEPRINT_APP_ID` from the CLI output; optionally set a
stable `A365_AGENTIC_TEMPLATE_ID` or let the renderer create one, then run:

```powershell
python scripts/render_agent365_manifest.py
```

After deploying the shared Agent/control-plane host and the MCP host, finish the lifecycle and
verify instance grants. Register the endpoint before Teams packaging so A365 can establish the
domain-qualified blueprint identifier URI required by ETS:

```powershell
python scripts/provision_agent365.py endpoint --url https://<agent-host>/api/messages
python scripts/provision_agent365.py publish --control-plane-url https://<agent-host>
atk provision --env dev -i false
atk publish --env dev -i false
python scripts/provision_agent365.py verify --instance
```

Preview contracts can change; rerun the generated contract tests after every SDK/CLI upgrade.

## Identity Model

- **Manager OBO** is mandatory for manager-owned delegated resources such as Work IQ. It requires
  a signed-in Agent SDK turn and never falls back to agentic-user or managed identity.
- **Agentic user** is the teammate's own first-class Agent ID. It supports autonomous work without
  an incoming human turn through `get_agentic_user_token(tenant, instance, user, scopes)`.
- **Managed identity** is used for Azure OpenAI and other Azure resources. No Azure access keys,
  storage account keys, connection-string keys, or SAS tokens are generated.
- **Native/bearer credentials** are injected only through named environment references.

The control plane validates Entra JWTs outside development mode. The public MCP facade requires
signature, issuer, audience, expiry, and scope validation outside explicit local development.
Each deployed Agent ID accepts mutations only from its assigned manager. Fleet principals may
read portfolio data but cannot start workflows or approve another manager's effects.

Teams calls `app.initialize()`/`notifySuccess()` immediately and acquires the signed-in manager
token with `authentication.getAuthToken()`. The server validates signature, expiry, tenant,
issuer, exact blueprint audience, `access_agent_as_user`, and the official Teams caller IDs.
Production never falls back to development headers or an unsigned browser session.

## Interactive UI

`solution.yaml` drives generated dashboards and HITL inboxes. The FastMCP facade publishes
declared manager views as MCP Apps (`ui://`, `text/html;profile=mcp-app`), while FastAPI exposes an
AG-UI HTTP/SSE endpoint for state, activity, lifecycle, and review interrupts. The Teams tabs use
the same AG-UI state model and provide filtering, sortable columns, pagination, row decisions, and
digest-bound bulk approval/reject/defer. The app-only review tool is hidden from the model.

## Agent 365 Observability

The Agent host configures `microsoft_agents_a365.observability.core` before importing the
`AgentApplication`. Every turn uses `InvokeAgentScope`; every capability uses `ExecuteToolScope`.
Responses/errors are recorded, exporter tokens are cached by exact `(agent_id, tenant_id)`, and
telemetry is flushed in a `finally` block.

Both variables must be true for authenticated export:

```dotenv
ENABLE_A365_OBSERVABILITY=true
ENABLE_A365_OBSERVABILITY_EXPORTER=true
```

The exporter token resolver returns a bare token, never a `Bearer `-prefixed value.

## Skills, MCP, And OpenAPI

Generated skills are in `app/generated_skills/<skill>/SKILL.md`. The Azure OpenAI reasoning path
surfaces their compact catalog and loads full instructions through `get_skill`. The optional
Copilot SDK extra (`pip install -e ".[dev,copilot]"`) provides an optional local development
runtime with native skill discovery. The deployable Agent SDK host always uses Azure OpenAI with
managed identity so Linux images never depend on a platform-specific Copilot wheel.

Existing MCP servers use streamable HTTP and the exact identity/scopes declared in
`solution.yaml`. OpenAPI tools are generated only for selected fixed operations. They reject
caller-controlled hosts/methods, non-HTTPS URLs, unapproved hosts, private/reserved addresses,
redirects, unsafe path parameters, non-JSON responses, and oversized responses.

Render the environment-specific BYO registration only after the MCP server has a public HTTPS URL:

```powershell
python scripts/render_byo_registration.py
a365 develop-mcp register-external-mcp-server -f scripts/byo-mcp-registration.json
```

An IT administrator must approve the BYO server. Production agent clients consume it through the
Agent 365 Tooling Gateway, not the raw MCP URL.

## Deployment

`infra/main.bicep` deploys two Azure Container Apps with one user-assigned managed identity. The
Agent SDK and FastAPI run as separate sidecar containers in the shared public host; FastMCP runs
in the second app behind the Tooling Gateway.
Build and publish `Dockerfile.agent`, `Dockerfile.controlplane`, and `Dockerfile.mcp`, then provide
the image references and Azure OpenAI endpoint as Bicep parameters. Grant the managed identity
least-privilege data-plane roles separately for the selected scenario resources.

The generated SQLite state/effect ledger is suitable for one local replica only. Before scaling
horizontally, production automatically requires the generated managed-identity Azure Table store.
The Agent SDK host remains at one replica until OAuth/conversation state is moved to shared storage.

## Readiness

`GET /health` reports each subsystem separately: Agent SDK service connection, manager OBO,
agentic-user identity, A365 observability, reasoning, MCP authentication, Tooling Gateway, and
state store. `status: ready` means every required live check is configured; `offline` and
`degraded` are intentionally visible states.
