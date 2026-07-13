"""
Read CSM/owner attributes from Microsoft Entra — never invent them.

The dashboards show each person's **role** and **region**. Those are not stored
in the fixtures (storing them would mean making them up); instead they are read
live from the directory:

* **role**   ← Entra ``jobTitle``
* **region** ← Entra ``officeLocation`` (falling back to ``city`` / ``state`` /
  ``country`` / ``usageLocation`` when office location is unset)

Reads use the app-only Graph token from :mod:`graph_app`. If the directory cannot
be read (no permission / offline), every value falls back to ``None`` and the UI
renders an em dash — it does **not** display a fabricated value. The user owns
these values by setting sensible job titles / office locations in Entra.

A small in-process cache lets synchronous view builders read attributes after an
async :func:`warm` (e.g. on control-plane startup); a cache miss is an honest
"unavailable", never a guess.
"""

from __future__ import annotations

import logging

from . import graph_app

logger = logging.getLogger(__name__)

_SELECT = "displayName,jobTitle,department,officeLocation,city,country,state,usageLocation"
_profile_cache: dict[str, dict] = {}
_rr_cache: dict[str, dict] = {}

_UNAVAILABLE = {"role": None, "region": None, "department": None, "source": "unavailable"}


def _region_from(profile: dict) -> str | None:
    for key in ("officeLocation", "city", "state", "country", "usageLocation"):
        val = (profile.get(key) or "").strip()
        if val:
            return val
    return None


def _to_rr(profile: dict | None) -> dict:
    if profile is None:
        return dict(_UNAVAILABLE)
    return {
        "role": (profile.get("jobTitle") or None),
        "region": _region_from(profile),
        "department": (profile.get("department") or None),
        "source": "entra",
    }


async def get_profile(object_id: str | None) -> dict | None:
    """Fetch selected Entra attributes for a user oid. None if unavailable."""
    if not object_id:
        return None
    if object_id in _profile_cache:
        return _profile_cache[object_id]
    token = await graph_app.app_token()
    if not token:
        return None
    data = await graph_app.graph_get(f"/users/{object_id}", token, params={"$select": _SELECT})
    if data is None:
        return None
    profile = {
        "displayName": data.get("displayName"),
        "jobTitle": data.get("jobTitle"),
        "department": data.get("department"),
        "officeLocation": data.get("officeLocation"),
        "city": data.get("city"),
        "state": data.get("state"),
        "country": data.get("country"),
        "usageLocation": data.get("usageLocation"),
    }
    _profile_cache[object_id] = profile
    _rr_cache[object_id] = _to_rr(profile)
    return profile


async def role_and_region(object_id: str | None) -> dict:
    """Return ``{'role': jobTitle|None, 'region': officeLocation|None, 'source': ...}`` for an oid."""
    if not object_id:
        return dict(_UNAVAILABLE)
    profile = await get_profile(object_id)
    rr = _to_rr(profile)
    _rr_cache[object_id] = rr
    return rr


def cached_role_region(object_id: str | None) -> dict:
    """Synchronous read of previously-warmed role/region (honest 'unavailable' on miss)."""
    if not object_id:
        return dict(_UNAVAILABLE)
    return _rr_cache.get(object_id, dict(_UNAVAILABLE))


async def warm(object_ids) -> None:
    """Best-effort: populate the cache for a set of oids so sync views can read them."""
    for oid in {o for o in (object_ids or []) if o}:
        try:
            await role_and_region(oid)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("directory warm failed for %s: %s", oid, exc)


def reset() -> None:
    _profile_cache.clear()
    _rr_cache.clear()
