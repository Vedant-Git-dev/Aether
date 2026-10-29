"""The settings catalog: every configuration field the Settings page can
edit, in plain words the backend curates.

The page renders what it is given — it hardcodes no labels, no choices,
no groupings. This module is the vocabulary: which fields exist in which
group, what each one is called, what it does, and which credentials can
be pasted (by name only — values never travel back). Kind and choices
are derived from the config models' own annotations, so the catalog
cannot drift from what ConfigManager will actually accept: a field the
model types as a Literal renders as a dropdown with exactly its options,
a bool renders as a toggle, a number as a number. A field the catalog
declares a model field renders as the model picker, with the sentinel
choices that apply to it — the models themselves are fetched live from
the configured keys, never hardcoded here.

Everything editable here flows through ConfigManager — the same
validation, persistence, live-apply and audit chat's /config uses — and
carries the plain-words apply note chat says, so the page can never
disagree with the chat path about when a change bites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

from .config import MODEL_DEFAULT, MODEL_OFF, AppConfig
from .config_store import _apply_note


@dataclass(frozen=True)
class FieldSpec:
    path: str  # the ConfigManager path — also the field's identity everywhere
    label: str  # how it reads on the page
    hint: str  # one plain sentence: what the setting does
    note: str = ""  # when it bites, when _apply_note has nothing
    none_ok: bool = False  # text fields where empty means "not set"
    model: bool = False  # a model picker: the feed names its sentinel choices


@dataclass(frozen=True)
class GroupSpec:
    key: str
    title: str
    note: str = ""  # the group's own caveat, up front
    restart: bool = False  # saving one of these restarts Aether
    fields: tuple[FieldSpec, ...] = ()
    toggles: str = ""  # enumerate this section's per-item on/off switches
    toggle_hint: str = ""


# The sentinel choices each model field offers, in order — the first is
# also what an empty value behaves as (the honest off for the senses,
# the main model for the first pass). The main model itself has no
# sentinels — something has to think — and neither does memory search:
# it is a forced choice of a real model. "base" is the legacy spelling
# of "default" and renders as it.
_MODEL_SENTINELS: dict[str, tuple[str, ...]] = {
    "llm.model": (),
    "llm.image_model": (MODEL_OFF, MODEL_DEFAULT),
    "llm.audio_model": (MODEL_OFF, MODEL_DEFAULT),
    "llm.video_model": (MODEL_OFF, MODEL_DEFAULT),
    "llm.salience_model": (MODEL_DEFAULT, MODEL_OFF),
    "llm.embedding_model": (),
}

# the plain words the sentinels read as on the page
_SENTINEL_LABELS = {
    MODEL_OFF: "None (off)",
    MODEL_DEFAULT: "Use default",
}

SETTINGS_GROUPS: tuple[GroupSpec, ...] = (
    GroupSpec(
        key="llm",
        title="Model",
        note="Saving one of these restarts Aether itself — it is back in a few seconds.",
        restart=True,
        fields=(
            FieldSpec(
                path="llm.model",
                label="Model",
                hint="The model Aether thinks with, picked from the ones your keys can reach.",
                model=True,
            ),
            FieldSpec(
                path="llm.image_model",
                label="Image model",
                hint="The model that reads images — none (off) leaves them "
                "unseen, or use default for the main model.",
                none_ok=True,
                model=True,
            ),
            FieldSpec(
                path="llm.audio_model",
                label="Audio model",
                hint="The model that listens to audio — none (off) leaves only "
                "transcription, or use default for the main model.",
                none_ok=True,
                model=True,
            ),
            FieldSpec(
                path="llm.video_model",
                label="Video model",
                hint="The model that watches video — none (off) falls back to "
                "frames, or use default for the main model.",
                none_ok=True,
                model=True,
            ),
            FieldSpec(
                path="llm.salience_model",
                label="First-pass model",
                hint="A cheaper model that skims what happened and picks what is "
                "worth mentioning — use default for the main model, or none "
                "to stop the skim and bring nothing up on its own.",
                none_ok=True,
                model=True,
            ),
            FieldSpec(
                path="llm.embedding_model",
                label="Memory search model",
                hint="The model that makes memory searchable by meaning, on your "
                "Gemini key — a real model is always picked here.",
                model=True,
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
                path="agent.turn_timeout_seconds",
                label="Reply time limit",
                hint="How many seconds one reply may take before Aether stops it and tells you.",
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
            FieldSpec(
                path="agent.max_searches_per_day",
                label="Daily web search cap",
                hint="The most web searches Aether runs in one day, chat and background together.",
                note="applies to the next search",
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
    GroupSpec(
        key="proactive",
        title="Background work",
        note="Off by default. Every cap here is a hard stop for the day.",
        fields=(
            FieldSpec(
                path="proactive.enabled",
                label="Background worker",
                hint="Whether Aether looks for useful things to do on its own, within these caps.",
            ),
            FieldSpec(
                path="proactive.interval_minutes",
                label="Check interval",
                hint="How often, in minutes, the background worker looks around.",
            ),
            FieldSpec(
                path="proactive.max_tasks_per_day",
                label="Daily task cap",
                hint="The most background tasks one day may run.",
            ),
            FieldSpec(
                path="proactive.max_tokens_per_day",
                label="Daily thinking budget",
                hint="The most background thinking one day may spend.",
            ),
            FieldSpec(
                path="proactive.max_sandbox_minutes_per_day",
                label="Daily sandbox time",
                hint="The most minutes background jobs may run on a throwaway "
                "cloud machine in one day.",
            ),
            FieldSpec(
                path="proactive.notify_daily_cap",
                label="Daily result cap",
                hint="The most result notifications one day carries — quiet hours "
                "ride their own budget.",
            ),
        ),
    ),
    GroupSpec(
        key="workbench",
        title="Cloud workbench",
        fields=(
            FieldSpec(
                path="workbench.provider",
                label="Default sandbox",
                hint="Where a background job runs first when it must not run on "
                "this computer. The others stand by as fallbacks.",
            ),
            FieldSpec(
                path="workbench.monthly_minutes_cap",
                label="Monthly batch minutes",
                hint="The monthly ceiling on the batch queue's wall-clock minutes.",
            ),
            FieldSpec(
                path="workbench.job_timeout_minutes",
                label="Job timeout",
                hint="One job's own time limit — exceeding it counts as a real "
                "timed-out result, never a fallback.",
            ),
            FieldSpec(
                path="workbench.actions_repo",
                label="Batch queue repository",
                hint="The repository (owner/name) whose runner workflow carries "
                "the batch queue's jobs. Empty leaves the batch queue off.",
            ),
            FieldSpec(
                path="workbench.actions_workflow",
                label="Batch queue workflow",
                hint="The workflow file name inside that repository (like aether-batch.yml).",
            ),
            FieldSpec(
                path="workbench.actions_ref",
                label="Batch queue branch",
                hint="The branch the workflow dispatches on — main unless yours differs.",
            ),
        ),
    ),
    GroupSpec(
        key="media",
        title="Media",
        note="Shared and generated images, voice, and video are kept encrypted.",
        fields=(
            FieldSpec(
                path="media.storage_quota_mb",
                label="Storage quota",
                hint="How many megabytes of media Aether keeps before the oldest "
                "unreferenced items are removed. A full store refuses new media "
                "in plain words.",
                note="applies to the next media stored",
            ),
            FieldSpec(
                path="media.image_provider",
                label="Image maker",
                hint="Who draws images when asked: a keyless free service, the "
                "Gemini key you already have, or nobody.",
                note="applies to the next image",
            ),
            FieldSpec(
                path="media.max_images_per_day",
                label="Daily image cap",
                hint="The most images Aether may make in one day, chat and background together.",
                note="applies to the next image",
            ),
            FieldSpec(
                path="media.max_prompt_chars",
                label="Prompt length cap",
                hint="The most characters an image prompt may run to — prompts "
                "travel to the image service, so they stay short.",
                note="applies to the next image",
            ),
            FieldSpec(
                path="media.max_sandbox_jobs_per_day",
                label="Daily media job cap",
                hint="The most video-frame and video-edit jobs one day may run "
                "on the cloud workbench.",
                note="applies to the next media job",
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
        name="GROQ_API_KEY",
        label="Voice transcription key",
        hint="Groq's free key — with it, Aether reads the voice notes you send.",
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
        name="TAVILY_API_KEY",
        label="Web search key",
        hint="Lets Aether search the web. Without it, web search says "
        "it isn't connected and nothing else changes.",
    ),
    CredentialSpec(
        name="MODAL_TOKEN_ID",
        label="Cloud sandbox key",
        hint="The public half of the key pair that lets background work "
        "run on a throwaway cloud machine. Paste both halves here.",
    ),
    CredentialSpec(
        name="MODAL_TOKEN_SECRET",
        label="Cloud sandbox secret",
        hint="The private half of the sandbox key pair — shown once when "
        "it's created. Without both halves, nothing runs off this computer.",
    ),
    CredentialSpec(
        name="VERCEL_TOKEN",
        label="Backup sandbox key",
        hint="An access token for the backup cloud sandbox. Paste it with "
        "the team and project ids below to give background work a second "
        "place to run.",
    ),
    CredentialSpec(
        name="VERCEL_TEAM_ID",
        label="Backup sandbox team",
        hint="The id of the team the backup sandbox project belongs to — "
        "copied from its settings page. All three backup values are needed "
        "before anything runs there.",
    ),
    CredentialSpec(
        name="VERCEL_PROJECT_ID",
        label="Backup sandbox project",
        hint="The id of the project the backup sandbox runs under — copied from its settings page.",
    ),
    CredentialSpec(
        name="GITHUB_ACTIONS_TOKEN",
        label="Batch queue key",
        hint="A fine-grained key that can start jobs in your own code "
        "repository. Only patient, non-sensitive work ever goes there, "
        "and only when the batch queue is switched on in the settings.",
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
    # a model field is declared, not derived: the value stays a plain
    # string (any id validates), but the page renders the picker with the
    # sentinel choices that apply to it
    sentinels: list[dict] = []
    if spec.model:
        kind = "model"
        choices = []
        sentinels = [
            {"value": word, "label": _SENTINEL_LABELS[word]} for word in _MODEL_SENTINELS[spec.path]
        ]
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
        "sentinels": sentinels,
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
            "sentinels": [],
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
