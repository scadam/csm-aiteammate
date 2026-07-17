# Solution Specification

`solution.yaml` is the source of truth for generated solutions. Keep organization-specific
resource IDs, secrets, and actual bearer tokens out of it.

## Top-Level Sections

| Section | Purpose |
| --- | --- |
| `solution` | Stable identifier, display name, description, domain, and dynamic terminology. |
| `runtime` | Mandatory split Agent SDK, FastAPI control-plane, and FastMCP hosts. |
| `agent` | Persona, role, instructions, introduction, reasoning provider, model, and tool-step cap. |
| `identity` | Development identity plus mandatory service connection, manager OBO, and agentic-user settings. |
| `observability` | Mandatory A365 service identity and turn/tool instrumentation policy. |
| `managers` | Manager identities used by fixtures and development. A live directory adapter may replace them. |
| `skills` | Progressive-disclosure scenario knowledge and its allowed capabilities. |
| `mcp_servers` | Existing remote MCP metadata, typed tools, transport, exact identity/scopes, and timeout. |
| `openapi_sources` | Local OpenAPI 3.x inputs and the fixed operations compiled into tools. |
| `mcp_exposure` | Governed FastMCP facade and A365 Tooling Gateway configuration names. |
| `user_interfaces` | MCP Apps resources, AG-UI transport, dashboard/HITL sources, columns, metrics, filters, sorting, and bulk actions. |
| `teams_app` | Fresh tabs-only Teams host, tab audiences/routes, and the A365-blueprint ETS/SSO contract. |
| `data_sources` | Inline or file fixtures plus metadata for future HTTP, SQL, or MCP adapters. |
| `capabilities` | Typed registry shared by reasoning, workflows, MCP, policy, auth, and telemetry. |
| `workflows` | Trigger modes, ordered stages, explicit argument bindings, review policy, and decisions. |
| `control_plane` | Manager and fleet titles, summary fields, and fleet metrics. |
| `a365` | CLI-managed Agent 365 config, manifest location, and required setup commands. |

## Reference Rules

- `workflow.subject_source` must reference `data_sources[].id`.
- `skills[].capabilities` must reference `capabilities[].id`.
- `workflow.stages[].capability` must reference `capabilities[].id` when present.
- A `fixture_query` capability must reference a data source.
- An `mcp_tool` capability must reference both an MCP server and a declared tool.
- An `openapi_operation` must match a selected local OpenAPI method/path/operation ID.
- A side effect must declare a non-`none` identity, explicit review mode, and required durable
  idempotency. A `required` or `workflow_policy` effect must follow a review stage.
- Manager-OBO and agentic-user capabilities must declare downstream scopes.
- Review conditions use dotted fields rooted at `input`, `subject`, `manager`, or `results`.
- Stage arguments may be literal or `{from: dotted.context.path}` and are validated against the
  capability's generated Pydantic model before invocation.
- MCP Apps resources use `ui://` and `text/html;profile=mcp-app`. Fleet resources remain in the
  role-checked control plane; manager resources may also be exposed through MCP.
- HITL UI resources must use `review_queue`, manager audience, and at least one bulk action.
- Teams manager/fleet tab paths must match their audience. Teams SSO is fixed to the CLI-created
  A365 blueprint and `access_agent_as_user`; no second Entra app is generated for SSO.

## Interactive UI

`user_interfaces.resources[]` declares a `dashboard` or `hitl` view over `workflow_runs` or
`review_queue`. The generator projects it into the Teams control plane and, when `surfaces`
contains `mcp`, a sandboxed MCP App. MCP Apps cannot receive tokens and call only same-server,
typed tools through JSON-RPC. `resolve_reviews` is app-only and hidden from model discovery.

AG-UI is served over authenticated HTTP/SSE at `/api/ag-ui`. It emits `RUN_STARTED`,
`STATE_SNAPSHOT`, `ACTIVITY_SNAPSHOT`, step events, and `RUN_FINISHED`; pending reviews produce an
interrupt outcome. Resume payloads must address the open interrupt and carry each review ID,
stored effect digest, decision, and full replacement edits. The existing workflow engine remains
the mutation authority.

`teams_app` generates `appPackage/manifest.json`, icons, safe environment metadata, a manifest
renderer, and `m365agents.yml`. Agents Toolkit creates only a fresh Teams catalog ID. The Entra
blueprint, scope, preauthorized Teams clients, and domain-qualified identifier URI remain owned by
the A365 CLI. Agent and control-plane processes therefore share one public FQDN in production.

## Identity Modes

| Mode | Use |
| --- | --- |
| `manager_obo` | Delegated access to data or actions owned by the signed-in manager. |
| `agentic_user` | Autonomous actions authored as the teammate's own Agent ID. |
| `managed_identity` | Azure service-to-service access controlled through RBAC. |
| `oauth_client_credentials` | External service application access using a configured token endpoint, client ID, and secret environment variables. |
| `bearer_env` | An environment-injected bearer token for a non-Azure integration. |
| `native` | A downstream service's supported non-Azure credential or workload identity. |
| `none` | Pure computation or fixture-only development behavior. |

Manager OBO requires a signed-in Agent SDK turn and fails closed for background work. Agentic-user
tokens use the SDK's four-argument federation call and can be minted without a manager turn. They
must never substitute for one another. Development control-plane headers are disabled in
production, where issuer/audience/signature/expiry claims are validated and `oid` maps to a manager.

For an OpenAPI source with `auth.mode: oauth_client_credentials`, declare
`token_url_env`, `client_id_env`, and `client_secret_env`; keep `scopes` empty only when the
provider's token contract omits scopes. The generated client sends credentials with HTTP Basic
authentication, caches tokens only until their reported expiry, allowlists both token and API
hosts, and never accepts a caller-provided token or authorization header. The generated deployment
marks the client secret as secure and injects it through a Container Apps `secretRef`. Never put
the secret itself in `solution.yaml`.

## Review Conditions

Supported operators are `eq`, `ne`, `in`, `not_in`, `exists`, `gt`, `gte`, `lt`, and `lte`.
Conditions are OR-combined: any matching condition requires review. Put more elaborate policy in
a deterministic capability, not in an LLM prompt.

## Offline Provenance

Every capability may provide `offline_result`. The generated app returns source-specific
`offline:mcp:*`, `offline:openapi:*`, or `offline:template:*` provenance. Live adapters return
`live:*`. Offline mode makes no network calls and `/health` reports `offline`.
Never remove this distinction from API responses or telemetry.
