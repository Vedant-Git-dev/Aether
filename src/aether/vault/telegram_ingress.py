"""Capture Telegram updates in the encrypted queue before confirming a page."""

from __future__ import annotations

import asyncio
import hmac
import logging

from telegram import Bot
from telegram.error import TelegramError

from ..message_catalog import render
from .errors import VaultProtocolError
from .ingress import Ingress, TransportDelivery
from .runtime import STORAGE_ERRORS

log = logging.getLogger("aether.vault.telegram")


async def send_lock_notice(
    bot: Bot, ingress: Ingress, receipt: bytes, conversation: bytes, chat_id: int
) -> None:
    async def send():
        await bot.send_message(chat_id=chat_id, text=render("vault.status.locked"))

    try:
        await ingress.notice(receipt, conversation, send)
    except (TelegramError, TimeoutError):
        log.warning("Telegram lock notice delivery could not be confirmed.")


_FILE_TYPES = {
    "document": "file",
    "video": "video.mp4",
    "video_note": "video.mp4",
    "voice": "voice.ogg",
    "audio": "audio.mp3",
    "animation": "animation.mp4",
    "sticker": "sticker.webp",
    "live_photo": "motion.mp4",
}


def _attachments(message):
    effective = message.effective_attachment
    if message.api_kwargs:
        # An older SDK can leave a new content field in api_kwargs.
        raise VaultProtocolError("transient")
    if effective is None:
        if message.text:
            return []
        raise VaultProtocolError("transient")
    if effective is getattr(message, "photo", None):
        return [(effective[-1], "photo.jpg")]
    for field, filename in _FILE_TYPES.items():
        if effective is getattr(message, field, None):
            attachments = [(effective, getattr(effective, "file_name", None) or filename)]
            if field == "live_photo" and effective.photo:
                attachments.append((effective.photo[-1], "photo.jpg"))
            return attachments
    # Location, polls, and other structured content have no sealed projection yet.
    raise VaultProtocolError("transient")


async def capture_page(
    bot: Bot, ingress: Ingress, owners: frozenset[int], secret: bytes, offset: int | None
) -> int | None:
    updates = await bot.get_updates(
        offset=offset, timeout=20, read_timeout=30, allowed_updates=["message"]
    )
    for update in sorted(updates, key=lambda item: item.update_id):
        message = update.message
        sender = update.effective_user
        if (
            message is None
            or sender is None
            or sender.is_bot
            or message.chat.type != "private"
            or sender.id not in owners
            or message.chat.id != sender.id
        ):
            continue

        attachments = _attachments(message)

        async def accept_message(message, sender, source_id, attachments):
            metadata = []
            for attachment, filename in attachments:
                size = attachment.file_size
                if type(size) is not int or size <= 0:
                    raise VaultProtocolError("transient")
                metadata.append({"filename": filename, "length": size})

            async def body():
                for (attachment, _), item in zip(attachments, metadata, strict=True):
                    file = await attachment.get_file()
                    data = bytes(await file.download_as_bytearray())
                    if len(data) != item["length"]:
                        raise VaultProtocolError("transient")
                    yield data

            async def authenticate():
                return TransportDelivery(
                    hmac.digest(secret, f"telegram:{source_id}".encode(), "sha256"),
                    {
                        "source": "telegram",
                        "source_id": source_id,
                        "kind": "message",
                        "sender": {
                            "platform": "telegram",
                            "handle": f"@{sender.username}"
                            if sender.username
                            else f"id:{sender.id}",
                            "provider_id": str(sender.id),
                        },
                        "chat_ref": str(message.chat.id),
                        "message_id": str(message.message_id),
                        "text": message.text or message.caption or "",
                        "occurred_at": message.date.isoformat() if message.date else None,
                        "attachments": metadata,
                    },
                    sum(item["length"] for item in metadata),
                )

            receipt = await ingress.accept(body(), authenticate)
            conversation = hmac.digest(
                secret, f"telegram:{bot.id}:chat:{message.chat.id}".encode(), "sha256"
            )
            await send_lock_notice(bot, ingress, receipt, conversation, message.chat.id)

        await accept_message(message, sender, f"{bot.id}:{update.update_id}", attachments)

    return max(update.update_id for update in updates) + 1 if updates else None


async def watch_telegram(
    ingress: Ingress, token: str, owners: frozenset[int], secret: bytes
) -> None:
    if not token or not owners:
        return
    # Start without an offset to replay any unconfirmed page. The provider's
    # confirmation is safe only because every earlier receipt already committed.
    log.warning("Telegram restarted. Coverage of messages during the outage is unconfirmed.")
    while True:
        try:
            async with Bot(token) as bot:
                offset = None
                while True:
                    offset = await capture_page(bot, ingress, owners, secret, offset)
        except (TelegramError, OSError, VaultProtocolError, *STORAGE_ERRORS):
            log.warning("Telegram capture incomplete. Waiting to retry from the safe checkpoint.")
            await asyncio.sleep(5)
