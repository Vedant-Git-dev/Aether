"""Secrets by name — pastes, .env, the environment — resolved on demand.

Chat never carries a secret's value: a key pasted in an /apps answer is
consumed before ingest (the walk handles it in the deterministic command
path — no model, no memory, no trace), lands in the encrypted app_secrets
store, and every reply names only the NAME. App configs reference keys by
name only (`$NAME`), expanded at open time by the MCP host. Nothing here
reads .env at import or boot — every resolve is fresh, so a key added
after boot is picked up without a restart. House principle: boot reads
Settings; acts read the resolver.

Precedence mirrors pydantic-settings with one addition: the paste store
sits first, because a paste is the most recent deliberate act — the user
just handed the key over, so it wins over an older .env line. The store
is consulted only for env-var-shaped names, so nothing outside the $NAME
grammar can ever be shadowed by a paste.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

if TYPE_CHECKING:
    from .secret_store import SecretStore

# $NAME and ${NAME} — a lone $ (or $lowercase) stays literal
_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}|\$([A-Z][A-Z0-9_]*)")
# env-var-looking names only — presence checks ignore PATH & friends
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class SecretEnvError(Exception):
    """A referenced name isn't set anywhere — the attempt fails with the
    name in the log; the host's retry loop connects the app the moment the
    key lands."""


def _read_file(path: Path) -> dict[str, str | None]:
    try:
        return dict(dotenv_values(path))
    except Exception:  # unreadable/missing file — same as absent keys
        return {}


class EnvResolver:
    """One place that turns a secret's name into its value, freshly.

    Precedence: the paste store, the real environment, then the .env file
    re-read on demand. The path is injectable so tests stay hermetic (the
    Settings(_env_file=None) rule).
    """

    def __init__(
        self,
        env_file: str | Path = ".env",
        secrets: SecretStore | None = None,
    ) -> None:
        self._path = Path(env_file)
        self._secrets = secrets

    async def resolve(self, name: str) -> str | None:
        if self._secrets is not None and _ENV_NAME.fullmatch(name):
            pasted = await self._secrets.get(name)
            if pasted:
                return pasted
        value = os.environ.get(name)
        if value:
            return value
        # re-read on demand — the staleness fix: a key added after boot is
        # here the moment it's saved, no restart needed
        return _read_file(self._path).get(name) or None

    async def known_names(self) -> set[str]:
        """Which secret names exist right now — names only, never values.
        The /apps walk asks this to answer 'is the key in yet?'"""
        present: set[str] = set()
        if self._secrets is not None:
            for name in await self._secrets.names():
                if _ENV_NAME.fullmatch(name):
                    present.add(name)
        for name, value in os.environ.items():
            if value and _ENV_NAME.match(name):
                present.add(name)
        for name, value in _read_file(self._path).items():
            if value and _ENV_NAME.match(name):
                present.add(name)
        return present

    async def expand_map(self, mapping: dict[str, str]) -> dict[str, str]:
        """Every value's $NAME / ${NAME} references resolved. A mapping of
        app config is turned into a mapping of real values — but only at
        open time; the stored config keeps carrying the names."""
        return {key: await self.expand_value(value) for key, value in mapping.items()}

    async def expand_value(self, value: str) -> str:
        if "$" not in value:
            return value
        out: list[str] = []
        pos = 0
        for match in _REF.finditer(value):
            name = match.group(1) or match.group(2)
            resolved = await self.resolve(name)
            if not resolved:
                raise SecretEnvError(
                    f"${name} isn't set yet — paste it in /apps or add it "
                    "to .env, and the app connects the moment it's there"
                )
            out.append(value[pos : match.start()])
            out.append(resolved)
            pos = match.end()
        out.append(value[pos:])
        return "".join(out)
