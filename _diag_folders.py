"""Quick folder discovery for dev and prod SharePoint sites."""
from __future__ import annotations
import sys, traceback
sys.path.insert(0, ".")

for env_name in ("dev", "prod"):
    print(f"\n=== {env_name.upper()} ===", flush=True)
    try:
        from src.config import load_settings
        from src.keyvault_client import maybe_build_provider
        from src.sharepoint_client import SharePointClient

        settings = load_settings(env_override=env_name)
        provider = maybe_build_provider(settings.key_vault)
        client_id, client_secret, tenant_id = provider.get_sharepoint_credentials(env_name)
        site_url = settings.sharepoint.site_url
        print(f"Site URL: {site_url}", flush=True)

        sp = SharePointClient(site_url, client_id, client_secret, tenant_id)

        # Web title
        web = sp._ctx.web
        sp._ctx.load(web)
        sp._ctx.execute_query()
        print(f"Site title: {web.properties.get('Title')}", flush=True)

        # Probe known library paths
        site_rel = site_url.replace("https://mycompany715.sharepoint.com", "")
        for lib in ("Shared Documents", "Documents", "General"):
            for subfolder in ("", "/General", "/Input for ETL", "/General/Input for ETL"):
                candidate = f"{site_rel}/{lib}{subfolder}"
                try:
                    folder = sp._ctx.web.get_folder_by_server_relative_url(candidate)
                    sp._ctx.load(folder)
                    sp._ctx.execute_query()
                    name = folder.properties.get("Name", "?")
                    print(f"  FOUND: {candidate}  (Name={name})", flush=True)
                except Exception as e:
                    short = str(e)[:60]
                    print(f"  miss : {candidate}  ({short})", flush=True)
    except Exception:
        traceback.print_exc()
