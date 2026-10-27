"""The provider definitions this build ships.

Milestone 0 ships the framework with no provider apps. Milestone 1 adds
Google and Gmail here; later milestones add the remaining providers.
"""

from __future__ import annotations

from .types import ProviderDefinition

PROVIDERS: list[ProviderDefinition] = []
