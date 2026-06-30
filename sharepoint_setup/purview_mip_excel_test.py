"""Validate that the SPN can open an OLE2-encrypted (MIP/Purview IDM protected) Excel workbook
via the Microsoft Graph Excel workbook APIs.

This test:
  1. Scans the configured SharePoint folder (and one level of subfolders) for Excel files
     whose first 8 bytes match the OLE2 Compound Document signature — the fingerprint of
     both legacy BIFF .xls files and sensitivity-label encrypted .xlsx/.xlsm files.
  2. For each OLE2 candidate, attempts a Graph Excel workbook session against it.
       - 501 Not Implemented → the file is a legacy BIFF .xls (Graph does not support it);
         skip to the next candidate.
       - 403 Forbidden         → Purview IDM / MIP policy is blocking the SPN; report failure.
       - 200 success           → the SPN has the required MIP rights; report PASS.
  3. If all OLE2 files are BIFF legacy (501), reports "no MIP-encrypted files found — skip".
  4. Reports the exact file URL tested in every outcome.

Why this check exists
─────────────────────
The SharePoint auth test (sharepoint_auth_test.py) confirms that the SPN token is valid and
can list folder contents.  However, a Purview Information Protection (MIP) sensitivity label
on a specific workbook can still block the Graph Excel ``createSession`` call with 403 Forbidden
even when folder-listing passes.  This test covers that gap.

Two kinds of OLE2 Excel file
─────────────────────────────
* **Legacy BIFF .xls** — OLE2 compound document with a ``Workbook`` stream.
  Graph Excel APIs return ``501 Not Implemented`` for these; they are not relevant
  to MIP/Purview testing.
* **MIP-encrypted .xlsx** — OLE2 compound document with ``EncryptedPackage`` /
  ``EncryptionInfo`` streams.  These are the files that may trigger a ``403 Forbidden``
  from Graph when Purview IDM policy denies app-only access.

The test automatically distinguishes between the two based on the Graph response code.

Example:
    python sharepoint_setup/purview_mip_excel_test.py --env dev \\
        --folder "/sites/data_ingest_dev/Shared Documents/IncomingFiles"

    python sharepoint_setup/purview_mip_excel_test.py --env all

Env-var folder fallback (no --folder required when set):
    SHAREPOINT_TEST_FOLDER_DEV=/sites/data_ingest_dev/Shared Documents/IncomingFiles
    SHAREPOINT_TEST_FOLDER_PROD=/sites/data_ingest_prod/Shared Documents/IncomingFiles
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure the project root is importable when running as:
# python sharepoint_setup/purview_mip_excel_test.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import KeyVaultSecretProvider, maybe_build_provider
from sharepoint_ingest.sharepoint_client import SharePointClient


SUPPORTED_ENVIRONMENTS = ("dev", "prod")

# OLE2 Compound Document magic bytes — present in all sensitivity-label encrypted
# .xlsx/.xlsm files (and legacy .xls BIFF workbooks).
# Copied inline intentionally: avoids pulling the full excel_processor stack into a
# setup script that runs before the ingestion package is fully configured.
_OLE2_MAGIC: bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Excel file extensions worth scanning
_EXCEL_EXTENSIONS: tuple[str, ...] = (".xlsx", ".xlsm", ".xls")


def _resolve_target_envs(env_arg: str) -> list[str]:
    normalized = env_arg.lower().strip()
    if normalized == "all":
        return list(SUPPORTED_ENVIRONMENTS)
    if normalized in SUPPORTED_ENVIRONMENTS:
        return [normalized]
    raise ValueError(f"Unsupported --env '{env_arg}'. Use dev, prod, or all.")


def _folder_for_env(env_name: str, default_folder: str | None) -> str:
    env_key = env_name.upper()
    env_specific = os.getenv(f"SHAREPOINT_TEST_FOLDER_{env_key}", "").strip()
    if env_specific:
        return env_specific
    if default_folder:
        return default_folder
    raise ValueError(
        f"No folder supplied for env '{env_name}'. "
        f"Use --folder or set SHAREPOINT_TEST_FOLDER_{env_key}."
    )


def _resolve_credentials_from_keyvault(
    provider: KeyVaultSecretProvider,
    env_name: str,
    settings,
) -> tuple[str, str, str]:
    """Return (client_id, client_secret, tenant_id) from Key Vault."""
    client_id, client_secret, tenant_id = provider.get_sharepoint_credentials(env_name)
    if not (client_id and client_secret and tenant_id):
        raise ValueError(
            f"SP_MISSING_CREDENTIALS: Key Vault did not return complete SharePoint credentials "
            f"for env '{env_name}'. Ensure dm-sharepoint-{env_name}-client-id, "
            f"dm-sharepoint-{env_name}-client-secret, and dm-sharepoint-{env_name}-tenant-id "
            f"are seeded in the vault."
        )
    return client_id, client_secret, tenant_id


def _is_excel_file(name: str) -> bool:
    return name.lower().endswith(_EXCEL_EXTENSIONS)


def _scan_for_ole2_excel(
    sp_client: SharePointClient,
    folder_url: str,
    max_scan: int,
    env_name: str,
) -> list[str]:
    """Scan *folder_url* (and one level of subfolders) for OLE2 Excel files.

    Downloads only the first 8 bytes of each candidate file to check the magic bytes.
    Returns all matching server-relative URLs found within *max_scan* total probes.
    Stops scanning once *max_scan* files have been probed.
    """
    probed = 0
    found: list[str] = []

    def _probe_file(file_url: str) -> bool:
        """Return True if the file starts with OLE2 magic bytes."""
        nonlocal probed
        probed += 1
        header = sp_client.download_file_range_bytes(file_url, 0, 7)
        return header[:8] == _OLE2_MAGIC

    # ── Top-level files ────────────────────────────────────────────────────
    try:
        top_files = sp_client.list_files(folder_url)
    except Exception as exc:
        raise RuntimeError(
            f"[{env_name}] Could not list files in '{folder_url}': {exc}"
        ) from exc

    excel_files = [f for f in top_files if _is_excel_file(f.name)]
    print(
        f"[{env_name}] Scanning top-level folder: {folder_url} "
        f"({len(excel_files)} Excel file(s) found)"
    )

    for file_item in excel_files:
        if probed >= max_scan:
            return found
        try:
            if _probe_file(file_item.server_relative_url):
                found.append(file_item.server_relative_url)
        except Exception as exc:
            print(f"[{env_name}]   Skipping '{file_item.name}' (header read failed: {exc})")

    # ── One level of subfolders ────────────────────────────────────────────
    try:
        subfolders = sp_client.list_folders(folder_url)
    except Exception as exc:
        # Non-fatal: report and continue with what we have
        print(f"[{env_name}]   Warning: could not list subfolders in '{folder_url}': {exc}")
        subfolders = []

    for subfolder in subfolders:
        if probed >= max_scan:
            return found
        try:
            sub_files = sp_client.list_files(subfolder.server_relative_url)
        except Exception as exc:
            print(
                f"[{env_name}]   Skipping subfolder '{subfolder.name}' "
                f"(list failed: {exc})"
            )
            continue

        sub_excel = [f for f in sub_files if _is_excel_file(f.name)]
        if sub_excel:
            print(
                f"[{env_name}]   Subfolder '{subfolder.name}': "
                f"{len(sub_excel)} Excel file(s)"
            )

        for file_item in sub_excel:
            if probed >= max_scan:
                return found
            try:
                if _probe_file(file_item.server_relative_url):
                    found.append(file_item.server_relative_url)
            except Exception as exc:
                print(
                    f"[{env_name}]   Skipping '{file_item.name}' "
                    f"(header read failed: {exc})"
                )

    return found


def _probe_graph_excel_session(sp_client: SharePointClient, file_url: str) -> None:
    """Attempt to open a Graph Excel workbook session and list worksheets.

    Raises on any failure (including 403 Forbidden from Purview IDM / MIP and
    501 Not Implemented for legacy BIFF .xls files).
    Always closes the session in the finally block when a session was created.
    """
    session_id = ""
    try:
        session_id = sp_client.create_excel_workbook_session(file_url, persist_changes=False)
        worksheets = sp_client.list_excel_worksheets(file_url, session_id)
        _ = worksheets  # shape confirmed; cell values are deliberately not accessed
    finally:
        if session_id:
            try:
                sp_client.close_excel_workbook_session(file_url, session_id)
            except Exception:
                pass  # session close failure does not mask the original error


def _is_biff_error(exc: Exception) -> bool:
    """Return True when the Graph error indicates a legacy BIFF .xls (501)."""
    message = str(exc)
    return "501" in message or "not implemented" in message.lower()


def _format_graph_excel_error(exc: Exception, file_url: str) -> str:
    """Map a Graph Excel API error to an actionable diagnostic message."""
    message = str(exc)
    lowered = message.lower()

    if "501" in message or "not implemented" in lowered:
        return (
            "[BIFF_FORMAT] Graph Excel APIs returned 501 Not Implemented for this file.\n"
            "  This means the file is a legacy BIFF .xls workbook (OLE2 compound document\n"
            "  with a Workbook stream), not a MIP/sensitivity-label encrypted .xlsx.\n"
            "  Graph Excel workbook APIs only support OOXML .xlsx/.xlsm files.\n"
            "  This is not a Purview IDM permission error.\n"
            f"  File         : {file_url}\n"
            f"  Raw error    : {message}"
        )

    if "403" in message or "forbidden" in lowered:
        return (
            "[PERMISSION] The SPN token is valid but is not authorised to open this workbook "
            f"via Graph Excel APIs (403 Forbidden).\n"
            f"  File tested        : {file_url}\n"
            "[PERMISSION] Check Graph application permissions/admin consent, SharePoint site "
            "access, and sensitivity-label/MIP rights for the SPN.\n"
            "  Remediation steps  :\n"
            "    1. Confirm the app registration has 'Files.ReadWrite.All' or "
            "'Sites.ReadWrite.All' on the Graph resource with admin consent granted.\n"
            "    2. Verify the SPN has been granted explicit SharePoint site access "
            "(Sites.Selected or site-level permission grant).\n"
            "    3. Check Microsoft Purview / MIP policy: sensitivity labels with "
            "'Encrypt-Only' or restrictive access policies may deny app-only tokens "
            "regardless of Graph permissions. An exemption for the SPN may be required "
            "in the label policy.\n"
            "    4. Confirm admin consent has propagated — wait 2–5 minutes after any "
            "permission change and retry.\n"
            f"  Raw error          : {message}"
        )

    if "401" in message or "unauthorized" in lowered or "invalid_client" in lowered:
        return (
            "[AUTH] Graph returned 401 — the SPN token could not be acquired or was rejected.\n"
            "  Common causes      : bad client_secret in Key Vault; "
            "missing Sites.ReadWrite.All Graph AppRoleAssignment; "
            "admin consent not yet propagated.\n"
            f"  File tested        : {file_url}\n"
            f"  Raw error          : {message}"
        )

    if "404" in message or "not found" in lowered:
        return (
            "[NOT_FOUND] The drive item was not found (404).\n"
            "  Check that the file still exists at the path reported below and that "
            "the SharePoint site URL in Key Vault is correct.\n"
            f"  File tested        : {file_url}\n"
            f"  Raw error          : {message}"
        )

    return (
        f"[GRAPH_ERROR] Unexpected error from Graph Excel APIs.\n"
        f"  File tested        : {file_url}\n"
        f"  Raw error          : {message}"
    )


def _run_for_env(env_name: str, default_folder: str | None, max_scan: int) -> None:
    settings = load_settings(env_override=env_name)
    provider = maybe_build_provider(settings.key_vault, settings.azure_auth)

    if provider is None:
        vault_url = settings.key_vault.vault_url or settings.key_vault.vault_name
        raise ValueError(
            f"SP_NO_KEYVAULT_CONFIGURED\n"
            f"  No Key Vault URL could be resolved for env '{env_name}'.\n"
            f"  Expected env vars   : KEY_VAULT_URL_{env_name.upper()} or "
            f"KEY_VAULT_NAME_{env_name.upper()}\n"
            f"  Current value       : {vault_url!r}\n"
            f"  Resolution          : Set KEY_VAULT_URL_{env_name.upper()}="
            "https://<vault-name>.vault.azure.net/ in .env"
        )

    client_id, client_secret, tenant_id = _resolve_credentials_from_keyvault(
        provider, env_name, settings
    )

    # Resolve site URL from Key Vault (same logic as sharepoint_auth_test)
    site_url = settings.sharepoint.site_url
    if not site_url:
        raise ValueError(
            f"SharePoint site URL not resolved for env '{env_name}'. "
            "Ensure the site-url secret is seeded in Key Vault."
        )

    folder = _folder_for_env(env_name, default_folder)

    sp_client = SharePointClient(
        site_url=site_url,
        client_id=client_id,
        client_secret=client_secret,
        tenant_id=tenant_id,
    )

    print(f"[{env_name}] Purview MIP / Graph Excel check started")
    print(f"[{env_name}] Site URL : {site_url}")
    print(f"[{env_name}] Folder   : {folder}")
    print(f"[{env_name}] Max scan : {max_scan} file(s)")

    # ── Scan for OLE2 Excel files ─────────────────────────────────────────
    ole2_candidates = _scan_for_ole2_excel(sp_client, folder, max_scan, env_name)

    if not ole2_candidates:
        print(
            f"[{env_name}] No OLE2-format Excel file found in the scanned folder(s) — "
            "sensitivity-label/Purview MIP test skipped.\n"
            f"[{env_name}] (This is expected on dev systems without protected workbooks. "
            "The test will run automatically once a sensitivity-label protected .xlsx "
            "is present in the configured folder.)"
        )
        return

    print(f"[{env_name}] OLE2 file(s) found : {len(ole2_candidates)}")

    # ── Probe each candidate, skipping legacy BIFF (501) files ───────────
    biff_skipped: list[str] = []

    for file_url in ole2_candidates:
        print(f"[{env_name}] Probing              : {file_url}")
        try:
            _probe_graph_excel_session(sp_client, file_url)
            # Success — this is a MIP-encrypted file the SPN can open
            print(f"[{env_name}] Tested file        : {file_url}")
            print(f"[{env_name}] Graph Excel / Purview MIP check: PASS")
            return
        except Exception as exc:
            if _is_biff_error(exc):
                # Legacy BIFF .xls — Graph doesn't support it; not a permission error
                print(
                    f"[{env_name}]   → legacy BIFF .xls (Graph 501 — not MIP-encrypted, "
                    "skipping)"
                )
                biff_skipped.append(file_url)
                continue
            # Any other error (403, 401, 404, …) is a real failure
            raise RuntimeError(_format_graph_excel_error(exc, file_url)) from exc

    # ── All OLE2 candidates were BIFF legacy files ────────────────────────
    print(
        f"[{env_name}] All {len(biff_skipped)} OLE2 file(s) found are legacy BIFF .xls "
        "(Graph returned 501 Not Implemented for each) — not MIP-encrypted.\n"
        f"[{env_name}] Sensitivity-label/Purview MIP test skipped.\n"
        f"[{env_name}] (This is expected on dev systems. The test will activate once a "
        "MIP-encrypted .xlsx is present in the configured folder.)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate SPN access to OLE2-encrypted (MIP/Purview protected) Excel workbooks "
            "via the Microsoft Graph Excel workbook APIs."
        )
    )
    parser.add_argument("--env", default="prod", help="Environment name: dev, prod, or all")
    parser.add_argument(
        "--folder",
        required=False,
        help=(
            "SharePoint server-relative folder path to scan for OLE2 Excel files, e.g. "
            "'/sites/data_ingest_dev/Shared Documents/IncomingFiles'. "
            "Alternatively set SHAREPOINT_TEST_FOLDER_<ENV>."
        ),
    )
    parser.add_argument(
        "--max-scan",
        type=int,
        default=20,
        help=(
            "Maximum number of Excel files to probe for OLE2 magic bytes before giving up "
            "the scan. Default: 20."
        ),
    )
    args = parser.parse_args()

    target_envs = _resolve_target_envs(args.env)
    failed_envs: list[str] = []

    for env_name in target_envs:
        try:
            _run_for_env(env_name, args.folder, args.max_scan)
        except Exception as exc:
            failed_envs.append(env_name)
            print(f"[{env_name}] FAILED: {exc}")

    if failed_envs:
        print(
            f"Purview MIP / Graph Excel pre-check failed for environment(s): "
            f"{', '.join(failed_envs)}"
        )
        return 1

    print("Purview MIP / Graph Excel pre-check passed for all requested environment(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
