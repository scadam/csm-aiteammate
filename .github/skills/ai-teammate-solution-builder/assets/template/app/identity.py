"""Identity protocol and development-only header implementation."""

from __future__ import annotations

from typing import Protocol

import jwt
from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field

from .spec import Manager, SolutionSpec
from . import config


class Principal(BaseModel):
    principal_id: str
    manager_id: str | None
    roles: set[str]
    inbound_assertion: str = Field(default="", exclude=True)


class IdentityProvider(Protocol):
    async def resolve(self, request: Request) -> Principal: ...


class PlatformIdentityProvider:
    """Development headers locally; signed Entra bearer identity in production."""

    def __init__(self, spec: SolutionSpec):
        self.spec = spec
        self.managers = {manager.id: manager for manager in spec.managers}
        self.fleet_principals = {
            principal.principal_id: principal
            for principal in spec.identity.fleet_principals
        }
        self._jwks = (
            jwt.PyJWKClient(config.CONTROL_PLANE_JWKS_URL, timeout=10)
            if not config.DEVELOPMENT_MODE and config.CONTROL_PLANE_JWKS_URL
            else None
        )

    async def resolve(self, request: Request) -> Principal:
        if config.DEVELOPMENT_MODE:
            return self._development(request)
        return self._production(request)

    def _development(self, request: Request) -> Principal:
        identity = self.spec.identity
        principal_id = request.headers.get(identity.principal_header, identity.default_principal_id)
        manager_id = request.headers.get(identity.manager_header, identity.default_manager_id)
        role_text = request.headers.get(identity.roles_header)
        roles = {
            role.strip() for role in (role_text.split(",") if role_text else identity.default_roles) if role.strip()
        }
        manager = self.managers.get(manager_id)
        if manager is not None and manager.principal_id == principal_id:
            return Principal(principal_id=principal_id, manager_id=manager_id, roles=set(manager.roles))
        fleet = self.fleet_principals.get(principal_id)
        if fleet is not None:
            return Principal(principal_id=principal_id, manager_id=None, roles=set(fleet.roles))
        raise HTTPException(status_code=403, detail="Principal has no declared assignment")

    def _production(self, request: Request) -> Principal:
        if not all(
            [
                self._jwks,
                config.CONTROL_PLANE_TOKEN_ISSUER,
                config.CONTROL_PLANE_TOKEN_AUDIENCE,
                config.CONTROL_PLANE_ALLOWED_CLIENT_IDS,
                config.AGENT_TENANT_ID,
                config.AGENT_BLUEPRINT_ID,
            ]
        ):
            raise HTTPException(status_code=503, detail="Production identity is not configured")
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Bearer token required")
        token = authorization.split(" ", 1)[1]
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                options={
                    "verify_aud": False,
                    "verify_iss": False,
                    "require": ["exp", "iat", "iss", "aud", "oid", "tid"],
                },
            )
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=401, detail="Invalid bearer token") from exc
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        allowed_audiences = {
            config.CONTROL_PLANE_TOKEN_AUDIENCE,
            config.AGENT_BLUEPRINT_ID,
        }
        if not set(map(str, audiences)).intersection(allowed_audiences):
            raise HTTPException(status_code=401, detail="Invalid bearer token audience")
        allowed_issuers = {
            config.CONTROL_PLANE_TOKEN_ISSUER.rstrip("/"),
            f"https://sts.windows.net/{config.AGENT_TENANT_ID}",
        }
        if str(claims["iss"]).rstrip("/") not in allowed_issuers:
            raise HTTPException(status_code=401, detail="Invalid bearer token issuer")
        if str(claims["tid"]) != config.AGENT_TENANT_ID:
            raise HTTPException(status_code=401, detail="Invalid bearer token tenant")
        principal_id = str(claims["oid"])
        scopes = set(str(claims.get("scp", "")).split())
        if config.CONTROL_PLANE_REQUIRED_SCOPE not in scopes:
            raise HTTPException(status_code=403, detail="Required delegated scope is missing")
        client_id = str(claims.get("azp") or claims.get("appid") or "")
        if client_id not in config.CONTROL_PLANE_ALLOWED_CLIENT_IDS:
            raise HTTPException(status_code=403, detail="Calling client is not allowed")
        manager = next(
            (item for item in self.spec.managers if item.principal_id == principal_id), None
        )
        fleet = self.fleet_principals.get(principal_id)
        if manager is None and fleet is None:
            raise HTTPException(status_code=403, detail="Principal has no declared assignment")
        return Principal(
            principal_id=principal_id,
            manager_id=manager.id if manager else None,
            roles=set(manager.roles if manager else fleet.roles),
            inbound_assertion=token,
        )


HeaderIdentityProvider = PlatformIdentityProvider


def manager_for(spec: SolutionSpec, manager_id: str) -> Manager:
    manager = next((item for item in spec.managers if item.id == manager_id), None)
    if manager is None:
        raise HTTPException(status_code=404, detail="Unknown manager")
    return manager


def can_view_fleet(principal: Principal, spec: SolutionSpec) -> bool:
    return bool(principal.roles.intersection(spec.identity.fleet_roles))


def require_fleet(principal: Principal, spec: SolutionSpec) -> None:
    if not can_view_fleet(principal, spec):
        raise HTTPException(status_code=403, detail="Fleet role required")


def require_manager(principal: Principal, spec: SolutionSpec, manager_id: str) -> Manager:
    if manager_id != principal.manager_id:
        raise HTTPException(status_code=403, detail="Manager mutation requires exact assignment")
    return manager_for(spec, manager_id)


def require_manager_read(principal: Principal, spec: SolutionSpec, manager_id: str) -> Manager:
    if manager_id != principal.manager_id and not can_view_fleet(principal, spec):
        raise HTTPException(status_code=403, detail="Cannot read another manager's scope")
    return manager_for(spec, manager_id)
