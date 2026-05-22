from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


def _as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y"}


@dataclass
class SqlSettings:
    host: str
    port: int
    username: str
    password: str
    auth_mode: str
    database: str
    odbc_driver: str
    trust_server_certificate: bool


@dataclass
class KeyVaultSettings:
    vault_name: str
    vault_url: str
    client_id_secret_name: str
    client_secret_secret_name: str
    tenant_id_secret_name: str
    sql_username_secret_name: Optional[str]
    sql_password_secret_name: Optional[str]


@dataclass
class SharePointSettings:
    site_url: str
    admin_url: Optional[str]


@dataclass
class EmailSettings:
    enabled: bool
    host: Optional[str]
    port: int
    username: Optional[str]
    password: Optional[str]
    use_tls: bool
    from_address: str


@dataclass
class AppSettings:
    env_name: str
    log_level: str
    default_load_strategy: str
    default_file_pattern: str
    null_alert_threshold: float
    enable_chunked_csv: bool
    enable_chunked_parquet: bool
    ingest_chunk_size_rows: int
    azure_subscription_id: Optional[str]
    azure_resource_group: Optional[str]
    sql: SqlSettings
    key_vault: KeyVaultSettings
    sharepoint: SharePointSettings
    email: EmailSettings


def _database_for_env(env_name: str) -> str:
    env_name = env_name.lower().strip()
    if env_name == "dev":
        return os.getenv("SQL_DATABASE_DEV", "ingest_dev")
    if env_name == "test":
        return os.getenv("SQL_DATABASE_TEST", "ingest_test")
    return os.getenv("SQL_DATABASE_PROD", "ingest_prod")


def _sql_host_for_env(env_name: str) -> str:
    env_key = env_name.upper().strip()
    return os.getenv(f"SQL_SERVER_HOST_{env_key}") or os.getenv("SQL_SERVER_HOST", "localhost")


def _sql_auth_mode_for_env(env_name: str) -> str:
    env_key = env_name.upper().strip()
    return (
        os.getenv(f"SQL_AUTH_MODE_{env_key}")
        or os.getenv("SQL_AUTH_MODE")
        or "sql_password"
    ).strip()


def _sql_username_for_env(env_name: str) -> str:
    env_key = env_name.upper().strip()
    return os.getenv(f"SQL_SERVER_USERNAME_{env_key}") or os.getenv("SQL_SERVER_USERNAME", "")


def _sql_password_for_env(env_name: str) -> str:
    env_key = env_name.upper().strip()
    return os.getenv(f"SQL_SERVER_PASSWORD_{env_key}") or os.getenv("SQL_SERVER_PASSWORD", "")


def _secret_name_for_env(base_env_var: str, env_name: str, default: str = "") -> str:
    env_key = env_name.upper().strip()
    return os.getenv(f"{base_env_var}_{env_key}") or os.getenv(base_env_var, default)


def _key_vault_name_for_env(env_name: str) -> str:
    return _secret_name_for_env("KEY_VAULT_NAME", env_name, default="")


def _key_vault_url_for_env(env_name: str, vault_name: str) -> str:
    resolved_url = _secret_name_for_env("KEY_VAULT_URL", env_name, default="")
    if resolved_url:
        return resolved_url
    if vault_name:
        return f"https://{vault_name}.vault.azure.net/"
    return ""


def _sharepoint_url_for_env(env_name: str) -> str:
    env_name = env_name.lower().strip()
    if env_name == "dev":
        return os.getenv("SHAREPOINT_SITE_URL_DEV", "")
    if env_name == "test":
        return os.getenv("SHAREPOINT_SITE_URL_TEST", "")
    return os.getenv("SHAREPOINT_SITE_URL_PROD", "")


def load_settings(env_override: Optional[str] = None) -> AppSettings:
    load_dotenv(override=False)

    env_name = (env_override or os.getenv("APP_ENV") or "prod").lower().strip()

    sql_settings = SqlSettings(
        host=_sql_host_for_env(env_name),
        port=int(os.getenv("SQL_SERVER_PORT", "1433")),
        username=_sql_username_for_env(env_name),
        password=_sql_password_for_env(env_name),
        auth_mode=_sql_auth_mode_for_env(env_name),
        database=_database_for_env(env_name),
        odbc_driver=os.getenv("SQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"),
        trust_server_certificate=_as_bool(os.getenv("SQL_TRUST_SERVER_CERTIFICATE"), default=True),
    )

    key_vault_name = _key_vault_name_for_env(env_name)
    key_vault_url = _key_vault_url_for_env(env_name, key_vault_name)

    key_vault_settings = KeyVaultSettings(
        vault_name=key_vault_name,
        vault_url=key_vault_url,
        client_id_secret_name=_secret_name_for_env(
            "KEYVAULT_CLIENT_ID_SECRET_NAME", env_name, default="dm-sharepoint-client-id"
        ),
        client_secret_secret_name=_secret_name_for_env(
            "KEYVAULT_CLIENT_SECRET_SECRET_NAME", env_name, default="dm-sharepoint-client-secret"
        ),
        tenant_id_secret_name=_secret_name_for_env(
            "KEYVAULT_TENANT_ID_SECRET_NAME", env_name, default="dm-sharepoint-tenant-id"
        ),
        sql_username_secret_name=_secret_name_for_env("KEYVAULT_SQL_USERNAME_SECRET_NAME", env_name),
        sql_password_secret_name=_secret_name_for_env("KEYVAULT_SQL_PASSWORD_SECRET_NAME", env_name),
    )

    sharepoint_settings = SharePointSettings(
        site_url=_sharepoint_url_for_env(env_name),
        admin_url=os.getenv("SHAREPOINT_ADMIN_URL_PROD"),
    )

    email_settings = EmailSettings(
        enabled=_as_bool(os.getenv("EMAIL_NOTIFICATIONS_ENABLED"), default=False),
        host=os.getenv("SMTP_HOST"),
        port=int(os.getenv("SMTP_PORT", "587")),
        username=os.getenv("SMTP_USER"),
        password=os.getenv("SMTP_PASSWORD"),
        use_tls=_as_bool(os.getenv("SMTP_USE_TLS"), default=True),
        from_address=os.getenv("EMAIL_FROM_ADDRESS", "sharepoint-ingest@company715.onmicrosoft.com"),
    )

    return AppSettings(
        env_name=env_name,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        default_load_strategy=os.getenv("DEFAULT_LOAD_STRATEGY", "TRUNCATE"),
        default_file_pattern=os.getenv("DEFAULT_FILE_PATTERN", "*"),
        null_alert_threshold=float(os.getenv("NULL_ALERT_THRESHOLD", "0.90")),
        enable_chunked_csv=_as_bool(os.getenv("ENABLE_CHUNKED_CSV"), default=False),
        enable_chunked_parquet=_as_bool(os.getenv("ENABLE_CHUNKED_PARQUET"), default=True),
        ingest_chunk_size_rows=max(1, int(os.getenv("INGEST_CHUNK_SIZE_ROWS", "5000"))),
        azure_subscription_id=os.getenv("AZURE_SUBSCRIPTION_ID") or os.getenv("AZURE_SUBSCRIPTION"),
        azure_resource_group=os.getenv("AZURE_RESOURCE_GROUP"),
        sql=sql_settings,
        key_vault=key_vault_settings,
        sharepoint=sharepoint_settings,
        email=email_settings,
    )
