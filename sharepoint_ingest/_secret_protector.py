"""Secure secret resolution with fallback chain: keyring → DPAPI file → plain env var.

This module provides a single entry point, :func:`resolve_secret`, that
resolves a sensitive value (e.g. a PFX password or client secret) from the
best-available storage location without the caller needing to know which
storage method was configured.

Resolution order (first non-empty value wins)
─────────────────────────────────────────────
1. **Keyring** — ``{base}_KEYRING`` env var names a keyring service/key pair.
   On Windows this uses the Windows Credential Manager (DPAPI-protected).
2. **DPAPI-encrypted file** — ``{base}_FILE`` env var points to a file
   containing DPAPI-encrypted bytes.  Only the Windows account that
   encrypted the file can decrypt it.
3. **Plain env var** — ``{base}`` env var holds the plaintext value.
   Suitable for local dev only.
4. **None** — no var set; the caller treats this as "no password" (valid for
   B2 cert-store auth or no-password PFX files).

The module degrades gracefully on non-Windows platforms: DPAPI file
decryption raises a clear error, while keyring and plain env vars work
everywhere.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform.startswith("win")


# ---------------------------------------------------------------------------
# Keyring support (optional dependency)
# ---------------------------------------------------------------------------

def _resolve_from_keyring(keyring_spec: str) -> Optional[str]:
    """Read a secret from the system keyring.

    ``keyring_spec`` is ``"service:key"`` — e.g. ``"azure-ingest:pfx-prod"``.
    Returns ``None`` if the keyring library is unavailable or the secret
    is not found.
    """
    try:
        import keyring  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("keyring package not installed; skipping keyring resolution")
        return None

    if ":" not in keyring_spec:
        logger.warning("Invalid keyring spec '%s' (expected 'service:key')", keyring_spec)
        return None

    service, _, key = keyring_spec.partition(":")
    service = service.strip()
    key = key.strip()
    if not service or not key:
        logger.warning("Invalid keyring spec '%s' (empty service or key)", keyring_spec)
        return None

    try:
        value = keyring.get_password(service, key)
    except Exception as exc:
        logger.warning("keyring.get_password('%s', '%s') failed: %s", service, key, exc)
        return None

    if value:
        logger.debug("Resolved secret from keyring (service=%s, key=%s)", service, key)
    return value


# ---------------------------------------------------------------------------
# DPAPI-encrypted file support (Windows only)
# ---------------------------------------------------------------------------

def _resolve_from_dpapi_file(file_path: str) -> Optional[str]:
    """Read and DPAPI-decrypt a secret from a file (Windows only).

    The file must have been created by ``tools/protect_secret.py`` (or any
    tool that writes ``CryptUnprotectData`` output as raw bytes).
    """
    if not _IS_WINDOWS:
        logger.warning(
            "DPAPI-encrypted file secrets are only supported on Windows; "
            "skipping configured file path: %s",
            file_path,
        )
        return None

    path = Path(file_path)
    if not path.is_file():
        logger.warning("DPAPI secret file not found: %s", file_path)
        return None

    try:
        encrypted_bytes = path.read_bytes()
    except OSError as exc:
        logger.warning("Could not read DPAPI secret file '%s': %s", file_path, exc)
        return None

    if not encrypted_bytes:
        logger.warning("DPAPI secret file is empty: %s", file_path)
        return None

    plaintext = _dpapi_unprotect(encrypted_bytes)
    if plaintext:
        logger.debug("Resolved secret from DPAPI file (path=%s)", file_path)
    return plaintext


def _dpapi_unprotect(encrypted_bytes: bytes) -> Optional[str]:
    """Decrypt bytes using Windows DPAPI ``CryptUnprotectData``."""
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        blob_in = DATA_BLOB(
            cbData=len(encrypted_bytes),
            pbData=ctypes.cast(
                ctypes.c_char_p(encrypted_bytes),
                ctypes.POINTER(ctypes.c_char),
            ),
        )
        blob_out = DATA_BLOB()

        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        ):
            error_code = ctypes.get_last_error()
            raise OSError(f"CryptUnprotectData failed (Win32 error {error_code})")

        try:
            plaintext = ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)

        return plaintext
    except Exception as exc:
        logger.error("DPAPI decryption failed: %s", exc)
        return None


def dpapi_protect(plaintext: str) -> bytes:
    """Encrypt a string using Windows DPAPI ``CryptProtectData``.

    The returned bytes can be written to a file and later decrypted by
    :func:`_dpapi_unprotect` — but only by the same Windows user account.
    """
    if not _IS_WINDOWS:
        raise RuntimeError("DPAPI encryption is only supported on Windows.")

    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    data_bytes = plaintext.encode("utf-8")
    blob_in = DATA_BLOB(
        cbData=len(data_bytes),
        pbData=ctypes.cast(
            ctypes.c_char_p(data_bytes),
            ctypes.POINTER(ctypes.c_char),
        ),
    )
    blob_out = DATA_BLOB()

    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        error_code = ctypes.get_last_error()
        raise OSError(f"CryptProtectData failed (Win32 error {error_code})")

    try:
        encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)

    return encrypted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _env_value(name: str, env_name: Optional[str] = None) -> Optional[str]:
    """Return env-specific value first, then shared value."""
    if env_name:
        env_key = env_name.upper().strip()
        specific = os.getenv(f"{name}_{env_key}")
        if specific and specific.strip():
            return specific
    value = os.getenv(name)
    return value if value and value.strip() else None


def resolve_secret(base_env_var: str, env_name: Optional[str] = None) -> Optional[str]:
    """Resolve a secret value using the fallback chain.

    Checks, in order:
    1. ``{base_env_var}_KEYRING`` → keyring (service:key format)
    2. ``{base_env_var}_FILE`` → DPAPI-encrypted file path
    3. ``{base_env_var}`` → plain env var
    4. None

    Parameters
    ----------
    base_env_var:
        The base environment variable name, e.g.
        ``"AZURE_CLIENT_CERTIFICATE_PASSWORD"`` or ``"AZURE_CLIENT_SECRET"``.

    Returns
    -------
    Optional[str]
        The secret value, or ``None`` if no source is configured.
    """
    # 1. Keyring
    keyring_var = f"{base_env_var}_KEYRING"
    keyring_spec = _env_value(keyring_var, env_name)
    if keyring_spec and keyring_spec.strip():
        value = _resolve_from_keyring(keyring_spec.strip())
        if value:
            return value
        logger.warning(
            "Keyring resolution configured (%s=%s) but returned no value; "
            "falling through to next method",
            keyring_var,
            keyring_spec,
        )

    # 2. DPAPI-encrypted file
    file_var = f"{base_env_var}_FILE"
    file_path = _env_value(file_var, env_name)
    if file_path and file_path.strip():
        value = _resolve_from_dpapi_file(file_path.strip())
        if value:
            return value
        logger.warning(
            "DPAPI file resolution configured (%s=%s) but returned no value; "
            "falling through to next method",
            file_var,
            file_path,
        )

    # 3. Plain env var
    plain_value = _env_value(base_env_var, env_name)
    if plain_value and plain_value.strip():
        logger.debug("Resolved secret from plain env var (%s)", base_env_var)
        return plain_value

    # 4. None
    logger.debug("No secret configured for %s (returning None)", base_env_var)
    return None