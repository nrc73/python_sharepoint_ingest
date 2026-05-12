"""Restore files from SharePoint Failed folders back to their input folders."""
from __future__ import annotations
import sys
sys.path.insert(0, ".")

from src.config import load_settings
from src.keyvault_client import maybe_build_provider
from src.sharepoint_client import SharePointClient

settings = load_settings(env_override="dev")
provider = maybe_build_provider(settings.key_vault)
client_id, client_secret, tenant_id = provider.get_sharepoint_credentials("dev")
sp = SharePointClient(settings.sharepoint.site_url, client_id, client_secret, tenant_id)

site_path = sp._site_path  # e.g. /sites/data_ingest_dev

# Map: failed_folder -> input_folder
folder_pairs = [
    ("/Documents/valid_transactions/Failed",        "/Documents/valid_transactions"),
    ("/Documents/valid_transactions_large/Failed",   "/Documents/valid_transactions_large"),
    ("/Documents/valid_customers/Failed",            "/Documents/valid_customers"),
]

# Prefix site_path so these become server-relative
def site_rel(path: str) -> str:
    return f"{site_path}{path}"

total_restored = 0
for failed_rel, input_rel in folder_pairs:
    full_failed = site_rel(failed_rel)
    full_input  = site_rel(input_rel)
    try:
        items = sp.list_files(full_failed)
    except Exception as e:
        print(f"Could not list {full_failed}: {e}")
        items = []

    if not items:
        print(f"{full_failed}: 0 files (nothing to restore)")
        continue

    print(f"{full_failed}: {len(items)} file(s) -> restoring to {full_input}")
    for item in items:
        try:
            new_url = sp.move_file(item.server_relative_url, full_input)
            print(f"  RESTORED: {item.name}  -> {new_url}")
            total_restored += 1
        except Exception as e:
            print(f"  ERROR restoring {item.name}: {e}")

print(f"\nTotal files restored: {total_restored}")
