# H — GitHub Copilot SDK as the agentic loop (decision & wiring)

**Question (H):** *"Why are you not always using the GitHub Copilot SDK as the
agentic loop? … GitHub pricing is now token-based and heavily model-dependent.
We should be using at least gpt-5.4 … GitHub natively supports skills and memory
and MCP tools."*

This note records the verified facts and the decision. Nothing here is invented —
the SDK shapes were read from the installed `github-copilot-sdk==1.0.0`.

## What the SDK actually is (verified)

`CopilotClient.create_session(...)` exposes (real parameters):

- `provider: ProviderConfig | None` — **BYOK**. `ProviderConfig` is a TypedDict:
  `type: 'openai'|'azure'|'anthropic'`, `base_url`, `api_key`, **`bearer_token`**,
  `azure: {api_version}`, `model_id`, `wire_api`, …
- `enable_skills: bool`, `skill_directories: list[str]`, `disabled_skills` —
  **native skills** (Claude/VS Code-style `SKILL.md` folders).
- `enable_session_store`, `infinite_sessions` — **native memory/session** store.
- `mcp_servers`, `mcp_oauth_token_storage` — **native MCP** tools.

So the user is right: the SDK natively supports skills, memory, and MCP. The
client still needs the **GitHub Copilot CLI runtime** (the native process the
client connects to) and an identity.

## The honest cost picture

- GitHub's **$0.04 / premium request** is **legacy** (the pricing page says so;
  Pro/Pro+ annual only). It is **not** how this deployment is billed.
- Our loop runs inference on **Azure OpenAI** via **managed identity**, so the
  real cost basis is **Azure OpenAI token pricing** ($/1M input + $/1M output,
  per model). `src/cost.py` is now token-based and **never fabricates a price**:
  only API-verified models are priced (seed: `gpt-4o`); others are priced only
  from the operator-supplied `AOAI_MODEL_PRICES`, otherwise reported as *price
  n/a*.

## Decision

1. **Use the Copilot SDK as the loop with a BYOK _Azure_ provider** authenticated
   by **managed identity** — no API key, **no GitHub token**, no premium
   requests. Wired in `src/copilot_session.py` (`COPILOT_PROVIDER=azure`,
   `_provider_config()` → `bearer_token` from `openai_client.aoai_bearer_token()`,
   `model_id = COPILOT_MODEL = gpt-5.4-1`).
2. **Turn on native skills** in the session (`enable_skills=True`,
   `skill_directories=[src/skills]`) so the SDK discovers the same five
   `SKILL.md` skills the rest of the system uses.
3. **Model = `gpt-5.4-1`** (the real Azure OpenAI deployment name for model
   `gpt-5.4`), set as the default for the loop and for SQL/draft generation.

## Why the Linux control plane still runs the deterministic engine

`github-copilot-sdk` ships a native CLI runtime; the control-plane container
image deliberately **excludes** the SDK and runs `src/control_plane/engine.py`
over the **same** `TOOL_SPECS`. To make the SDK the loop **everywhere**, the
remaining step is infrastructure, not code: **add the GitHub Copilot CLI runtime
to the control-plane image**, then point it at the BYOK Azure provider exactly as
the bot host now does. Until then both surfaces share one tool registry, one set
of skills, and one (token-based) cost model, so behaviour and accounting stay
consistent.

## What must never happen

- No GitHub token is required or used for the Azure provider path. The previously
  leaked token must be **revoked**; it is not used anywhere.
- No API keys for Azure OpenAI — managed identity only.
- No fabricated prices — unpriced models show *price n/a* with token counts.
