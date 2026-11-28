from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_lease_http import paired as paired_fixture

from aether.vault import telegram_ingress

paired = paired_fixture


async def test_page_retry_captures_exact_attachment_and_skips_unknown_sender():
    body = b"numbered attachment 17"
    polls = []
    receipts = {}
    downloads = 0

    async def download_as_bytearray():
        nonlocal downloads
        downloads += 1
        if downloads == 1:
            raise OSError("attachment temporarily unavailable")
        return bytearray(body)

    async def get_file():
        return SimpleNamespace(download_as_bytearray=download_as_bytearray)

    def update(number, user):
        sender = SimpleNamespace(id=user, is_bot=False, username=None)
        document = SimpleNamespace(file_name="attachment", file_size=len(body), get_file=get_file)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=user, type="private"),
            from_user=sender,
            message_id=number,
            text=None,
            caption=f"numbered event {number}",
            document=document,
            effective_attachment=document,
            api_kwargs={},
            date=None,
        )
        return SimpleNamespace(update_id=number, message=message, effective_user=sender)

    async def get_updates(*, offset, **kwargs):
        polls.append(offset)
        return [update(16, 999), update(17, 42)] if offset is None else []

    async def accept(stream, authenticate):
        delivery = await authenticate()
        receipts.setdefault(
            delivery.receipt_token, (delivery.event, b"".join([b async for b in stream]))
        )
        return b"queue"

    bot = SimpleNamespace(get_updates=get_updates, id=100)

    async def notice(*args):
        pass

    ingress = SimpleNamespace(accept=accept, notice=notice)
    with pytest.raises(OSError):
        await telegram_ingress.capture_page(bot, ingress, frozenset({42}), b"secret", None)
    assert receipts == {}
    offset = await telegram_ingress.capture_page(bot, ingress, frozenset({42}), b"secret", None)
    assert len(receipts) == 1
    event, received = next(iter(receipts.values()))
    assert event["source_id"] == "100:17"
    assert event["text"] == "numbered event 17"
    assert received == body
    assert (
        await telegram_ingress.capture_page(bot, ingress, frozenset({42}), b"secret", offset)
        is None
    )
    assert polls == [None, None, 18]
    assert downloads == 2


@pytest.mark.integration
async def test_ambiguous_notice_is_unconfirmed_across_restart(db, paired):
    from aether.vault.ingress import Ingress, TransportDelivery
    from aether.vault.telegram_ingress import send_lock_notice

    _, _, phone = paired
    await db.execute("UPDATE vault_state SET state = 'locked' WHERE vault_id = $1", phone.vault_id)
    ingress = Ingress(db, quota_bytes=8192)

    async def authenticate():
        return TransportDelivery(b"a" * 32, {"source": "telegram"}, 0)

    async def body():
        yield b""

    receipt = await ingress.accept(body(), authenticate)
    attempts = []

    async def send_message(**kwargs):
        from telegram.error import TimedOut

        attempts.append(kwargs)
        raise TimedOut()

    bot = SimpleNamespace(send_message=send_message)
    await send_lock_notice(bot, ingress, receipt, b"b" * 32, 42)
    await send_lock_notice(bot, Ingress(db, quota_bytes=8192), receipt, b"b" * 32, 42)
    assert attempts == [{"chat_id": 42, "text": "The vault is locked."}]
    assert await db.fetchval("SELECT state FROM vault_ingress_notice") == "unconfirmed"


@pytest.mark.parametrize("kind", ["location", "live_photo"])
async def test_real_update_preserves_safe_offset_and_live_photo_bytes(kind):
    from telegram import Bot, Update

    from aether.vault.errors import VaultProtocolError

    client = Bot("1234:fixture")
    content = {
        "location": {"latitude": 12.5, "longitude": 23.5},
        "live_photo": {
            "file_id": "motion",
            "file_unique_id": "unique-motion",
            "width": 1,
            "height": 1,
            "duration": 1,
            "file_size": 6,
            "photo": [
                {
                    "file_id": "still",
                    "file_unique_id": "unique-still",
                    "width": 1,
                    "height": 1,
                    "file_size": 5,
                }
            ],
        },
    }
    update = Update.de_json(
        {
            "update_id": 17,
            "message": {
                "message_id": 3,
                "date": 0,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "first_name": "Fixture"},
                kind: content[kind],
            },
        },
        client,
    )
    receipts = []
    offsets = []

    async def get_updates(*, offset, **kwargs):
        offsets.append(offset)
        return [update]

    async def get_file(file_id, **kwargs):
        async def download_as_bytearray():
            return b"motion" if file_id == "motion" else b"still!"

        return SimpleNamespace(download_as_bytearray=download_as_bytearray)

    async def accept(stream, authenticate):
        event = (await authenticate()).event
        body = b"".join([chunk async for chunk in stream])
        receipts.append((event, body))
        return b"receipt"

    async def notice(*args):
        pass

    bot = SimpleNamespace(id=7, get_updates=get_updates, get_file=get_file)
    if kind == "live_photo":
        update.message.live_photo.set_bot(bot)
        update.message.live_photo.photo[-1].set_bot(bot)
    ingress = SimpleNamespace(accept=accept, notice=notice)
    if kind == "location":
        with pytest.raises(VaultProtocolError, match="transient"):
            await telegram_ingress.capture_page(bot, ingress, frozenset({42}), b"fixture", 17)
        assert not receipts
    else:
        with pytest.raises(VaultProtocolError, match="transient"):
            await telegram_ingress.capture_page(bot, ingress, frozenset({42}), b"fixture", 17)
        assert not receipts

        async def get_correct_file(file_id, **kwargs):
            async def download_as_bytearray():
                return b"motion" if file_id == "motion" else b"still"

            return SimpleNamespace(download_as_bytearray=download_as_bytearray)

        bot.get_file = get_correct_file
        assert (
            await telegram_ingress.capture_page(bot, ingress, frozenset({42}), b"fixture", 17) == 18
        )
        [(event, data)] = receipts
        assert [item["length"] for item in event["attachments"]] == [6, 5]
        assert data == b"motionstill"
    assert offsets == ([17] if kind == "location" else [17, 17])


@pytest.mark.integration
async def test_bot_token_rotation_keeps_telegram_receipts_and_unconfirmed_notice(
    db, paired, monkeypatch
):
    import asyncio
    from contextlib import asynccontextmanager

    from aether.vault.ingress import Ingress

    _, _, phone = paired
    await db.execute("UPDATE vault_state SET state = 'locked' WHERE vault_id = $1", phone.vault_id)
    send_attempts = []
    credentials = []

    @asynccontextmanager
    async def bot(token):
        credentials.append(token)
        offsets = []
        owner = SimpleNamespace(id=42, is_bot=False, username="fixture_owner")

        async def get_updates(*, offset, **kwargs):
            offsets.append(offset)
            if len(offsets) > 1:
                raise asyncio.CancelledError
            return [
                SimpleNamespace(
                    update_id=number,
                    effective_user=owner,
                    message=SimpleNamespace(
                        chat=SimpleNamespace(id=42, type="private"),
                        message_id=number,
                        text="fixture",
                        date=None,
                        caption=None,
                        effective_attachment=None,
                        api_kwargs={},
                    ),
                )
                for number in range(1, len(credentials) + 1)
            ]

        async def send_message(**kwargs):
            send_attempts.append(kwargs)
            raise TimeoutError

        yield SimpleNamespace(id=7, get_updates=get_updates, send_message=send_message)

    monkeypatch.setattr(telegram_ingress, "Bot", bot)
    for token in ("first-credential", "replacement-credential"):
        with pytest.raises(asyncio.CancelledError):
            await telegram_ingress.watch_telegram(
                Ingress(db, quota_bytes=8192),
                token,
                frozenset({42}),
                b"stable-installation-key",
            )

    assert credentials == ["first-credential", "replacement-credential"]
    assert len(send_attempts) == 1
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_receipt") == 2
    assert await db.fetchval("SELECT state FROM vault_ingress_notice") == "unconfirmed"
