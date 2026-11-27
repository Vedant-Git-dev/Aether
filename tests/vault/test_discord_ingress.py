import asyncio
from types import SimpleNamespace

import pytest
from test_lease_http import paired as paired_fixture

from aether.vault.discord_ingress import capture_history
from aether.vault.ingress import Ingress

paired = paired_fixture


async def test_discord_restart_recovers_ordered_history_and_refreshes_attachment():
    state = {}
    downloads = []
    owner = SimpleNamespace(id=17, bot=False)
    messages = []

    def add(number, *, fail=False):
        data = f"attachment {number}".encode()

        async def read():
            downloads.append(number)
            if fail:
                raise OSError("expired URL")
            return data

        attachment = SimpleNamespace(filename="example", size=len(data), read=read)
        messages.append(
            SimpleNamespace(
                id=number,
                content=str(number),
                author=owner,
                attachments=[attachment],
                created_at=None,
            )
        )

    async def history(*, limit, after=None, oldest_first=False):
        for item in messages if oldest_first else reversed(messages):
            if after is None or item.id > after.id:
                yield item

    async def received(token):
        return token in state

    async def accept(stream, authenticate):
        delivery = await authenticate()
        body = b"".join([part async for part in stream])
        state[delivery.receipt_token] = (delivery.event["source_id"], body)
        return delivery.receipt_token

    async def notice(*args):
        pass

    ingress = SimpleNamespace(received=received, accept=accept, notice=notice)
    channel = SimpleNamespace(id=31, history=history)
    add(11)
    add(12, fail=True)
    with pytest.raises(OSError, match="expired URL"):
        await capture_history(channel, ingress, owner.id, b"secret")
    messages.pop()
    add(12)
    add(13)
    await capture_history(channel, ingress, owner.id, b"secret")
    await capture_history(channel, ingress, owner.id, b"secret")
    assert list(state.values()) == [
        ("discord:31:11", b"attachment 11"),
        ("discord:31:12", b"attachment 12"),
        ("discord:31:13", b"attachment 13"),
    ]
    assert downloads == [11, 12, 12, 13]


@pytest.mark.parametrize("source", ["telegram", "discord"])
async def test_empty_history_after_a_provider_outage_reports_uncertainty(
    monkeypatch, caplog, source
):
    from contextlib import asynccontextmanager

    from aether.vault import discord_ingress, telegram_ingress

    scanned = asyncio.Event()

    async def empty_history(**kwargs):
        if kwargs["oldest_first"]:
            scanned.set()
        for item in ():
            yield item

    async def login(token):
        assert token == "fixture"

    async def create_dm(owner):
        assert owner.id == 42
        return SimpleNamespace(history=empty_history)

    @asynccontextmanager
    async def client(*args, **kwargs):
        yield SimpleNamespace(login=login, create_dm=create_dm, user=SimpleNamespace(id=7))

    # Both provider views are empty once their formerly retained items are gone.
    if source == "telegram":
        polls = []

        async def updates(**kwargs):
            polls.append(kwargs["offset"])
            if len(polls) == 1:
                return []
            raise asyncio.CancelledError

        @asynccontextmanager
        async def telegram_client(*args):
            yield SimpleNamespace(get_updates=updates)

        monkeypatch.setattr(telegram_ingress, "Bot", telegram_client)
        with pytest.raises(asyncio.CancelledError):
            await telegram_ingress.watch_telegram(
                SimpleNamespace(), "fixture", frozenset({42}), b"stable-key"
            )
        assert polls == [None, None]
    else:
        monkeypatch.setattr(discord_ingress.discord, "Client", client)
        worker = asyncio.create_task(
            discord_ingress.watch_discord(
                SimpleNamespace(), "fixture", frozenset({42}), b"stable-key"
            )
        )
        try:
            await asyncio.wait_for(scanned.wait(), 2)
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
    assert "Coverage of messages during the outage is unconfirmed." in caplog.text


@pytest.mark.integration
async def test_discord_notice_stays_unconfirmed_after_receipt_replay(db, paired):
    _, _, phone = paired
    await db.execute("UPDATE vault_state SET state = 'locked' WHERE vault_id = $1", phone.vault_id)
    attempts = []
    owner = SimpleNamespace(id=42, bot=False)
    unknown = SimpleNamespace(id=99, bot=False)

    async def history(*, limit, after, oldest_first):
        assert oldest_first and after.id == 10
        for number, author in [(11, unknown), (12, owner)]:
            yield SimpleNamespace(
                id=number, author=author, content="Private input", attachments=[], created_at=None
            )

    async def send(text, *, nonce, enforce_nonce):
        attempts.append((text, nonce, enforce_nonce))
        raise TimeoutError

    channel = SimpleNamespace(id=31, history=history, send=send)
    for _ in range(2):
        await capture_history(
            channel, Ingress(db, quota_bytes=8192), owner.id, b"fixture", SimpleNamespace(id=10)
        )

    assert await db.fetchval("SELECT count(*) FROM vault_ingress_receipt") == 1
    assert await db.fetchval("SELECT state FROM vault_ingress_notice") == "unconfirmed"
    [(text, nonce, enforce_nonce)] = attempts
    assert text == "The vault is locked."
    assert nonce and enforce_nonce is True


async def test_discord_owner_discovery_retries_independently(monkeypatch):
    from contextlib import asynccontextmanager

    from aether.vault import discord_ingress

    attempts = []
    healthy = asyncio.Event()
    retried = asyncio.Event()

    async def login(token):
        pass

    async def create_dm(owner):
        attempts.append(owner.id)
        if owner.id == 42:
            if attempts.count(42) == 2:
                retried.set()
            raise OSError("unavailable conversation")
        return SimpleNamespace(id=owner.id)

    async def capture(channel, ingress, owner, secret, after=None):
        assert channel.id == owner == 43
        healthy.set()

    @asynccontextmanager
    async def client(**kwargs):
        yield SimpleNamespace(login=login, create_dm=create_dm, user=SimpleNamespace(id=7))

    monkeypatch.setattr(discord_ingress.discord, "Client", client)
    monkeypatch.setattr(discord_ingress, "capture_history", capture)
    monkeypatch.setattr(discord_ingress, "_POLL_SECONDS", 0.01, raising=False)
    worker = asyncio.create_task(
        discord_ingress.watch_discord(
            SimpleNamespace(),
            "fixture-credential",
            frozenset({42, 43}),
            b"stable-key",
        )
    )
    try:
        await asyncio.wait_for(healthy.wait(), 2)
        await asyncio.wait_for(retried.wait(), 2)
        assert attempts.count(43) == 1
    finally:
        worker.cancel()
        results = await asyncio.gather(worker, return_exceptions=True)
        if not isinstance(results[0], asyncio.CancelledError):
            raise results[0]


@pytest.mark.integration
async def test_bot_token_rotation_keeps_discord_checkpoint_and_unconfirmed_notice(
    db, paired, monkeypatch
):
    from contextlib import asynccontextmanager

    from aether.vault import discord_ingress

    _, _, phone = paired
    await db.execute("UPDATE vault_state SET state = 'locked' WHERE vault_id = $1", phone.vault_id)
    account = SimpleNamespace(id=7)
    owner = SimpleNamespace(id=42, bot=False, name="fixture_owner")
    checkpoints = []
    send_attempts = []
    credentials = []
    finished = asyncio.Event()

    async def history(*, limit, after=None, oldest_first=False):
        numbers = range(11, 11 + len(credentials))
        if oldest_first:
            checkpoints.append(after.id if after else None)
        for number in numbers if oldest_first else reversed(numbers):
            if after is None or number > after.id:
                yield SimpleNamespace(
                    id=number,
                    author=owner,
                    content="Private input",
                    attachments=[],
                    created_at=None,
                )

    async def send(text, **kwargs):
        send_attempts.append(text)
        raise TimeoutError

    async def login(token):
        credentials.append(token)

    async def create_dm(owner):
        return SimpleNamespace(id=31, history=history, send=send)

    @asynccontextmanager
    async def client(**kwargs):
        yield SimpleNamespace(login=login, create_dm=create_dm, user=account)

    async def capture(*args):
        checkpoint = await capture_history(*args)
        finished.set()
        return checkpoint

    monkeypatch.setattr(discord_ingress.discord, "Client", client)
    monkeypatch.setattr(discord_ingress, "capture_history", capture)
    for token in ("first-credential", "replacement-credential"):
        finished.clear()
        worker = asyncio.create_task(
            discord_ingress.watch_discord(
                Ingress(db, quota_bytes=8192),
                token,
                frozenset({42}),
                b"stable-installation-key",
            )
        )
        try:
            await asyncio.wait_for(finished.wait(), 3)
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    assert credentials == ["first-credential", "replacement-credential"]
    assert checkpoints == [None, 11]
    assert send_attempts == ["The vault is locked."]
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_receipt") == 2
    assert await db.fetchval("SELECT state FROM vault_ingress_notice") == "unconfirmed"
