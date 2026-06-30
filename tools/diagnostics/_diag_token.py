"""Decode both Graph and SharePoint-scoped tokens to compare roles claims."""
from __future__ import annotations
import json, base64, sys
PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import maybe_build_provider
from msal import ConfidentialClientApplication

def decode_jwt(token: str) -> dict:
    p = token.split(".")[1]
    p += "=" * (4 - len(p) % 4)
    return json.loads(base64.b64decode(p).decode())

settings = load_settings(env_override="dev")
provider = maybe_build_provider(settings.key_vault, settings.azure_auth)
client_id, client_secret, tenant_id = provider.get_sharepoint_credentials("dev")

msal_app = ConfidentialClientApplication(
    client_id=client_id,
    client_credential=client_secret,
    authority=f"https://login.microsoftonline.com/{tenant_id}",
)

# 1. Graph token
g = msal_app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
gd = decode_jwt(g["access_token"])
print(f"Graph token   aud={gd.get('aud')}  roles={gd.get('roles', [])}")

# 2. SharePoint token
sp = msal_app.acquire_token_for_client(scopes=["https://mycompany715.sharepoint.com/.default"])
spd = decode_jwt(sp["access_token"])
print(f"SP token      aud={spd.get('aud')}  roles={spd.get('roles', [])}")
print(f"SP token      scp={spd.get('scp', '')}  appid={spd.get('appid','')}")
