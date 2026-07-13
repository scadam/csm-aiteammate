"""
Publish the CSM Autopilot Teams app package to the tenant app catalog.

Uses the signed-in Azure CLI identity (svasireddy, Global Admin in the demo
tenant) to obtain a Microsoft Graph token and POST/upload the app package zip to
``/appCatalogs/teamsApps``. If the app already exists in the catalog, updates it.

Requires the Graph delegated permission ``AppCatalog.ReadWrite.All`` (admin).
"""

from __future__ import annotations

import sys

import requests
from azure.identity import AzureCliCredential

ZIP_PATH = sys.argv[1] if len(sys.argv) > 1 else "appPackage/build/csm-autopilot.zip"
TEAMS_APP_ID = sys.argv[2] if len(sys.argv) > 2 else None

cred = AzureCliCredential()
token = cred.get_token("https://graph.microsoft.com/.default").token
H = {"Authorization": f"Bearer {token}"}

with open(ZIP_PATH, "rb") as fh:
    zip_bytes = fh.read()

# Is the app already in the catalog? (search by the manifest's external id)
existing = None
if TEAMS_APP_ID:
    r = requests.get(
        "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps"
        f"?$filter=externalId eq '{TEAMS_APP_ID}'&$expand=appDefinitions",
        headers=H,
        timeout=30,
    )
    if r.status_code == 200:
        vals = r.json().get("value", [])
        if vals:
            existing = vals[0]

zip_headers = {**H, "Content-Type": "application/zip"}

if existing:
    catalog_id = existing["id"]
    print(f"App already in catalog (id={catalog_id}); updating…")
    r = requests.post(
        f"https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/{catalog_id}/appDefinitions"
        "?$select=id",
        headers=zip_headers,
        data=zip_bytes,
        timeout=60,
    )
    print("update status:", r.status_code)
    print(r.text[:800])
else:
    print("Publishing new app to the tenant catalog…")
    r = requests.post(
        "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps",
        headers=zip_headers,
        data=zip_bytes,
        timeout=60,
    )
    print("publish status:", r.status_code)
    print(r.text[:1200])
    if r.status_code in (200, 201):
        body = r.json()
        print("\nCATALOG_APP_ID:", body.get("id"))
        print("DISPLAY_NAME:", (body.get("displayName") or ""))
