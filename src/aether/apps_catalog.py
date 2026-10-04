"""The app catalog: known apps ship as recipes — links, the key asks, and
the server config, all plain data.

A recipe is everything the /apps walk needs to connect an app without the
user going looking for any of it: where to click (exact links, numbered
steps), what to paste (one ask per key, by name only — values are handed
over in chat and consumed before ingest), and the exact mcp_servers entry
to write. Server fragments reference keys by name (`$NAME`), expanded at
open time by the resolver, so the pinned config copy never carries a
secret value either. OAuth recipes (Google) stop short of the tokens: each
user brings their own client — the credentials are per PROVIDER, not per
app, so the walk collects the client ID and secret once and every Google
app after is just a consent link — and the tokens arrive
provider→callback and live encrypted in Postgres.

Anything not in the catalog gets the same standardized walk over the
generic questions — the catalog is convenience, not a gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# the publish-to-production step both Google recipes teach — Testing
# status expires refresh tokens every 7 days, which would silently break
# a connected app a week later
_PUBLISH_STEP = (
    "open https://console.cloud.google.com/apis/credentials/consent — "
    "create an External consent screen and publish it to Production "
    "(Testing status drops refresh tokens after 7 days; the unverified-app "
    "warning is a one-time click-through for you)"
)


@dataclass(frozen=True)
class AppRecipe:
    key: str  # the walk's token for this app (menu order, oauth state)
    name: str  # how it reads in chat
    blurb: str  # what it gives the user
    kind: Literal["mcp", "messaging"]
    env_names: tuple[str, ...] = ()  # the key names the recipe needs
    steps: tuple[str, ...] = ()  # numbered plain-words setup, links first
    keys_line: str = ""  # the lead-in above the steps
    asks: tuple[str, ...] = ()  # one paste ask per key, in env_names order
    stake: str = ""  # "your code" — what connecting it touches
    oauth: str = ""  # provider key when it signs in via OAuth ("google")
    scopes: str = ""  # the consent link's scopes (OAuth recipes)
    enable_note: str = ""  # OAuth: enable this app's APIs when creds predate it
    apply_path: str = ""  # messaging: the toggle this recipe turns on
    server: dict[str, Any] = field(default_factory=dict)  # mcp: the add value, $NAME refs intact

    def ask_for(self, name: str) -> str:
        """The chat line that asks for one key's value — the recipe's own
        wording when it has it, the name-keyed default otherwise."""
        if name in self.env_names:
            idx = self.env_names.index(name)
            if idx < len(self.asks):
                return self.asks[idx]
        return f"paste the {name} here — I'll take it from there."


CATALOG: tuple[AppRecipe, ...] = (
    AppRecipe(
        key="telegram",
        name="telegram",
        blurb="talk to me there",
        kind="messaging",
        apply_path="messaging.telegram.enabled",
        env_names=("TELEGRAM_BOT_TOKEN",),
        keys_line="one token needed:",
        stake="your chats there",
        steps=(
            "open https://t.me/BotFather and send it /newbot — it hands you a token",
        ),
        asks=("paste the token here — I'll take it from there.",),
    ),
    AppRecipe(
        key="discord",
        name="discord",
        blurb="talk to me there",
        kind="messaging",
        apply_path="messaging.discord.enabled",
        env_names=("DISCORD_BOT_TOKEN",),
        keys_line="one token needed:",
        stake="your chats there",
        steps=(
            "open https://discord.com/developers/applications and create an "
            "application — under Bot, Reset Token copies its token",
        ),
        asks=("paste the token here — I'll take it from there.",),
    ),
    AppRecipe(
        key="slack",
        name="slack",
        blurb="talk to me there",
        kind="messaging",
        apply_path="messaging.slack.enabled",
        env_names=("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
        keys_line="two tokens needed:",
        stake="your workspace",
        steps=(
            "open https://api.slack.com/apps and create an app — under OAuth & "
            "Permissions, Install to Workspace copies the bot token (xoxb…)",
            "under Basic Info, App-Level Tokens generates the app token "
            "(xapp…) with the connections:write scope",
        ),
        asks=(
            "paste the bot token (xoxb…) here — I'll take it from there.",
            "now the app token (xapp…) — paste it here.",
        ),
    ),
    AppRecipe(
        key="gmail",
        name="gmail",
        blurb="my inbox: read threads, write drafts, sort labels",
        kind="mcp",
        env_names=("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
        keys_line=(
            "this one signs in with Google, so there's a one-time setup "
            "(every Google app after is just a link):"
        ),
        stake="your mail",
        oauth="google",
        scopes="https://www.googleapis.com/auth/gmail.modify",
        enable_note=(
            "first enable \"Gmail API\" and \"Gmail MCP API\" in the same "
            "Google Cloud project — https://console.cloud.google.com/apis/library"
        ),
        steps=(
            "open https://console.cloud.google.com/apis/library — enable "
            "\"Gmail API\" and \"Gmail MCP API\" in your project",
            _PUBLISH_STEP,
            "open https://console.cloud.google.com/apis/credentials — create "
            "an OAuth client (Web application) with redirect URI {redirect}",
        ),
        asks=(
            "paste the client ID here — I'll take it from there.",
            "now the client secret — paste it here.",
        ),
        server={
            "name": "gmail",
            "transport": {
                "type": "http",
                "url": "https://gmailmcp.googleapis.com/mcp",
                "headers": {"Authorization": "Bearer $GOOGLE_OAUTH_ACCESS_TOKEN"},
            },
        },
    ),
    AppRecipe(
        key="gcal",
        name="google calendar",
        blurb="my schedule: events and invites",
        kind="mcp",
        env_names=("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
        keys_line=(
            "this one signs in with Google, so there's a one-time setup "
            "(every Google app after is just a link):"
        ),
        stake="your calendar",
        oauth="google",
        scopes="https://www.googleapis.com/auth/calendar",
        enable_note=(
            "first enable \"Calendar API\" and \"Calendar MCP API\" in the "
            "same Google Cloud project — https://console.cloud.google.com/apis/library"
        ),
        steps=(
            "open https://console.cloud.google.com/apis/library — enable "
            "\"Calendar API\" and \"Calendar MCP API\" in your project",
            _PUBLISH_STEP,
            "open https://console.cloud.google.com/apis/credentials — create "
            "an OAuth client (Web application) with redirect URI {redirect}",
        ),
        asks=(
            "paste the client ID here — I'll take it from there.",
            "now the client secret — paste it here.",
        ),
        server={
            "name": "calendar",
            "transport": {
                "type": "http",
                "url": "https://calendarmcp.googleapis.com/mcp",
                "headers": {"Authorization": "Bearer $GOOGLE_OAUTH_ACCESS_TOKEN"},
            },
        },
    ),
    AppRecipe(
        key="github",
        name="github",
        blurb="my code: repos, issues, pull requests",
        kind="mcp",
        env_names=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        keys_line="one key needed:",
        stake="your code",
        steps=(
            "open https://github.com/settings/personal-access-tokens/new and "
            "create a token (repo, issues and pull requests permissions are enough)",
        ),
        asks=("paste the token here — I'll take it from there.",),
        server={
            "name": "github",
            "transport": {
                "type": "http",
                "url": "https://api.githubcopilot.com/mcp/",
                "headers": {"Authorization": "Bearer $GITHUB_PERSONAL_ACCESS_TOKEN"},
            },
        },
    ),
    AppRecipe(
        key="notion",
        name="notion",
        blurb="my notes: pages and databases",
        kind="mcp",
        env_names=("NOTION_TOKEN",),
        keys_line="one key needed:",
        stake="your notes",
        steps=(
            "open https://www.notion.so/profile/integrations and create an "
            "integration — its token is the secret",
            "open the pages it may see — the ⋯ menu → Connections → add the "
            "integration",
        ),
        asks=("paste the integration token here — I'll take it from there.",),
        server={
            "name": "notion",
            "transport": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@notionhq/notion-mcp-server"],
                "env": {"NOTION_TOKEN": "$NOTION_TOKEN"},
            },
        },
    ),
)


def recipe_for(key: str) -> AppRecipe | None:
    """The catalog recipe by its walk token, or None (the generic walk's)."""
    return next((r for r in CATALOG if r.key == key), None)
