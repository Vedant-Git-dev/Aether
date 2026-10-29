"""The provider definitions this build ships."""

from __future__ import annotations

from .github import GITHUB_PROVIDER
from .google import GOOGLE_PROVIDER
from .linkedin import LINKEDIN_PROVIDER
from .types import ProviderDefinition

PROVIDERS: list[ProviderDefinition] = [GOOGLE_PROVIDER, GITHUB_PROVIDER, LINKEDIN_PROVIDER]
