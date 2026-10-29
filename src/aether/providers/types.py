"""Provider definitions and the small shared types around them.

A provider is data plus HTTP handlers. The runtime owns OAuth, storage,
refresh, revocation, tool registration, and the gate; a provider module only
declares what it needs and how one action is performed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .runtime import ProviderContext

AuthMode = Literal["oauth", "token"]
ToolEffect = Literal["read", "write", "destructive"]


@dataclass(frozen=True)
class StaticTokenSetup:
    """The recipe for a provider that accepts a pasted bearer token."""

    token_label: str
    token_prefix: str
    expires: bool
    instructions: tuple[str, ...]
    permissions: tuple[str, ...]
    token_url: str = ""


@dataclass(frozen=True)
class ToolDefinition:
    key: str
    description: str
    effect: ToolEffect
    input_schema: dict[str, Any]
    handler: Callable[[ProviderContext, dict[str, Any]], Awaitable[Any]]
    required_scopes: tuple[str, ...] = ()
    available_in: tuple[AuthMode, ...] = ("oauth", "token")


@dataclass(frozen=True)
class PollDefinition:
    key: str
    every_minutes: float
    handler: Callable[[ProviderContext], Awaitable[Any]]


@dataclass(frozen=True)
class AppDefinition:
    key: str
    name: str
    provider: str
    scopes: tuple[str, ...]
    tools: tuple[ToolDefinition, ...]
    poll: PollDefinition | None = None
    api_base: str = ""
    note: str = ""


@dataclass(frozen=True)
class ProviderDefinition:
    key: str
    name: str
    auth_modes: tuple[AuthMode, ...] = ("oauth",)
    default_auth_mode: AuthMode = "oauth"
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    revocation_endpoint: str | None = None
    userinfo_endpoint: str | None = None
    api_base: str = ""
    client_id_name: str | None = None
    client_secret_name: str | None = None
    pkce: bool = True
    prompt_consent_param: bool = True
    base_scopes: tuple[str, ...] = ()
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    setup_url: str = ""
    setup_steps: tuple[str, ...] = ()
    static_token: StaticTokenSetup | None = None
    validate_token: Callable[[Any, str], Awaitable[None]] | None = None
    picker_api_key_name: str = ""
    picker_app_id_name: str = ""
    picker_scope: str = ""
    forbidden_plain: str = ""
    not_found_plain: str = ""
    apps: tuple[AppDefinition, ...] = ()

    def app(self, key: str) -> AppDefinition | None:
        return next((app for app in self.apps if app.key == key), None)


@dataclass
class ProviderConnection:
    """One decrypted connection row. The token values never leave this object
    except into an Authorization header or the token store's cipher."""

    provider: str
    auth_mode: AuthMode
    account_label: str = ""
    account_id: str = ""
    app: str = ""
    access_token: str = ""
    refresh_token: str | None = None
    key_id: str = ""
    token_type: str = "Bearer"
    scopes: str = ""
    expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    status: str = "active"
    last_refreshed_at: datetime | None = None
    last_error: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ProviderAppView:
    """The sanitized panel shape for one app."""

    key: str
    name: str
    provider: str
    connected: bool
    status: str
    identity: str
    auth_mode: str
    scopes: tuple[str, ...]
    actions: int
    setup_required: bool
    setup_url: str
    setup_steps: tuple[str, ...]
    redirect_uri: str = ""
    last_error: str = ""
    auth_modes: tuple[str, ...] = ()
    default_auth_mode: str = "oauth"
    token_label: str = ""
    token_steps: tuple[str, ...] = ()
    token_expires: bool = False
    token_url: str = ""
    note: str = ""


@dataclass(frozen=True)
class PollResult:
    app: str
    tool: str
    payload: Any


@dataclass(frozen=True)
class ConnectResult:
    app: str
    provider: str
    redirect_url: str = ""
    setup_required: bool = False
    token_required: bool = False
    setup_url: str = ""
    setup_steps: tuple[str, ...] = ()
    redirect_uri: str = ""
    client_id_name: str = ""
    client_secret_name: str = ""
    already_connected: bool = False


class ProviderError(Exception):
    """Base class for provider failures with a plain sentence for the user."""

    plain = "that app isn't responding right now"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        if detail:
            self.plain = detail


class ProviderNotConfigured(ProviderError):
    plain = "that app isn't set up yet"


class ProviderReauthRequired(ProviderError):
    plain = "that app needs to be reconnected"


class ProviderAuthError(ProviderError):
    plain = "that app didn't accept the sign-in"


class ProviderHTTPError(ProviderError):
    def __init__(self, status: int, plain: str, detail: str = "") -> None:
        super().__init__(detail or plain)
        self.status = status
        self.plain = plain


class ProviderRateLimited(ProviderError):
    plain = "that app is asking me to slow down; I'll try again shortly"


class ProviderCryptoError(ProviderError):
    plain = "that app's saved sign-in can't be read; reconnect it"
