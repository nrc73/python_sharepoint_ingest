"""Test user token vs app-only token against SharePoint REST, print all headers."""
import requests, base64, json, sys
sys.path.insert(0, ".")

TOKEN_FILE = r"E:\DockerTemp\sp_user_token.txt"

t = open(TOKEN_FILE).read().strip()
p = t.split(".")[1]
p += "=" * (4 - len(p) % 4)
d = json.loads(base64.b64decode(p).decode())
print(f"User token: aud={d.get('aud')}  ver={d.get('ver')}  scp={d.get('scp')}  upn={d.get('upn')}")

r = requests.get(
    "https://mycompany715.sharepoint.com/sites/data_ingest_dev/_api/Web?$select=Title",
    headers={"Authorization": f"Bearer {t}", "Accept": "application/json;odata=nometadata"},
)
print(f"  status={r.status_code}")
for k, v in r.headers.items():
    print(f"  {k}: {v}")
print(f"  body={r.text[:400]}")
