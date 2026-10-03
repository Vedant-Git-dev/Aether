"""The guided /apps walk: connect an app without leaving chat.

Bare /apps opens the status and the add menu; a known app hands over its
recipe — the exact links, the numbered steps, the paste ask — and
anything else gets the same standardized questions. Keys paste right here:
each paste is consumed before ingest (no model, no memory, no trace) and
lands in the encrypted store, and the reply names only the key's name —
a value is never echoed. The pick-and-paste is the approval: a
walk-driven connect applies directly instead of parking, audited
`allow · builtin:walk-connect`. OAuth apps bring their own client — the
walk collects a provider's credentials once (Google: the client ID and
secret), and every app after is just a consent link; a second app that
needs more permissions gets one union consent that keeps both working,
because a gmail-scoped token cannot drive the calendar MCP.

The wizard stays a pure state machine in the ConfigWizard's discipline:
no I/O of its own, nothing ingested, memory-only state, a 15-minute idle
timeout, cancel words from any stage, and a menu answer that's a word or
two at most (anything longer is a changed subject and the walk steps
aside — except a pasted key at `paste`, which is exactly the one long
answer that's wanted). OAuth recipes pause at `oauth_wait`: the callback
lands out of band and the loop clears the walk itself.
"""

from __future__ import annotations

import re
import shlex
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..apps_catalog import CATALOG, AppRecipe

# the loop's shared applier: (op, path[, value][, origin]) -> the reply to relay
ApplyFn = Callable[..., Awaitable[str]]
# which secret names exist right now — names only, never values
EnvNamesFn = Callable[[], Awaitable[set[str]]]
# the /apps status render, loop-side (config + live registry)
StatusFn = Callable[[], Awaitable[str]]
# app key, redirect URI -> the consent link (None when the deployment can't do OAuth)
ConsentUrlFn = Callable[[str, str], Awaitable[str | None]]
# key name, value, app name -> was it stored? False answers honestly below
SaveSecretFn = Callable[[str, str, str], Awaitable[bool]]
# provider, the scopes an app asks for -> "covers" | "partial" | "none"
OAuthStateFn = Callable[[str, str], Awaitable[str]]

_CANCEL_WORDS = frozenset(
    {"stop", "cancel", "done", "quit", "exit", "nevermind", "never mind"}
)
_IDLE_SECONDS = 15 * 60  # a walk nobody answers stops intercepting messages
_MAX_MENU_TOKEN = 24  # longer than this at a menu is a changed subject
_STRIP_TAIL = "\"'.),!;"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

_GOODBYE = "ok — anytime. /apps to start again."
_MENU_NUDGE = "reply 1 to add an app — or \"done\""
_OAUTH_KEY_NEEDED = (
    "google sign-in needs AETHER_ENCRYPTION_KEY set in .env — a permanent "
    "one, since the tokens are stored encrypted. add it, then /apps again."
)
_WAITING = (
    "still waiting on Google — open the link I sent and approve it there; "
    "say stop to give up."
)
_NOT_A_VALUE = "just the value itself — paste only the value"
_MENU_NUMBER = "that's a menu number — paste the key's value itself"


class AppsWizard:
    """One guided walk, in memory. `handle` returns the walk's reply, or
    None when the message wasn't part of it — the caller drops the walk and
    lets the message be normal chat. `alive` says whether a next answer has
    anywhere to land."""

    def __init__(
        self,
        *,
        apply: ApplyFn,
        status: StatusFn,
        env_names: EnvNamesFn,
        save_secret: SaveSecretFn,
        consent_url: ConsentUrlFn,
        oauth_state: OAuthStateFn,
        redirect: str = "",
    ) -> None:
        self._apply = apply
        self._status = status
        self._env_names = env_names
        self._save_secret = save_secret
        self._consent_url = consent_url
        self._oauth_state = oauth_state
        self._initial_redirect = redirect
        self._stage = "menu"
        self._at = time.monotonic()
        self._recipe: AppRecipe | None = None
        self._redirect = redirect
        # the keys this walk is still waiting to receive, in paste order
        self._paste_names: list[str] = []
        # the generic walk's answers, one at a time
        self._generic_name = ""
        self._generic_kind = ""
        self._generic_server: dict[str, Any] = {}

    @property
    def alive(self) -> bool:
        """False once the walk is over — goodbye, changed subject, stale, or
        done (an applied change ends at its reply; nothing is waiting)."""
        return self._stage != "dead"

    async def start(self) -> str:
        """The opening status + menu — also what a mid-walk /apps returns."""
        self._stage = "menu"
        self._recipe = None
        self._redirect = self._initial_redirect
        self._paste_names = []
        self._generic_name = ""
        self._generic_kind = ""
        self._generic_server = {}
        self._at = time.monotonic()
        return f"{await self._status()}\n1 — add an app    (or \"done\")"

    async def handle(self, text: str, now: float | None = None) -> str | None:
        """One inbound message, answered or not. A str is the walk's reply;
        None means the message should fall through to normal chat."""
        stamp = time.monotonic() if now is None else now
        if stamp - self._at > _IDLE_SECONDS:
            self._stage = "dead"  # stale: gone quietly, never intercept this one
            return None
        self._at = stamp
        text = text.strip()
        lowered = text.lower()
        # "done" is the paste stage's .env-fallback check and the oauth
        # pause's "I clicked approve"; everywhere else it's a way out like
        # the rest of the cancel words
        done_answers = lowered == "done" and self._stage in ("paste", "oauth_wait")
        if lowered in _CANCEL_WORDS and not done_answers:
            self._stage = "dead"
            return _GOODBYE
        stage = self._stage
        if stage == "menu":
            return await self._answer_menu(text)
        if stage == "add_pick":
            return await self._answer_add_pick(text)
        if stage == "public_url":
            return await self._answer_public_url(text)
        if stage == "paste":
            return await self._answer_paste(text)
        if stage == "oauth_wait":
            return self._answer_oauth_wait(text)
        if stage == "generic_name":
            return self._answer_generic_name(text)
        if stage == "generic_kind":
            return await self._answer_generic_kind(text)
        if stage == "generic_target":
            return self._answer_generic_target(text)
        if stage == "generic_env":
            return await self._answer_generic_env(text)
        self._stage = "dead"
        return None

    # -- menu plumbing -----------------------------------------------------------

    @staticmethod
    def _menu_pick(text: str, span: int) -> int | None:
        """The numbered choice in a menu answer, or None when the text isn't
        one of this menu's numbers."""
        token = text.strip().lower().rstrip(_STRIP_TAIL)
        if not token or " " in token or not token.isdigit():
            return None
        choice = int(token)
        return choice if 1 <= choice <= span else None

    @staticmethod
    def _changed_subject(text: str) -> bool:
        """A menu answer is a word or two at most. Anything longer means the
        user started talking about something else — the walk steps aside."""
        stripped = text.strip()
        return len(stripped) > _MAX_MENU_TOKEN or " " in stripped

    def _nudge(self, span: int) -> str:
        return f"I didn't get that — reply with 1-{span}, or stop"

    # -- the stages ---------------------------------------------------------------

    async def _answer_menu(self, text: str) -> str | None:
        if self._menu_pick(text, 1) is None:
            if self._changed_subject(text):
                self._stage = "dead"
                return None
            return _MENU_NUDGE
        self._stage = "add_pick"
        return self._add_menu()

    def _add_menu(self) -> str:
        lines = ["add which one?"]
        for i, recipe in enumerate(CATALOG, 1):
            lines.append(f"{i} — {recipe.name} — {recipe.blurb}")
        lines.append(f"{len(CATALOG) + 1} — something else — any app that speaks MCP")
        return "\n".join(lines)

    async def _answer_add_pick(self, text: str) -> str | None:
        span = len(CATALOG) + 1
        pick = self._menu_pick(text, span)
        if pick is None:
            if self._changed_subject(text):
                self._stage = "dead"
                return None
            return self._nudge(span)
        if pick == span:  # something else — the standardized generic walk
            self._recipe = None
            self._stage = "generic_name"
            return "what's the app called? (one word, like weather)"
        recipe = CATALOG[pick - 1]
        self._recipe = recipe
        if recipe.oauth:
            return await self._route_oauth(recipe)
        return await self._serve_recipe(recipe)

    async def _answer_public_url(self, text: str) -> str | None:
        token = text.strip()
        if not token or " " in token or not token.lower().startswith(("http://", "https://")):
            if self._changed_subject(text):
                self._stage = "dead"
                return None
            return "just the address — like https://your-app.onrender.com"
        # the state riding the consent link carries this, so the code
        # exchange replays the exact redirect the user registered
        self._redirect = token.rstrip("/") + "/oauth/callback"
        recipe = self._recipe
        assert recipe is not None
        return await self._route_oauth(recipe)

    async def _route_oauth(self, recipe: AppRecipe) -> str:
        """The OAuth fork: a sign-in that covers the app connects it now; a
        sign-in that doesn't gets one union consent (both keep working); no
        sign-in yet needs the provider's client first — collected once, in
        chat, and every Google app after is just the link."""
        state = await self._oauth_state(recipe.oauth, recipe.scopes)
        if state == "covers":
            return await self._apply_recipe(
                recipe,
                head=(
                    f"you already let me in with Google — connecting "
                    f"{recipe.name} now."
                ),
            )
        if not self._redirect:
            self._stage = "public_url"
            return (
                "this one signs in with Google — first, what's this "
                "deployment's public address? (the address you open this chat "
                "at, like https://your-app.onrender.com)"
            )
        if state == "partial":
            return await self._serve_consent(recipe, reconsent=True, from_paste=False)
        if await self._missing_names(recipe):
            return await self._serve_recipe(recipe)
        return await self._serve_consent(recipe, reconsent=False, from_paste=False)

    async def _answer_paste(self, text: str) -> str | None:
        """One key's value, straight to the encrypted store. The value dies
        with this call — never kept, never echoed, never ingested; the
        reply names only what was stored."""
        recipe = self._recipe
        assert recipe is not None
        if text.lower() == "done":
            # the .env fallback: nothing pasted, but maybe the user added
            # the line by hand — check honestly and keep waiting if not
            missing = await self._missing_names(recipe)
            self._paste_names = missing
            if missing:
                names = " and ".join(missing)
                word = "it" if len(missing) == 1 else "them"
                verb = "isn't" if len(missing) == 1 else "aren't"
                return (
                    f"not yet — {names} {verb} set. paste {word} here, or "
                    "add to .env and reply \"done\"."
                )
            head = f"the key's there — connecting {recipe.name} now."
            return await self._finish_keys(recipe, head, from_paste=False)
        if " " in text or "\n" in text:
            # a paste is the one long answer that's wanted — a space in it
            # means a mis-paste, not a changed subject. The way out still
            # works from here (cancel words answer above), so the re-ask
            # never traps anyone.
            return _NOT_A_VALUE
        if text.isdigit() and len(text) <= 6:
            return _MENU_NUMBER
        name = self._paste_names[0]
        value = text.strip("\"'`")
        if not value:
            return _NOT_A_VALUE
        stored = await self._save_secret(name, value, recipe.name)
        if not stored:
            return (
                "that didn't work — nothing was stored. add "
                f"{name} to .env and reply \"done\", or paste it again."
            )
        self._paste_names.pop(0)
        if self._paste_names:
            return f"stored. {recipe.ask_for(self._paste_names[0])}"
        return await self._finish_keys(recipe, from_paste=True)

    async def _missing_names(self, recipe: AppRecipe) -> list[str]:
        known = await self._env_names()
        return [n for n in recipe.env_names if n not in known]

    async def _serve_recipe(self, recipe: AppRecipe) -> str:
        # every path that serves a recipe stores it — the catalog picks and
        # the generic walk's built one alike — so the paste stage always
        # has a recipe
        self._recipe = recipe
        missing = await self._missing_names(recipe)
        if not missing:
            found = (
                "the keys are already there"
                if len(recipe.env_names) > 1
                else "the key's already there"
            )
            head = f"{found} — connecting {recipe.name} now."
            return await self._apply_recipe(recipe, head=head)
        self._paste_names = missing
        self._stage = "paste"
        return self._recipe_text(recipe, missing[0])

    def _recipe_text(self, recipe: AppRecipe, first_missing: str) -> str:
        """The recipe in chat: what it gives, where to click, and the ask
        for the first key still missing — links always, values never."""
        ask = recipe.ask_for(first_missing)
        if not recipe.steps:
            # a generic walk's key ask — the recipe is just the key's name
            return f"{recipe.name} needs a key.\n{ask}"
        lines = [f"{recipe.name} — {recipe.blurb}."]
        if recipe.keys_line:
            lines.append(recipe.keys_line)
        for i, step in enumerate(recipe.steps, 1):
            lines.append(f"{i} — {step.replace('{redirect}', self._redirect)}")
        lines.append(ask)
        return "\n".join(lines)

    async def _finish_keys(self, recipe: AppRecipe, head: str = "", *, from_paste: bool) -> str:
        """The last key landed: OAuth recipes go to the consent link, the
        rest connect now — the pick-and-paste was the approval."""
        if recipe.oauth:
            return await self._serve_consent(recipe, reconsent=False, from_paste=from_paste)
        if not head:
            if recipe.kind == "messaging":
                head = f"stored — switching {recipe.name} on now."
            else:
                head = f"stored — connecting {recipe.name} now."
        return await self._apply_recipe(recipe, head=head)

    async def _serve_consent(
        self, recipe: AppRecipe, *, reconsent: bool, from_paste: bool
    ) -> str:
        """The one secret that never passes through chat: the consent link
        is the whole sign-in, and the callback carries the rest."""
        url = await self._consent_url(recipe.key, self._redirect)
        if url is None:
            self._stage = "dead"
            return _OAUTH_KEY_NEEDED
        self._stage = "oauth_wait"
        parts: list[str] = []
        # a second Google app may need its APIs enabled in the same
        # project — the walk can't check, so it says so plainly
        if recipe.enable_note and not from_paste:
            parts.append(recipe.enable_note)
        if reconsent:
            parts.append(
                f"one more approval — {recipe.name} needs a permission your "
                "last sign-in didn't include, and both keep working after this:"
            )
        if not parts:
            parts.append("stored. last step — let me in:" if from_paste else "last step — let me in:")
        else:
            # a consent with a preamble introduces itself — the user may
            # not have seen this app's recipe at all
            parts.insert(0, f"{recipe.name} — {recipe.blurb}.")
        parts.append(f"open this and approve: {url}")
        parts.append("(I'll take it from there)")
        return "\n".join(parts)

    def _answer_oauth_wait(self, text: str) -> str | None:
        if self._changed_subject(text):
            self._stage = "dead"
            return None
        return _WAITING

    async def _apply_recipe(self, recipe: AppRecipe, head: str) -> str:
        """Hand the change to the shared applier and relay its reply. The
        pick-and-paste was the approval, so a walk-driven connect applies
        directly through the executor; a user rule that forces a park
        anyway relays verbatim. The walk ends here: the honest verdict is
        the applier's own words."""
        if recipe.kind == "messaging":
            reply = await self._apply("set", recipe.apply_path, True, origin="walk")
        else:
            reply = await self._apply("add", "mcp_servers", recipe.server, origin="walk")
        self._stage = "dead"
        if head:
            return f"{head}\n{reply}"
        return reply

    # -- the generic walk ------------------------------------------------------------

    def _answer_generic_name(self, text: str) -> str:
        name = text.strip().strip("\"'").lower()
        if not name or " " in name:
            return "just the name — one word, like weather"
        self._generic_name = name
        self._stage = "generic_kind"
        return (
            f"how does {name} connect?\n"
            "1 — stdio — a command run locally\n"
            "2 — http — a URL I call"
        )

    async def _answer_generic_kind(self, text: str) -> str | None:
        words = text.strip().lower().split()
        if words and words[0] in ("stdio", "http"):
            kind = words[0]
        else:
            pick = self._menu_pick(text, 2)
            if pick is None:
                if self._changed_subject(text):
                    self._stage = "dead"
                    return None
                return self._nudge(2)
            kind = ("stdio", "http")[pick - 1]
        self._generic_kind = kind
        self._stage = "generic_target"
        if kind == "stdio":
            return "what's the command? (e.g. npx -y mcp-weather)"
        return "what's the URL?"

    def _answer_generic_target(self, text: str) -> str | None:
        name = self._generic_name
        if self._generic_kind == "stdio":
            try:
                parts = shlex.split(text)
            except ValueError:
                return "that command didn't parse — check the quotes and try again"
            if not parts:
                return "what's the command? (e.g. npx -y mcp-weather)"
            self._generic_server = {
                "name": name,
                "transport": {"type": "stdio", "command": parts[0], "args": parts[1:]},
            }
        else:
            url = text.strip()
            if not url or " " in url:
                return "just the URL — like https://mcp.example.com/mcp"
            self._generic_server = {"name": name, "transport": {"type": "http", "url": url}}
        self._stage = "generic_env"
        return (
            f"does {name} need a key or token? reply with the key's name "
            "(like WEATHER_API_KEY), or \"none\""
        )

    async def _answer_generic_env(self, text: str) -> str | None:
        token = text.strip()
        if token.lower() in ("none", "no", "n"):
            return await self._apply_recipe(self._generic_recipe(()), head="")
        if _ENV_NAME.fullmatch(token):
            return await self._serve_recipe(self._generic_recipe((token,)))
        if self._changed_subject(text):
            self._stage = "dead"
            return None
        return "just the key's name (like WEATHER_API_KEY) — or \"none\""

    def _generic_recipe(self, env_names: tuple[str, ...]) -> AppRecipe:
        """The generic walk's answers as one recipe — the written config
        references the key by name only, exactly like a catalog one."""
        server = dict(self._generic_server)
        transport = dict(server["transport"])
        if env_names and transport["type"] == "stdio":
            transport["env"] = {env_names[0]: f"${env_names[0]}"}
        elif env_names:
            transport["headers"] = {"Authorization": f"Bearer ${env_names[0]}"}
        server["transport"] = transport
        return AppRecipe(
            key="generic",
            name=self._generic_name,
            blurb="",
            kind="mcp",
            env_names=env_names,
            keys_line="one key needed:",
            server=server,
        )
