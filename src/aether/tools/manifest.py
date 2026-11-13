"""Tool manifests: the policy each native tool declares about itself.

Every native tool ships one YAML file under ``manifests/``. The file is the
source of truth for the prompt-index line, the declared risk, whether the
tool may run without asking, and whether it needs the owner's own turn to
mutate anything. The Python spec keeps the handler, the schema and the
description; it no longer keeps the tier.

A new native tool without a manifest is a boot failure in `enforce` mode
(the default), so a tool can never quietly inherit a weaker rule than the
one somebody wrote down for it. Provider and MCP tools are unaffected:
their policy comes from the provider's declared effect, and MCP stays on the
fail-safe ladder because its namespace is remote and dynamic.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from .model import MAX_SUMMARY, Risk

MANIFESTS_DIR = Path(__file__).with_name("manifests")

Decision = Literal["allow", "approval"]
Provenance = Literal["none", "owner"]

_DECISIONS = ("allow", "approval")
_PROVENANCES = ("none", "owner")
_RISKS = ("read", "write", "external")


class ManifestError(ValueError):
    """A manifest is malformed or contradicts the registry."""


@dataclass(frozen=True)
class ToolManifest:
    id: str
    legacy: str
    risk: Risk
    decision: Decision
    provenance: Provenance
    core: bool
    summary: str

    @property
    def wire_name(self) -> str:
        return self.id.replace(".", "__")


@lru_cache(maxsize=1)
def manifests() -> dict[str, ToolManifest]:
    """Every declared tool, keyed by ``capability.intent``."""
    found: dict[str, ToolManifest] = {}
    for path in sorted(MANIFESTS_DIR.glob("*.yaml")):
        manifest = _load(path)
        if manifest.id in found:
            raise ManifestError(f"two manifests declare {manifest.id!r}")
        found[manifest.id] = manifest
    if not found:
        raise ManifestError(f"no tool manifests under {MANIFESTS_DIR}")
    return found


def _load(path: Path) -> ToolManifest:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{path.name}: a manifest must be a mapping")
    tool_id = str(raw.get("id") or "").strip()
    if not tool_id or "." not in tool_id:
        raise ManifestError(f"{path.name}: id must be capability.intent, got {tool_id!r}")
    risk = str(raw.get("risk") or "").strip().lower()
    if risk not in _RISKS:
        raise ManifestError(f"{path.name}: unknown risk {risk!r}")
    decision = str(raw.get("decision") or "").strip().lower()
    if decision not in _DECISIONS:
        raise ManifestError(f"{path.name}: unknown decision {decision!r}")
    provenance = str(raw.get("provenance") or "none").strip().lower()
    if provenance not in _PROVENANCES:
        raise ManifestError(f"{path.name}: unknown provenance {provenance!r}")
    summary = " ".join(str(raw.get("summary") or "").split())
    if not summary:
        raise ManifestError(f"{path.name}: summary is required")
    if len(summary) > MAX_SUMMARY:
        raise ManifestError(
            f"{path.name}: summary is {len(summary)} chars — the limit is {MAX_SUMMARY}"
        )
    return ToolManifest(
        id=tool_id,
        legacy=str(raw.get("legacy") or "").strip(),
        risk=risk,  # type: ignore[arg-type]
        decision=decision,  # type: ignore[arg-type]
        provenance=provenance,  # type: ignore[arg-type]
        core=bool(raw.get("core", False)),
        summary=summary,
    )


def manifest_for(name: str) -> ToolManifest | None:
    """The declaration for one native tool, by id, wire name or first name.

    The registrar passes the first name it has (`create_alert`); the audit
    passes the provider-safe wire name (`alerts__create`). Both land here.
    """
    found = manifests()
    direct = found.get(name.replace("__", ".", 1))
    if direct is not None:
        return direct
    for manifest in found.values():
        if name in (manifest.legacy, manifest.wire_name):
            return manifest
    return None


# kept for call sites that name the wire shape explicitly
manifest_for_wire = manifest_for


def audit_native_manifests(registered: list[str]) -> list[str]:
    """Every registered native wire name must resolve to a manifest.

    Optional subsystems (media, workspace, traces) register only when their
    store is wired, so an unused manifest is fine; a registered tool without
    one is the violation this catches.
    """
    return [
        f"{wire}: no manifest under tools/manifests"
        for wire in registered
        if manifest_for(wire) is None
    ]
