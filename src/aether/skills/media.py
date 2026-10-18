"""The media skill — a shared file that never got a note, described.

A document shared in chat without any caption is an opportunity: run a
description job in the cloud workbench over the text Aether already
extracted, and keep the result as the task's own summary. The job is
the workbench seam in miniature — the spec carries the script and the
file it needs, nothing else; the routing ladder decides where it runs;
the outcome reports the provider and the seconds it spent, so the
daily budgets charge what was truly used.

Keyed by its event id, like every one-shot opportunity: a file can
never become two tasks.
"""

from __future__ import annotations

from typing import Any

from ..proactive.store import Opportunity, ProactiveTask
from ..proactive.worker import RunOutcome
from ..workbench.base import (
    BACKGROUND,
    LABEL,
    LATENCY_CLASS,
    ORDINARY,
    SENSITIVITY,
    TASK_ID,
    JobSpec,
)
from .base import EventReader, Executor, ProfileSource, Runner

# the whole job: read the document, print a one-line description. The
# point is the seam, not the linguistics — a real caption job would do
# its work here, in the sandbox, with only this file in its world.
_SCRIPT = (
    "text = open('document.txt', encoding='utf-8').read()\n"
    "words = text.split()\n"
    "opening = ' '.join(words[:24])\n"
    "print(f'a {len(words)}-word document that opens with: {opening}')\n"
)


class MediaSkill:
    name: str = "media"
    description: str = "Describe a shared file that never got a note"
    declared_actions: tuple[str, ...] = ("run a description job in the cloud workbench",)
    sensitivity: str = ORDINARY  # background work, ordinary class

    async def detect(
        self, profile: ProfileSource | None, events: EventReader | None, executor: Executor | None
    ) -> list[Opportunity]:
        if events is None:
            return []
        found: list[Opportunity] = []
        for event in await events.recent_memorable(limit=50):
            if getattr(event, "kind", "") != "shared_doc":
                continue
            payload = getattr(event, "payload", None)
            if not isinstance(payload, dict):
                continue
            filename = str(payload.get("filename", "")).strip()
            text = str(payload.get("text", "")).strip()
            if not filename or not text or payload.get("caption"):
                # nothing to describe, or the file came with its own note
                continue
            found.append(
                Opportunity(
                    key=f"media:{event.id}",
                    skill=self.name,
                    label=f"describe the shared file {filename[:40]}",
                    score=3.0,
                    detail={"event_id": event.id, "filename": filename, "text": text[:2000]},
                )
            )
        return found

    async def plan(
        self,
        opportunity: Opportunity,
        profile: ProfileSource | None,
        executor: Executor | None,
    ) -> dict[str, Any]:
        filename = str(opportunity.detail.get("filename", ""))
        return {"step": "run the description job", "script": "describe.py", "filename": filename}

    async def run(
        self, task: ProactiveTask, workbench: Runner | None, executor: Executor | None
    ) -> RunOutcome:
        if workbench is None:
            raise RuntimeError("no cloud workbench is connected, so the file job couldn't run")
        detail = task.opportunity
        spec = JobSpec(
            script_name="describe.py",
            files={"describe.py": _SCRIPT, "document.txt": str(detail.get("text", ""))},
            meta={
                TASK_ID: task.id,
                LABEL: task.label,
                SENSITIVITY: ORDINARY,
                LATENCY_CLASS: BACKGROUND,
            },
        )
        result = await workbench(spec)
        seconds = int(result.duration)
        if not result.exit_ok:
            # the job ran and failed its own way — the row says so, and
            # the time it truly spent is still charged
            return RunOutcome(
                summary=f"the file job failed its own way — {result.stderr[:160]}",
                provider=result.provider,
                sandbox_seconds=seconds,
                failed=True,
            )
        notes = result.stdout.strip()[:200] or "the job finished quietly"
        return RunOutcome(
            summary=f'notes on "{str(detail.get("filename", ""))[:40]}": {notes}',
            provider=result.provider,
            sandbox_seconds=seconds,
        )

    async def revalidate(
        self, opportunity: Opportunity, profile: ProfileSource | None, events: EventReader | None
    ) -> bool:
        """The file still has to be in memory when an approval fires — a
        file that vanished has nothing left to describe."""
        if events is None:
            return False
        event = await events.get(int(opportunity.detail.get("event_id", 0)))
        if event is None:
            return False
        payload = getattr(event, "payload", None)
        return isinstance(payload, dict) and bool(payload.get("filename"))
