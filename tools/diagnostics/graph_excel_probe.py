"""Probe Microsoft Graph Excel workbook extraction for a SharePoint file.

This diagnostic is intentionally conservative: it reports worksheet/range shape
only and does not print workbook cell values.  Use it in the company tenant to
validate whether Graph Excel APIs can open sensitivity-label protected workbooks
with the configured SharePoint app-only credentials.

Example:
    python tools/diagnostics/graph_excel_probe.py \
        --env dev \
        --file-url "/sites/data_ingest_dev/Shared Documents/IncomingFiles/protected.xlsx"
"""

from __future__ import annotations

import argparse
import sys

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import maybe_build_provider
from sharepoint_ingest.main import _resolve_sharepoint_credentials
from sharepoint_ingest.sharepoint_client import SharePointClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe Graph Excel workbook APIs")
    parser.add_argument("--env", default="dev", help="Environment name used for settings/Key Vault")
    parser.add_argument(
        "--file-url",
        required=True,
        help="SharePoint server-relative URL of the Excel workbook",
    )
    parser.add_argument(
        "--sheet",
        default="",
        help="Optional worksheet name/id to probe. Defaults to the first worksheet returned by Graph.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    settings = load_settings(env_override=args.env)
    provider = maybe_build_provider(settings.key_vault)
    client_id, client_secret, tenant_id = _resolve_sharepoint_credentials(settings, provider)

    if not settings.sharepoint.site_url:
        raise ValueError(
            "SharePoint site URL is required. Configure Key Vault site-url secret "
            "or SHAREPOINT_SITE_URL_<ENV> for diagnostics."
        )

    sp = SharePointClient(settings.sharepoint.site_url, client_id, client_secret, tenant_id)

    print("Graph Excel probe")
    print(f"  env: {settings.env_name}")
    print(f"  file: {args.file_url}")
    print("  auth: app-only SharePoint client credentials")

    session_id = ""
    try:
        item = sp.get_file_item(args.file_url)
        print(f"  driveItem resolved: yes  size_bytes={item.get('size', '<unknown>')}")

        session_id = sp.create_excel_workbook_session(args.file_url, persist_changes=False)
        print("  createSession: success")

        worksheets = sp.list_excel_worksheets(args.file_url, session_id)
        print(f"  worksheets: {len(worksheets)}")
        for idx, worksheet in enumerate(worksheets[:20], start=1):
            print(f"    {idx}. name={worksheet.get('name', '<unnamed>')} id={worksheet.get('id', '<no-id>')}")

        if not worksheets:
            print("  usedRange: skipped because no worksheets were returned")
            return 2

        selected = None
        if args.sheet:
            for worksheet in worksheets:
                if args.sheet in {str(worksheet.get("id", "")), str(worksheet.get("name", ""))}:
                    selected = worksheet
                    break
            if selected is None:
                raise ValueError(f"Worksheet '{args.sheet}' was not found")
        else:
            selected = worksheets[0]

        worksheet_id = str(selected.get("id") or selected.get("name"))
        used_range = sp.get_excel_used_range(args.file_url, session_id, worksheet_id, values_only=True)
        values = list(used_range.get("values") or [])
        row_count = used_range.get("rowCount", len(values))
        column_count = used_range.get("columnCount", len(values[0]) if values else 0)
        print(
            "  usedRange: success  "
            f"sheet={selected.get('name', worksheet_id)}  "
            f"rowCount={row_count}  columnCount={column_count}"
        )
        print("  result: Graph Excel app-only extraction appears viable for this file")
        return 0
    except Exception as exc:
        print(f"  result: failed  {type(exc).__name__}: {exc}")
        print(
            "  note: if this fails for a protected workbook that opens in Excel Online "
            "with your user account, test delegated Graph auth next."
        )
        return 1
    finally:
        if session_id:
            try:
                sp.close_excel_workbook_session(args.file_url, session_id)
                print("  closeSession: success")
            except Exception as exc:
                print(f"  closeSession: failed  {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))