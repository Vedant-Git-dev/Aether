"""The guided /apps walk — the state machine alone, with fake callbacks.

These pin the transcripts from the design (they are the spec): every
menu, every recipe with its links and its paste ask, the one-at-a-time
token queues, the honest "not yet", the BYOA credential walk, the union
consent for a second Google app — and that each completed walk hands the
right (op, path, value, origin) to the loop's shared applier, origin
"walk" and all. No recorded reply ever carries a value: the save double
takes it, and every transcript asserts names only. The gate's half —
that "walk" really does allow through the real policy — lives in
test_agent_loop.
"""

from __future__ import annotations

from aether.agent.apps_wizard import AppsWizard

_MENU = "1 — add an app    (or \"done\")"
_MENU_NUDGE = "reply 1 to add an app — or \"done\""
_ADD_MENU = (
    "add which one?\n"
    "1 — telegram — talk to me there\n"
    "2 — discord — talk to me there\n"
    "3 — slack — talk to me there\n"
    "4 — gmail — my inbox: read threads, write drafts, sort labels\n"
    "5 — google calendar — my schedule: events and invites\n"
    "6 — github — my code: repos, issues, pull requests\n"
    "7 — notion — my notes: pages and databases\n"
    "8 — something else — any app that speaks MCP"
)
_GITHUB_RECIPE = (
    "github — my code: repos, issues, pull requests.\n"
    "one key needed:\n"
    "1 — open https://github.com/settings/personal-access-tokens/new and "
    "create a token (repo, issues and pull requests permissions are enough)\n"
    "paste the token here — I'll take it from there."
)
_GITHUB_SERVER = {
    "name": "github",
    "transport": {
        "type": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"Authorization": "Bearer $GITHUB_PERSONAL_ACCESS_TOKEN"},
    },
}
_TELEGRAM_RECIPE = (
    "telegram — talk to me there.\n"
    "one token needed:\n"
    "1 — open https://t.me/BotFather and send it /newbot — it hands you a token\n"
    "paste the token here — I'll take it from there."
)
_ADDRESS_ASK = (
    "this one signs in with Google — first, what's this deployment's "
    "public address? (the address you open this chat at, like "
    "https://your-app.onrender.com)"
)
# the loop's executor reply, relayed verbatim — the honest verdict is the
# applier's own words, the walk only adds its head line
_APPLY_GITHUB = (
    "mcp_servers updated — 1 entries now (config.yaml has 0). — github "
    "connected · 24 actions in my vocabulary"
)
_APPLY_TELEGRAM = (
    "messaging.telegram.enabled set to true (config.yaml says false). — "
    "telegram is starting up"
)
_APPLY_SLACK = (
    "messaging.slack.enabled set to true (config.yaml says false). — "
    "slack is starting up"
)
_APPLY_WEATHER = (
    "mcp_servers updated — 1 entries now (config.yaml has 0). — weather "
    "connected · 5 actions in my vocabulary"
)
# what a user rule forcing a hold still answers — relayed verbatim too
_PARK_CARD = (
    "🔒 that one's security-shaped — held for your one-tap approval (#21). "
    "Tap approve and it's done."
)
_GOODBYE = "ok — anytime. /apps to start again."
_WAITING = (
    "still waiting on Google — open the link I sent and approve it there; "
    "say stop to give up."
)
_KEY_NEEDED = (
    "google sign-in needs AETHER_ENCRYPTION_KEY set in .env — a permanent "
    "one, since the tokens are stored encrypted. add it, then /apps again."
)

_UNSET = object()


class FakeApplier:
    """The loop's _apply_config double: records the handoff (origin and
    all), answers canned — the executor's own reply, whatever it is."""

    def __init__(self, reply: str = _APPLY_GITHUB) -> None:
        self.calls: list[tuple[str, str, object, str | None]] = []
        self.reply = reply

    async def __call__(
        self, op: str, path: str, value: object = _UNSET, origin: str | None = None
    ) -> str:
        self.calls.append((op, path, None if value is _UNSET else value, origin))
        return self.reply


class FakeConsent:
    """consent_url(app_key, redirect) — records both, answers canned."""

    def __init__(self, url: str | None = "https://accounts.test/consent?x=1") -> None:
        self.url = url
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, app_key: str, redirect: str) -> str | None:
        self.calls.append((app_key, redirect))
        return self.url


class FakeSave:
    """save_secret(name, value, app) — records the handoff, answers canned.
    The value is kept here so tests can pin stripping; no reply ever
    carries it back — that's the production contract, pinned in
    test_agent_loop."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str, str]] = []  # (name, value, app)

    async def __call__(self, name: str, value: str, app: str) -> bool:
        self.calls.append((name, value, app))
        return self.ok


class FakeOAuthState:
    """oauth_state(provider, scopes) — covers | partial | none."""

    def __init__(self, state: str = "none") -> None:
        self.state = state
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, provider: str, scopes: str) -> str:
        self.calls.append((provider, scopes))
        return self.state


def _wizard(
    *,
    status: str = "📱 your apps: nothing connected yet.",
    env: set[str] | None = None,
    redirect: str = "",
    consent: FakeConsent | None = None,
    oauth_state: FakeOAuthState | None = None,
    save: FakeSave | None = None,
    applier: FakeApplier | None = None,
) -> tuple[AppsWizard, FakeApplier, FakeConsent, FakeSave, set[str]]:
    env = set() if env is None else env
    applier = applier or FakeApplier()
    consent = consent or FakeConsent()
    save = save or FakeSave()

    async def env_names() -> set[str]:
        return set(env)

    async def status_fn() -> str:
        return status

    wizard = AppsWizard(
        apply=applier,
        status=status_fn,
        env_names=env_names,
        save_secret=save,
        consent_url=consent,
        oauth_state=oauth_state or FakeOAuthState(),
        redirect=redirect,
    )
    return wizard, applier, consent, save, env


async def _say(wizard: AppsWizard, *texts: str) -> list[str]:
    return [reply for text in texts if (reply := await wizard.handle(text)) is not None]


# -- the opening and the pick menus ----------------------------------------------


async def test_start_opens_with_the_status_and_the_menu() -> None:
    wizard, applier, _, _, _ = _wizard()
    assert await wizard.start() == f"📱 your apps: nothing connected yet.\n{_MENU}"
    assert wizard.alive
    assert applier.calls == []


async def test_the_add_menu_lists_the_catalog_and_the_generic_option() -> None:
    wizard, _, _, _, _ = _wizard()
    await wizard.start()
    (reply,) = await _say(wizard, "1")
    assert reply == _ADD_MENU
    assert wizard.alive  # the pick is still ahead


async def test_menu_answers_that_are_not_picks_get_the_nudge() -> None:
    wizard, _, _, _, _ = _wizard()
    await wizard.start()
    assert await _say(wizard, "yes", "7") == [_MENU_NUDGE, _MENU_NUDGE]
    assert wizard.alive  # a nudge never ends the walk


# -- a pasteable-key app: the github transcript ----------------------------------


async def test_the_github_walk_pastes_the_key_and_connects_directly() -> None:
    wizard, applier, _, save, _ = _wizard()
    await wizard.start()
    _menu, recipe = await _say(wizard, "1", "6")
    assert _menu == _ADD_MENU
    assert recipe == _GITHUB_RECIPE

    (applied,) = await _say(wizard, "ghp_abc123")
    assert applied == f"stored — connecting github now.\n{_APPLY_GITHUB}"
    assert save.calls == [("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_abc123", "github")]
    # the pick-and-paste was the approval — the connect rides origin "walk"
    assert applier.calls == [("add", "mcp_servers", _GITHUB_SERVER, "walk")]
    assert not wizard.alive


async def test_done_before_the_key_checks_the_env_honestly() -> None:
    wizard, applier, _, save, env = _wizard()
    await wizard.start()
    await _say(wizard, "1", "6")
    (not_yet,) = await _say(wizard, "done")
    assert not_yet == (
        "not yet — GITHUB_PERSONAL_ACCESS_TOKEN isn't set. paste it here, "
        "or add to .env and reply \"done\"."
    )
    assert wizard.alive
    assert save.calls == []

    # .env by hand is still a way in — done checks honestly and connects
    env.add("GITHUB_PERSONAL_ACCESS_TOKEN")
    (applied,) = await _say(wizard, "done")
    assert applied == f"the key's there — connecting github now.\n{_APPLY_GITHUB}"
    assert applier.calls == [("add", "mcp_servers", _GITHUB_SERVER, "walk")]
    assert not wizard.alive


async def test_keys_already_set_connect_without_asking_again() -> None:
    wizard, applier, _, save, _ = _wizard(env={"GITHUB_PERSONAL_ACCESS_TOKEN"})
    await wizard.start()
    _menu, applied = await _say(wizard, "1", "6")
    assert applied == f"the key's already there — connecting github now.\n{_APPLY_GITHUB}"
    assert save.calls == []  # nothing was asked for — it was all there
    assert applier.calls == [("add", "mcp_servers", _GITHUB_SERVER, "walk")]
    assert not wizard.alive


async def test_a_mis_paste_is_reasked_and_a_menu_number_refused() -> None:
    wizard, applier, _, save, _ = _wizard()
    await wizard.start()
    await _say(wizard, "1", "6")
    # two words is a mis-paste, not a changed subject — re-ask, keep waiting
    (reask,) = await _say(wizard, "ghp_one ghp_two")
    assert reask == "just the value itself — paste only the value"
    # a short all-digits paste is a menu number — storing it would shadow
    # a good .env line, so it's refused before anything is stored
    (refused,) = await _say(wizard, "1")
    assert refused == "that's a menu number — paste the key's value itself"
    assert save.calls == []
    assert applier.calls == []
    assert wizard.alive

    # a long numeric value is a value — tokens can legitimately be digits
    (applied,) = await _say(wizard, "1234567890")
    assert applied == f"stored — connecting github now.\n{_APPLY_GITHUB}"
    assert save.calls == [("GITHUB_PERSONAL_ACCESS_TOKEN", "1234567890", "github")]


async def test_surrounding_quotes_are_stripped_before_storing() -> None:
    wizard, _, _, save, _ = _wizard()
    await wizard.start()
    await _say(wizard, "1", "6")
    await _say(wizard, '"ghp_abc123"')
    assert save.calls == [("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_abc123", "github")]


async def test_a_failed_save_falls_back_to_env_honestly() -> None:
    wizard, applier, _, _, _ = _wizard(save=FakeSave(ok=False))
    await wizard.start()
    await _say(wizard, "1", "6")
    (refused,) = await _say(wizard, "ghp_abc123")
    assert refused == (
        "that didn't work — nothing was stored. add GITHUB_PERSONAL_ACCESS_TOKEN "
        "to .env and reply \"done\", or paste it again."
    )
    assert wizard.alive  # the paste is re-askable — nothing was lost
    assert applier.calls == []


async def test_slack_asks_for_its_two_tokens_one_at_a_time() -> None:
    wizard, applier, _, save, _ = _wizard(applier=FakeApplier(reply=_APPLY_SLACK))
    await wizard.start()
    _menu, recipe = await _say(wizard, "1", "3")
    assert recipe == (
        "slack — talk to me there.\n"
        "two tokens needed:\n"
        "1 — open https://api.slack.com/apps and create an app — under OAuth & "
        "Permissions, Install to Workspace copies the bot token (xoxb…)\n"
        "2 — under Basic Info, App-Level Tokens generates the app token "
        "(xapp…) with the connections:write scope\n"
        "paste the bot token (xoxb…) here — I'll take it from there."
    )
    (next_ask,) = await _say(wizard, "xoxb-111")
    assert next_ask == "stored. now the app token (xapp…) — paste it here."
    (applied,) = await _say(wizard, "xapp-222")
    assert applied == f"stored — switching slack on now.\n{_APPLY_SLACK}"
    assert save.calls == [
        ("SLACK_BOT_TOKEN", "xoxb-111", "slack"),
        ("SLACK_APP_TOKEN", "xapp-222", "slack"),
    ]
    assert applier.calls == [("set", "messaging.slack.enabled", True, "walk")]
    assert not wizard.alive


async def test_the_telegram_walk_pastes_and_switches_on_directly() -> None:
    wizard, applier, _, save, _ = _wizard(applier=FakeApplier(reply=_APPLY_TELEGRAM))
    await wizard.start()
    _menu, recipe = await _say(wizard, "1", "1")
    assert recipe == _TELEGRAM_RECIPE
    (applied,) = await _say(wizard, "123:AAbbCC")
    assert applied == f"stored — switching telegram on now.\n{_APPLY_TELEGRAM}"
    assert save.calls == [("TELEGRAM_BOT_TOKEN", "123:AAbbCC", "telegram")]
    assert applier.calls == [("set", "messaging.telegram.enabled", True, "walk")]
    assert not wizard.alive


async def test_a_user_rule_forcing_a_hold_relays_its_card_verbatim() -> None:
    """Origin "walk" is the loop's argument, not a promise: a user rule
    that says park still parks, and the walk relays the card as-is."""
    wizard, _, _, save, _ = _wizard(applier=FakeApplier(reply=_PARK_CARD))
    await wizard.start()
    await _say(wizard, "1", "6")
    (card,) = await _say(wizard, "ghp_abc123")
    assert card == f"stored — connecting github now.\n{_PARK_CARD}"
    assert save.calls == [("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_abc123", "github")]
    assert not wizard.alive


# -- an oauth app: the address, the BYOA credentials, the link -------------------


async def test_the_gmail_walk_collects_the_byoa_credentials_then_the_link() -> None:
    wizard, applier, consent, save, _ = _wizard()
    await wizard.start()
    _menu, address_ask = await _say(wizard, "1", "4")
    assert address_ask == _ADDRESS_ASK
    (recipe,) = await _say(wizard, "https://your-app.onrender.com")
    assert recipe == (
        "gmail — my inbox: read threads, write drafts, sort labels.\n"
        "this one signs in with Google, so there's a one-time setup (every "
        "Google app after is just a link):\n"
        "1 — open https://console.cloud.google.com/apis/library — enable "
        "\"Gmail API\" and \"Gmail MCP API\" in your project\n"
        "2 — open https://console.cloud.google.com/apis/credentials/consent — "
        "create an External consent screen and publish it to Production "
        "(Testing status drops refresh tokens after 7 days; the unverified-app "
        "warning is a one-time click-through for you)\n"
        "3 — open https://console.cloud.google.com/apis/credentials — create "
        "an OAuth client (Web application) with redirect URI "
        "https://your-app.onrender.com/oauth/callback\n"
        "paste the client ID here — I'll take it from there."
    )
    (next_ask,) = await _say(wizard, "id-123.apps.googleusercontent.com")
    assert next_ask == "stored. now the client secret — paste it here."
    (link,) = await _say(wizard, "GOCSPX-xyz")
    assert link == (
        "stored. last step — let me in:\n"
        "open this and approve: https://accounts.test/consent?x=1\n"
        "(I'll take it from there)"
    )
    assert save.calls == [
        ("GOOGLE_CLIENT_ID", "id-123.apps.googleusercontent.com", "gmail"),
        ("GOOGLE_CLIENT_SECRET", "GOCSPX-xyz", "gmail"),
    ]
    # the link was asked for with the redirect the walk collected — the one
    # the state carries, so the exchange replays the exact same URI
    assert consent.calls == [("gmail", "https://your-app.onrender.com/oauth/callback")]
    assert applier.calls == []  # nothing is applied until the callback lands

    # the pause holds: chat can't complete it, only the callback can
    (still,) = await _say(wizard, "done")
    assert still == _WAITING
    assert wizard.alive


async def test_the_ask_is_for_the_first_missing_key_only() -> None:
    """Per provider, not per app — and per key: with the ID already set
    from gmail's walk, only the secret is asked for, by the recipe's own
    wording for that key."""
    wizard, _, _, save, _ = _wizard(
        env={"GOOGLE_CLIENT_ID"},
        redirect="https://aether.example.com/oauth/callback",
    )
    await wizard.start()
    _menu, recipe = await _say(wizard, "1", "4")
    assert recipe.endswith("now the client secret — paste it here.")
    (link,) = await _say(wizard, "GOCSPX-xyz")
    assert link.startswith("stored. last step — let me in:")
    assert save.calls == [("GOOGLE_CLIENT_SECRET", "GOCSPX-xyz", "gmail")]


async def test_a_pinned_public_address_skips_the_address_question() -> None:
    # the loop passes the full callback URI (callback_url(public_url))
    wizard, _, consent, _, _ = _wizard(redirect="https://aether.example.com/oauth/callback")
    await wizard.start()
    replies = await _say(wizard, "1", "4")
    assert len(replies) == 2  # menu → recipe directly, no address ask
    assert "redirect URI https://aether.example.com/oauth/callback" in replies[1]
    assert consent.calls == []  # the link only comes after the pastes


async def test_no_encryption_key_ends_the_walk_with_plain_instructions() -> None:
    wizard, applier, _, save, _ = _wizard(consent=FakeConsent(url=None))
    await wizard.start()
    await _say(wizard, "1", "4", "https://your-app.onrender.com")
    replies = await _say(wizard, "id-123.apps.googleusercontent.com", "GOCSPX-xyz")
    assert replies == ["stored. now the client secret — paste it here.", _KEY_NEEDED]
    assert not wizard.alive
    assert applier.calls == []
    assert wizard.alive is False


async def test_a_covering_sign_in_connects_the_second_app_directly() -> None:
    wizard, applier, consent, save, _ = _wizard(
        redirect="https://aether.example.com/oauth/callback",
        oauth_state=FakeOAuthState(state="covers"),
    )
    await wizard.start()
    _menu, applied = await _say(wizard, "1", "5")
    assert applied == (
        "you already let me in with Google — connecting google calendar now.\n"
        f"{_APPLY_GITHUB}"
    )
    assert applier.calls == [(
        "add", "mcp_servers",
        {
            "name": "calendar",
            "transport": {
                "type": "http",
                "url": "https://calendarmcp.googleapis.com/mcp",
                "headers": {"Authorization": "Bearer $GOOGLE_OAUTH_ACCESS_TOKEN"},
            },
        },
        "walk",
    )]
    assert consent.calls == []  # no new approval — the sign-in covers it
    assert save.calls == []
    assert not wizard.alive


async def test_a_second_google_app_gets_the_union_consent_and_the_enable_note() -> None:
    """gcal after gmail: the credentials are per provider so they aren't
    asked for again, but a gmail-scoped token can't drive the calendar
    MCP — one union consent, and the enable note says what the walk can't
    check (the app's APIs may not be on in the project)."""
    wizard, applier, consent, save, _ = _wizard(
        env={"GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"},
        redirect="https://aether.example.com/oauth/callback",
        oauth_state=FakeOAuthState(state="partial"),
    )
    await wizard.start()
    _menu, link = await _say(wizard, "1", "5")
    assert link == (
        "google calendar — my schedule: events and invites.\n"
        "first enable \"Calendar API\" and \"Calendar MCP API\" in the same "
        "Google Cloud project — https://console.cloud.google.com/apis/library\n"
        "one more approval — google calendar needs a permission your last "
        "sign-in didn't include, and both keep working after this:\n"
        "open this and approve: https://accounts.test/consent?x=1\n"
        "(I'll take it from there)"
    )
    assert consent.calls == [("gcal", "https://aether.example.com/oauth/callback")]
    assert save.calls == []  # the provider's credentials are already there
    assert applier.calls == []  # the callback finishes it, not chat
    assert wizard.alive  # oauth_wait holds


# -- the generic walk: any app that speaks mcp ------------------------------------


async def test_the_generic_stdio_walk_writes_a_by_name_reference() -> None:
    wizard, applier, _, save, _ = _wizard(applier=FakeApplier(reply=_APPLY_WEATHER))
    await wizard.start()
    replies = await _say(
        wizard, "1", "8", "weather", "1", "npx -y mcp-weather", "WEATHER_API_KEY"
    )
    assert replies[1] == "what's the app called? (one word, like weather)"
    assert replies[2] == (
        "how does weather connect?\n"
        "1 — stdio — a command run locally\n"
        "2 — http — a URL I call"
    )
    assert replies[3] == "what's the command? (e.g. npx -y mcp-weather)"
    assert replies[4] == (
        "does weather need a key or token? reply with the key's name "
        "(like WEATHER_API_KEY), or \"none\""
    )
    assert replies[5] == (
        "weather needs a key.\n"
        "paste the WEATHER_API_KEY here — I'll take it from there."
    )
    (applied,) = await _say(wizard, "wx-123")
    assert applied == f"stored — connecting weather now.\n{_APPLY_WEATHER}"
    assert save.calls == [("WEATHER_API_KEY", "wx-123", "weather")]
    assert applier.calls == [(
        "add", "mcp_servers",
        {
            "name": "weather",
            "transport": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "mcp-weather"],
                # the written config references the key by name only
                "env": {"WEATHER_API_KEY": "$WEATHER_API_KEY"},
            },
        },
        "walk",
    )]


async def test_the_generic_http_walk_gets_a_bearer_header() -> None:
    wizard, applier, _, _, _ = _wizard(applier=FakeApplier(reply=_APPLY_WEATHER))
    await wizard.start()
    await _say(wizard, "1", "8", "weather", "http", "https://mcp.example.com/mcp")
    await _say(wizard, "WEATHER_API_KEY", "wx-123")
    assert applier.calls == [(
        "add", "mcp_servers",
        {
            "name": "weather",
            "transport": {
                "type": "http",
                "url": "https://mcp.example.com/mcp",
                "headers": {"Authorization": "Bearer $WEATHER_API_KEY"},
            },
        },
        "walk",
    )]


async def test_a_generic_app_with_no_key_applies_directly() -> None:
    wizard, applier, _, save, _ = _wizard(applier=FakeApplier(reply=_APPLY_WEATHER))
    await wizard.start()
    await _say(wizard, "1", "8", "weather", "1", "npx -y mcp-weather")
    (applied,) = await _say(wizard, "none")
    assert applied == _APPLY_WEATHER  # no head — there was nothing to paste
    assert applier.calls == [(
        "add", "mcp_servers",
        {"name": "weather",
         "transport": {"type": "stdio", "command": "npx", "args": ["-y", "mcp-weather"]}},
        "walk",
    )]
    assert save.calls == []
    assert not wizard.alive


async def test_the_generic_walk_nudges_each_question() -> None:
    wizard, _, _, _, _ = _wizard()
    await wizard.start()
    replies = await _say(
        wizard, "1", "8", "two words", "weather", "maybe", "2", "not a url"
    )
    assert replies[2] == "just the name — one word, like weather"
    assert replies[4] == "I didn't get that — reply with 1-2, or stop"
    assert replies[6] == "just the URL — like https://mcp.example.com/mcp"
    assert wizard.alive

    # a command that doesn't parse is a nudge, not a dead end — the stdio path
    wizard, _, _, _, _ = _wizard()
    await wizard.start()
    await _say(wizard, "1", "8", "weather", "1")
    (reask,) = await _say(wizard, "npx -y \"unterminated")
    assert reask == "that command didn't parse — check the quotes and try again"
    assert wizard.alive


# -- the walk mechanics -------------------------------------------------------------


async def test_cancel_words_end_the_walk_from_any_stage() -> None:
    for goodbye in ("stop", "done", "quit", "never mind"):
        wizard, _, _, _, _ = _wizard()
        await wizard.start()
        (bye,) = await _say(wizard, goodbye)
        assert bye == _GOODBYE
        assert not wizard.alive

    # mid-paste, "stop" is still the way out — and nothing was stored
    wizard, applier, _, save, _ = _wizard()
    await wizard.start()
    await _say(wizard, "1", "6")
    (bye,) = await _say(wizard, "stop")
    assert bye == _GOODBYE
    assert applier.calls == []
    assert save.calls == []
    assert not wizard.alive


async def test_a_sentence_at_a_menu_steps_aside_for_normal_chat() -> None:
    wizard, _, _, _, _ = _wizard()
    await wizard.start()
    await _say(wizard, "1")  # now at the catalog menu
    assert await _say(wizard, "hey can you just add github for me please") == []
    assert not wizard.alive  # nothing is waiting; the message is normal chat


async def test_fifteen_minutes_of_silence_quits_intercepting() -> None:
    wizard, _, _, _, _ = _wizard()
    await wizard.start()
    assert await wizard.handle("1", now=100.0) == _ADD_MENU
    # the clock restarts at every answer — 899 seconds later is still in
    assert await wizard.handle("6", now=999.0) == _GITHUB_RECIPE
    # 900.6 after the last one, the walk is gone and the message is normal chat
    assert await wizard.handle("ghp_abc", now=1899.6) is None
    assert not wizard.alive
