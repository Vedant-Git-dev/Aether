"""Tool manifests: the declared policy is data, and a missing one is a boot
failure rather than a silently weaker rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from aether.agent.tools import _declared_spec
from aether.authz.policy import Decision, Policy
from aether.config import AppConfig
from aether.llm.types import WireSpec
from aether.tools.manifest import (
    ManifestError,
    _load,
    audit_native_manifests,
    manifest_for,
    manifests,
)


def test_every_manifest_is_valid_and_unique() -> None:
    found = manifests()

    assert found
    for tool_id, manifest in found.items():
        assert manifest.id == tool_id
        assert "." in manifest.id
        assert manifest.summary
        assert manifest.decision in ("allow", "approval")
        assert manifest.provenance in ("none", "owner")


def test_a_native_tool_resolves_by_id_wire_or_first_name() -> None:
    by_id = manifest_for("alerts.create")
    by_wire = manifest_for("alerts__create")
    by_first = manifest_for("create_alert")

    assert by_id is not None
    assert by_id is by_wire is by_first
    assert by_id.provenance == "owner"
    assert by_id.decision == "allow"


def test_an_offer_always_asks() -> None:
    offer = manifest_for("offer_alert")

    assert offer is not None and offer.decision == "approval"
    # an offer is exactly the unprompted case, so it must not need an owner turn
    assert offer.provenance == "none"


def test_a_manifest_is_taken_into_the_registry_spec() -> None:
    spec = _declared_spec(WireSpec(name="create_alert", description="set an alert"))

    assert spec.id == "alerts.create"
    assert spec.core is True
    assert spec.decision == "allow"
    assert spec.provenance == "owner"
    assert spec.summary == manifest_for("create_alert").summary


def test_a_missing_manifest_falls_back_to_the_ladder_not_a_weaker_rule() -> None:
    spec = _declared_spec(WireSpec(name="mystery__action", description="an unknown tool"))

    assert spec.decision is None
    assert spec.risk == "external"


def test_the_policy_reads_the_declared_decision() -> None:
    policy = Policy([])

    allowed = policy.classify("alerts__create", native=True, declared="allow")
    asked = policy.classify("alerts__offer", native=True, declared="approval")

    assert allowed.decision is Decision.ALLOW
    assert allowed.matched_rule == "builtin:manifest"
    assert asked.decision is Decision.REQUIRE_APPROVAL
    assert asked.matched_rule == "builtin:manifest-approval"


def test_an_undeclared_native_tool_keeps_the_legacy_ladder() -> None:
    policy = Policy([])

    # no declaration: the old classifier decides, and the boot audit is what
    # stops an unmanifested production tool from ever getting here
    ruling = policy.classify("note_entity", native=True, declared=None)

    assert ruling.matched_rule == "builtin:internal"


def test_the_audit_names_a_registered_tool_without_a_manifest() -> None:
    assert audit_native_manifests(["create_alert", "memory__search"]) == []
    assert audit_native_manifests(["mystery__action"]) == [
        "mystery__action: no manifest under tools/manifests"
    ]


def test_the_manifest_audit_defaults_to_enforce() -> None:
    assert AppConfig().tools.manifest_audit == "enforce"


def test_a_malformed_manifest_is_refused(tmp_path: Path) -> None:
    bad_risk = tmp_path / "x.yaml"
    bad_risk.write_text("id: x.y\nrisk: whatever\ndecision: allow\nsummary: x\n", encoding="utf-8")
    long_summary = tmp_path / "y.yaml"
    long_summary.write_text(
        f"id: x.z\nrisk: read\ndecision: allow\nsummary: {'x' * 91}\n", encoding="utf-8"
    )

    with pytest.raises(ManifestError):
        _load(bad_risk)
    with pytest.raises(ManifestError):
        _load(long_summary)
