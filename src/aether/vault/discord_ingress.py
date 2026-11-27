"""Capture authenticated owner DMs from Discord's retained message history."""

from __future__ import annotations

import asyncio
import hmac
import logging
from secrets import token_hex

import discord

from ..message_catalog import render
from .errors import VaultProtocolError
from .ingress import Ingress, TransportDelivery
from .runtime import STORAGE_ERRORS

log = logging.getLogger("aether.vault.discord")
_POLL_SECONDS = 10


def _source_id(channel, message) -> str:
    return f"discord:{channel.id}:{message.id}"


def _token(secret: bytes, value: str) -> bytes:
    return hmac.digest(secret, value.encode(), "sha256")


async def _capture(channel, ingress, message, secret):
    source_id = _source_id(channel, message)
    attachments = [{"filename": item.filename, "length": item.size} for item in message.attachments]
    if any(type(item["length"]) is not int or item["length"] <= 0 for item in attachments):
        raise VaultProtocolError("transient")

    async def body():
        for item in message.attachments:
            data = await item.read()
            if len(data) != item.size:
                raise VaultProtocolError("transient")
            yield data

    async def authenticate():
        return TransportDelivery(
            _token(secret, source_id),
            {
                "source": "discord",
                "source_id": source_id,
                "sender": {
                    "platform": "discord",
                    "handle": getattr(message.author, "name", None) or str(message.author.id),
                    "provider_id": str(message.author.id),
                },
                "chat_ref": str(channel.id),
                "message_id": str(message.id),
                "text": message.content,
                "occurred_at": message.created_at.isoformat() if message.created_at else None,
                "attachments": attachments,
            },
            sum(item["length"] for item in attachments),
        )

    receipt = await ingress.accept(body(), authenticate)

    async def send():
        await channel.send(render("vault.status.locked"), nonce=token_hex(12), enforce_nonce=True)

    try:
        await ingress.notice(receipt, _token(secret, f"discord:chat:{channel.id}"), send)
    except (discord.HTTPException, TimeoutError):
        log.warning("Discord lock notice delivery could not be confirmed.")


async def capture_history(channel, ingress: Ingress, owner: int, secret: bytes, after=None):
    def belongs_to_owner(message):
        return message.author.id == owner and not message.author.bot

    if after is None:
        # Recover the checkpoint from encrypted receipts and provider history.
        # This scan holds at most a provider page and reads no attachment bytes.
        async for message in channel.history(limit=None, oldest_first=False):
            if belongs_to_owner(message) and await ingress.received(
                _token(secret, _source_id(channel, message))
            ):
                after = discord.Object(id=message.id)
                break

    async for message in channel.history(limit=None, after=after, oldest_first=True):
        if belongs_to_owner(message):
            await _capture(channel, ingress, message, secret)
        after = discord.Object(id=message.id)
    return after


async def _watch_owner(client, ingress: Ingress, owner: int, secret: bytes) -> None:
    channel = None
    checkpoint = None
    while True:
        try:
            if channel is None:
                channel = await client.create_dm(discord.Object(id=owner))
            checkpoint = await capture_history(channel, ingress, owner, secret, checkpoint)
        except discord.NotFound:
            channel = None
            checkpoint = None
            log.warning(
                "A Discord owner conversation is unavailable. Capture coverage is unconfirmed."
            )
        except (discord.DiscordException, VaultProtocolError, *STORAGE_ERRORS):
            log.warning("A Discord owner conversation has incomplete capture. Waiting to retry.")
        await asyncio.sleep(_POLL_SECONDS)


async def watch_discord(
    ingress: Ingress, token: str, owners: frozenset[int], secret: bytes
) -> None:
    if not token or not owners:
        return
    log.warning("Discord restarted. Coverage of messages during the outage is unconfirmed.")
    while True:
        try:
            async with discord.Client(intents=discord.Intents.none()) as client:
                await client.login(token)
                bot_secret = _token(secret, f"discord:bot:{client.user.id}")
                async with asyncio.TaskGroup() as workers:
                    for owner in sorted(owners):
                        workers.create_task(_watch_owner(client, ingress, owner, bot_secret))
        except (discord.DiscordException, *STORAGE_ERRORS):
            log.warning("Discord is unavailable. Waiting to reconnect.")
            await asyncio.sleep(_POLL_SECONDS)
