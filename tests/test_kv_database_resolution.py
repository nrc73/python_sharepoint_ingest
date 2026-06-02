"""Tests for _resolve_database_names() in main.py.

Database names (aud / stg / int) must be sourced exclusively from Azure Key
Vault.  There is no env-var fallback — the application must raise immediately
if Key Vault is unavailable or any secret is absent.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from sharepoint_ingest.config import (
    AppSettings,
    EmailSettings,
    KeyVaultSettings,
    SharePointSettings,
    SqlSettings,
)
from sharepoint_ingest.main import _resolve_database_names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sql(database: str = "") -> SqlSettings:
    return SqlSettings(
        host="localhost",
        port=1433,
        username="",
        password="",
        auth_mode="sspi",
        database=database,
        odbc_driver="ODBC Driver 18 for SQL Server",
        trust_server_certificate=True,
    )


def _make_kv_settings() -> KeyVaultSettings:
    return KeyVaultSettings(
        vault_name="kv-sp-ingest-dev",
        vault_url="https://kv-sp-ingest-dev.vault.azure.net/",
        client_id_secret_name="dm-sharepoint-dev-client-id",
        client_secret_secret_name="dm-sharepoint-dev-client-secret",
        tenant_id_secret_name="dm-sharepoint-dev-tenant-id",
        site_url_secret_name="dm-sharepoint-dev-site-url",
        sql_server_secret_name="dm-sql-dev-server",
        sql_int_database_secret_name="dm-sql-dev-int-database",
        sql_stg_database_secret_name="dm-sql-dev-stg-database",
        sql_aud_database_secret_name="dm-sql-dev-aud-database",
        sql_username_secret_name=None,
        sql_password_secret_name=None,
    )


def _make_settings() -> AppSettings:
    return AppSettings(
        env_name="dev",
        log_level="INFO",
        allow_test_data_in_prod=False,
        default_load_strategy="TRUNCATE",
        default_file_pattern="*",
        null_alert_threshold=0.9,
        enable_chunked_csv=False,
        enable_chunked_parquet=True,
        ingest_chunk_size_rows=5000,
        azure_subscription_id=None,
        azure_resource_group=None,
        sql=_make_sql(),       # aud — empty placeholder from load_settings()
        sql_stg=_make_sql(),   # stg — empty placeholder
        sql_int=_make_sql(),   # int — empty placeholder
        key_vault=_make_kv_settings(),
        sharepoint=SharePointSettings(site_url="", admin_url=None),
        email=EmailSettings(
            enabled=False,
            host=None,
            port=587,
            username=None,
            password=None,
            use_tls=True,
            from_address="test@example.com",
        ),
    )


def _make_provider(
    aud_database: str | None = "ingest_audit_dev",
    stg_database: str | None = "ingest_stg_dev",
    int_database: str | None = "ingest_int_dev",
    server: str | None = "dev-sql.local",
) -> MagicMock:
    provider = MagicMock()
    provider.get_sql_connection_info.return_value = {
        "server": server,
        "aud_database": aud_database,
        "stg_database": stg_database,
        "int_database": int_database,
    }
    return provider


_LOGGER = logging.getLogger("test")


# ---------------------------------------------------------------------------
# Happy-path: all three names resolved from KV
# ---------------------------------------------------------------------------

def test_all_three_databases_resolved_from_kv():
    provider = _make_provider(
        aud_database="ingest_audit_dev",
        stg_database="ingest_stg_dev",
        int_database="ingest_int_dev",
    )
    result = _resolve_database_names(_make_settings(), provider, _LOGGER)

    assert result.sql.database == "ingest_audit_dev"
    assert result.sql_stg.database == "ingest_stg_dev"
    assert result.sql_int.database == "ingest_int_dev"


def test_kv_values_override_placeholder_empty_strings():
    """load_settings() sets database="" — KV values must replace them."""
    settings = _make_settings()
    assert settings.sql.database == ""
    assert settings.sql_stg.database == ""
    assert settings.sql_int.database == ""

    provider = _make_provider(
        aud_database="real_audit_db",
        stg_database="real_stg_db",
        int_database="real_int_db",
    )
    result = _resolve_database_names(settings, provider, _LOGGER)

    assert result.sql.database == "real_audit_db"
    assert result.sql_stg.database == "real_stg_db"
    assert result.sql_int.database == "real_int_db"


def test_other_sql_settings_are_preserved_after_injection():
    """Host, port, auth_mode etc. must not be affected by database name injection."""
    settings = replace(
        _make_settings(),
        sql=_make_sql(database=""),
    )
    # patch a distinctive host so we can assert it is preserved
    settings = replace(settings, sql=replace(settings.sql, host="custom-host"))

    provider = _make_provider(aud_database="aud_db")
    result = _resolve_database_names(settings, provider, _LOGGER)

    assert result.sql.host == "custom-host"
    assert result.sql.database == "aud_db"


def test_get_sql_connection_info_is_called_once():
    provider = _make_provider()
    _resolve_database_names(_make_settings(), provider, _LOGGER)
    provider.get_sql_connection_info.assert_called_once()


# ---------------------------------------------------------------------------
# Error cases: provider is None
# ---------------------------------------------------------------------------

def test_raises_when_provider_is_none():
    with pytest.raises(ValueError, match="Azure Key Vault is not configured"):
        _resolve_database_names(_make_settings(), None, _LOGGER)


# ---------------------------------------------------------------------------
# Error cases: individual secrets missing or blank
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "missing_role",
    ["aud_database", "stg_database", "int_database"],
)
def test_raises_when_single_database_secret_is_none(missing_role):
    kwargs = {
        "aud_database": "aud_db",
        "stg_database": "stg_db",
        "int_database": "int_db",
    }
    kwargs[missing_role] = None
    provider = _make_provider(**kwargs)

    with pytest.raises(ValueError, match="could not be resolved from"):
        _resolve_database_names(_make_settings(), provider, _LOGGER)


@pytest.mark.parametrize(
    "blank_role",
    ["aud_database", "stg_database", "int_database"],
)
def test_raises_when_single_database_secret_is_blank_string(blank_role):
    kwargs = {
        "aud_database": "aud_db",
        "stg_database": "stg_db",
        "int_database": "int_db",
    }
    kwargs[blank_role] = ""
    provider = _make_provider(**kwargs)

    with pytest.raises(ValueError, match="could not be resolved from"):
        _resolve_database_names(_make_settings(), provider, _LOGGER)


def test_raises_when_all_database_secrets_are_missing():
    provider = _make_provider(
        aud_database=None,
        stg_database=None,
        int_database=None,
    )
    with pytest.raises(ValueError, match="could not be resolved from"):
        _resolve_database_names(_make_settings(), provider, _LOGGER)


def test_error_message_names_the_missing_roles():
    """The ValueError message should tell the operator exactly which roles failed."""
    provider = _make_provider(aud_database=None, stg_database=None, int_database="ok")
    with pytest.raises(ValueError) as exc_info:
        _resolve_database_names(_make_settings(), provider, _LOGGER)

    msg = str(exc_info.value)
    assert "aud_database" in msg
    assert "stg_database" in msg
    assert "int_database" not in msg or "ok" in msg  # 'int_database' resolved fine


# ---------------------------------------------------------------------------
# Server name from KV (resolved but not injected into settings — smoke test)
# ---------------------------------------------------------------------------

def test_server_name_in_kv_response_does_not_cause_error():
    """get_sql_connection_info() may return a 'server' key; this should not break resolution."""
    provider = _make_provider(server="kv-resolved-server.database.windows.net")
    result = _resolve_database_names(_make_settings(), provider, _LOGGER)
    # database names still injected correctly
    assert result.sql.database == "ingest_audit_dev"
