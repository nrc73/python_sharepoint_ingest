"""Test Microsoft Graph API for SharePoint (Sites.ReadWrite.All on Graph) vs SPO REST."""
from __future__ import annotations
import json, base64, sys
PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests
from msal import ConfidentialClientApplication
from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import maybe_build_provider

settings = load_settings(env_override="dev")
provider = maybe_build_provider(settings.key_vault)
client_id, client_secret, tenant_id = provider.get_sharepoint_credentials("dev")

def decode(token: str) -> dict:
    p = token.split(".")[1]
    p += "=" * (4 - len(p) % 4)
    return json.loads(base64.b64decode(p).decode())

msal_app = ConfidentialClientApplication(
    client_id=client_id, client_credential=client_secret,
    authority=f"https://login.microsoftonline.com/{tenant_id}",
)

# ── Test 1: SharePoint REST (current path) ──────────────────────────────────
spo_result = msal_app.acquire_token_for_client(
    scopes=["https://mycompany715.sharepoint.com/.default"]
)
spo_token = spo_result["access_token"]
sd = decode(spo_token)
print("=== SPO REST token ===")
print(f"  aud={sd['aud']}  roles={sd.get('roles',[])}  appid={sd.get('appid')}")
r1 = requests.get(
    "https://mycompany715.sharepoint.com/sites/data_ingest_dev/_api/Web?$select=Title",
    headers={"Authorization": f"Bearer {spo_token}", "Accept": "application/json;odata=nometadata"},
)
print(f"  status={r1.status_code}  body={r1.text[:200]}")

# ── Test 2: Graph API (alternative path) ─────────────────────────────────────
graph_result = msal_app.acquire_token_for_client(
    scopes=["https://graph.microsoft.com/.default"]
)
graph_token = graph_result["access_token"]
gd = decode(graph_token)
print("\n=== Graph API token ===")
print(f"  aud={gd['aud']}  roles={gd.get('roles',[])}  appid={gd.get('appid')}")

# Look up the site via Graph
r2 = requests.get(
    "https://graph.microsoft.com/v1.0/sites/mycompany715.sharepoint.com:/sites/data_ingest_dev",
    headers={"Authorization": f"Bearer {graph_token}", "Accept": "application/json"},
)
print(f"  GET site  status={r2.status_code}  body={r2.text[:300]}")

if r2.status_code == 200:
    site_id = r2.json()["id"]
    print(f"  site_id={site_id}")
    # List root drive
    r3 = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root/children",
        headers={"Authorization": f"Bearer {graph_token}", "Accept": "application/json"},
    )
    print(f"  LIST drive  status={r3.status_code}  items={[i['name'] for i in r3.json().get('value',[])][:5]}")
