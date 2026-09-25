"""Two-stage salience scoring — no human ever labels what matters.

Stage 1 is a cheap heuristic pre-gate in code: obvious noise (stop-list
terms, sources flooding past the rate cap) scores 0 and never costs an LLM
call. Stage 2 is an LLM judge turn returning strict JSON. Judge failures
fall back to a conservative default score and never crash the pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import SalienceConfig
from ..llm.json_utils import extract_json
from ..llm.types import Message
from .events import Event, EventStore, event_text

log = logging.getLogger("aether.memory.salience")

STOP_TERMS = (
    "unsubscribe",
    "newsletter",
    "no-reply@",
    "noreply@",
    "donotreply",
    "this is an automated message",
    "promotional",
    "privacy policy update",
    "terms of service update",
)

DEFAULT_JUDGE_SCORE = 5.0


class JudgeError(RuntimeError):
    """The judge could not produce a usable judgment."""


def heuristic_gate(text: str) -> str | None:
    """None when the event is worth judging; otherwise the noise reason."""
    lowered = (text or "").lower()
    for term in STOP_TERMS:
        if term in lowered:
            return f"stop-list:{term}"
    return None


@dataclass(frozen=True)
class Judgment:
    salience: float
    category: str
    actionability: str
    one_line: str


class Judge(Protocol):
    async def judge(self, text: str) -> Judgment: ...


class LLMJudge:
    """Adapts any llm.Provider into a salience judge; always one short turn."""

    PROMPT = (
        "You score how salient one event from a personal feed is to its owner.\n"
        "0-2 noise (marketing, automated, irrelevant). 3-5 minor. "
        "6-8 noteworthy (a real person's message, a commitment, a plan). "
        "9-10 urgent or a personal milestone.\n"
        'Reply with strict JSON only: {"salience": <0-10>, "category": <short>, '
        '"actionability": <short>, "one_line": <one sentence>}'
    )

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def judge(self, text: str) -> Judgment:
        turn = await self._provider.complete(self.PROMPT, [Message.user(text)], tools=[])
        data = extract_json(turn.text)
        try:
            return Judgment(
                salience=float(data["salience"]),
                category=str(data.get("category", "")),
                actionability=str(data.get("actionability", "")),
                one_line=str(data.get("one_line", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise JudgeError(f"judge response missing or invalid fields: {exc}") from exc


class Salience:
    """Scores stored events and marks them memorable above the threshold."""

    def __init__(
        self,
        store: EventStore,
        judge: Judge,
        config: SalienceConfig,
    ) -> None:
        self._store = store
        self._judge = judge
        self._config = config

    async def score_event(self, event_id: int) -> float:
        """Score one event and persist the result. Never raises on judge
        trouble — a failure scores conservatively and the event survives."""
        event = await self._store.get(event_id)
        if event is None:
            log.warning("salience asked to score missing event %s", event_id)
            return 0.0

        text = event_text(event)

        # stage 1 — heuristic pre-gate (no LLM call)
        noise = heuristic_gate(text)
        if noise is None:
            recent = await self._store.count_recent(event.source, hours=1)
            if recent > self._config.rate_cap_per_hour:
                noise = f"rate-capped:{event.source}"
        if noise is not None:
            await self._store.update_salience(event_id, 0.0, memorable=False, category="noise")
            log.debug("event %s pre-gated as noise (%s)", event_id, noise)
            return 0.0

        # stage 2 — LLM judge
        try:
            judgment = await self._judge.judge(text)
            score = _clamp(judgment.salience)
        except Exception:
            log.exception(
                "salience judge failed for event %s — defaulting to %s",
                event_id,
                DEFAULT_JUDGE_SCORE,
            )
            judgment = Judgment(DEFAULT_JUDGE_SCORE, "", "", "")
            score = DEFAULT_JUDGE_SCORE

        memorable = score >= self._config.threshold
        await self._store.update_salience(
            event_id, score, memorable, judgment.category, judgment.actionability
        )
        return score


def _clamp(score: float) -> float:
    return max(0.0, min(10.0, float(score)))
