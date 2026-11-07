"""The choices, read from the repository's own catalogs.

Nothing is hardcoded twice: providers come from the ``Literal`` in
``aether.config``, direct apps from the provider definitions, chat channels
from the recipe catalog, and optional services from the quickstart table. When
the wizard runs outside a checkout (an installed wheel), sensible fallbacks
keep it usable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from .plan import SetupPlan

# Used only when the repository's own files are not on disk.
_FALLBACK_PROVIDERS = ["anthropic", "openai", "gemini", "ollama", "nvidia"]
_FALLBACK_MODEL = "claude-opus-5"
_FALLBACK_SERVICES = [
    "Web search",
    "Voice-note transcription",
    "Background jobs",
    "Image generation",
]
_FALLBACK_APPS = [
    ("gmail", "Gmail"),
    ("calendar", "Google Calendar"),
    ("drive", "Google Drive"),
    ("docs", "Google Docs"),
    ("github", "GitHub"),
    ("linkedin", "LinkedIn"),
]
_FALLBACK_CHANNELS = [("telegram", "telegram"), ("discord", "discord")]

REGIONS = ["oregon", "ohio", "virginia", "frankfurt", "singapore"]
SETUP_TARGETS = [
    ("local", "Run locally on this machine"),
    ("render", "Deploy to Render"),
]
DATABASES = [
    ("neon", "Neon free Postgres (recommended)"),
    ("existing", "An existing Postgres connection string"),
    ("render", "Render Postgres (expires after 30 days)"),
]
PLANS = [
    ("free", "Free - 512 MB, spins down when idle, ephemeral disk"),
    ("starter", "Starter - about $7/month, no spin-down, persistent disk"),
]
DEPLOY_SOURCES = [
    ("public", "Public upstream repo (fastest)"),
    ("fork", "My own fork (needed for API automation)"),
]
UPTIME_MODES = [
    ("api", "UptimeRobot with an API key"),
    ("email", "UptimeRobot email confirmation (no API key)"),
    ("manual", "Print the steps and open the page"),
    ("skip", "Skip monitoring"),
]
IMAGE_PROVIDERS = [
    ("pollinations", "Pollinations (no key)"),
    ("gemini", "Gemini (uses the Gemini key)"),
    ("none", "No image generation"),
]
WORKBENCH_PROVIDERS = [
    ("modal", "Modal"),
    ("vercel", "Vercel"),
    ("github_actions", "GitHub Actions"),
    ("none", "No background sandbox"),
]


@dataclass(frozen=True)
class Choice:
    key: str
    label: str


def repo_root(start: Path | None = None) -> Path | None:
    """Find the checkout root, or None when running from an installed wheel."""
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").is_file() and (parent / "config.example.yaml").is_file():
            return parent
    for candidate in (Path.cwd(), Path.cwd().parent):
        if (candidate / "config.example.yaml").is_file():
            return candidate
    return None


def _read(relative: str, root: Path | None) -> str:
    if root is None:
        return ""
    path = root / relative
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def providers() -> list[str]:
    try:
        from aether.config import LLMConfig

        annotation: Any = LLMConfig.model_fields["provider"].annotation
        values = [str(item) for item in get_args(annotation)]
        if values:
            return values
    except Exception:
        pass
    return list(_FALLBACK_PROVIDERS)


def default_model(root: Path | None = None) -> str:
    text = _read("config.example.yaml", root)
    match = re.search(r"(?m)^\s+model:\s*(\S+)", text)
    return match.group(1) if match else _FALLBACK_MODEL


def example_model(provider: str, root: Path | None = None) -> str:
    text = _read("config.example.yaml", root)
    match = re.search(rf"{re.escape(provider)},\s*model:\s*([A-Za-z0-9._/-]+)", text)
    return match.group(1) if match else default_model(root)


def optional_services(root: Path | None = None) -> list[Choice]:
    text = _read("docs/quickstart.md", root)
    found: list[Choice] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## Optional services"):
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if not inside or not line.startswith("|"):
            continue
        capability = line.strip("|").split("|")[0].strip()
        if not capability or capability in {"Capability", "---"} or set(capability) <= {"-"}:
            continue
        lowered = capability.lower()
        # Semantic memory has its own on/off question; screen capture needs
        # the companion and is not offered here.
        if "screen capture" in lowered or "semantic memory" in lowered:
            continue
        found.append(Choice(capability.lower().replace(" ", "_"), capability))
    if found:
        return found
    return [Choice(item.lower().replace(" ", "_"), item) for item in _FALLBACK_SERVICES]


def direct_apps(root: Path | None = None) -> list[Choice]:
    try:
        from aether.providers.catalog import PROVIDERS

        found = [Choice(app.key, app.name) for provider in PROVIDERS for app in provider.apps]
        if found:
            return found
    except Exception:
        pass
    return [Choice(key, name) for key, name in _FALLBACK_APPS]


def chat_channels() -> list[Choice]:
    try:
        from aether.apps_catalog import CATALOG

        found = [
            Choice(recipe.key, recipe.name) for recipe in CATALOG if recipe.kind == "messaging"
        ]
        if found:
            return found
    except Exception:
        pass
    return [Choice(key, name) for key, name in _FALLBACK_CHANNELS]


def labels(choices: list[Choice]) -> list[str]:
    return [choice.label for choice in choices]


def keys_for_labels(choices: list[Choice], selected: list[int]) -> list[str]:
    return [choices[index].key for index in selected]


def plan_defaults(root: Path | None = None) -> SetupPlan:
    """The defaults a bare Enter accepts on every question."""
    return SetupPlan(
        llm_provider=providers()[0],
        llm_model=example_model(providers()[0], root),
        services=[],
        apps=[],
        channels=[],
        image_provider="none",
        workbench_provider="none",
    )


def apply_dependencies(plan: SetupPlan) -> SetupPlan:
    """Keep the dependent image/workbench answers in step with the services."""
    selected = {item.lower() for item in plan.services}
    if not any("image generation" in item for item in selected):
        plan.image_provider = "none"
    if not any("background jobs" in item for item in selected):
        plan.workbench_provider = "none"
    return plan
