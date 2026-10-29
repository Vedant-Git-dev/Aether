"""The follow-ups skill — promises found in memory, drafted for a reply.

Detection is deterministic: a memorable event whose text carries a
promise the user made ("i'll", "i will", "remind", "follow up", "get
back") is an opportunity, keyed by its event id so the same promise can
never become two tasks. Planning composes the draft from the promise's
own words — no model, no guessing. Running proposes the reply through
the executor: over the surface the promise arrived on when Aether knows
that surface's send (an external send parks for approval, exactly like
any other outbound action), and into the user's own chat otherwise —
a reminder they can act on themselves.
"""

from __future__ import annotations

from typing import Any

from ..llm.types import ToolCall
from ..proactive.store import Opportunity, ProactiveTask
from ..proactive.worker import RunOutcome
from ..workbench.base import SENSITIVE
from .base import DENIED, HELD, EventReader, Executor, ProfileSource, Runner, held_approval

_MARKERS = ("i'll", "i will", "remind", "follow up", "get back")
# the one send this example knows by name — the codebase's own
# documented app action. Surfaces without a known send fall back to a
# reminder in the user's own chat, which never leaves Aether.
_REPLY_TOOLS = {"gmail": "gmail__send_email"}


def _text_of(payload: Any) -> str:
    """The words an event carries — its string fields, nothing else, so
    the markers match what was said rather than metadata about it."""
    if not isinstance(payload, dict):
        return ""
    return " ".join(v for v in payload.values() if isinstance(v, str)).strip()


class FollowupsSkill:
    name: str = "followups"
    description: str = "Spot promises you made and draft the follow-up"
    declared_actions: tuple[str, ...] = ("send a reply", "send you a reminder")
    sensitivity: str = SENSITIVE  # its work can leave Aether, so it says so

    async def detect(
        self, profile: ProfileSource | None, events: EventReader | None, executor: Executor | None
    ) -> list[Opportunity]:
        if events is None:
            return []
        found: list[Opportunity] = []
        for event in await events.recent_memorable(limit=50):
            text = _text_of(getattr(event, "payload", None))
            if not any(m in text.lower() for m in _MARKERS):
                continue
            payload = getattr(event, "payload", {}) or {}
            sender = payload.get("_sender") if isinstance(payload, dict) else None
            address = ""
            if isinstance(sender, dict):
                handle = str(sender.get("handle", ""))
                if "@" in handle:
                    address = handle
            found.append(
                Opportunity(
                    key=f"followup:{event.id}",
                    skill=self.name,
                    label=f"follow up on: {text[:60]}",
                    score=5.0,
                    detail={
                        "event_id": event.id,
                        "text": text[:400],
                        "source": str(getattr(event, "source", "")),
                        "address": address,
                    },
                )
            )
        return found

    async def plan(
        self,
        opportunity: Opportunity,
        profile: ProfileSource | None,
        executor: Executor | None,
    ) -> dict[str, Any]:
        text = str(opportunity.detail.get("text", ""))
        return {
            "step": "draft the reply",
            "draft": f"Following up on what you mentioned — {text[:200]}",
        }

    async def run(
        self, task: ProactiveTask, workbench: Runner | None, executor: Executor | None
    ) -> RunOutcome:
        if executor is None:
            raise RuntimeError("the worker isn't wired to tools, so nothing could be sent")
        detail = task.opportunity
        text = str(detail.get("text", ""))
        draft = f"Following up on what you mentioned — {text[:200]}"
        tool = _REPLY_TOOLS.get(str(detail.get("source", "")))
        if tool is not None and detail.get("address"):
            call = ToolCall(
                id=f"task-{task.id}",
                name=tool,
                arguments={
                    "to": str(detail["address"]),
                    "subject": "following up",
                    "body": draft,
                },
                plain="send the follow-up reply",
            )
        else:
            call = ToolCall(
                id=f"task-{task.id}",
                name="send_chat_message",
                arguments={"text": f"You said you'd get back to this: {draft}"},
                plain="send you the reminder",
            )
        reply = await executor(call)
        if reply.startswith(DENIED):
            return RunOutcome(summary=reply, refused=True)
        if reply.startswith(HELD):
            # the staged action's id rides back so the task's own Approve
            # button carries exactly this one out (after revalidation)
            return RunOutcome(
                summary="I drafted the follow-up reply and it's waiting for your OK",
                tokens=0,
                approval_id=held_approval(reply),
            )
        return RunOutcome(summary="the reminder is in your chat", tokens=0)

    async def revalidate(
        self, opportunity: Opportunity, profile: ProfileSource | None, events: EventReader | None
    ) -> bool:
        """The promise still has to be in memory when an approval fires —
        an event that vanished means the thread moved on without us."""
        if events is None:
            return False
        event = await events.get(int(opportunity.detail.get("event_id", 0)))
        return event is not None
