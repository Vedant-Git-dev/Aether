"""The outbound guard: what may leave the agent at all.

A model can return nothing, whitespace, or a placeholder stand-in for
"nothing" ("[empty]", "<empty>", "None", ...). None of those are words for a
person, so no surface ever sends one, no transcript stores one, and no later
turn recalls one. The guard is applied at the channel layer — the fanout and
every connector's own send — so a caller that forgets cannot leak it.

Kept free of package imports so both the connector layer and the LLM tool
loop can use it without a cycle.
"""

from __future__ import annotations

# The exact strings that mean "there is nothing here", matched
# case-insensitively against the whole trimmed text. A message that merely
# contains one of these as part of a sentence is untouched.
PLACEHOLDERS: frozenset[str] = frozenset(
    {"[empty]", "<empty>", "(empty)", "none", "null", "undefined"}
)


def is_deliverable(text: str | None) -> bool:
    """True when `text` is words a person can read: not absent, not
    whitespace-only, and not a bare placeholder."""
    if text is None:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    return stripped.lower() not in PLACEHOLDERS


def clean_outbound(text: str | None) -> str:
    """The text if it is deliverable, else "". Callers that want the words
    unchanged when they are fine should use `is_deliverable` and keep the
    original; this strips only the empty/placeholder signal to nothing."""
    return text.strip() if is_deliverable(text) else ""
