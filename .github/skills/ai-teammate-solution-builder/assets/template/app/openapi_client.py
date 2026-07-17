"""Execute only preselected OpenAPI operations from solution.yaml."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from . import agent_identity, config
from .spec import OpenApiOperation, OpenApiSource


class OpenApiSecurityError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenApiInvocation:
    data: Any
    provenance: str


class OpenApiInvoker:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport
        self._oauth_tokens: dict[str, tuple[str, float]] = {}

    async def invoke(
        self,
        source: OpenApiSource,
        operation: OpenApiOperation,
        arguments: dict[str, Any],
        offline_result: Any,
        idempotency_key: str = "",
        idempotency_header: str = "",
    ) -> OpenApiInvocation:
        base_url = os.getenv(source.base_url_env, "")
        if config.OFFLINE_MODE:
            if offline_result is None:
                raise RuntimeError(
                    f"OpenAPI operation {source.id}.{operation.operation_id} has no offline result"
                )
            return OpenApiInvocation(
                offline_result, f"offline:openapi:{source.id}:{operation.operation_id}"
            )
        if not base_url:
            raise RuntimeError(
                f"OpenAPI base URL environment variable {source.base_url_env} is not configured"
            )
        self._validate_base_url(base_url, source.allowed_hosts)
        path, query, headers, body = self._bind(operation, arguments)
        if source.auth.mode != "none":
            if source.auth.mode == "oauth_client_credentials":
                token = await self._client_credentials_token(source)
            else:
                token = await agent_identity.require_token(
                    source.auth.mode, source.auth.scopes, source.auth.token_env
                )
            headers["Authorization"] = f"Bearer {token}"
        if idempotency_key and idempotency_header:
            headers[idempotency_header] = idempotency_key
        url = base_url.rstrip("/") + path
        timeout = httpx.Timeout(float(source.timeout_seconds))
        async with httpx.AsyncClient(
            transport=self.transport, timeout=timeout, follow_redirects=False
        ) as client:
            response = await client.request(
                operation.method, url, params=query, headers=headers, json=body
            )
        response.raise_for_status()
        if len(response.content) > config.MAX_HTTP_RESPONSE_BYTES:
            raise OpenApiSecurityError("OpenAPI response exceeded the configured size limit")
        content_type = response.headers.get("content-type", "")
        if response.content and "json" not in content_type.lower():
            raise OpenApiSecurityError("OpenAPI operation returned a non-JSON response")
        data = response.json() if response.content else {"statusCode": response.status_code}
        return OpenApiInvocation(
            data, f"live:openapi:{source.id}:{operation.operation_id}"
        )

    async def _client_credentials_token(self, source: OpenApiSource) -> str:
        cached = self._oauth_tokens.get(source.id)
        if cached and cached[1] > time.time():
            return cached[0]
        token_url = os.getenv(source.auth.token_url_env, "")
        client_id = os.getenv(source.auth.client_id_env, "")
        client_secret = os.getenv(source.auth.client_secret_env, "")
        if not all([token_url, client_id, client_secret]):
            raise PermissionError(
                f"OAuth client credentials are not configured for {source.id}"
            )
        parsed = urlparse(token_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise OpenApiSecurityError("OAuth token URL must use HTTPS")
        allowed = {host.lower() for host in source.allowed_hosts}
        if parsed.hostname.lower() not in allowed:
            raise OpenApiSecurityError("OAuth token host is not allowlisted")
        form = {"grant_type": "client_credentials"}
        if source.auth.scopes:
            form["scope"] = " ".join(source.auth.scopes)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=httpx.Timeout(float(source.timeout_seconds)),
            follow_redirects=False,
        ) as client:
            response = await client.post(
                token_url,
                data=form,
                auth=httpx.BasicAuth(client_id, client_secret),
                headers={"Accept": "application/json"},
            )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise PermissionError("OAuth token response did not include access_token")
        instance_url = payload.get("instance_url")
        if instance_url:
            instance_host = urlparse(str(instance_url)).hostname or ""
            if instance_host.lower() not in allowed:
                raise OpenApiSecurityError("OAuth instance_url host is not allowlisted")
        expires_in = int(payload.get("expires_in", 600))
        self._oauth_tokens[source.id] = (str(token), time.time() + max(30, expires_in - 60))
        return str(token)

    @staticmethod
    def _bind(
        operation: OpenApiOperation, arguments: dict[str, Any]
    ) -> tuple[str, dict[str, Any], dict[str, str], Any]:
        path = operation.path
        query: dict[str, Any] = {}
        headers: dict[str, str] = {"Accept": "application/json"}
        body: Any = None
        properties = operation.input_schema.get("properties", {})
        for name, value in arguments.items():
            location = properties.get(name, {}).get("x-in", "query")
            if location == "path":
                text = str(value)
                if any(part in {".", ".."} for part in text.replace("\\", "/").split("/")):
                    raise OpenApiSecurityError("Unsafe path parameter")
                path = path.replace("{" + name + "}", quote(text, safe=""))
            elif location == "body":
                body = value
            elif location == "query":
                query[name] = value
            else:
                raise OpenApiSecurityError(f"Unsupported OpenAPI parameter location {location!r}")
        if "{" in path or "}" in path:
            raise ValueError("Required OpenAPI path parameter was not provided")
        return path, query, headers, body

    def _validate_base_url(self, base_url: str, allowed_hosts: list[str]) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise OpenApiSecurityError("OpenAPI base URL must use HTTPS")
        host = parsed.hostname.lower()
        if host not in {allowed.lower() for allowed in allowed_hosts}:
            raise OpenApiSecurityError("OpenAPI host is not allowlisted")
        if self.transport is not None:
            return
        for record in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM):
            address = ipaddress.ip_address(record[4][0])
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
                raise OpenApiSecurityError("OpenAPI host resolved to a private or reserved address")
