"""Tests that SQL connection settings are resolved per-environment.

Ensures ``--env dev`` and ``--env prod`` pick up separate SQL Server hosts,
ports, auth modes, ODBC drivers and trust-cert flags rather than sharing a
single set of connection parameters.
"""
from __future__ import annotations

import os
from unittest.mock import patch

from sharepoint_ingest.config import (
    _sql_host_for_env,
    _sql_port_for_env,
    _sql_auth_mode_for_env,
    _sql_odbc_driver_for_env,
    _sql_trust_cert_for_env,
    _make_sql_settings,
)


_BASE_ENV = {
    # dev server
    "SQL_SERVER_HOST_DEV": "dev-sql.internal",
    "SQL_SERVER_PORT_DEV": "1433",
    "SQL_AUTH_MODE_DEV": "sspi",
    "SQL_ODBC_DRIVER_DEV": "ODBC Driver 18 for SQL Server",
    "SQL_TRUST_SERVER_CERTIFICATE_DEV": "1",
    # prod server — deliberately different values
    "SQL_SERVER_HOST_PROD": "prod-sql.database.windows.net",
    "SQL_SERVER_PORT_PROD": "1433",
    "SQL_AUTH_MODE_PROD": "ad_password",
    "SQL_ODBC_DRIVER_PROD": "ODBC Driver 17 for SQL Server",
    "SQL_TRUST_SERVER_CERTIFICATE_PROD": "0",
    # shared fallback — should NOT be used when env-specific vars are set
    "SQL_SERVER_HOST": "SHARED-FALLBACK",
    "SQL_AUTH_MODE": "sql_password",
}


def test_dev_host_resolves_to_dev_specific_value():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        assert _sql_host_for_env("dev") == "dev-sql.internal"


def test_prod_host_resolves_to_prod_specific_value():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        assert _sql_host_for_env("prod") == "prod-sql.database.windows.net"


def test_dev_and_prod_hosts_are_different():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        assert _sql_host_for_env("dev") != _sql_host_for_env("prod")


def test_shared_fallback_not_used_when_env_specific_host_set():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        assert _sql_host_for_env("dev") != "SHARED-FALLBACK"
        assert _sql_host_for_env("prod") != "SHARED-FALLBACK"


def test_shared_fallback_used_when_no_env_specific_host():
    env = {"SQL_SERVER_HOST": "fallback-sql.internal"}
    with patch.dict(os.environ, env, clear=True):
        assert _sql_host_for_env("dev") == "fallback-sql.internal"
        assert _sql_host_for_env("prod") == "fallback-sql.internal"


def test_dev_auth_mode_is_sspi():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        assert _sql_auth_mode_for_env("dev") == "sspi"


def test_prod_auth_mode_is_ad_password():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        assert _sql_auth_mode_for_env("prod") == "ad_password"


def test_dev_trust_cert_true():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        assert _sql_trust_cert_for_env("dev") is True


def test_prod_trust_cert_false():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        assert _sql_trust_cert_for_env("prod") is False


def test_dev_odbc_driver():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        assert "18" in _sql_odbc_driver_for_env("dev")


def test_prod_odbc_driver():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        assert "17" in _sql_odbc_driver_for_env("prod")


def test_make_sql_settings_dev_uses_dev_host():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        settings = _make_sql_settings("dev", "ingest_audit_dev")
        assert settings.host == "dev-sql.internal"
        assert settings.auth_mode == "sspi"
        assert settings.database == "ingest_audit_dev"


def test_make_sql_settings_prod_uses_prod_host():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        settings = _make_sql_settings("prod", "ingest_audit_prod")
        assert settings.host == "prod-sql.database.windows.net"
        assert settings.auth_mode == "ad_password"
        assert settings.database == "ingest_audit_prod"


def test_make_sql_settings_dev_prod_have_different_hosts():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        dev = _make_sql_settings("dev", "ingest_audit_dev")
        prod = _make_sql_settings("prod", "ingest_audit_prod")
        assert dev.host != prod.host
