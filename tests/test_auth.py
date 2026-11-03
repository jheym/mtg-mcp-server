"""Tests for optional Google sign-in (config validation, provider, allowlist)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.authorization import AuthContext
from pydantic import ValidationError

from mtg_mcp_server.auth import (
    GOOGLE_SCOPES,
    build_auth_middleware,
    build_auth_provider,
    build_client_storage,
    email_allowlist_check,
    parse_allowed_emails,
)
from mtg_mcp_server.config import Settings

_AUTH_ENV = {
    "MTG_MCP_GOOGLE_CLIENT_ID": "123.apps.googleusercontent.com",
    "MTG_MCP_GOOGLE_CLIENT_SECRET": "GOCSPX-test-secret",
    "MTG_MCP_AUTH_BASE_URL": "https://mtg.example.app",
    "MTG_MCP_AUTH_ALLOWED_EMAILS": "Me@Example.com, friend@example.com",
}


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key, value in _AUTH_ENV.items():
        monkeypatch.setenv(key, value)
    return _AUTH_ENV


def _token(email: str | None, verified: object = "true") -> AccessToken:
    claims: dict[str, object] = {"sub": "1"}
    if email is not None:
        claims["email"] = email
    if verified is not None:
        claims["email_verified"] = verified
    return AccessToken(token="t", client_id="c", scopes=[], claims=claims)


def _ctx(token: AccessToken | None) -> AuthContext:
    return AuthContext(token=token, component=MagicMock())


class TestParseAllowedEmails:
    def test_splits_and_lowercases(self):
        assert parse_allowed_emails(" A@x.com, b@y.com;c@z.com  d@w.com ") == frozenset(
            {"a@x.com", "b@y.com", "c@z.com", "d@w.com"}
        )

    def test_empty(self):
        assert parse_allowed_emails("") == frozenset()
        assert parse_allowed_emails(" , ") == frozenset()


class TestSettingsValidation:
    def test_auth_off_by_default(self):
        settings = Settings()
        assert settings.google_client_id == ""
        assert build_auth_provider(settings) is None

    def test_full_config_valid(self, auth_env):
        settings = Settings()
        assert settings.auth_base_url == "https://mtg.example.app"
        assert settings.google_client_secret.get_secret_value() == "GOCSPX-test-secret"

    @pytest.mark.parametrize(
        "missing",
        [
            "MTG_MCP_GOOGLE_CLIENT_SECRET",
            "MTG_MCP_AUTH_BASE_URL",
            "MTG_MCP_AUTH_ALLOWED_EMAILS",
        ],
    )
    def test_partial_config_fails_closed(self, auth_env, monkeypatch, missing):
        monkeypatch.setenv(missing, "")
        with pytest.raises(ValidationError, match=missing):
            Settings()

    def test_secrets_not_in_repr(self, auth_env):
        assert "GOCSPX-test-secret" not in repr(Settings())


class TestEmailAllowlistCheck:
    check = staticmethod(email_allowlist_check(frozenset({"me@example.com"})))

    def test_allowed_email(self):
        assert self.check(_ctx(_token("me@example.com"))) is True

    def test_case_insensitive(self):
        assert self.check(_ctx(_token("ME@Example.COM"))) is True

    def test_bool_verified_claim(self):
        assert self.check(_ctx(_token("me@example.com", verified=True))) is True

    def test_other_email_denied(self):
        with pytest.raises(AuthorizationError, match="not allowed"):
            self.check(_ctx(_token("stranger@example.com")))

    @pytest.mark.parametrize("verified", ["false", False, None])
    def test_unverified_denied(self, verified):
        with pytest.raises(AuthorizationError, match="verified"):
            self.check(_ctx(_token("me@example.com", verified=verified)))

    def test_missing_email_denied(self):
        with pytest.raises(AuthorizationError, match="verified"):
            self.check(_ctx(_token(None)))

    def test_no_token_denied(self):
        with pytest.raises(AuthorizationError, match="Sign-in required"):
            self.check(_ctx(None))

    def test_wildcard_allows_any_verified(self):
        check = email_allowlist_check(frozenset({"*"}))
        assert check(_ctx(_token("anyone@gmail.com"))) is True
        with pytest.raises(AuthorizationError):
            check(_ctx(_token("anyone@gmail.com", verified="false")))

    def test_empty_allowlist_denies_everyone(self):
        check = email_allowlist_check(frozenset())
        with pytest.raises(AuthorizationError):
            check(_ctx(_token("me@example.com")))


class TestBuildAuthProvider:
    def test_google_provider_when_configured(self, auth_env, tmp_path, monkeypatch):
        import fastmcp

        monkeypatch.setattr(fastmcp.settings, "home", tmp_path)
        from fastmcp.server.auth.providers.google import GoogleProvider

        provider = build_auth_provider(Settings())
        assert isinstance(provider, GoogleProvider)
        assert str(provider.base_url).rstrip("/") == "https://mtg.example.app"
        assert set(provider.required_scopes or []) >= {"openid"}

    def test_scopes_are_minimal(self):
        assert GOOGLE_SCOPES == ["openid", "email"]

    def test_no_redis_url_uses_default_storage(self, auth_env):
        assert build_client_storage(Settings()) is None


class TestAllowlistMiddlewareEndToEnd:
    """The middleware filters lists and blocks calls for non-allowlisted users."""

    @staticmethod
    def _server(settings: Settings) -> FastMCP:
        server = FastMCP("t")

        @server.tool
        def hello() -> str:
            return "hi"

        server.add_middleware(build_auth_middleware(settings))
        return server

    async def test_allowed_user_sees_and_calls_tools(self, auth_env):
        server = self._server(Settings())
        with patch(
            "fastmcp.server.middleware.authorization.get_access_token",
            return_value=_token("friend@example.com"),
        ):
            async with Client(server) as client:
                tools = await client.list_tools()
                assert [t.name for t in tools] == ["hello"]
                result = await client.call_tool("hello", {})
                assert result.content[0].text == "hi"

    async def test_other_user_sees_nothing_and_is_blocked(self, auth_env):
        server = self._server(Settings())
        with patch(
            "fastmcp.server.middleware.authorization.get_access_token",
            return_value=_token("stranger@example.com"),
        ):
            async with Client(server) as client:
                assert await client.list_tools() == []
                result = await client.call_tool("hello", {}, raise_on_error=False)
                assert result.is_error

    async def test_unauthenticated_blocked(self, auth_env):
        server = self._server(Settings())
        with patch(
            "fastmcp.server.middleware.authorization.get_access_token",
            return_value=None,
        ):
            async with Client(server) as client:
                assert await client.list_tools() == []


class TestRedisStorage:
    def test_redis_url_builds_encrypted_store(self, auth_env, monkeypatch):
        pytest.importorskip("redis")
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

        monkeypatch.setenv("MTG_MCP_AUTH_REDIS_URL", "redis://localhost:6379/0")
        storage = build_client_storage(Settings())
        assert isinstance(storage, FernetEncryptionWrapper)
