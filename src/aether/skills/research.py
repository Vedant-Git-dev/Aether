"""The research skill — a topic from your profile, looked up and kept.

Each confirmed profile entry names something you care about; once a
week the skill looks one up on the web and keeps the notes as a
workspace fact. The key carries the ISO week, so a topic can recur
without ever becoming two tasks in the same week — the dedup the store
enforces does the rest. The search and the note both run through the
executor's gate; neither one leaves Aether, so nothing parks unless the
policy says otherwise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..llm.types import ToolCall
from ..proactive.store import Opportunity, ProactiveTask
from ..proactive.worker import RunOutcome
from ..web import _NOT_CONNECTED
from ..workbench.base import ORDINARY
from .base import EventReader, Executor, ProfileSource, Runner

# the search's own plain sentences for "this did not run" — a look that
# didn't happen must never be saved as though it did
_NOT_RUN = (
    "not searched:",
    "the search took too long",
    "the search couldn't",
    "the daily search cap",
    "web_search needs a query",
)
_SAVED = "Saved to the workspace"


def _week(now: datetime | None = None) -> str:
    """The ISO week a research key is bucketed by — one look per topic
    per week, the only repeat the dedup allows."""
    iso = (now or datetime.now()).isocalendar()
    return f"{iso.year}-W{iso.week}"


class ResearchSkill:
    name: str = "research"
    description: str = "Look up something you care about and keep the notes"
    declared_actions: tuple[str, ...] = ("save a note in your workspace",)
    sensitivity: str = ORDINARY  # reads the web, writes only to the workspace

    async def detect(
        self, profile: ProfileSource | None, events: EventReader | None, executor: Executor | None
    ) -> list[Opportunity]:
        if profile is None:
            return []
        found: list[Opportunity] = []
        for entry in profile.profile():
            topic = str(getattr(entry, "text", "")).strip()
            if not topic:
                continue
            found.append(
                Opportunity(
                    key=f"research:{topic[:80].lower()}:{_week()}",
                    skill=self.name,
                    label=f"look up {topic[:60]}",
                    score=3.0,
                    detail={"topic": topic[:200]},
                )
            )
        return found

    async def plan(
        self,
        opportunity: Opportunity,
        profile: ProfileSource | None,
        executor: Executor | None,
    ) -> dict[str, Any]:
        topic = str(opportunity.detail.get("topic", ""))
        return {"step": "search the web and keep the digest", "query": topic}

    async def run(
        self, task: ProactiveTask, workbench: Runner | None, executor: Executor | None
    ) -> RunOutcome:
        if executor is None:
            raise RuntimeError("the worker isn't wired to tools, so nothing could be looked up")
        topic = str(task.opportunity.get("topic", ""))
        results = await executor(
            ToolCall(
                id=f"task-{task.id}",
                name="web_search",
                arguments={"query": topic},
                plain="search the web",
            )
        )
        if results == _NOT_CONNECTED or results.startswith(_NOT_RUN):
            # the look never happened — the row says so, honestly
            raise RuntimeError(results)
        if results.startswith("No results"):
            return RunOutcome(summary=f"I looked up {topic[:60]} and found nothing to keep")
        digest = (
            f"Research notes on {topic} (looked up {datetime.now():%Y-%m-%d}):\n\n{results[:600]}"
        )
        saved = await executor(
            ToolCall(
                id=f"task-{task.id}",
                name="workspace_remember",
                arguments={"text": digest, "kind": "fact", "category": "Research"},
                plain="keep the research note",
            )
        )
        if not saved.startswith(_SAVED):
            raise RuntimeError(saved)
        return RunOutcome(summary=f"I looked up {topic[:60]} and kept the notes in your workspace")

    async def revalidate(
        self, opportunity: Opportunity, profile: ProfileSource | None, events: EventReader | None
    ) -> bool:
        """The topic still has to be part of the confirmed profile when an
        approval fires — a topic you took off your profile is not
        something to act on."""
        if profile is None:
            return False
        topic = str(opportunity.detail.get("topic", ""))
        return any(str(getattr(e, "text", "")) == topic for e in profile.profile())
