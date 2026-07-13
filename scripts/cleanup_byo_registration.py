"""
Clean up leftover Entra apps and Power Platform connectors from a failed/partial
A365 BYO MCP registration, so ``a365 develop-mcp register-external-mcp-server``
can be retried from a clean state.

Why this is needed
------------------
``register-external-mcp-server`` provisions, in order:
  1. three Entra proxy apps  ``<server>-A365Proxy`` / ``-RemoteProxy`` / ``-PublicClients``
  2. one BYO Entra app        ``<server> - BYO``  (holds a deterministic identifierUri
     ``https://agent365.svc.cloud.microsoft/agents/servers/<server>/tenants/<tid>``)
  3. two Power Platform custom connectors ``shared_<server>`` and ``shared_<server>P``
     in the A365-managed (MCC) Dataverse environment.

When any step fails the CLI rolls back the Dataverse side but **does not** delete the
Entra apps, and a soft-deleted Entra app *retains* its identifierUri — so the next
retry fails with "Another object with the same value for property identifierUris
already exists". Leftover connectors likewise make "create connector" return HTTP 400.

This utility:
  • finds every Entra app whose displayName starts with the server name, soft-deletes
    it, then **purges** it from ``/directory/deletedItems`` (freeing the identifierUri);
  • deletes every ``<server>``* custom connector in the A365 MCC environment.

Auth: uses the current ``az login`` identity (AzureCliCredential). The signed-in user
must be able to delete the apps/connectors (the same identity that ran the CLI).

Usage:
    python -m scripts.cleanup_byo_registration                 # server ext_CsmTeammate
    python -m scripts.cleanup_byo_registration --server ext_Foo --env <mcc-env-guid>
"""

from __future__ import annotations

import argparse
import time

import requests
from azure.identity import AzureCliCredential

# A365-managed (MCC) Dataverse environment that holds the BYO connectors. This is an
# internal environment created by A365 on first BYO registration; it is not listed by
# ``a365 develop-mcp list-environments`` and returns 404 from the BAP admin API.
DEFAULT_MCC_ENV = "7d8e0d01-ee92-edd8-a296-dbda85d16206"
DEFAULT_SERVER = "ext_CsmTeammate"

_GRAPH = "https://graph.microsoft.com/.default"
_POWERAPPS = "https://service.powerapps.com/.default"


def _bearer(cred: AzureCliCredential, scope: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cred.get_token(scope).token}"}


def purge_entra_apps(cred: AzureCliCredential, server: str) -> int:
    """Soft-delete then permanently purge every app whose name starts with ``server``."""
    headers = _bearer(cred, _GRAPH)
    url = (
        "https://graph.microsoft.com/v1.0/applications"
        f"?$filter=startswith(displayName,'{server}')&$select=id,displayName"
    )
    apps: list[dict] = []
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        apps.extend(body.get("value", []))
        url = body.get("@odata.nextLink")

    for app in apps:
        r = requests.delete(
            f"https://graph.microsoft.com/v1.0/applications/{app['id']}",
            headers=headers,
            timeout=30,
        )
        print(f"soft-delete {app['displayName']} ({app['id']}) -> {r.status_code}")

    if apps:
        time.sleep(4)  # let the soft-delete propagate before purging

    for app in apps:
        r = requests.delete(
            f"https://graph.microsoft.com/v1.0/directory/deletedItems/{app['id']}",
            headers=headers,
            timeout=30,
        )
        print(f"purge {app['displayName']} ({app['id']}) -> {r.status_code}")
    return len(apps)


def delete_connectors(cred: AzureCliCredential, server: str, env_id: str) -> int:
    """Delete every ``server``* custom connector in the A365 MCC environment."""
    headers = _bearer(cred, _POWERAPPS)
    list_url = (
        "https://api.powerapps.com/providers/Microsoft.PowerApps/apis"
        f"?api-version=2016-11-01&$filter=environment eq '{env_id}'"
    )
    resp = requests.get(list_url, headers=headers, timeout=30)
    resp.raise_for_status()
    needle = server.lower()
    hits = [
        a
        for a in resp.json().get("value", [])
        if needle
        in (a.get("name", "") + a.get("properties", {}).get("displayName", "")).lower()
    ]
    for api in hits:
        name = api["name"]
        del_url = (
            f"https://api.powerapps.com/providers/Microsoft.PowerApps/apis/{name}"
            f"?api-version=2016-11-01&$filter=environment eq '{env_id}'"
        )
        r = requests.delete(del_url, headers=headers, timeout=30)
        display = api.get("properties", {}).get("displayName", name)
        print(f"delete connector {display} -> {r.status_code}")
    return len(hits)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=DEFAULT_SERVER, help="BYO server name (ext_*)")
    parser.add_argument("--env", default=DEFAULT_MCC_ENV, help="A365 MCC environment id")
    args = parser.parse_args()

    cred = AzureCliCredential()
    n_apps = purge_entra_apps(cred, args.server)
    n_conn = delete_connectors(cred, args.server, args.env)
    print(f"\nCleaned {n_apps} Entra app(s) and {n_conn} connector(s) for '{args.server}'.")


if __name__ == "__main__":
    main()
