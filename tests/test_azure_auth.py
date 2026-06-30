from __future__ import annotations

from unittest.mock import MagicMock, patch

from sharepoint_ingest._secret_protector import resolve_secret
from sharepoint_ingest._azure_credential import _env_value


def test_resolve_secret_prefers_keyring_over_file_and_plain(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET_KEYRING", "svc:key")
    monkeypatch.setenv("AZURE_CLIENT_SECRET_FILE", "C:\\secret.enc")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "plain")

    with patch("sharepoint_ingest._secret_protector._resolve_from_keyring", return_value="from-keyring"):
        assert resolve_secret("AZURE_CLIENT_SECRET") == "from-keyring"


def test_resolve_secret_falls_back_to_dpapi_file_when_keyring_empty(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET_KEYRING", "svc:key")
    monkeypatch.setenv("AZURE_CLIENT_SECRET_FILE", "C:\\secret.enc")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "plain")

    with patch("sharepoint_ingest._secret_protector._resolve_from_keyring", return_value=None), \
         patch("sharepoint_ingest._secret_protector._resolve_from_dpapi_file", return_value="from-file"):
        assert resolve_secret("AZURE_CLIENT_SECRET") == "from-file"


def test_resolve_secret_falls_back_to_plain_env(monkeypatch):
    monkeypatch.delenv("AZURE_CLIENT_SECRET_KEYRING", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_SECRET_FILE", raising=False)
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "plain")

    assert resolve_secret("AZURE_CLIENT_SECRET") == "plain"


def test_resolve_secret_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("AZURE_CLIENT_CERTIFICATE_PASSWORD_KEYRING", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_CERTIFICATE_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_CERTIFICATE_PASSWORD", raising=False)

    assert resolve_secret("AZURE_CLIENT_CERTIFICATE_PASSWORD") is None


def test_resolve_secret_prefers_env_specific_variants(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "plain-shared")
    monkeypatch.setenv("AZURE_CLIENT_SECRET_PROD", "plain-prod")

    assert resolve_secret("AZURE_CLIENT_SECRET", "prod") == "plain-prod"


def test_env_value_prefers_env_specific_value(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "shared-client")
    monkeypatch.setenv("AZURE_CLIENT_ID_DEV", "dev-client")

    assert _env_value("AZURE_CLIENT_ID", "dev") == "dev-client"


def test_build_env_secret_credential_uses_secret_fallback(monkeypatch):
    from sharepoint_ingest import _azure_credential

    monkeypatch.setenv("AZURE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-id")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "plain-secret")

    with patch.object(_azure_credential, "ClientSecretCredential", create=True) as _unused:
        pass

    with patch("azure.identity.ClientSecretCredential") as mock_credential:
        mock_credential.return_value = MagicMock(name="credential")
        result = _azure_credential._build_env_secret_credential()

    assert result is mock_credential.return_value
    mock_credential.assert_called_once_with(
        tenant_id="tenant-id",
        client_id="client-id",
        client_secret="plain-secret",
    )
