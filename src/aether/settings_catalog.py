"""The settings catalog: every configuration field the Settings page can
edit, in plain words the backend curates.

The page renders what it is given — it hardcodes no labels, no choices,
no groupings. This module is the vocabulary: which fields exist in which
group, what each one is called, what it does, and which credentials can
be pasted (by name only — values never travel back). Kind and choices
are derived from the config models' own annotations, so the catalog
cannot drift from what ConfigManager will actually accept: a field the
model types as a Literal renders as a dropdown with exactly its options,
a bool renders as a toggle, a number as a number.

Everything editable here flows through ConfigManager — the same
validation, persistence, live-apply and audit chat's /config uses — and
carries the plain-words apply note chat says, so the page can never
disagree with the chat path about when a change bites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

from .config import AppConfig
from .config_store import _apply_note


@dataclass(frozen=True)
class FieldSpec:
    path: str  # the ConfigManager path — also the field's identity everywhere
    label: str  # how it reads on the page
    hint: str  # one plain sentence: what the setting does
    note: str = ""  # when it bites, when _apply_note has nothing
    none_ok: bool = False  # text fields where empty means "not set"


@dataclass(frozen=True)
class GroupSpec:
    key: str
    title: str
    note: str = ""  # the group's own caveat, up front
    restart: bool = False  # saving one of these restarts Aether
    fields: tuple[FieldSpec, ...] = ()
    toggles: str = ""  # enumerate this section's per-item on/off switches
    toggle_hint: str = ""


SETTINGS_GROUPS: tuple[GroupSpec, ...] = (
    GroupSpec(
        key="llm",
        title="Model",
        note="Saving one of these restarts Aether itself — it is back in a few seconds.",
        restart=True,
        fields=(
            FieldSpec(
                path="llm.provider",
                label="Provider",
                hint="Which AI service Aether thinks with.",
            ),
            FieldSpec(
                path="llm.model",
                label="Model",
                hint="The model name the provider answers to.",
            ),
            FieldSpec(
                path="llm.vision_model",
                label="Vision model",
                hint="A second model that reads images. Empty means none.",
                none_ok=True,
            ),
            FieldSpec(
                path="llm.salience_model",
                label="First-pass model",
                hint="A cheaper model that skims what happened and picks what is "
                "worth mentioning. Empty uses the main model.",
                none_ok=True,
            ),
            FieldSpec(
                path="llm.embedding_model",
                label="Memory search model",
                hint="The model that makes memory searchable by meaning. "
                "Empty turns that search off.",
                none_ok=True,
            ),
            FieldSpec(
                path="llm.max_tokens",
                label="Response limit",
                hint="The most one reply can run to.",
            ),
            FieldSpec(
                path="llm.ollama_vision",
                label="Local vision",
                hint="Whether the local setup's model can read images.",
            ),
        ),
    ),
    GroupSpec(
        key="agent",
        title="Behavior",
        fields=(
            FieldSpec(
                path="agent.tick_seconds",
                label="Check interval",
                hint="How often, in seconds, Aether looks around between messages.",
                note="applies from the next tick",
            ),
            FieldSpec(
                path="agent.max_tool_iterations",
                label="Tool call limit",
                hint="The most tool calls one reply may make before Aether stops and answers.",
                note="applies from the next turn",
            ),
            FieldSpec(
                path="agent.daily_surface_cap",
                label="Daily message cap",
                hint="The most unprompted messages Aether sends in one day.",
            ),
            FieldSpec(
                path="agent.quiet_hours",
                label="Quiet hours",
                hint="A window Aether stays quiet in unless something urgent "
                "happens — e.g. 23:00-08:00. Empty means no quiet hours.",
                note="the quiet window is live right away",
                none_ok=True,
            ),
            FieldSpec(
                path="agent.quiet_urgent_salience",
                label="Urgency to break quiet hours",
                hint="How urgent (0-10) something must be to interrupt the quiet window.",
            ),
        ),
    ),
    GroupSpec(
        key="salience",
        title="Attention",
        fields=(
            FieldSpec(
                path="salience.threshold",
                label="Notice threshold",
                hint="How interesting (0-10) something must be before Aether brings it up.",
                note="applies to the next scored event",
            ),
            FieldSpec(
                path="salience.rate_cap_per_hour",
                label="Hourly cap",
                hint="The most things Aether brings up on its own in one hour.",
                note="applies to the next scored event",
            ),
        ),
    ),
    GroupSpec(
        key="messaging",
        title="Chat surfaces",
        note="A surface needs its token pasted below before it can come on.",
        toggles="messaging",
        toggle_hint="Whether Aether listens and replies here.",
    ),
    GroupSpec(
        key="mcp_servers",
        title="App servers",
        note="Servers Aether may call for actions.",
        toggles="mcp_servers",
        toggle_hint="Whether Aether may use this server's actions.",
    ),
    GroupSpec(
        key="authz",
        title="Approvals",
        fields=(
            FieldSpec(
                path="authz.approval_ttl_hours",
                label="Approval window",
                hint="How many hours a one-tap approval stays valid.",
            ),
        ),
    ),
)


@dataclass(frozen=True)
class CredentialSpec:
    name: str  # the env name the paste stores under — shown, never the value
    label: str
    hint: str
    platform: str = ""  # a messaging surface: paste + its toggle reconnects it


CREDENTIALS: tuple[CredentialSpec, ...] = (
    CredentialSpec(
        name="ANTHROPIC_API_KEY",
        label="Provider API key",
        hint="Needed when the provider is set to the matching one.",
    ),
    CredentialSpec(
        name="OPENAI_API_KEY",
        label="Provider API key",
        hint="Needed when the provider is set to the matching one.",
    ),
    CredentialSpec(
        name="GEMINI_API_KEY",
        label="Provider API key",
        hint="Needed when the provider is set to the matching one.",
    ),
    CredentialSpec(
        name="TELEGRAM_BOT_TOKEN",
        label="Telegram bot token",
        hint="From the bot you made for Aether. Paste it here, then turn Telegram on above.",
        platform="telegram",
    ),
    CredentialSpec(
        name="DISCORD_BOT_TOKEN",
        label="Discord bot token",
        hint="From the bot you added for Aether. Paste it here, then turn Discord on above.",
        platform="discord",
    ),
    CredentialSpec(
        name="SLACK_BOT_TOKEN",
        label="Slack bot token",
        hint="From the app you installed for Aether's workspace.",
        platform="slack",
    ),
    CredentialSpec(
        name="SLACK_APP_TOKEN",
        label="Slack app token",
        hint="The app-level token that lets Aether listen in real time.",
        platform="slack",
    ),
    CredentialSpec(
        name="COMPOSIO_API_KEY",
        label="App hub key",
        hint="One key for the app hub — email, code repos, notes and more. "
        "The Apps page connects each one.",
    ),
)


def credential_for(name: str) -> CredentialSpec | None:
    """The paste spec by env name, or None — a stranger can't mint one."""
    return next((c for c in CREDENTIALS if c.name == name), None)


def _walk(config: AppConfig, path: str) -> tuple[type[BaseModel], str, Any]:
    """(model class, field name, live value) for one catalog path — the
    object the path names, so the annotations stay the source of truth."""
    parts = path.split(".")
    obj: Any = config
    for part in parts[:-1]:
        if isinstance(obj, list):  # mcp_servers: one server by its name
            obj = next(s for s in obj if s.name == part)
        else:
            obj = getattr(obj, part)
    return type(obj), parts[-1], getattr(obj, parts[-1])


def _field_type(model: type[BaseModel], field: str) -> tuple[str, list[str]]:
    """(kind, choices) from the model's own annotation — a Literal renders
    as a dropdown with exactly its options, bool as a toggle, a number as
    a number, everything else as text."""
    ann = model.model_fields[field].annotation
    if get_origin(ann) is Literal:
        return "choice", [str(a) for a in get_args(ann)]
    args = [a for a in get_args(ann) if a is not type(None)] or [ann]
    inner = args[0]
    if inner is bool:
        return "toggle", []
    if inner is int or inner is float:
        return "number", []
    return "text", []


def _field_feed(config: AppConfig, spec: FieldSpec, changed: set[str]) -> dict:
    model, field, value = _walk(config, spec.path)
    kind, choices = _field_type(model, field)
    note = spec.note or _apply_note(spec.path).removeprefix(" — ")
    return {
        "path": spec.path,
        "label": spec.label,
        "hint": spec.hint,
        "note": note,
        "kind": kind,
        "choices": choices,
        "value": value,
        "none_ok": spec.none_ok,
        "changed": spec.path in changed,
    }


def _toggle_feed(config: AppConfig, group: GroupSpec, changed: set[str]) -> list[dict]:
    """The per-item on/off switches, enumerated from the live config — a
    chat surface or an app server that isn't configured never shows."""
    section = getattr(config, group.toggles)
    items: list[tuple[str, str]]  # (item key, label)
    if isinstance(section, list):
        items = [(s.name, s.name) for s in section]
    else:
        items = [(key, key.capitalize()) for key in type(section).model_fields]
    return [
        {
            "path": f"{group.toggles}.{key}.enabled",
            "label": label,
            "hint": group.toggle_hint,
            "note": "",
            "kind": "toggle",
            "choices": [],
            "value": (
                next(s for s in section if s.name == key).enabled
                if isinstance(section, list)
                else getattr(section, key).enabled
            ),
            "none_ok": False,
            "changed": f"{group.toggles}.{key}.enabled" in changed,
        }
        for key, label in items
    ]


def settings_feed(config: AppConfig, changed: set[str], known: set[str]) -> dict:
    """The whole editable surface: one entry per group with its fields
    (kinds and choices derived, values effective, plain-words notes), plus
    the credential paste fields with their set flags — names only."""
    groups = []
    for group in SETTINGS_GROUPS:
        fields = [_field_feed(config, f, changed) for f in group.fields]
        if group.toggles:
            fields.extend(_toggle_feed(config, group, changed))
        groups.append(
            {
                "key": group.key,
                "title": group.title,
                "note": group.note,
                "restart": group.restart,
                "fields": fields,
            }
        )
    credentials = [
        {"name": c.name, "label": c.label, "hint": c.hint, "set": c.name in known}
        for c in CREDENTIALS
    ]
    return {"groups": groups, "credentials": credentials}
