"""Compare delegated vs app-only token claims and test REST access with each."""
from __future__ import annotations
import json, base64, subprocess, sys
PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests

def decode(token: str) -> dict:
    p = token.split(".")[1]
    p += "=" * (4 - len(p) % 4)
    return json.loads(base64.b64decode(p).decode())

# 1. Get delegated user token from az CLI
proc = subprocess.run(
    ["az", "account", "get-access-token",
     "--resource", "https://mycompany715.sharepoint.com",
     "--query", "accessToken", "-o", "tsv"],
    capture_output=True, text=True
)
user_token = proc.stdout.strip()
ud = decode(user_token)
print("=== Delegated (az login) token ===")
print(f"  aud={ud.get('aud')}  ver={ud.get('ver')}  iss={ud.get('iss')}")
print(f"  scp={ud.get('scp','')}  roles={ud.get('roles',[])}  upn={ud.get('upn','')}")

r = requests.get(
    "https://mycompany715.sharepoint.com/sites/data_ingest_dev/_api/Web?$select=Title",
    headers={"Authorization": f"Bearer {user_token}", "Accept": "application/json;odata=nometadata"},
)
print(f"  REST status={r.status_code}  body={r.text[:200]}")

# 2. App-only token
from msal import ConfidentialClientApplication
from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import maybe_build_provider

settings = load_settings(env_override="dev")
provider = maybe_build_provider(settings.key_vault, settings.azure_auth)
client_id, client_secret, tenant_id = provider.get_sharepoint_credentials("dev")

msal_app = ConfidentialClientApplication(
    client_id=client_id, client_credential=client_secret,
    authority=f"https://login.microsoftonline.com/{tenant_id}",
)
msal_app.remove_tokens_for_account   # clear cache trick
result = msal_app.acquire_token_for_client(
    scopes=["https://mycompany715.sharepoint.com/.default"]
)
app_token = result["access_token"]
ad = decode(app_token)
print("\n=== App-only (MSAL client creds) token ===")
print(f"  aud={ad.get('aud')}  ver={ad.get('ver')}  iss={ad.get('iss')}")
print(f"  roles={ad.get('roles',[])}  appid={ad.get('appid')}")

r2 = requests.get(
    "https://mycompany715.sharepoint.com/sites/data_ingest_dev/_api/Web?$select=Title",
    headers={"Authorization": f"Bearer {app_token}", "Accept": "application/json;odata=nometadata"},
)
print(f"  REST status={r2.status_code}  body={r2.text[:200]}")
