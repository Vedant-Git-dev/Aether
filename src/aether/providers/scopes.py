"""Plain words for OAuth scopes — the audit's vocabulary.

The scope URLs themselves never reach the panel. This catalog turns the
ones the shipped apps request into a phrase a person can judge ("read your
mail", "write drafts and send mail you ask for"), and an unknown scope into
a neutral sentence rather than a URL or an internal name.

The audit compares what an app requests with what the connection actually
holds. A grant can be narrower than the request (a consent screen the user
trimmed, an older connection made before a scope was added), which hides
some actions without breaking the connection; the app card says so plainly
and offers the reconnect that asks for the rest.
"""

from __future__ import annotations

SCOPE_LABELS: dict[str, str] = {
    # Google
    "https://www.googleapis.com/auth/gmail.readonly": "read your mail",
    "https://www.googleapis.com/auth/gmail.compose": "write drafts and send mail you ask for",
    "https://www.googleapis.com/auth/gmail.modify": "label, archive and trash mail",
    "https://www.googleapis.com/auth/calendar.readonly": "read your calendar",
    "https://www.googleapis.com/auth/calendar.events": "add and change calendar events",
    "https://www.googleapis.com/auth/drive.file": "the files you pick",
    "https://www.googleapis.com/auth/drive.readonly": "read your Drive",
    "https://www.googleapis.com/auth/documents": "read and edit Google Docs",
    # GitHub
    "repo": "your repositories, issues and pull requests",
    "read:user": "your profile",
    "notifications": "your GitHub notifications",
    "read:org": "your organizations",
    "offline_access": "keep the connection alive",
    # LinkedIn
    "openid": "your basic profile",
    "profile": "your profile",
    "email": "your email address",
    "w_member_social": "post on your behalf",
}

_GENERIC = "an extra permission this app asks for"


def describe(scope: str) -> str:
    """One scope in plain words, never the URL."""
    return SCOPE_LABELS.get((scope or "").strip(), _GENERIC)


def describe_many(scopes: tuple[str, ...] | list[str]) -> str:
    """A comma-joined list of plain phrases, de-duplicated in order."""
    phrases: list[str] = []
    for scope in scopes:
        phrase = describe(scope)
        if phrase not in phrases:
            phrases.append(phrase)
    return ", ".join(phrases)
