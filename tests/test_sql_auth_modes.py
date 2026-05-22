from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from sharepoint_ingest.sql_client import SqlClient


def _settings(**overrides):
    base = {
        "odbc_driver": "ODBC Driver 18 for SQL Server",
        "trust_server_certificate": True,
        "auth_mode": "sql_password",
        "username": "user",
        "password": "pass",
        "host": ".",
        "port": 1433,
        "database": "ingest_dev",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _extract_conn_parts(conn_str: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urlparse(conn_str)
    return parsed.netloc, parse_qs(parsed.query)


@pytest.mark.parametrize(
    "auth_mode",
    [
        "windows",
        "sspi",
        "trusted_connection",
        "ad_integrated",
        "active_directory_integrated",
    ],
)
def test_build_engine_uses_trusted_connection_for_integrated_aliases(auth_mode: str) -> None:
    with patch("sharepoint_ingest.sql_client.create_engine") as mock_create_engine:
        mock_create_engine.return_value = MagicMock()
        SqlClient(_settings(auth_mode=auth_mode, username="", password=""))

    conn_str = mock_create_engine.call_args[0][0]
    netloc, query = _extract_conn_parts(conn_str)
    assert "@" in netloc
    assert "user:pass" not in conn_str
    assert query.get("Trusted_Connection") == ["yes"]
    assert "Authentication" not in query


@pytest.mark.parametrize("auth_mode", ["ad_password", "active_directory_password"])
def test_build_engine_uses_active_directory_password_auth(auth_mode: str) -> None:
    with patch("sharepoint_ingest.sql_client.create_engine") as mock_create_engine:
        mock_create_engine.return_value = MagicMock()
        SqlClient(_settings(auth_mode=auth_mode, username="svc_user", password="svc_pass"))

    conn_str = mock_create_engine.call_args[0][0]
    netloc, query = _extract_conn_parts(conn_str)
    assert "svc_user:svc_pass" in netloc
    assert query.get("Authentication") == ["ActiveDirectoryPassword"]
    assert "Trusted_Connection" not in query


def test_build_engine_uses_sql_password_without_ad_auth_param() -> None:
    with patch("sharepoint_ingest.sql_client.create_engine") as mock_create_engine:
        mock_create_engine.return_value = MagicMock()
        SqlClient(_settings(auth_mode="sql_password", username="svc_user", password="svc_pass"))

    conn_str = mock_create_engine.call_args[0][0]
    netloc, query = _extract_conn_parts(conn_str)
    assert "svc_user:svc_pass" in netloc
    assert "Authentication" not in query
    assert query.get("TrustServerCertificate") == ["yes"]


def test_build_engine_uses_managed_identity_without_credentials() -> None:
    with patch("sharepoint_ingest.sql_client.create_engine") as mock_create_engine:
        mock_create_engine.return_value = MagicMock()
        SqlClient(_settings(auth_mode="managed_identity", username="", password=""))

    conn_str = mock_create_engine.call_args[0][0]
    netloc, query = _extract_conn_parts(conn_str)
    assert "@" in netloc
    assert "user:pass" not in conn_str
    assert query.get("Authentication") == ["ActiveDirectoryMsi"]
    assert "Trusted_Connection" not in query


def test_build_engine_requires_credentials_for_sql_password() -> None:
    with pytest.raises(ValueError, match="required for credential-based auth modes"):
        SqlClient(_settings(auth_mode="sql_password", username="", password=""))


def test_build_engine_rejects_unsupported_auth_mode() -> None:
    with pytest.raises(ValueError, match="Unsupported SQL auth mode"):
        SqlClient(_settings(auth_mode="unsupported_mode"))
