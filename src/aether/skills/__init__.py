"""The skill registry — every skill the worker can run, built here.

The worker filters this list by ``config.proactive.skills``, so a name
that isn't registered simply never runs; adding a skill is a new module
plus a line here, and nothing else changes.
"""

from __future__ import annotations

from .base import Skill
from .followups import FollowupsSkill
from .media import MediaSkill
from .research import ResearchSkill

__all__ = [
    "FollowupsSkill",
    "MediaSkill",
    "ResearchSkill",
    "Skill",
    "build_skills",
]


def build_skills() -> list[Skill]:
    """The registry, in a stable order — the example three."""
    return [FollowupsSkill(), ResearchSkill(), MediaSkill()]
