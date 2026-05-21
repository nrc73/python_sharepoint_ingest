"""Raw HTTP probe to SharePoint REST to get the exact 401 body and headers."""
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

msal_app = ConfidentialClientApplication(
    client_id=client_id,
    client_credential=client_secret,
    authority=f"https://login.microsoftonline.com/{tenant_id}",
)

result = msal_app.acquire_token_for_client(
    scopes=["https://mycompany715.sharepoint.com/.default"]
)
token = result["access_token"]

# Decode token for inspection
p = token.split(".")[1]
p += "=" * (4 - len(p) % 4)
claims = json.loads(base64.b64decode(p).decode())
print(f"Token aud={claims.get('aud')}  ver={claims.get('ver')}  iss={claims.get('iss')}")
print(f"      roles={claims.get('roles',[])}  appid={claims.get('appid')}")

# Raw GET to /_api/Web
resp = requests.get(
    "https://mycompany715.sharepoint.com/sites/data_ingest_dev/_api/Web",
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json;odata=nometadata",
    },
)
print(f"\nStatus: {resp.status_code}")
for k, v in resp.headers.items():
    print(f"  {k}: {v}")
print(f"Body: {resp.text[:400]}")
