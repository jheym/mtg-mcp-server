"""Optional Google sign-in for the HTTP transport.

When ``MTG_MCP_GOOGLE_CLIENT_ID`` is set, the server runs FastMCP's
``GoogleProvider`` (an OAuth proxy in front of Google) so MCP clients such as
Claude can sign in with OAuth 2.1 + Dynamic Client Registration. A global
``AuthMiddleware`` then allows only verified Google emails listed in
``MTG_MCP_AUTH_ALLOWED_EMAILS``.

Nothing here runs when the client ID is empty: stdio and unauthenticated HTTP
deployments behave exactly as before. FastMCP skips auth checks on stdio.

Storage: the OAuth proxy keeps client registrations and encrypted upstream
tokens. By default FastMCP writes them to an encrypted file store on local
disk, which is lost when a container restarts (users then reconnect). Set
``MTG_MCP_AUTH_REDIS_URL`` to keep that state in Redis instead (requires the
``auth-redis`` extra).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.middleware.authorization import AuthMiddleware

if TYPE_CHECKING:
    from fastmcp.server.auth import AuthProvider
    from fastmcp.server.auth.authorization import AuthCheck, AuthContext
    from key_value.aio.protocols import AsyncKeyValue

    from mtg_mcp_server.config import Settings

__all__ = [
    "GOOGLE_SCOPES",
    "WILDCARD",
    "build_auth_middleware",
    "build_auth_provider",
    "build_client_storage",
    "email_allowlist_check",
    "parse_allowed_emails",
]

log = structlog.get_logger(service="auth")

# Minimal scopes: identity + email. Keeping to openid/email also avoids Google's
# 7-day refresh-token expiry for OAuth apps in "Testing" publishing status.
GOOGLE_SCOPES: list[str] = ["openid", "email"]

# Allowlist entry that admits any verified Google account.
WILDCARD = "*"

_SPLIT_RE = re.compile(r"[\s,;]+")
_STORAGE_SALT = "mtg-mcp-oauth-storage-encryption-key"


def parse_allowed_emails(raw: str) -> frozenset[str]:
    """Parse a comma/space/semicolon-separated email list into a lowercase set."""
    return frozenset(e.lower() for e in _SPLIT_RE.split(raw.strip()) if e)


def _is_verified(value: object) -> bool:
    """Google's tokeninfo returns ``"true"`` (string); userinfo returns ``True``."""
    return value is True or (isinstance(value, str) and value.lower() == "true")


def email_allowlist_check(allowed: frozenset[str]) -> AuthCheck:
    """Build an auth check that admits only verified emails in ``allowed``.

    ``"*"`` in ``allowed`` admits any verified Google account. An empty set
    admits nobody (fail closed).
    """

    def check(ctx: AuthContext) -> bool:
        token = ctx.token
        if token is None:
            raise AuthorizationError("Sign-in required.")
        claims = token.claims or {}
        email = str(claims.get("email") or "").lower()
        if not email or not _is_verified(claims.get("email_verified")):
            raise AuthorizationError("A verified Google email is required.")
        if WILDCARD in allowed or email in allowed:
            return True
        log.warning("auth.email_denied", email=email)
        raise AuthorizationError(f"{email} is not allowed on this server.")

    return check


def build_client_storage(settings: Settings) -> AsyncKeyValue | None:
    """Return a persistent, encrypted OAuth-state store, or None for FastMCP's default.

    Raises:
        RuntimeError: If a Redis URL is set but the ``redis`` package is missing.
    """
    redis_url = settings.auth_redis_url.get_secret_value()
    if not redis_url:
        return None

    try:
        from key_value.aio.stores.redis import RedisStore
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise RuntimeError(
            "MTG_MCP_AUTH_REDIS_URL is set but the 'redis' package is not installed. "
            'Install with: pip install "mtg-mcp-server[auth-redis]"'
        ) from exc

    from cryptography.fernet import Fernet
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    material = (
        settings.auth_jwt_signing_key.get_secret_value()
        or settings.google_client_secret.get_secret_value()
    )
    key = derive_jwt_key(high_entropy_material=material, salt=_STORAGE_SALT)
    return FernetEncryptionWrapper(
        key_value=RedisStore(url=redis_url),
        fernet=Fernet(key=key),
        raise_on_decryption_error=False,  # key rotation = re-register, not crash
    )


def build_auth_provider(settings: Settings) -> AuthProvider | None:
    """Return a Google OAuth provider when configured, else None."""
    if not settings.google_client_id:
        return None

    from fastmcp.server.auth.providers.google import GoogleProvider

    jwt_key = settings.auth_jwt_signing_key.get_secret_value()
    return GoogleProvider(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        base_url=settings.auth_base_url,
        required_scopes=GOOGLE_SCOPES,
        client_storage=build_client_storage(settings),
        jwt_signing_key=jwt_key or None,
    )


def build_auth_middleware(settings: Settings) -> AuthMiddleware:
    """Global middleware that enforces the email allowlist on every component."""
    allowed = parse_allowed_emails(settings.auth_allowed_emails)
    return AuthMiddleware(auth=email_allowlist_check(allowed))
