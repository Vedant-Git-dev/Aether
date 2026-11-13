"""The source manifest: which scopes exist and which ones can fire.

Loaded once from ``sources.yaml``. The tool schema's scope enum, the default
notification style for a new alert and the plain-language refusal for a scope
with no event source all come from this data.

A manifest that names a scope twice, or an alternative that does not exist,
is a programming error: boot fails rather than shipping a half-known
vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_MANIFEST_PATH = Path(__file__).with_name("sources.yaml")


@dataclass(frozen=True)
class SourceScope:
    key: str
    label: str
    supports_events: bool
    event_kinds: tuple[str, ...]
    default_style: str
    alternative: str = ""


@dataclass(frozen=True)
class SourceManifest:
    scopes: tuple[SourceScope, ...]
    examples: tuple[str, ...]
    event_labels: dict[str, str]

    def by_key(self, key: str) -> SourceScope | None:
        wanted = (key or "").strip().lower()
        return next((scope for scope in self.scopes if scope.key == wanted), None)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(scope.key for scope in self.scopes)

    @property
    def event_kinds(self) -> tuple[str, ...]:
        """Every event kind at least one scope emits, in manifest order."""
        seen: list[str] = []
        for scope in self.scopes:
            for kind in scope.event_kinds:
                if kind not in seen:
                    seen.append(kind)
        return tuple(seen)

    def style_for(self, key: str) -> str:
        scope = self.by_key(key)
        return scope.default_style if scope is not None else "immediate"

    def label_for_kind(self, kind: str) -> str:
        """The event kind in plain words, never the raw token."""
        clean = (kind or "").strip()
        known = self.event_labels.get(clean)
        if known:
            return known
        return clean.replace("_", " ")


@lru_cache(maxsize=1)
def manifest() -> SourceManifest:
    return load_manifest(_MANIFEST_PATH)


def load_manifest(path: str | Path) -> SourceManifest:
    data: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("the source manifest must be a mapping")
    raw_scopes = data.get("scopes")
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raise ValueError("the source manifest needs at least one scope")
    scopes: list[SourceScope] = []
    seen: set[str] = set()
    for raw in raw_scopes:
        if not isinstance(raw, dict):
            raise ValueError("every source scope must be a mapping")
        key = str(raw.get("key") or "").strip().lower()
        if not key or key in seen:
            raise ValueError(f"duplicate or empty source scope {key!r}")
        seen.add(key)
        scopes.append(
            SourceScope(
                key=key,
                label=str(raw.get("label") or key),
                supports_events=bool(raw.get("supports_events", False)),
                event_kinds=tuple(str(kind) for kind in raw.get("event_kinds") or ()),
                default_style=str(raw.get("default_style") or "immediate"),
                alternative=str(raw.get("alternative") or ""),
            )
        )
    known = {scope.key for scope in scopes}
    for scope in scopes:
        if scope.alternative and scope.alternative not in known:
            raise ValueError(
                f"source scope {scope.key!r} names an unknown alternative {scope.alternative!r}"
            )
        if scope.supports_events and not scope.event_kinds:
            raise ValueError(f"source scope {scope.key!r} supports events but names no event kind")
    examples = tuple(str(item) for item in data.get("examples") or ())
    raw_labels = data.get("event_labels") or {}
    if not isinstance(raw_labels, dict):
        raise ValueError("event_labels must be a mapping of kind to plain words")
    event_labels = {str(key): str(value) for key, value in raw_labels.items()}
    return SourceManifest(scopes=tuple(scopes), examples=examples, event_labels=event_labels)


def supported_scopes() -> tuple[SourceScope, ...]:
    """The scopes that can fire today, in manifest order."""
    return tuple(scope for scope in manifest().scopes if scope.supports_events)


def nearest_alternative(key: str) -> SourceScope | None:
    """The working scope to offer instead, from the manifest — or None."""
    scope = manifest().by_key(key)
    if scope is None or not scope.alternative:
        return None
    return manifest().by_key(scope.alternative)
