"""JWT verification for the generated Agent 365 Tooling Gateway MCP facade."""

from __future__ import annotations

import time

import jwt
from mcp.server.auth.provider import AccessToken

from . import config


class EntraTokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        required_scope: str,
        allowed_client_ids: set[str] | None = None,
    ):
        if not all([issuer, audience, jwks_url, required_scope]):
            raise ValueError("MCP issuer, audience, JWKS URL, and required scope are mandatory")
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.required_scope = required_scope
        self.allowed_client_ids = allowed_client_ids or set()
        self.jwks = jwt.PyJWKClient(jwks_url, timeout=10)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
            scopes = set(str(claims.get("scp", "")).split())
            principal_id = str(claims.get("oid") or "")
            client_id = str(claims.get("azp") or claims.get("appid") or "")
            if not principal_id or self.required_scope not in scopes:
                return None
            if self.allowed_client_ids and client_id not in self.allowed_client_ids:
                return None
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=sorted(scopes),
                expires_at=int(claims["exp"]),
                resource=self.audience,
            )
        except (jwt.PyJWTError, ValueError, KeyError):
            return None


def build_token_verifier() -> EntraTokenVerifier | None:
    if config.MCP_ALLOW_DEV_NO_AUTH:
        return None
    return EntraTokenVerifier(
        issuer=config.MCP_TOKEN_ISSUER,
        audience=config.MCP_TOKEN_AUDIENCE,
        jwks_url=config.MCP_JWKS_URL,
        required_scope=config.MCP_REQUIRED_SCOPE,
        allowed_client_ids=config.MCP_ALLOWED_CLIENT_IDS,
    )
