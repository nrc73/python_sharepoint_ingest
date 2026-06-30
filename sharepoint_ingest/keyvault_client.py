"""Azure Key Vault secret provider helpers with environment fallback."""

from __future__ import annotations

import os
from typing import Optional

from azure.keyvault.secrets import SecretClient

from sharepoint_ingest._azure_credential import build_azure_credential
from sharepoint_ingest.config import AzureAuthSettings, KeyVaultSettings


class KeyVaultSecretProvider:
    def __init__(
        self,
        settings: KeyVaultSettings,
        azure_auth: Optional[AzureAuthSettings] = None,
    ):
        self._settings = settings
        self._azure_auth = azure_auth
        self._credential = build_azure_credential(
            auth_method=azure_auth.auth_method if azure_auth else None,
            allow_interactive_browser=(
                azure_auth.allow_interactive_browser if azure_auth else False
            ),
            env_name=getattr(azure_auth, "env_name", None),
        )
        self._client = SecretClient(vault_url=settings.vault_url, credential=self._credential)

    def get_secret(self, secret_name: str) -> str:
        return self._client.get_secret(secret_name).value

    @staticmethod
    def _env_with_fallback(base_name: str, env_name: Optional[str] = None) -> Optional[str]:
        if env_name:
            env_key = env_name.upper().strip()
            env_specific = os.getenv(f"{base_name}_{env_key}")
            if env_specific:
                return env_specific
        return os.getenv(base_name)

    def get_sharepoint_credentials(self, env_name: Optional[str] = None) -> tuple[str, str, str]:
        """
        Returns (client_id, client_secret, tenant_id).
        Falls back to environment variables when Key Vault is unavailable.
        """
        try:
            client_id = self.get_secret(self._settings.client_id_secret_name)
            client_secret = self.get_secret(self._settings.client_secret_secret_name)
            tenant_id = self.get_secret(self._settings.tenant_id_secret_name)
            return client_id, client_secret, tenant_id
        except Exception:
            client_id = self._env_with_fallback("SHAREPOINT_CLIENT_ID", env_name)
            client_secret = self._env_with_fallback("SHAREPOINT_CLIENT_SECRET", env_name)
            tenant_id = self._env_with_fallback("SHAREPOINT_TENANT_ID", env_name)
            if client_id and client_secret and tenant_id:
                return client_id, client_secret, tenant_id
            raise

    def get_sql_credentials(self, env_name: Optional[str] = None) -> tuple[str, str]:
        """
        Returns (username, password) for SQL authentication modes that require
        explicit credentials.
        Falls back to environment variables when Key Vault is unavailable.
        """
        secret_user_name = (self._settings.sql_username_secret_name or "").strip()
        secret_password_name = (self._settings.sql_password_secret_name or "").strip()

        if secret_user_name and secret_password_name:
            try:
                username = self.get_secret(secret_user_name)
                password = self.get_secret(secret_password_name)
                return username, password
            except Exception:
                pass

        username = self._env_with_fallback("SQL_SERVER_USERNAME", env_name)
        password = self._env_with_fallback("SQL_SERVER_PASSWORD", env_name)
        if username and password:
            return username, password

        raise ValueError(
            "SQL credentials not available from Key Vault or environment fallback. "
            "Configure KEYVAULT_SQL_USERNAME_SECRET_NAME[_ENV] / "
            "KEYVAULT_SQL_PASSWORD_SECRET_NAME[_ENV], or SQL_SERVER_USERNAME[_ENV] / SQL_SERVER_PASSWORD[_ENV]."
        )

    def _try_get_secret(self, secret_name: Optional[str]) -> Optional[str]:
        """Return the secret value or None if the name is blank or the read fails."""
        if not secret_name or not secret_name.strip():
            return None
        try:
            value = self.get_secret(secret_name.strip())
            return value if value and value.strip() else None
        except Exception:
            return None

    def get_sql_connection_info(self) -> dict[str, Optional[str]]:
        """
        Resolve SQL server hostname and per-role database names from Key Vault.

        Returns a dict with keys ``server``, ``aud_database``, ``stg_database``,
        and ``int_database``.  Each value is either the resolved string from Key
        Vault or ``None`` when the secret name is not configured or the read
        fails (callers should fall back to env-var defaults in that case).
        """
        return {
            "server":       self._try_get_secret(self._settings.sql_server_secret_name),
            "aud_database": self._try_get_secret(self._settings.sql_aud_database_secret_name),
            "stg_database": self._try_get_secret(self._settings.sql_stg_database_secret_name),
            "int_database": self._try_get_secret(self._settings.sql_int_database_secret_name),
        }


def maybe_build_provider(
    settings: KeyVaultSettings,
    azure_auth: Optional[AzureAuthSettings] = None,
) -> Optional[KeyVaultSecretProvider]:
    if not settings.vault_url:
        return None
    return KeyVaultSecretProvider(settings, azure_auth=azure_auth)
