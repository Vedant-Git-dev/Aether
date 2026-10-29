"""The guided /apps walk: connect an app without leaving chat.

Bare /apps is just the status — what's connected and what the gate does
with it. /apps add <name> is the front door: a chat surface (telegram or
discord) hands over its recipe — the exact link, the paste ask — then a
direct provider app starts its own sign-in, and anything else gets the
generic MCP walk's standardized questions.

A direct provider app owns its OAuth client and tokens. When the client is
missing, the walk asks for the two values here using the ordinary paste
stage (consumed before ingest, encrypted, never echoed), then serves the
provider's authorization URL. When the callback lands, the walk's last
question is the gate's: what may Aether do with this app — read freely
(the default), everything asks first, or act freely — and the stricter
answers write an authz rule through the same applier, which parks it for
the one-tap because authz is a security root even from a walk.

The wizard stays a pure state machine in the ConfigWizard's discipline:
no I/O of its own, nothing ingested, memory-only state, a 15-minute idle
timeout, cancel words from any stage, and a menu answer that's a word or
two at most (anything longer is a changed subject and the walk steps
aside — except a pasted key at `paste`, which is exactly the one long
answer that's wanted).
"""

from __future__ import annotations

import re
import shlex
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..apps_catalog import AppRecipe, recipe_for
from ..providers.types import ConnectResult

# the loop's shared applier: (op, path[, value][, origin]) -> the reply to relay
ApplyFn = Callable[..., Awaitable[str]]
# which secret names exist right now — names only, never values
EnvNamesFn = Callable[[], Awaitable[set[str]]]
# the /apps status render, loop-side (config + live registry + providers)
StatusFn = Callable[[], Awaitable[str]]
# key name, value, app name -> was it stored? False answers honestly below
SaveSecretFn = Callable[[str, str, str], Awaitable[bool]]
# direct-provider apps: () -> [(app key, display name)]
ProviderAppsFn = Callable[[], Awaitable[list[tuple[str, str]]]]
# direct-provider app key -> the sign-in result (link or setup recipe)
ConnectProviderFn = Callable[[str], Awaitable[ConnectResult]]

_CANCEL_WORDS = frozenset({"stop", "cancel", "done", "quit", "exit", "nevermind", "never mind"})
_IDLE_SECONDS = 15 * 60  # a walk nobody answers stops intercepting messages
_MAX_MENU_TOKEN = 24  # longer than this at a menu is a changed subject
_STRIP_TAIL = "\"'.),!;"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

_GOODBYE = "ok — anytime. /apps to start again."
_NOT_A_VALUE = "just the value itself — paste only the value"
_MENU_NUMBER = "that's a menu number — paste the key's value itself"
_ADD_HINT = "add one with /apps add <name> — like /apps add gmail"


def _slugify(name: str) -> str:
    """A spoken app name as a server-name-safe token ("google calendar" ->
    "google-calendar") — the generic walk builds config with it."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"


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
        provider_apps: ProviderAppsFn | None = None,
        connect_provider: ConnectProviderFn | None = None,
    ) -> None:
        self._apply = apply
        self._status = status
        self._env_names = env_names
        self._save_secret = save_secret
        self._provider_apps = provider_apps
        self._connect_provider = connect_provider
        self._reset()

    def _reset(self) -> None:
        self._stage = "dead"
        self._at = time.monotonic()
        self._recipe: AppRecipe | None = None
        # the keys this walk is still waiting to receive, in paste order
        self._paste_names: list[str] = []
        # the app the permissions question is about, and the authz prefix
        # its answer writes ("<app>__")
        self._permissions_app: tuple[str, str] | None = None
        self._pending_prefix: str | None = None
        # the direct provider app being connected and the client names
        # still waiting to be pasted
        self._provider_app: tuple[str, str] | None = None
        self._provider_paste_names: list[str] = []
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
        """Bare /apps: what's connected, honestly, and how to add one. No
        question is outstanding — the walk ends with the answer."""
        self._reset()
        return f"{await self._status()}\n{_ADD_HINT}"

    async def start_add(self, name: str) -> str:
        """/apps add <name> — resolve the name and walk the connect."""
        self._reset()
        name = name.strip().strip("\"'").lower()
        if not name:
            self._stage = "app_query"
            return "which app? say the name — like gmail"
        return await self._resolve_name(name)

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
        # "done" is the paste stages' .env-fallback check; everywhere else
        # it's a way out like the rest of the cancel words
        done_answers = lowered == "done" and self._stage in ("paste", "provider_setup_paste")
        if lowered in _CANCEL_WORDS and not done_answers:
            self._stage = "dead"
            return _GOODBYE
        stage = self._stage
        if stage == "app_query":
            return await self._answer_app_query(text)
        if stage == "generic_offer":
            return await self._answer_generic_offer(text)
        if stage == "paste":
            return await self._answer_paste(text)
        if stage == "provider_setup_paste":
            return await self._answer_provider_setup_paste(text)
        if stage == "permissions":
            return await self._answer_permissions(text)
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

    async def _resolve_name(self, name: str) -> str:
        """Chat-surface recipe first (those stay native), then a direct
        provider app. Anything else gets the generic MCP walk offered."""
        recipe = recipe_for(name)
        if recipe is not None:
            self._recipe = recipe
            return await self._serve_recipe(recipe)
        provider_app = await self._provider_match(name)
        if provider_app is not None:
            return await self._begin_provider(*provider_app)
        self._generic_name = _slugify(name)  # a later "by hand" keeps the name
        return self._offer_generic(f"I don't know an app called {name}.")

    async def _provider_match(self, name: str) -> tuple[str, str] | None:
        if self._provider_apps is None:
            return None
        wanted = name.strip().lower()
        for key, title in await self._provider_apps():
            if wanted in (key.lower(), title.lower()):
                return key, title
        return None

    async def _begin_provider(self, key: str, title: str) -> str:
        """A direct provider app: the loop owns the OAuth URL and the
        client-setup recipe. If the client is missing, the walk asks for
        the two values here, using the same paste discipline as every
        other key; once they are stored it starts the sign-in again."""
        self._provider_app = (key, title)
        if self._connect_provider is None:
            self._stage = "dead"
            return f"{title} isn't wired on this instance."
        try:
            result = await self._connect_provider(key)
        except Exception as exc:
            self._stage = "dead"
            return getattr(exc, "plain", f"couldn't start the {title} connection.")
        return await self._handle_provider_result(result)

    async def _handle_provider_result(self, result: ConnectResult) -> str:
        app = self._provider_app
        assert app is not None
        key, title = app
        if result.token_required:
            self._stage = "dead"
            lines = [f"{title} needs a token."]
            if result.setup_url:
                lines.append(f"open {result.setup_url}")
            lines.extend(result.setup_steps)
            lines.append("create the token, then paste it in the panel.")
            return "\n".join(lines)
        if result.setup_required:
            names = [name for name in (result.client_id_name, result.client_secret_name) if name]
            missing = [name for name in names if name not in await self._env_names()]
            if not missing:
                # the values arrived through .env or another surface — try
                # the sign-in again now that the client is configured
                try:
                    retry = await self._connect_provider(key)  # type: ignore[misc]
                except Exception as exc:
                    self._stage = "dead"
                    return getattr(exc, "plain", f"couldn't start the {title} connection.")
                if retry.setup_required:
                    self._stage = "dead"
                    return (
                        f"{title}'s client is saved but the provider still "
                        "doesn't see it — check the names and try /apps add again."
                    )
                return await self._handle_provider_result(retry)
            self._provider_paste_names = missing
            self._stage = "provider_setup_paste"
            lines = [f"{title} needs a one-time setup before it can connect."]
            if result.setup_url:
                lines.append(f"open {result.setup_url}")
            for i, step in enumerate(result.setup_steps, 1):
                lines.append(f"{i} — {step}")
            if result.redirect_uri:
                lines.append(f"add this redirect URI exactly: {result.redirect_uri}")
            lines.append(
                f"paste {missing[0]} here — the value is stored encrypted and never shown again."
            )
            return "\n".join(lines)
        self._stage = "dead"
        if result.redirect_url:
            return (
                f"{title} — last step, let me in:\n"
                f"open this and approve: {result.redirect_url}\n"
                "(I'll take it from there)"
            )
        return f"couldn't start the {title} connection — nothing was connected."

    async def _answer_provider_setup_paste(self, text: str) -> str | None:
        app = self._provider_app
        assert app is not None
        key, title = app
        if text.lower() == "done":
            missing = [n for n in self._provider_paste_names if n not in await self._env_names()]
            self._provider_paste_names = missing
            if missing:
                names = " and ".join(missing)
                return (
                    f"not yet — {names} isn't set. paste it here, or add it "
                    'to .env and reply "done".'
                )
            if self._connect_provider is None:
                self._stage = "dead"
                return f"{title} isn't wired on this instance."
            try:
                result = await self._connect_provider(key)
            except Exception as exc:
                self._stage = "dead"
                return getattr(exc, "plain", f"couldn't start the {title} connection.")
            return await self._handle_provider_result(result)
        if " " in text or "\n" in text:
            return _NOT_A_VALUE
        if text.isdigit() and len(text) <= 6:
            return _MENU_NUMBER
        name = self._provider_paste_names[0]
        value = text.strip("\"'`")
        if not value:
            return _NOT_A_VALUE
        stored = await self._save_secret(name, value, title)
        if not stored:
            return (
                "that didn't work — nothing was stored. add "
                f'{name} to .env and reply "done", or paste it again.'
            )
        self._provider_paste_names.pop(0)
        if self._provider_paste_names:
            return (
                f"stored. paste {self._provider_paste_names[0]} here — "
                "the value is stored encrypted and never shown again."
            )
        if self._connect_provider is None:
            self._stage = "dead"
            return f"{title} isn't wired on this instance."
        try:
            result = await self._connect_provider(key)
        except Exception as exc:
            self._stage = "dead"
            return getattr(exc, "plain", f"couldn't start the {title} connection.")
        return await self._handle_provider_result(result)

    def _offer_generic(self, why: str) -> str:
        self._stage = "generic_offer"
        return f"{why} Set it up by hand over MCP?\n1 — yes, walk me through it\n2 — no"

    async def _answer_app_query(self, text: str) -> str | None:
        name = text.strip().strip("\"'").lower()
        if not name or len(name) > 40 or "\n" in name:
            self._stage = "dead"  # a sentence isn't an app name — let it be chat
            return None
        return await self._resolve_name(name)

    async def _answer_generic_offer(self, text: str) -> str | None:
        pick = self._menu_pick(text, 2)
        if pick is None:
            word = text.strip().lower()
            if word in ("yes", "y", "sure", "ok"):
                pick = 1
            elif word in ("no", "n", "nope"):
                pick = 2
            else:
                if self._changed_subject(text):
                    self._stage = "dead"
                    return None
                return self._nudge(2)
        if pick == 2:
            self._stage = "dead"
            return _GOODBYE
        self._stage = "generic_kind"
        name = self._generic_name
        return (
            f"how does {name} connect?\n1 — stdio — a command run locally\n2 — http — a URL I call"
        )

    def enter_permissions(self, slug: str, identity: str, prefix: str | None = None) -> str:
        """The OAuth callback finished — the loop moves the walk (this one
        or a fresh one) to the gate's question: what may Aether do with the
        new app. `prefix` is the authz tool prefix, always "<app>__"."""
        self._reset()
        self._permissions_app = (slug, slug)
        self._pending_prefix = prefix or f"{slug}__"
        self._stage = "permissions"
        who = f" — {identity}" if identity else ""
        return (
            f"{slug} connected{who}.\n"
            f"what may I do with {slug}?\n"
            "1 — read freely, ask before acting (the default)\n"
            "2 — everything asks first\n"
            "3 — act freely"
        )

    async def _answer_permissions(self, text: str) -> str | None:
        pending = self._permissions_app
        if pending is None:
            self._stage = "dead"
            return None
        slug, _name = pending
        pick = self._menu_pick(text, 3)
        if pick is None:
            word = text.strip().lower()
            if word in ("read", "reading", "default"):
                pick = 1
            elif word in ("ask", "everything"):
                pick = 2
            elif word in ("free", "freely", "act"):
                pick = 3
            else:
                if self._changed_subject(text):
                    self._stage = "dead"
                    return None
                return self._nudge(3)
        self._stage = "dead"
        if pick == 1:
            return (
                f"that's the default — {slug} reads run free; sends, "
                "deletes and anything new will ask first."
            )
        # the gate's own vocabulary: one authz rule over the app's tools.
        # authz is a security root — it parks for the one-tap even from a
        # walk, and the applier's own words relay that honestly
        decision = "approve" if pick == 2 else "allow"
        note = (
            f"{slug} connected in chat — everything asks first"
            if pick == 2
            else f"{slug} connected in chat — freed to act"
        )
        return await self._apply(
            "add",
            "authz.rules",
            {
                "tool_pattern": self._pending_prefix or f"{slug}__",
                "decision": decision,
                "note": note,
            },
            origin="walk",
        )

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
                    'add to .env and reply "done".'
                )
            head = f"the key's there — connecting {recipe.name} now."
            return await self._finish_keys(recipe, head)
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
                f'{name} to .env and reply "done", or paste it again.'
            )
        self._paste_names.pop(0)
        if self._paste_names:
            return f"stored. {recipe.ask_for(self._paste_names[0])}"
        return await self._finish_keys(recipe)

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
            lines.append(f"{i} — {step}")
        lines.append(ask)
        return "\n".join(lines)

    async def _finish_keys(self, recipe: AppRecipe, head: str = "") -> str:
        """The last key landed. Anything else connects now — the
        pick-and-paste was the approval."""
        if not head:
            if recipe.kind == "messaging":
                head = f"stored — switching {recipe.name} on now."
            else:
                head = f"stored — connecting {recipe.name} now."
        return await self._apply_recipe(recipe, head=head)

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
            '(like WEATHER_API_KEY), or "none"'
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
        return 'just the key\'s name (like WEATHER_API_KEY) — or "none"'

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
