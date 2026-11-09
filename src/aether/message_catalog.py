"""The message catalog: fixed user-facing text as data.

The backend owns a small number of sentences that are not model-authored —
an ask's option layout, the caveat a composer may need, a safe fallback.
Those live in ``messages.yaml`` as keyed strings with ``{placeholders}``,
per locale. A missing locale falls back to the default; a missing key is a
programming error, not a reason to invent words at runtime.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("aether.messages")

_CATALOG_PATH = Path(__file__).with_name("messages.yaml")


class MessageCatalog:
    """One loaded catalog, with locale fallback and parameter rendering."""

    def __init__(self, data: dict[str, Any], *, locale: str = "en") -> None:
        self._data = data
        self._default = str(data.get("default_locale") or "en")
        self._locale = locale or self._default

    @classmethod
    def load(cls, path: str | Path | None = None, *, locale: str = "en") -> MessageCatalog:
        target = Path(path) if path is not None else _CATALOG_PATH
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("the message catalog must be a mapping")
        return cls(data, locale=locale)

    def has(self, key: str) -> bool:
        return key in self._locale_map(self._locale) or key in self._locale_map(self._default)

    def render(self, key: str, /, **params: Any) -> str:
        """The sentence for `key`, formatted with `params`."""
        text = self._lookup(key)
        if text is None:
            raise KeyError(f"message catalog has no key {key!r}")
        try:
            return text.format(**params)
        except KeyError as exc:
            raise KeyError(f"message catalog key {key!r} needs {exc.args[0]!r}") from exc

    def _locale_map(self, locale: str) -> dict[str, str]:
        locales = self._data.get("locales") or {}
        found = locales.get(locale) if isinstance(locales, dict) else None
        return found if isinstance(found, dict) else {}

    def _lookup(self, key: str) -> str | None:
        value = self._locale_map(self._locale).get(key)
        if value is None:
            value = self._locale_map(self._default).get(key)
        return str(value) if value is not None else None


_CACHE: dict[str, MessageCatalog] = {}


def catalog(locale: str = "en") -> MessageCatalog:
    """The shared catalog for one locale — loaded once per process."""
    key = locale or "en"
    found = _CACHE.get(key)
    if found is None:
        found = MessageCatalog.load(locale=key)
        _CACHE[key] = found
    return found


def render(key: str, /, *, locale: str = "en", **params: Any) -> str:
    return catalog(locale).render(key, **params)
