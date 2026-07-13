# Copilot Instructions — CSM AI Teammate (Agent 365 style)

These instructions tell GitHub Copilot how to build and extend this repository. Follow
them for every change. They describe an **AI Teammate** agent: a **Digital Customer Success
Manager (CSM) for a financial markets & data business** that has **its own identity** but always acts **on behalf of its
manager** (the human CSM it is assigned to).

> **Read this first.** The Microsoft 365 Agents SDK and the GitHub Copilot SDK are **new
> and in Public Preview**. APIs change and are easy to get wrong. **Do not invent SDK
> APIs.** Only use the import paths, class names, and call signatures that are shown in
> this document — they are copied verbatim from the official Microsoft samples cited in
> [References](#references). If something you need is not documented here, find it in the
> referenced samples before writing code, and keep this file updated.

> **Use the A365 CLI and Observability SDK — never provision by hand.** All Agent 365
> setup — the Entra **blueprint** app, the agent **instance** identity, **MCP** and **bot**
> OAuth2 permissions/consent, and the supporting **Azure** infrastructure — **must** be done
> with the **`a365` CLI** (`Microsoft.Agents.A365.DevTools.Cli`). All A365 telemetry **must**
> go through the **`microsoft-agents-a365`** Observability SDK. The authoritative reference
> for both is **[`A365_SDK_AND_CLI_GUIDE.md`](../A365_SDK_AND_CLI_GUIDE.md)** — read it
> before doing any A365 setup, observability, or MCP-permission work, and follow its exact
> commands, env vars, and call shapes. **Do not** click through the Entra/Azure portals,
> hand-write app registrations, or hand-roll OTLP export to do what the CLI/SDK already do.

---

## 1. What we are building

An **AI Teammate** — a persona-driven agent that behaves like a member of a team (here, a
**Digital Customer Success Manager / CSM** teammate for a **financial markets & data business**, looking after its
customers and the adoption of its products such as FlowDesk and CheckMate). Each
deployed **agent instance**:

- Has **its own agent identity** (an Agent 365 / Microsoft Entra **Agent ID**). It is a
  first-class actor, not an anonymous bot. It introduces itself by its own name and role.
- Is assigned to exactly **one manager** (the human CSM it works for). The agent acts **on
  behalf of** that manager using **On-Behalf-Of (OBO)** token exchange, so every downstream
  call carries the manager's delegated permissions, not a generic app identity.
- Runs as a **Microsoft 365 Agent** built with the **Microsoft 365 Agents SDK (Python)**.
- Uses the **GitHub Copilot SDK** for its reasoning/LLM loop and tool-calling.
- Grounds in and reasons over **Microsoft 365** data (email, meetings/calendar, OneDrive/
  SharePoint documents, Teams messages, people, enterprise search) through the **Work IQ MCP
  server** — a remote MCP server that exposes Microsoft 365 intelligence as a small set of
  generic tools and can invoke Microsoft 365 Copilot for natural-language reasoning. **Do not
  use the Copilot Chat API for this**; use Work IQ MCP (see §8.1).
- Exposes and consumes capabilities through **MCP (Model Context Protocol)**. The agent's own
  **MCP server is registered on the Agent 365 (A365) Tooling Gateway**, and it **consumes**
  the **Work IQ MCP** server as a tool source.
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

# Agent 365 Observability SDK (telemetry to the A365 service; see A365_SDK_AND_CLI_GUIDE.md)
microsoft-agents-a365

# NL-to-SQL over the (simulated) Snowflake schema — generate SQL with Azure OpenAI.
# Managed identity ONLY (DefaultAzureCredential); never key-based. NOT Snowflake Cortex.
# Mirror the lseg-snowflake repo approach.
openai
azure-identity

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
data/                # Static JSON that SIMULATES all back-end systems (Snowflake, Gainsight)
  signals.json       # Computed signals table (Layer 2)
  accounts.json      # CRM/account context (Gainsight CS + Salesforce)
  enhancements.json  # Tagged enhancement releases (six-field tags)
  content_library.json   # Approved content blocks / playbooks
  voc.json           # Voice-of-customer feedback (knowledge base 1)
  csm_voice.json     # CSM voice archive (knowledge base 4)
  review_queue.json  # CSM review inbox items
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
configure_otel_providers(service_name="csm_ai_teammate")

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

The agent's reasoning/LLM loop runs through the **GitHub Copilot SDK**. For grounding in and
reasoning over Microsoft 365 work data, the agent uses the **Work IQ MCP** server (§8.1)
rather than the Copilot Chat API.

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
    system_message={"content": CSM_TEAMMATE_PERSONA},
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

class GetAccountHealthParams(BaseModel):
    account_id: str = Field(description="Customer account identifier")

@define_tool(description="Get the current adoption/health status of a customer account")
async def get_account_health(params: GetAccountHealthParams) -> str:
    ...  # read from data/accounts.json, return a human-readable string
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

- `AGENT_APP.auth` is the `Authorization` instance. The **verified** OBO call shape (from the
  `obo-authorization` sample) is **three positional arguments**:
  `exchange_token(context, scopes: list[str], auth_handler_id: str) -> TokenResponse`
  (the sample uses `exchange_token(context, [scope], "MCS")`). A 4th optional
  `exchange_connection` argument may exist in the SDK but is **not** demonstrated in the
  sample — confirm against the SDK source before relying on it.
- Sign-out takes the handler id: `await AGENT_APP.auth.sign_out(context, "<HANDLER_ID>")`.
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

## 7. Observability — A365 Observability SDK (and OTEL)

> **Authoritative reference:** [`A365_SDK_AND_CLI_GUIDE.md`](../A365_SDK_AND_CLI_GUIDE.md),
> Part 2 ("The Observability SDK") and Part 3 (troubleshooting). Use the
> **`microsoft-agents-a365`** package (`microsoft_agents_a365.observability.core`) to emit
> A365 telemetry: call `configure(...)` once at startup with an `Agent365ExporterOptions`,
> and wrap each agent turn in `InvokeAgentScope` and each tool/MCP call in
> `ExecuteToolScope`. Put `token_resolver` **inside** `Agent365ExporterOptions` (the
> top-level arg is ignored), return the **bare** token (no `"Bearer "` prefix), and
> `force_flush()` the tracer provider in a `finally` block. Both
> `ENABLE_A365_OBSERVABILITY` **and** `ENABLE_A365_OBSERVABILITY_EXPORTER` must be `true`
> for real export — otherwise scopes are silent no-ops or fall back to the console exporter.
> Set `use_s2s_endpoint=True` for service-to-service (app-only) hosts. Follow the guide's
> required span attributes exactly, or the exporter silently drops the span.

Follow the Microsoft OTEL sample. **Call `configure_otel_providers()` as the very first
thing in `main.py`, before importing the agent or server**, so global providers are
installed before any instrumented library loads.

- Export **traces, metrics, and logs** via **OTLP/gRPC**.
- The export endpoint is the **A365 observability endpoint**, configured via
  `OTEL_EXPORTER_OTLP_ENDPOINT` (do not hard-code it).
- Set a meaningful `service.name` (e.g. `csm_ai_teammate`) and include the agent's identity
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

> **Provision with the `a365` CLI — not by hand.** The Entra blueprint, agent instance, and
> the **MCP** and **bot** OAuth2 permissions/consent that make the Tooling Gateway and MCP
> tools work are created with the CLI flows in
> [`A365_SDK_AND_CLI_GUIDE.md`](../A365_SDK_AND_CLI_GUIDE.md), Part 1. Use, in order:
> `a365 setup requirements`, `a365 setup blueprint`, `a365 setup permissions mcp`,
> `a365 setup permissions bot` (or `a365 setup all --agent-name <name>` with Global Admin).
> Inspect with `a365 query-entra blueprint-scopes` / `instance-scopes`, manage local MCP
> servers with `a365 develop` / `develop-mcp`, and undo with `a365 cleanup`. **Do not**
> hand-create Entra app registrations, OAuth2 grants, or Azure infra for any of this.

- Expose this agent's tools through an **MCP server** (`src/mcp/server.py`) using the `mcp`
  package (FastMCP). The **one** custom MCP server combines all skills — Snowflake NL-to-SQL,
  the Gainsight CS/PX REST tools, knowledge-base search, content build, signals, and Work IQ
  pass-throughs — from the single `src/tools/TOOL_SPECS` registry, so the Copilot and MCP
  surfaces never drift.
- **Register the custom MCP server as a BYO (bring-your-own) MCP server on the Agent 365
  Tooling Gateway** (`src/mcp/gateway.py`) using
  `a365 develop-mcp register-external-mcp-server` (EntraOAuth, `remoteScopes`
  `api://<blueprint-app-id>/access_agent_as_user`). Registration needs a **public HTTPS**
  endpoint (`MCP__PUBLIC_URL`) and is then **approved by an IT admin** in the Microsoft 365
  admin center. Once approved, Agent 365 routes all invocations through the **Tooling
  Gateway**, and the agent's reasoning loop consumes the tools via the **gateway endpoint**
  (`A365__TOOLING_GATEWAY__MCP_ENDPOINT`), **never the raw MCP endpoint**.
- The reasoning loop wires its remote MCP servers in `create_session(mcp_servers=...)`:
  **Work IQ MCP** (OBO token) for Microsoft 365 grounding, plus the **custom MCP via the
  gateway**. Keep every URL/scope/id in configuration (env), never hard-coded.
- The MCP/bot **permissions and consent** behind the Gateway are granted by the `a365` CLI
  (`a365 setup permissions mcp` / `bot`); do not grant them manually in the portal.
- Tool calls made through the Gateway on the manager's behalf must carry the **OBO token**
  obtained in §6, not the bare agent app token.

> The exact A365 Tooling Gateway registration contract is environment-specific and evolving.
> Implement it behind a small, well-named interface in `gateway.py` and drive every endpoint,
> id, and credential from configuration so it can be updated without touching agent logic.

### 8.1 Consuming the Work IQ MCP server (Microsoft 365 grounding)

The agent reasons over Microsoft 365 work data — and invokes Microsoft 365 Copilot for
natural-language answers — by **consuming the Work IQ MCP server** as an MCP client, **not**
via the Copilot Chat API. See the Microsoft documentation:
<https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/overview>.

- Work IQ MCP is a **remote MCP server** with a **fixed surface of 10 generic tools** that act
  on relative Microsoft Graph resource paths (the path is the resource, the tool is the verb):
  - **Entity tools** — `fetch`, `create_entity`, `update_entity`, `delete_entity`,
    `do_action`, `call_function` (CRUD and actions over M365 resources, e.g.
    `fetch /me/messages`, `do_action /me/sendMail`, `call_function /search/query`).
  - **Copilot tools** — `ask` (invoke Microsoft 365 Copilot for natural-language reasoning) and
    `list_agents` (discover available Copilot agents).
  - **Schema tools** — `get_schema`, `search_paths` (discover paths and OpenAPI schemas at
    runtime; introspect rather than enumerating thousands of types into context).
- **Authentication:** Work IQ MCP uses **Microsoft Entra ID** and is **delegated-only**
  (application-only is not supported); clients discover the auth configuration via the
  `/.well-known/oauth-protected-resource` endpoint. Every Work IQ call is permission-trimmed
  and policy-enforced on the **manager's behalf**, so it **must** carry the manager **OBO
  token** (§6), never the bare agent app token. The Work IQ API app id is
  `fdcc1f02-fc51-4226-8753-f668596af7f7`, App ID URI `api://workiq.svc.cloud.microsoft`, and
  the OBO scope is `api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask` (admin-consent required;
  an org admin must first create the Work IQ service principal). Host:
  `workiq.svc.cloud.microsoft`.
- Keep the Work IQ MCP endpoint and any client settings in **configuration** (env), never
  hard-coded. Implement the client behind a small, well-named interface so the endpoint and
  auth contract — both in Public Preview — can change without touching agent logic.
- **The Microsoft 365 / Work IQ back end is REAL, not simulated.** `src/workiq_client.py` is a
  real remote MCP client (streamable HTTP) that calls Work IQ with the manager OBO token;
  `src/tools/workiq.py` and the `ask` / `list_agents` tools invoke it. The static
  `data/workiq.json` fixture is an **offline-dev fallback only**, used when
  `WORKIQ__MCP__ENDPOINT` is unset.

---

## 9. Back-end systems: real vs. simulated

- **Real back ends (do not simulate):**
  - **Microsoft 365 / Graph / Work IQ** — consumed live via the **Work IQ MCP** server on the
    manager's behalf (OBO); see §8.1. `data/workiq.json` is an offline-dev fallback only.
  - **Snowflake** — the relational store is a real Snowflake database (`CSM_DB.ADOPTION`),
    queried read-only via NL-to-SQL (§11.5). An in-memory SQLite simulation seeded from
    `data/*.json` is used only when no Snowflake account is configured (tests/offline).
- **Still simulated — but behind the *real* vendor REST contracts — with static JSON under
  `data/`:** **Gainsight CS & PX**. `src/gainsight/` implements the real Gainsight NXT REST
  surface (Company, Person, Timeline, Cockpit/CTA, and PX/Aptrinsic endpoints, the `accesskey`
  header, and the `{result, errorCode, requestId, data, message}` envelope) served in-process
  from the fixtures — "simulated-real". The CSM tools build real Gainsight payloads and parse
  real envelopes; set `GAINSIGHT__LIVE=true` with a real domain + access key to call the live
  API without changing tool logic. **Email is real** — `send_email` delivers via Work IQ
  `do_action /me/sendMail` (OBO), not a stub.
- **Still simulated with static JSON under `data/`:** the CSM knowledge-base/content fixtures
  (accounts, signals, routing rules, content library, VOC, CSM voice, PX engagement, review
  queue, managers).
- Access JSON only through a thin repository/service layer (`data_store.py`), so tools and the
  agent never read files directly — a clean seam for replacing each simulated back end with a
  real system.
- Treat the JSON as **read-mostly fixtures**. If a tool "writes", keep the mutation in-memory
  (scoped by manager/conversation) unless a fixture file is explicitly intended to be updated;
  do not depend on persistence across restarts.
- Keep fixtures small, realistic, and CSM-relevant, consistent with the functional spec in §11.

---

## 10. Configuration (`env.TEMPLATE`)

Provide an `env.TEMPLATE` (copied to `.env` locally; **never commit real secrets**). Use the
Microsoft `CONNECTIONS__...` and `AGENTAPPLICATION__...` double-underscore hierarchy parsed
by `load_configuration_from_env`.

> The Entra app registrations these settings reference (the agent's own identity, the
> blueprint, and the OBO connection) are created by the **`a365` CLI** (§8 and
> [`A365_SDK_AND_CLI_GUIDE.md`](../A365_SDK_AND_CLI_GUIDE.md), Part 1) — copy the resulting
> client/tenant ids into `.env`; do not create those apps by hand. The two observability
> toggles below (`ENABLE_A365_OBSERVABILITY`, `ENABLE_A365_OBSERVABILITY_EXPORTER`) gate the
> A365 Observability SDK (§7) and **both must be `true`** for real telemetry export.

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
AGENT__DISPLAY_NAME=CSM AI Teammate

# --- GitHub Copilot SDK ---
# COPILOT_MODEL=gpt-4.1

# --- Azure OpenAI (NL-to-SQL + constrained drafts; managed identity, never key-based; not Cortex) ---
AZURE_OPENAI_ENDPOINT=                 # https://<resource>.openai.azure.com/openai/v1/
# AZURE_OPENAI_SCOPE=https://cognitiveservices.azure.com/.default
# AZURE_OPENAI_SQL_DEPLOYMENT=gpt-4.1
# AZURE_OPENAI_DRAFT_DEPLOYMENT=gpt-4.1

# --- A365 Tooling Gateway ---
A365__TOOLING_GATEWAY__URL=
A365__TOOLING_GATEWAY__REGISTRATION_ID=

# --- Work IQ MCP (Microsoft 365 grounding; consumed as a remote MCP server) ---
# Public Preview — endpoint/auth may change; keep configuration-driven.
WORKIQ__MCP__ENDPOINT=

# --- A365 Observability (OTEL / OTLP gRPC) ---
OTEL_EXPORTER_OTLP_ENDPOINT=

# --- A365 Observability SDK toggles (both must be true for real export; see guide Part 2) ---
ENABLE_A365_OBSERVABILITY=true
ENABLE_A365_OBSERVABILITY_EXPORTER=true
# A365_OBSERVABILITY_DOMAIN_OVERRIDE=     # leave unset for production
```

> Variable names under `AGENT__*`, `A365__*` are project-specific (not SDK-parsed); the
> `CONNECTIONS__*` and `AGENTAPPLICATION__*` names are consumed by the Agents SDK and must
> match SDK expectations. Confirm exact OBO handler keys against the current SDK sample.

---

## 11. Functional requirements (from the repository PDFs)

The functional behavior of this agent is defined by the design documents committed to the
repository (the authoritative source of scope):

- `110348.PDF` — CSM Agent, Executive Summary ("Proactive Agentic Adoption Journey")
- `110349.PDF` — CSM Agent, Engineering Detail (eight-layer architecture)
- `CSM Agent - Exec Summary 4.8.2026.pdf` / `CSM Agent - Engineering Detail 4.8.2026.pdf`
  are rights-protected (Microsoft Purview/RMS) duplicates of the above and cannot be read;
  use the `1103xx.PDF` versions.

### 11.1 What the CSM Agent is

The **CSM Agent** is a collection of **five specialised AI agents** that monitor product
usage across the customer base, identify **adoption gaps** and relevant **product
releases**, draft **personalised outreach in each CSM's voice**, and route messages through
the right channel. **CSMs review the interactions that need their judgment; everything else
is delivered at scale.** It is built to work across all of the company's products; **FlowDesk** and
**CheckMate** are the proof points.

> **Solution note — we do NOT use Microsoft Copilot Studio.** The original design documents
> describe coordinating the five agents in **Microsoft Copilot Studio** over a Snowflake +
> Gainsight stack. **This is not the technical solution we are building.** Our solution is an
> **Agent 365 AI Teammate** with its **own agent identity** that acts **on behalf of its
> manager** (OBO), built on the **Microsoft 365 Agents SDK**, reasoning via the **GitHub
> Copilot SDK** and grounding over Microsoft 365 work data through the **Work IQ MCP** server
> (not the Copilot Chat API; see §8.1), exposing its capabilities as
> **skills** (Pydantic `@define_tool` tools) and over **MCP** registered on the A365 Tooling
> Gateway. The five "agents" are realised as the skills/tools described in §11.3 and §11.7,
> not as Copilot Studio topics or flows. In **this repository the Snowflake + Gainsight back
> ends are simulated with static JSON** (see §9).

### 11.2 The end-to-end flow (model the teammate's behaviour on this)

`Signal Detected → Context Built → Action Decided → Content Built → Prioritised & Reviewed
→ Delivered → System Learns`

1. **Signal Detected** — usage data flags an adoption gap, a risk, or a relevant new release.
2. **Context Built** — gather customer history, past feedback, and what they have already
   been shown.
3. **Action Decided** — rules determine what to send, through which channel, and whether a
   CSM must review first.
4. **Content Built** — assemble a personalised message in the CSM's voice from **approved
   content only** (never invent product claims), quality-checked before routing.
5. **Prioritised & Reviewed** — inbound requests and outbound drafts are prioritised and
   routed to CSMs where needed.
6. **Delivered** — email, in-product prompt, or a prepared brief for a CSM-led conversation.
7. **System Learns** — outcomes and CSM decisions feed back into every component.

### 11.3 The five agents (each becomes a tool / set of tools)

1. **Signal Detection Agent** — monitors the signals table on a schedule; filters for signals
   above the severity threshold and passes signal + user + account + severity onward. **Pure
   detection logic, no AI generation.**
2. **VOC Personalisation Agent** — gathers everything needed before a decision: usage signals,
   customer feedback (VOC), in-product engagement history, and **live** account data.
   Produces a structured context summary. AI is used **only** to summarise VOC search
   results, not to make decisions.
3. **Next Best Action Agent** — looks up the **signal-to-action mapping** and **routing
   rules** tables; returns message type, channel, content source, and **whether CSM review
   is required**. **Deterministic rules lookup — fully auditable, no AI generation.**
4. **Content Build Agent** — retrieves relevant approved content blocks and CSM voice
   examples, then generates a personalised draft, constrained to retrieved content, with a
   quality/assurance check before routing. (Delivered as an early MVP.)
5. **Assessment & Prioritization Agent** — (a) assesses and prioritises inbound requests from
   the product org for customer communications, and (b) routes outbound drafts to the right
   CSM for review when conditions require it.

### 11.4 Review vs. automatic send (encode as routing rules, not code)

- **CSM reviews first:** high-influence or frustrated customers; an enhancement that directly
  matches a customer's prior request; complex topics; first outreach to a new senior contact;
  any strategic-account interaction.
- **Sends automatically:** routine onboarding nudges; low-complexity feature tips for active
  users; product-release alerts for self-service features; long-tail accounts without
  dedicated CSM coverage; message types with consistently high unedited acceptance rates.

> **Architecture principle (carry into this repo):** keep decision logic in **data**, not in
> agent code. The signal-to-action mapping and the CSM review routing rules live in
> data tables (here: `data/*.json`) so they can change without a code deploy. Keep agent/tool
> logic minimal.

### 11.5 The four knowledge bases (simulated as JSON)

These are the four searchable stores the agents draw on. **Do not use Snowflake Cortex.**
Treat Snowflake purely as a relational database; in this repository each store is a static
JSON fixture. Where "search" over a store is required, prefer simple structured
filtering/lookup over the JSON. If natural-language search is genuinely needed, **build a
small NL-to-SQL step that uses OpenAI to generate SQL** against the (simulated) Snowflake
schema — mirroring the approach in the **`lseg-snowflake`** repo — rather than any built-in
vector/semantic search service. Keep generated SQL read-only and parameterised/validated.

1. **Customer feedback / VOC** — surveys, call summaries, CSM health notes.
2. **Approved content & playbooks** — templates, playbooks, enhancement descriptions.
3. **PX engagement history** — what in-product content a user has already been shown (avoid
   repetition).
4. **CSM voice archive** — each CSM's accepted, unedited messages, used as style anchors when
   drafting. Grows from accepted drafts.

### 11.6 Delivery channels and the CSM review queue

- **Channels:** (1) email, (2) in-product prompt, (3) CSM-sent-after-review, (4) a prepared
  brief/task for a CSM-led conversation.
- **CSM review inbox:** a prioritised queue with **Accept / Edit / Discard**; every decision
  is logged (the learning loop). Reviewing an item must take **under a minute**.

### 11.7 Capabilities to expose (the six "skills")

Each of these becomes a Pydantic-typed `@define_tool` and a matching MCP tool, backed by
`data/*.json`: **Snowflake query** (read signals, mapping, routing rules, content library),
**knowledge-base search** over the four stores (structured lookup, or **OpenAI-generated
NL-to-SQL** as in the `lseg-snowflake` repo — **not** Snowflake Cortex), **Gainsight CS**
(account context; create review tasks; trigger email sends), **Gainsight PX** (trigger
in-product messages; read engagement history), **AI draft generation** (Content Build Agent
only, constrained to retrieved content), and **Snowflake write** (write outcomes and CSM
decisions back for the learning loop).

Every capability above should be: a Pydantic-typed `@define_tool` (and a matching MCP tool),
backed by `data/*.json`, traced with a manual OTEL span (with `agent.id`, `manager.id`,
`tool.name`), and — for any manager-scoped action — authorized via the manager **OBO** token.

---

## 12. Guardrails — do's and don'ts

**Do**
- Copy SDK call shapes from the cited samples; keep this file updated when APIs change.
- Use the **`a365` CLI** for all A365 setup (blueprint, instance, MCP/bot permissions, Azure
  infra) and the **`microsoft-agents-a365`** SDK for telemetry, per
  [`A365_SDK_AND_CLI_GUIDE.md`](../A365_SDK_AND_CLI_GUIDE.md).
- Keep `AGENT_APP`, adapter, connection manager, storage as import-time singletons.
- Configure OTEL before any other import in `main.py`.
- Obtain an **OBO token** before any action taken on the manager's behalf.
- Key all sessions/state by `manager_id:conversation_id`.
- Drive every endpoint, id, and secret from configuration; keep secrets in `.env` only.
- Keep all back-end access behind the `data/` repository layer.

**Don't**
- Don't invent SDK classes, methods, or import paths. If unsure, check the samples first.
- Don't manually create Entra apps, OAuth2 grants/consent, or Azure infra for A365 — use the
  **`a365` CLI** (see [`A365_SDK_AND_CLI_GUIDE.md`](../A365_SDK_AND_CLI_GUIDE.md)).
- Don't return a `"Bearer "`-prefixed token from the SDK `token_resolver`, and don't forget
  to set both observability env toggles — either mistake silently drops all telemetry.
- Don't use `microsoft.agents` (dotted) imports — it is `microsoft_agents` (underscores).
- Don't perform manager-scoped actions with the bare agent app token (no OBO = not allowed).
- Don't commit secrets, tokens, or real `.env` values.
- Don't hard-code the A365 Tooling Gateway URL or the OTEL endpoint.
- Don't approve all Copilot permissions (`PermissionHandler.approve_all`) outside local dev.
- Don't add real external back ends yet — simulate with static JSON.
- Don't generate product/enhancement claims from scratch — constrain drafts to **approved
  content** retrieved from the content library (Content Build Agent only).
- Don't embed signal-to-action or CSM-review routing logic in agent code — keep it in the
  `data/*.json` rules tables so it can change without a deploy.
- Don't auto-send where the spec requires CSM review (high-influence/frustrated customers,
  strategic accounts, first senior-contact outreach, complex topics).
- Don't use **Snowflake Cortex** (or any built-in vector/semantic search). Use Snowflake as a
  plain relational DB; for NL search, generate read-only SQL with **Azure OpenAI** (the
  `lseg-snowflake` pattern). In this repo, back it with the `data/*.json` fixtures.
- Don't authenticate to Azure OpenAI with an API key — use **managed identity**
  (`DefaultAzureCredential` + `get_bearer_token_provider`), exactly like `lseg-snowflake`.

---

## References

Verified Microsoft sources used to author these instructions (all in
[`microsoft/Agents`](https://github.com/microsoft/Agents), verified against commit
`2e6d5b84b24df6b942c609d96aced3d969e29963` on `main`):

- Quickstart (base M365 Agent): `samples/python/quickstart`
- GitHub Copilot SDK in an M365 Agent: `samples/python/copilot-sdk`
- On-Behalf-Of (OBO) authorization: `samples/python/obo-authorization`
- OpenTelemetry: `samples/python/otel`
- Microsoft 365 Agents SDK overview & Python quickstart docs:
  <https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/agents-sdk-overview> and
  <https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/quickstart?pivots=python>
- GitHub Copilot SDK (PyPI): `github-copilot-sdk` (import name `copilot`)
- Work IQ MCP server (Microsoft 365 grounding via MCP):
  <https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/overview>
  (tool reference:
  <https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/tool-reference>)

Agent 365 setup, MCP/bot permissions, and observability (authoritative for this repo):

- [`A365_SDK_AND_CLI_GUIDE.md`](../A365_SDK_AND_CLI_GUIDE.md) — the **`a365` CLI** (Entra
  blueprint, agent instance, MCP/bot OAuth2 permissions, Azure infra) and the
  **`microsoft-agents-a365`** Observability SDK. **All A365 provisioning and telemetry must
  follow this guide; never provision by hand.**

Functional/business sources (committed to this repository):

- `110348.PDF` — CSM Agent, Executive Summary
- `110349.PDF` — CSM Agent, Engineering Detail

> Agent 365 (A365) specifics — Entra **Agent ID**, **Tooling Gateway** registration, and the
> **observability endpoint** — are new and may change. Treat the A365 integration points as
> configuration-driven interfaces and verify them against current Microsoft guidance before
> implementation.
