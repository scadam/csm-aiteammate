"""
Shared Azure OpenAI client using managed-identity authentication.

Mirrors the ``lseg-snowflake`` approach (``server/nl_to_sql.py``): authenticate
with ``DefaultAzureCredential`` + a bearer token provider — **never an API
key**. The same client is reused for NL-to-SQL translation and constrained
draft generation.

The credential is deliberately built **excluding the shared-token-cache and
VS Code credentials**, because on developer machines those can resolve to a
different signed-in identity (e.g. a corporate guest account) than the intended
demo identity. With those excluded, the chain falls through to the Azure CLI
credential (``az login``), which is the identity this agent runs as. An explicit
tenant can be pinned via ``AZURE_TENANT_ID`` (or ``AGENT__IDENTITY__TENANT_ID``).
"""

from __future__ import annotations

import logging
import os

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import BadRequestError, OpenAI

from . import config

logger = logging.getLogger(__name__)

_client: OpenAI | None = None

# Models that require ``max_completion_tokens`` instead of ``max_tokens`` on the
# chat.completions API (gpt-5.x and the o-series reasoning models). Configurable.
_COMPLETION_TOKEN_PREFIXES: tuple[str, ...] = tuple(
    p.strip().lower()
    for p in os.getenv("AOAI_MAX_COMPLETION_TOKENS_MODELS", "gpt-5,o1,o3,o4").split(",")
    if p.strip()
)


def _needs_completion_tokens(model: str) -> bool:
    m = (model or "").lower()
    return any(m.startswith(p) for p in _COMPLETION_TOKEN_PREFIXES)


def build_credential() -> DefaultAzureCredential:
    """Build the DefaultAzureCredential used for Azure OpenAI (demo-identity safe)."""
    # Exclude the developer credentials that can resolve to a corporate/guest
    # identity from a shared token cache; this lets the chain fall through to the
    # Azure CLI credential (``az login``) — the intended demo identity. The Azure
    # CLI credential uses the tenant the CLI is signed in to.
    return DefaultAzureCredential(
        exclude_shared_token_cache_credential=True,
        exclude_visual_studio_code_credential=True,
        exclude_interactive_browser_credential=True,
    )


def get_client() -> OpenAI:
    """Return a cached Azure OpenAI client authenticated with managed identity."""
    global _client
    if _client is None:
        if not config.AZURE_OPENAI_ENDPOINT:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT is not configured.")
        token_provider = get_bearer_token_provider(
            build_credential(),
            config.AZURE_OPENAI_SCOPE,
        )
        _client = OpenAI(
            base_url=config.AZURE_OPENAI_ENDPOINT,
            api_key=token_provider,  # type: ignore[arg-type]
        )
        logger.info(
            "Azure OpenAI client initialised (managed identity): %s",
            config.AZURE_OPENAI_ENDPOINT,
        )
    return _client


def aoai_bearer_token() -> str | None:
    """Return a fresh Azure OpenAI access token (managed identity), or None on failure.

    Used to authenticate the GitHub Copilot SDK BYOK *Azure* provider so the
    agentic loop runs on Azure OpenAI with **no API key and no GitHub token** —
    just the host's managed identity (``Cognitive Services OpenAI User``).
    """
    try:
        token = build_credential().get_token(config.AZURE_OPENAI_SCOPE)
        return token.token
    except Exception as exc:  # pragma: no cover - depends on live identity
        logger.warning("Could not acquire Azure OpenAI bearer token: %s", exc)
        return None


def chat_completion(*, model: str, messages: list, max_tokens: int | None = None,
                    temperature: float | None = None, **kwargs):
    """Model-agnostic chat completion.

    Newer models (gpt-5.x, o-series) require ``max_completion_tokens`` instead of
    ``max_tokens``; older ones use ``max_tokens``. This picks the right parameter
    by model name and, if the service still objects, retries once with the other
    parameter — so the same call site works across model generations.
    """
    client = get_client()
    params: dict = dict(model=model, messages=messages, **kwargs)
    if temperature is not None:
        params["temperature"] = temperature
    token_key = "max_completion_tokens" if _needs_completion_tokens(model) else "max_tokens"
    if max_tokens is not None:
        params[token_key] = max_tokens
    try:
        return client.chat.completions.create(**params)
    except BadRequestError as exc:
        msg = str(exc)
        if max_tokens is not None and "max_tokens" in msg and "max_completion_tokens" in msg:
            params.pop("max_tokens", None)
            params.pop("max_completion_tokens", None)
            other = "max_completion_tokens" if token_key == "max_tokens" else "max_tokens"
            params[other] = max_tokens
            logger.info("Retrying completion with %s for model %s", other, model)
            return client.chat.completions.create(**params)
        raise
