"""Azure Key Vault secret provider helpers with environment fallback."""

from __future__ import annotations

import os
from typing import Optional

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

from sharepoint_ingest.config import KeyVaultSettings


class KeyVaultSecretProvider:
    def __init__(self, settings: KeyVaultSettings):
        self._settings = settings
        self._credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
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


def maybe_build_provider(settings: KeyVaultSettings) -> Optional[KeyVaultSecretProvider]:
    if not settings.vault_url:
        return None
    return KeyVaultSecretProvider(settings)
