"""Azure OpenAI client authenticated only with DefaultAzureCredential."""

from __future__ import annotations

import asyncio
from typing import Any

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import OpenAI

from . import config
from .spec import Capability


_client: OpenAI | None = None
_credential: DefaultAzureCredential | None = None


def get_client() -> OpenAI:
    global _client, _credential
    if _client is None:
        if not config.AZURE_OPENAI_ENDPOINT:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT is required outside offline mode")
        _credential = DefaultAzureCredential(
            exclude_interactive_browser_credential=True,
            exclude_shared_token_cache_credential=True,
        )
        provider = get_bearer_token_provider(_credential, config.AZURE_OPENAI_SCOPE)
        _client = OpenAI(base_url=config.AZURE_OPENAI_ENDPOINT, api_key=provider)
    return _client


async def complete(**kwargs: Any):
    return await asyncio.to_thread(get_client().chat.completions.create, **kwargs)


async def generate_grounded_text(capability: Capability, inputs: dict[str, Any]) -> str:
    response = await complete(
        model=config.MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Generate only from the supplied facts. Do not add claims. "
                    f"Task: {capability.description}"
                ),
            },
            {"role": "user", "content": str(inputs)},
        ],
        temperature=0.1,
    )
    return response.choices[0].message.content or ""
