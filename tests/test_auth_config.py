from __future__ import annotations

import pytest

from notes_vault_mcp.auth import READ_SCOPE, WRITE_SCOPE, AuthConfig, auth_config
from notes_vault_mcp.config import PLUGIN_ENV_PREFIX, ConfigError

VARIABLES = (
    "VAULT_TOKEN",
    "VAULT_PUBLIC_URL",
    "VAULT_OIDC_ISSUER",
    "VAULT_OIDC_AUDIENCE",
    "VAULT_OIDC_READ_GROUP",
    "VAULT_OIDC_WRITE_GROUP",
    "VAULT_OIDC_SCOPES",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(PLUGIN_ENV_PREFIX + name, raising=False)


def test_bearer_refuses_to_start_without_a_token():
    with pytest.raises(ConfigError, match="VAULT_TOKEN"):
        auth_config("bearer")


def test_bearer_takes_the_token_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_TOKEN", "s3cret")
    assert auth_config("bearer").bearer_token == "s3cret"


def test_oidc_refuses_to_start_without_a_public_url():
    with pytest.raises(ConfigError, match="VAULT_PUBLIC_URL"):
        auth_config("oidc")


def test_oidc_refuses_a_plain_http_public_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "http://vault.example.com")
    monkeypatch.setenv("VAULT_OIDC_ISSUER", "https://idp.example.com")
    with pytest.raises(ConfigError, match="VAULT_PUBLIC_URL"):
        auth_config("oidc")


def test_oidc_allows_http_on_localhost(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "http://localhost:8765")
    monkeypatch.setenv("VAULT_OIDC_ISSUER", "https://idp.example.com")
    assert auth_config("oidc").public_url == "http://localhost:8765"


def test_oidc_refuses_to_start_without_an_issuer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com")
    with pytest.raises(ConfigError, match="VAULT_OIDC_ISSUER"):
        auth_config("oidc")


def test_oidc_reads_the_issuer_audience_and_groups(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com/")
    monkeypatch.setenv("VAULT_OIDC_ISSUER", "https://idp.example.com/")
    monkeypatch.setenv("VAULT_OIDC_AUDIENCE", "vault")
    monkeypatch.setenv("VAULT_OIDC_READ_GROUP", "readers")
    monkeypatch.setenv("VAULT_OIDC_WRITE_GROUP", "writers")
    config = auth_config("oidc")
    assert (config.issuer, config.audience) == ("https://idp.example.com", "vault")
    assert (config.read_group, config.write_group) == ("readers", "writers")


def test_oidc_defaults_the_groups_to_vault_and_vault_writers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com")
    monkeypatch.setenv("VAULT_OIDC_ISSUER", "https://idp.example.com")
    config = auth_config("oidc")
    assert (config.read_group, config.write_group) == ("vault", "vault-writers")
    assert config.audience == ""


def test_oidc_asks_the_provider_for_openid_profile_email_and_groups(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com")
    monkeypatch.setenv("VAULT_OIDC_ISSUER", "https://idp.example.com")
    assert auth_config("oidc").idp_scopes == ("openid", "profile", "email", "groups")


def test_the_provider_scopes_come_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com")
    monkeypatch.setenv("VAULT_OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("VAULT_OIDC_SCOPES", "openid  groups")
    assert auth_config("oidc").idp_scopes == ("openid", "groups")


def test_oidc_advertises_the_provider_scopes_and_builtin_advertises_vault_read():
    oidc = AuthConfig(mode="oidc", public_url="https://vault.example.com", idp_scopes=("openid", "groups"))
    builtin = AuthConfig(mode="builtin", public_url="https://vault.example.com")
    assert oidc.advertised_scopes == ("openid", "groups")
    assert builtin.advertised_scopes == (READ_SCOPE,)


def test_a_public_url_without_a_path_has_no_prefix():
    assert AuthConfig(mode="oidc", public_url="https://vault.example.com").path_prefix == ""


def test_the_path_prefix_is_the_path_of_the_public_url():
    assert AuthConfig(mode="oidc", public_url="https://host.example.com/home-vault").path_prefix == "/home-vault"


def test_the_path_prefix_drops_a_trailing_slash():
    assert AuthConfig(mode="oidc", public_url="https://host.example.com/home-vault/").path_prefix == "/home-vault"


def test_the_resource_url_is_the_mcp_endpoint_under_the_public_url():
    config = AuthConfig(mode="oidc", public_url="https://host.example.com/home-vault")
    assert config.resource_url == "https://host.example.com/home-vault/mcp"


def test_the_scopes_are_read_and_write():
    assert (READ_SCOPE, WRITE_SCOPE) == ("vault:read", "vault:write")
