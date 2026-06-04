# Copilot Instructions — PMO AI Teammate (Agent 365 style)

These instructions tell GitHub Copilot how to build and extend this repository. Follow
them for every change. They describe an **AI Teammate** agent: a digital project manager
that has **its own identity** but always acts **on behalf of its manager**.

> **Read this first.** The Microsoft 365 Agents SDK and the GitHub Copilot SDK are **new
> and in Public Preview**. APIs change and are easy to get wrong. **Do not invent SDK
> APIs.** Only use the import paths, class names, and call signatures that are shown in
> this document — they are copied verbatim from the official Microsoft samples cited in
> [References](#references). If something you need is not documented here, find it in the
> referenced samples before writing code, and keep this file updated.

---

## 1. What we are building

An **AI Teammate** — a persona-driven agent that behaves like a member of a team (here, a
**digital project manager / PMO** teammate). Each deployed **agent instance**:

- Has **its own agent identity** (an Agent 365 / Microsoft Entra **Agent ID**). It is a
  first-class actor, not an anonymous bot. It introduces itself by its own name and role.
- Is assigned to exactly **one manager** (a human user). The agent acts **on behalf of**
  that manager using **On-Behalf-Of (OBO)** token exchange, so every downstream call
  carries the manager's delegated permissions, not a generic app identity.
- Runs as a **Microsoft 365 Agent** built with the **Microsoft 365 Agents SDK (Python)**.
- Uses the **GitHub Copilot SDK** for its reasoning/LLM loop and tool-calling.
- Exposes and consumes capabilities through **MCP (Model Context Protocol)**. The **MCP
  server is registered on the Agent 365 (A365) Tooling Gateway**.
- **Simulates all back-end systems with static JSON** (no real external systems yet).
- **Emits OpenTelemetry (OTEL)** to the **A365 observability endpoint**.

### Identity model (core principle)

```
Manager (human user, has an Entra identity)
        │  signs in once → user token
        ▼
AI Teammate agent  ── has its OWN Entra Agent ID (its identity)
        │  OBO token exchange (acts as the manager, with the agent's identity)
        ▼
Downstream tools / MCP servers / simulated back ends
```

- The agent's **own identity** is used to authenticate the agent itself and to register on
  the A365 Tooling Gateway and to emit telemetry.
- The **manager's delegated authority** is obtained through OBO and is what authorizes any
  action the agent takes "for" the manager. **Never** perform a manager-scoped action with
  only the agent's app identity; always exchange for an OBO token first.

---

## 2. Technology stack and exact dependencies

Use **Python 3.10+**. The project is hosted as an **aiohttp** web service exposing
`POST /api/messages`, exactly like the Microsoft samples.

`requirements.txt` should contain (names are exact; the SDK packages use **hyphens** on
PyPI but are **imported with underscores** as `microsoft_agents.*`):

```text
python-dotenv
aiohttp
pydantic

# Microsoft 365 Agents SDK (hosting + auth + activity)
microsoft-agents-activity
microsoft-agents-hosting-core
microsoft-agents-hosting-aiohttp
microsoft-agents-authentication-msal

# GitHub Copilot SDK (PyPI: github-copilot-sdk, imported as `copilot`)
github-copilot-sdk

# MCP
mcp

# OpenTelemetry (OTLP/gRPC export to the A365 observability endpoint)
opentelemetry-api
opentelemetry-sdk
opentelemetry-exporter-otlp
opentelemetry-instrumentation
opentelemetry-instrumentation-aiohttp-server
opentelemetry-instrumentation-aiohttp-client
opentelemetry-instrumentation-requests
opentelemetry-instrumentation-logging
```

> **Import name vs package name:** `github-copilot-sdk` is imported as `from copilot
> import ...`. All `microsoft-agents-*` packages are imported as `microsoft_agents.*`
> (underscores, **not** `microsoft.agents`).

---

## 3. Project structure

Mirror the Microsoft sample layout. Run the agent with `python -m src.main`.

```text
src/
  main.py            # Entry point: configure OTEL FIRST, then build app, then start server
  agent.py           # AgentApplication + Copilot SDK wiring + handlers + OBO
  start_server.py    # aiohttp hosting: POST /api/messages
  telemetry.py       # configure_otel_providers() — OTLP export to A365 observability
  identity.py        # Agent identity + manager resolution helpers (OBO)
  copilot_session.py # Copilot SDK client/session lifecycle (optional split from agent.py)
  mcp/
    server.py        # MCP server exposing this agent's tools (registered on A365 Gateway)
    gateway.py       # A365 Tooling Gateway registration of the MCP server
  tools/             # Individual tool implementations (Pydantic params + @define_tool)
    __init__.py
    <capability>.py
data/                # Static JSON that SIMULATES all back-end systems
  projects.json
  tasks.json
  ...
.env                 # From env.TEMPLATE; never commit real secrets
env.TEMPLATE
requirements.txt
```

- `src/` is run as a package via `python -m src.main`. Keep imports relative within `src`
  (e.g. `from .agent import AGENT_APP`).
- Keep **all simulated back-end data in `data/*.json`** and load it through a thin
  repository/service layer so it can later be swapped for real systems without touching
  tool or agent logic.

---

## 4. Microsoft 365 Agents SDK pattern (verbatim — do not deviate)

### 4.1 Building the agent application (`agent.py`)

```python
from os import environ
from dotenv import load_dotenv

from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.hosting.core import (
    Authorization,
    AgentApplication,
    TurnState,
    TurnContext,
    MemoryStorage,
)
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.activity import load_configuration_from_env

load_dotenv()
agents_sdk_config = load_configuration_from_env(environ)

STORAGE = MemoryStorage()
CONNECTION_MANAGER = MsalConnectionManager(**agents_sdk_config)
ADAPTER = CloudAdapter(connection_manager=CONNECTION_MANAGER)
AUTHORIZATION = Authorization(STORAGE, CONNECTION_MANAGER, **agents_sdk_config)

AGENT_APP = AgentApplication[TurnState](
    storage=STORAGE, adapter=ADAPTER, authorization=AUTHORIZATION, **agents_sdk_config
)
```

These objects are **module-level globals constructed once at import time**. Do not
recreate them per request.

### 4.2 Handlers

```python
@AGENT_APP.conversation_update("membersAdded")
async def on_members_added(context: TurnContext, _state: TurnState):
    # Introduce the teammate by its OWN name/role here.
    await context.send_activity("…")
    return True

@AGENT_APP.activity("message")
async def on_message(context: TurnContext, _state: TurnState):
    ...

@AGENT_APP.error
async def on_error(context: TurnContext, error: Exception):
    ...
```

- `@AGENT_APP.message(re.compile(r"^...$"))` matches on text and is checked **before** the
  catch-all `@AGENT_APP.activity("message")`.
- Handler signature is always `async def fn(context: TurnContext, state: TurnState)`.

### 4.3 Hosting (`start_server.py`)

```python
from os import environ
from microsoft_agents.hosting.core import AgentApplication, AgentAuthConfiguration
from microsoft_agents.hosting.aiohttp import (
    start_agent_process,
    jwt_authorization_middleware,
    CloudAdapter,
)
from aiohttp.web import Request, Response, Application, run_app


def start_server(agent_application: AgentApplication, auth_configuration: AgentAuthConfiguration):
    async def entry_point(req: Request) -> Response:
        agent: AgentApplication = req.app["agent_app"]
        adapter: CloudAdapter = req.app["adapter"]
        return await start_agent_process(req, agent, adapter)

    APP = Application(middlewares=[jwt_authorization_middleware])
    APP.router.add_post("/api/messages", entry_point)
    APP["agent_configuration"] = auth_configuration
    APP["agent_app"] = agent_application
    APP["adapter"] = agent_application.adapter
    run_app(APP, host="localhost", port=int(environ.get("PORT", 3978)))
```

### 4.4 Entry point (`main.py`)

```python
# 1) OTEL must be configured BEFORE importing the agent/server (see §7)
from .telemetry import configure_otel_providers
configure_otel_providers(service_name="pmo_ai_teammate")

# 2) Standard logging for the SDK
import logging
ms_agents_logger = logging.getLogger("microsoft_agents")
ms_agents_logger.addHandler(logging.StreamHandler())
ms_agents_logger.setLevel(logging.INFO)

# 3) Build app and start server
from .agent import AGENT_APP, CONNECTION_MANAGER
from .start_server import start_server

start_server(
    agent_application=AGENT_APP,
    auth_configuration=CONNECTION_MANAGER.get_default_connection_configuration(),
)
```

---

## 5. GitHub Copilot SDK integration (verbatim patterns)

Import surface (from `github-copilot-sdk`, imported as `copilot`):

```python
from copilot import CopilotClient, define_tool
from copilot.session import PermissionHandler
from copilot.generated.session_events import SessionEventType
```

### 5.1 Client + session lifecycle

- Create **one** `CopilotClient`, `await client.start()`, and share it across conversations.
- Create/reuse a **session per `user_id:conversation_id`** so each manager gets isolated,
  multi-turn context.
- Set the teammate persona via `system_message={"content": <persona>}`.
- Stream responses via session events.

```python
client = CopilotClient()
await client.start()

session = await client.create_session(
    model=environ.get("COPILOT_MODEL", "gpt-4.1"),
    on_permission_request=PermissionHandler.approve_all,  # OK for local/dev only
    tools=[...],                # @define_tool callables
    streaming=True,
    github_token=github_token,  # per-user token (see auth below)
    system_message={"content": PMO_TEAMMATE_PERSONA},
)
```

Streaming loop (subscribe, send, await idle):

```python
def on_event(evt):
    if evt.type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
        delta = getattr(getattr(evt, "data", None), "delta_content", None)
        if delta:
            context.streaming_response.queue_text_chunk(delta)
    elif evt.type == SessionEventType.SESSION_IDLE:
        done_event.set()
    elif evt.type == SessionEventType.SESSION_ERROR:
        ...

unsubscribe = session.on(on_event)
try:
    await session.send(user_text)
    await done_event.wait()
finally:
    unsubscribe()
    await context.streaming_response.end_stream()
```

### 5.2 Defining tools

Tools use Pydantic parameter models and the `@define_tool` decorator. Tools must be
**async** and return a string. Back the tool with **static JSON** from `data/`.

```python
from pydantic import BaseModel, Field
from copilot import define_tool

class GetProjectStatusParams(BaseModel):
    project_id: str = Field(description="Project identifier")

@define_tool(description="Get the current status of a project")
async def get_project_status(params: GetProjectStatusParams) -> str:
    ...  # read from data/projects.json, return a human-readable string
```

---

## 6. Identity & acting on behalf of the manager (OBO)

This is the defining behavior of the AI Teammate. It follows the Microsoft Agents SDK
**OBO authorization** sample.

### 6.1 Auth handler configuration (env)

Configure two app registrations: the agent's **own identity** (`SERVICE_CONNECTION`) and an
**OBO exchange** connection used to act for the manager. Wire them through the
`AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__*` settings (see §8). Auto sign-in obtains
the manager's token on first turn.

### 6.2 OBO token exchange (verbatim API)

```python
# Acting on behalf of the manager: exchange the manager's token for a downstream-scoped token.
token_response = await AGENT_APP.auth.exchange_token(context, [scope], "<HANDLER_ID>")
downstream_token = token_response.token
```

- `AGENT_APP.auth` is the `Authorization` instance. Its public OBO API is:
  `exchange_token(context, scopes: list[str] | None, auth_handler_id: str | None,
  exchange_connection: str | None) -> TokenResponse`.
- Sign-out: `await AGENT_APP.auth.sign_out(context)`.
- Gate a handler behind auth with `@AGENT_APP.message(re.compile(r".*"),
  auth_handlers=["<HANDLER_ID>"])` so the token is guaranteed available inside the handler.

### 6.3 Per-user Copilot identity

When using the Copilot SDK with per-user GitHub identity, retrieve the user's token and pass
it to the session:

```python
token_response = await AGENT_APP.auth.get_token(context, "GITHUB")
github_token = token_response.token if token_response else None
```

Key the agent's sessions and any cached state by **manager (user) id + conversation id** so
that one manager's context never leaks to another.

> **Agentic identity note.** The SDK also exposes `AgenticUserAuthorization` (an agentic
> user-authorization handler) in addition to the standard `_UserAuthorization`. Prefer the
> handler that matches the Agent 365 agentic-identity guidance for "agent has its own
> identity, acts on behalf of a manager." Verify the exact handler/connection settings
> against the current SDK before relying on them, since this area is evolving.

---

## 7. Observability — OTEL to the A365 observability endpoint

Follow the Microsoft OTEL sample. **Call `configure_otel_providers()` as the very first
thing in `main.py`, before importing the agent or server**, so global providers are
installed before any instrumented library loads.

- Export **traces, metrics, and logs** via **OTLP/gRPC**.
- The export endpoint is the **A365 observability endpoint**, configured via
  `OTEL_EXPORTER_OTLP_ENDPOINT` (do not hard-code it).
- Set a meaningful `service.name` (e.g. `pmo_ai_teammate`) and include the agent's identity
  (Agent ID) and the manager id as resource/span attributes where available, so telemetry
  is attributable to the specific agent instance and its manager.
- Reuse the sample's provider wiring: `TracerProvider` + `SimpleSpanProcessor` +
  `OTLPSpanExporter`; `MeterProvider` + `PeriodicExportingMetricReader` +
  `OTLPMetricExporter`; `LoggerProvider` + `BatchLogRecordProcessor` + `OTLPLogExporter`;
  then `instrument_libraries()` for aiohttp client/server and `requests`.
- The incoming `POST /api/messages` is auto-traced by the aiohttp server instrumentation;
  add **manual spans around each agent turn and each tool/MCP call** with attributes such as
  `agent.id`, `manager.id`, `tool.name`, and `mcp.server` so A365 can correlate activity.

---

## 8. MCP and the A365 Tooling Gateway

- Expose this agent's tools through an **MCP server** (`src/mcp/server.py`) using the `mcp`
  package. Each MCP tool should map to (or reuse) a `@define_tool` capability and read from
  the static JSON back ends.
- **Register the MCP server on the Agent 365 Tooling Gateway** (`src/mcp/gateway.py`). The
  Gateway is the governed entry point through which the agent discovers and calls tools;
  registration must use the **agent's own identity**. Keep the gateway URL and registration
  parameters in configuration (env), never hard-coded.
- Tool calls made through the Gateway on the manager's behalf must carry the **OBO token**
  obtained in §6, not the bare agent app token.

> The exact A365 Tooling Gateway registration contract is environment-specific and evolving.
> Implement it behind a small, well-named interface in `gateway.py` and drive every endpoint,
> id, and credential from configuration so it can be updated without touching agent logic.

---

## 9. Simulating back-end systems with static JSON

- **All** external/back-end data is simulated with static JSON files under `data/`.
- Access JSON only through a thin repository/service layer (e.g. `data_store.py`), so tools
  and the agent never read files directly. This keeps a clean seam for later replacement
  with real systems.
- Treat the JSON as **read-mostly fixtures**. If a tool "writes", keep the mutation
  in-memory (scoped by manager/conversation) unless a fixture file is explicitly intended to
  be updated; do not depend on persistence across restarts.
- Keep fixtures small, realistic, and PMO-relevant (projects, tasks, milestones, risks,
  status reports, stakeholders, etc.), consistent with the functional spec in §11.

---

## 10. Configuration (`env.TEMPLATE`)

Provide an `env.TEMPLATE` (copied to `.env` locally; **never commit real secrets**). Use the
Microsoft `CONNECTIONS__...` and `AGENTAPPLICATION__...` double-underscore hierarchy parsed
by `load_configuration_from_env`.

```dotenv
# --- Agent's OWN identity (Azure Bot / Entra app registration) ---
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=

# --- OBO exchange connection (acts on behalf of the manager) ---
CONNECTIONS__OBO__SETTINGS__CLIENTID=
CONNECTIONS__OBO__SETTINGS__CLIENTSECRET=
CONNECTIONS__OBO__SETTINGS__TENANTID=

# --- User authorization handlers ---
AGENTAPPLICATION__USERAUTHORIZATION__AUTOSIGNIN=true
AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__OBO__SETTINGS__AZUREBOTOAUTHCONNECTIONNAME=
AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__OBO__SETTINGS__OBOCONNECTIONNAME=OBO

# --- Manager assignment (each instance has exactly one manager) ---
AGENT__MANAGER__USER_ID=
AGENT__IDENTITY__AGENT_ID=          # the agent's own Entra Agent ID
AGENT__DISPLAY_NAME=PMO AI Teammate

# --- GitHub Copilot SDK ---
# COPILOT_MODEL=gpt-4.1

# --- A365 Tooling Gateway ---
A365__TOOLING_GATEWAY__URL=
A365__TOOLING_GATEWAY__REGISTRATION_ID=

# --- A365 Observability (OTEL / OTLP gRPC) ---
OTEL_EXPORTER_OTLP_ENDPOINT=
```

> Variable names under `AGENT__*`, `A365__*` are project-specific (not SDK-parsed); the
> `CONNECTIONS__*` and `AGENTAPPLICATION__*` names are consumed by the Agents SDK and must
> match SDK expectations. Confirm exact OBO handler keys against the current SDK sample.

---

## 11. Functional requirements (from the repository PDFs)

The functional behavior of this agent is defined by the design documents in the repository
root:

- `CSM Agent - Exec Summary 4.8.2026.pdf`
- `CSM Agent - Engineering Detail 4.8.2026.pdf`

> ⚠️ **These PDFs are currently rights-protected (Microsoft Purview/RMS encrypted) and could
> not be read automatically.** Before implementing functional capabilities, obtain an
> unprotected copy (or the extracted requirements) and **fill in this section** with the
> concrete capabilities, workflows, tools, and data entities the teammate must support.
> Until then, treat the PMO/"digital project manager" scope (projects, tasks, milestones,
> status reporting, risks/stakeholders) as the working assumption, and keep functional logic
> isolated in `tools/` and the `data/` fixtures so it is easy to align once the spec is
> confirmed.

Each capability described in the PDFs should become: a Pydantic-typed `@define_tool` (and a
matching MCP tool), backed by `data/*.json`, traced with a manual OTEL span, and authorized
via the manager OBO token.

---

## 12. Guardrails — do's and don'ts

**Do**
- Copy SDK call shapes from the cited samples; keep this file updated when APIs change.
- Keep `AGENT_APP`, adapter, connection manager, storage as import-time singletons.
- Configure OTEL before any other import in `main.py`.
- Obtain an **OBO token** before any action taken on the manager's behalf.
- Key all sessions/state by `manager_id:conversation_id`.
- Drive every endpoint, id, and secret from configuration; keep secrets in `.env` only.
- Keep all back-end access behind the `data/` repository layer.

**Don't**
- Don't invent SDK classes, methods, or import paths. If unsure, check the samples first.
- Don't use `microsoft.agents` (dotted) imports — it is `microsoft_agents` (underscores).
- Don't perform manager-scoped actions with the bare agent app token (no OBO = not allowed).
- Don't commit secrets, tokens, or real `.env` values.
- Don't hard-code the A365 Tooling Gateway URL or the OTEL endpoint.
- Don't approve all Copilot permissions (`PermissionHandler.approve_all`) outside local dev.
- Don't add real external back ends yet — simulate with static JSON.

---

## References

Verified Microsoft sources used to author these instructions (all in
[`microsoft/Agents`](https://github.com/microsoft/Agents)):

- Quickstart (base M365 Agent): `samples/python/quickstart`
- GitHub Copilot SDK in an M365 Agent: `samples/python/copilot-sdk`
- On-Behalf-Of (OBO) authorization: `samples/python/obo-authorization`
- OpenTelemetry: `samples/python/otel`
- Microsoft 365 Agents SDK overview & Python quickstart docs:
  <https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/agents-sdk-overview> and
  <https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/quickstart?pivots=python>
- GitHub Copilot SDK (PyPI): `github-copilot-sdk` (import name `copilot`)

> Agent 365 (A365) specifics — Entra **Agent ID**, **Tooling Gateway** registration, and the
> **observability endpoint** — are new and may change. Treat the A365 integration points as
> configuration-driven interfaces and verify them against current Microsoft guidance before
> implementation.
