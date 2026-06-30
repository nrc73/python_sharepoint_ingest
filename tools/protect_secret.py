"""CLI utility for encrypting secrets with Windows DPAPI.

Usage
-----
Run this **as the service account** that will later decrypt the secret.
DPAPI ties encryption to the Windows user identity, so only the same
account can decrypt.

Examples
--------
Encrypt a PFX password and write to a file::

    python tools/protect_secret.py \\
        --value "my-pfx-password" \\
        --output "C:\\ProgramData\\IngestAuth\\pfx-prod.enc"

Encrypt a client secret::

    python tools/protect_secret.py \\
        --value "my-client-secret" \\
        --output "C:\\ProgramData\\IngestAuth\\client-secret-prod.enc"

Read the secret from stdin (avoids leaving it in command history)::

    echo "my-secret" | python tools/protect_secret.py \\
        --stdin \\
        --output "C:\\ProgramData\\IngestAuth\\client-secret-prod.enc"

Verify decryption works::

    python tools/protect_secret.py \\
        --decrypt \\
        --input "C:\\ProgramData\\IngestAuth\\pfx-prod.enc"

Then set in .env::

    AZURE_CLIENT_CERTIFICATE_PASSWORD_FILE=C:\\ProgramData\\IngestAuth\\pfx-prod.enc
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sharepoint_ingest._secret_protector import dpapi_protect, _dpapi_unprotect


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Encrypt or decrypt a secret using Windows DPAPI. "
        "Run as the service account that will use the secret."
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--encrypt", action="store_true", help="Encrypt a secret")
    mode.add_argument("--decrypt", action="store_true", help="Decrypt a secret (for verification)")

    # Encryption options
    parser.add_argument("--value", help="Plaintext secret value to encrypt (avoid in shared terminals)")
    parser.add_argument("--stdin", action="store_true", help="Read secret from stdin instead of --value")
    parser.add_argument("--output", help="Output file path for encrypted bytes (encrypt mode)")

    # Decryption options
    parser.add_argument("--input", help="Input file path for encrypted bytes (decrypt mode)")

    args = parser.parse_args()

    if args.encrypt:
        # Get plaintext value
        if args.stdin:
            plaintext = sys.stdin.read().strip()
        elif args.value:
            plaintext = args.value
        else:
            parser.error("--value or --stdin is required for encryption")

        if not plaintext:
            parser.error("Secret value is empty")

        if not args.output:
            parser.error("--output is required for encryption")

        # Encrypt
        try:
            encrypted = dpapi_protect(plaintext)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(encrypted)

        # Set restrictive ACL (Windows only — best effort)
        username = "unknown"
        try:
            import subprocess
            username = subprocess.check_output(
                ["whoami"], text=True, shell=True
            ).strip()
            subprocess.run(
                ["icacls", str(output_path), "/inheritance:r",
                 "/grant:r", f"{username}:R"],
                capture_output=True, timeout=10,
            )
            print(f"[INFO] Set restrictive ACL on {output_path} for user {username}")
        except Exception:
            print(f"[WARN] Could not set ACL on {output_path}. Set manually with icacls.")

        print(f"[PASS] Encrypted secret written to: {output_path}")
        print(f"[INFO] File size: {len(encrypted)} bytes")
        print(f"[INFO] Only the current Windows account ({username}) can decrypt this file.")
        print()
        print("Configure in .env:")
        print(f"  AZURE_CLIENT_CERTIFICATE_PASSWORD_FILE={output_path}")
        print()
        print("Verify decryption:")
        print(f"  python tools/protect_secret.py --decrypt --input \"{output_path}\"")

    elif args.decrypt:
        if not args.input:
            parser.error("--input is required for decryption")

        input_path = Path(args.input)
        if not input_path.is_file():
            print(f"Error: File not found: {input_path}", file=sys.stderr)
            return 1

        encrypted = input_path.read_bytes()
        plaintext = _dpapi_unprotect(encrypted)

        if plaintext is None:
            print("Error: Decryption failed. Ensure you are running as the same "
                  "Windows account that encrypted the file.", file=sys.stderr)
            return 1

        print(f"[PASS] Decryption successful. Secret length: {len(plaintext)} characters")
        print(f"[INFO] Secret preview: {plaintext[:3]}...{plaintext[-2:] if len(plaintext) > 5 else ''}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())