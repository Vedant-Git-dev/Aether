"""The provider definitions this build ships."""

from __future__ import annotations

from .google import GOOGLE_PROVIDER
from .types import ProviderDefinition

PROVIDERS: list[ProviderDefinition] = [GOOGLE_PROVIDER]
