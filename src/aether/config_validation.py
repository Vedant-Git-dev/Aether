"""The required-configuration rules, in one place.

A required value is not just "non-empty": a placeholder ("change-me",
"your_key_here", "<...>") is treated as missing, so a generated file or a
copied example can never stand in for a real answer. Boot refuses to start
when a required value is missing and prints one plain list; the setup
wizard refuses each question until its answer is valid; the Settings page
refuses to clear a required field.

Only the rules live here. The wizard's live probes (a real database
connection, a model-list call, a bot identity call) live in
``aether.setup.validation`` so this module stays import-light.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode

if TYPE_CHECKING:
    from .config import AppConfig, Settings

# The words the examples used to ship, and the shapes a person uses when
# they mean "fill this in later". Compared stripped and lower-cased.
_PLACEHOLDER_WORDS = frozenset(
    {
        "change-me",
        "change_me",
        "changeme",
        "change me",
        "your_key_here",
        "your-key-here",
        "yourkeyhere",
        "your key here",
        "your_api_key",
        "your-api-key",
        "yourapikey",
        "your api key",
        "paste_here",
        "paste-here",
        "paste your key here",
        "replace_me",
        "replace-me",
        "replaceme",
        "todo",
        "tbd",
        "placeholder",
        "example",
        "example_key",
        "example-key",
    }
)

# <your-key>, {api_key}, [token] — the "<...>" placeholders.
_PLACEHOLDER_SHAPE = re.compile(r"^[<{\[].*[>}\]]$")

# The literal value the example .env shipped with.
_EXAMPLE_DATABASE_URL = "postgresql://user:pass@host/dbname?sslmode=require"

# The env name that belongs to each provider. Ollama has no key: its
# address (OLLAMA_BASE_URL, with a working default) is what makes it
# reachable, so it never appears here.
PROVIDER_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}

# Settings the page may not save blank or clear. A reset (back to
# config.yaml, or the built-in default) is still allowed: the default is
# a real value. Everything else can be cleared.
REQUIRED_SETTINGS_PATHS: frozenset[str] = frozenset(
    {
        "llm.model",
        "llm.embedding_model",
    }
)


# libpq connection options that asyncpg forwards to the server as settings,
# where Postgres then rejects them ("unrecognized configuration parameter").
# Cloud dashboards add these to otherwise valid strings, so drop them before
# asyncpg sees the URL. Client certificates and sslmode stay: asyncpg reads
# those itself.
_LIBPQ_ONLY_QUERY = frozenset(
    {
        "channel_binding",
        "connect_timeout",
        "connection_limit",
        "gssencmode",
        "keepalives",
        "keepalives_count",
        "keepalives_idle",
        "keepalives_interval",
        "load_balance_hosts",
        "passfile",
        "pgbouncer",
        "pool_timeout",
        "schema",
        "service",
        "sslcompression",
        "statement_cache_size",
        "tcp_user_timeout",
    }
)


def normalize_database_url(url: str) -> str:
    """A Postgres URL asyncpg can consume, without changing what it means.

    Only libpq-only query options are removed. Everything else (host, user,
    password, database, sslmode, server settings) is kept byte for byte.
    """
    text = (url or "").strip()
    base, question, rest = text.partition("?")
    if not question:
        return text
    query, hash_sep, fragment = rest.partition("#")
    pairs = parse_qsl(query, keep_blank_values=True)
    kept = [(key, value) for key, value in pairs if key.lower() not in _LIBPQ_ONLY_QUERY]
    if len(kept) == len(pairs):
        return text
    normalized = f"{base}?{urlencode(kept)}" if kept else base
    return f"{normalized}#{fragment}" if hash_sep else normalized


def is_blank(value: object) -> bool:
    """True for None, empty, or whitespace-only."""
    return value is None or not str(value).strip()


def is_placeholder(value: object) -> bool:
    """True for the words and shapes that mean "not really filled in"."""
    text = str(value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if lowered in _PLACEHOLDER_WORDS:
        return True
    if text == _EXAMPLE_DATABASE_URL:
        return True
    if _PLACEHOLDER_SHAPE.match(text):
        return True
    # Separator-insensitive forms: "Your-Key-Here", "your key here".
    compact = re.sub(r"[\s_-]+", "", lowered)
    return compact in {"yourkeyhere", "yourapikey", "replaceme", "changeme"}


def is_missing(value: object) -> bool:
    """A required value is missing when it is blank or a placeholder."""
    return is_blank(value) or is_placeholder(value)


def provider_key_name(provider: str) -> str | None:
    """The env name the chosen provider's key lives under, or None."""
    return PROVIDER_KEY_ENV.get(provider)


def provider_key_value(settings: Settings, provider: str) -> str:
    """The resolved settings value for the chosen provider's key."""
    return {
        "anthropic": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
        "gemini": settings.gemini_api_key,
        "nvidia": settings.nvidia_api_key,
    }.get(provider, "")


def embedding_is_on(config: AppConfig) -> bool:
    """Whether the vector arm is explicitly on.

    Empty, ``off``, ``base`` and ``default`` all mean the vector arm is
    off. (The embedding seam has no "main model does it" rung: the main
    model cannot embed.) This is what makes an absent key an honest off
    instead of a silently dead feature.
    """
    from .config import MODEL_BASE, MODEL_DEFAULT, MODEL_OFF

    model = config.llm.embedding_model
    return bool(model) and model not in (MODEL_OFF, MODEL_BASE, MODEL_DEFAULT)


def startup_problems(
    settings: Settings,
    config: AppConfig,
    *,
    require_keys: bool = True,
) -> list[str]:
    """Every reason boot must refuse, as plain sentences with the fix.

    ``require_keys=False`` is the phase before the encrypted secret store
    is open: it checks only values that cannot be pasted after boot
    (database, content key, panel token, a non-empty model). The second
    phase passes the resolved settings, so a key pasted in the panel
    counts exactly like one in .env.
    """
    problems: list[str] = []

    if is_missing(settings.database_url):
        problems.append(
            "DATABASE_URL is not set. Put a Postgres connection string in .env "
            "(a free Neon database works)."
        )
    if is_missing(settings.encryption_key) and not settings.dev_ephemeral_key:
        problems.append(
            "AETHER_ENCRYPTION_KEY is not set. Generate one with: "
            'python -c "from aether.memory.crypto import generate_key_b64; '
            'print(generate_key_b64())"'
        )
    if is_missing(settings.api_token):
        problems.append(
            "AETHER_TOKEN is not set. Generate one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    if is_missing(config.llm.model):
        problems.append("llm.model is empty. Set it to a model id for the chosen provider.")

    if not require_keys:
        return problems

    if config.llm.provider != "ollama":
        key_name = provider_key_name(config.llm.provider)
        if key_name and is_missing(provider_key_value(settings, config.llm.provider)):
            problems.append(
                f"{key_name} is not set and llm.provider is {config.llm.provider}. "
                "Add it to .env or paste it in Settings > Credentials, or choose "
                "another provider."
            )

    if embedding_is_on(config) and is_missing(settings.gemini_api_key):
        problems.append(
            "GEMINI_API_KEY is not set and memory search is on. Add it, or set "
            "llm.embedding_model to off in config.yaml."
        )

    if config.messaging.telegram.enabled and is_missing(settings.telegram_bot_token):
        problems.append(
            "TELEGRAM_BOT_TOKEN is not set and messaging.telegram is on. Add it, "
            "or set messaging.telegram.enabled to false."
        )
    if config.messaging.discord.enabled and is_missing(settings.discord_bot_token):
        problems.append(
            "DISCORD_BOT_TOKEN is not set and messaging.discord is on. Add it, "
            "or set messaging.discord.enabled to false."
        )
    return problems


def format_startup_problems(problems: list[str]) -> str:
    """One clear list, the shape a boot failure prints."""
    body = "\n".join(f"  - {problem}" for problem in problems)
    return f"Aether cannot start until these are fixed:\n{body}"
