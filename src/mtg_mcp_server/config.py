"""Application configuration via environment variables.

All settings use the ``MTG_MCP_`` prefix and can be overridden via a ``.env``
file. No credentials are required — all APIs are public. Optional Google
sign-in for remote HTTP deployments is enabled by ``MTG_MCP_GOOGLE_CLIENT_ID``
(see :mod:`mtg_mcp_server.auth`).

Example::

    MTG_MCP_ENABLE_EDHREC = false  # Disable fragile EDHREC scraping
    MTG_MCP_TRANSPORT = http  # Use streamable HTTP instead of stdio
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings

__all__ = ["Settings"]


class Settings(BaseSettings):
    """MTG MCP server settings loaded from environment variables.

    All fields have sensible defaults and can be overridden with
    ``MTG_MCP_``-prefixed environment variables or a ``.env`` file.
    """

    # --- Transport ---
    transport: Literal["stdio", "http"] = "stdio"
    http_port: int = 8000

    # --- Logging ---
    log_level: str = "INFO"

    # --- Backend URLs ---
    # Each service client reads its base URL from here so tests can point
    # at a local mock server via env var override.
    scryfall_base_url: str = "https://api.scryfall.com"
    scryfall_rate_limit_ms: int = 100
    spellbook_base_url: str = "https://backend.commanderspellbook.com"
    seventeen_lands_base_url: str = "https://www.17lands.com"
    edhrec_base_url: str = "https://json.edhrec.com"
    moxfield_base_url: str = "https://api2.moxfield.com"
    spicerack_base_url: str = "https://api.spicerack.gg"
    spicerack_api_key: str = ""  # Optional — sent as X-API-Key header if non-empty
    mtggoldfish_base_url: str = "https://www.mtggoldfish.com"

    # --- Feature flags ---
    # Optional backends can be disabled without code changes.
    enable_17lands: bool = True
    enable_edhrec: bool = True  # Behind flag — scrapes undocumented endpoints
    enable_moxfield: bool = True  # Behind flag — reverse-engineered API
    enable_spicerack: bool = True  # Documented public API
    enable_mtggoldfish: bool = True  # Behind flag — scrapes HTML (no API)
    enable_bulk_data: bool = True  # Scryfall Oracle Cards bulk download (~30MB)

    # --- Caching ---
    disable_cache: bool = False  # Set True in tests to bypass TTLCache

    # --- Scryfall Bulk Data ---
    bulk_data_refresh_hours: int = 12

    # --- Comprehensive Rules ---
    rules_url: str = "https://media.wizards.com/2025/downloads/MagicCompRules%2020250404.txt"
    rules_refresh_hours: int = 168  # Weekly check (rules update ~4x/year)
    enable_rules: bool = True

    # --- Code Mode ---
    enable_code_mode: bool = False  # FastMCP CodeMode transform (Phase 3)

    # --- Optional Google sign-in (HTTP transport) ---
    # Empty client ID = auth off (default). When set, secret, public base URL,
    # and a non-empty allowlist are required; startup fails otherwise.
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    auth_base_url: str = ""  # Public URL of this server, e.g. https://example.fastmcp.app
    auth_allowed_emails: str = ""  # Comma-separated verified Google emails; "*" = any
    auth_jwt_signing_key: SecretStr = SecretStr("")  # Optional; default derives from secret
    auth_redis_url: SecretStr = SecretStr("")  # Optional persistent OAuth state (auth-redis)

    @model_validator(mode="after")
    def _check_auth(self) -> Settings:
        """Fail closed: a partial Google auth config must not start an open server."""
        if not self.google_client_id:
            return self
        missing = [
            name
            for name, value in (
                ("MTG_MCP_GOOGLE_CLIENT_SECRET", self.google_client_secret.get_secret_value()),
                ("MTG_MCP_AUTH_BASE_URL", self.auth_base_url),
                ("MTG_MCP_AUTH_ALLOWED_EMAILS", self.auth_allowed_emails.strip()),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "MTG_MCP_GOOGLE_CLIENT_ID is set, so these are required: " + ", ".join(missing)
            )
        return self

    model_config = {"env_prefix": "MTG_MCP_", "env_file": ".env", "extra": "ignore"}
