"""Discord surface: gateway over WebSocket (no public URL), DMs in, replies
and approval buttons out. DMs only — the bot ignores servers and channels.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from ..authz.approvals import APPROVED, DENIED
from .base import ApprovalDecider, DecisionCallback, InboundHandler, InboundMessage, MessagingConnector

log = logging.getLogger("aether.connectors.discord")


class ApprovalView(discord.ui.View):
    """In-memory approve/deny buttons; they live exactly as long as the bot
    process. Approvals that outlive a restart surface in the web panel."""

    def __init__(self, decide: Any, approval_id: int) -> None:
        super().__init__(timeout=None)
        self._decide = decide
        self._approval_id = approval_id

    @discord.ui.button(label="Approve ✅", style=discord.ButtonStyle.green)
    async def approve(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await self._finish(interaction, APPROVED)

    @discord.ui.button(label="Deny ❌", style=discord.ButtonStyle.red)
    async def deny(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await self._finish(interaction, DENIED)

    async def _finish(self, interaction: Any, decision: str) -> None:
        result = await self._decide(self._approval_id, decision)
        note = "handled." if result is not None else "already decided or expired."
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Approval {decision}: {note}", view=self
        )


def _is_dm(channel: Any) -> bool:
    """Duck-typed so unit tests can pass lightweight fakes."""
    return getattr(channel, "type", None) == discord.ChannelType.private


class DiscordConnector(MessagingConnector):
    name = "discord"

    def __init__(
        self,
        *,
        token: str,
        enabled: bool = True,
        approvals: ApprovalDecider | None = None,
        on_decision: DecisionCallback | None = None,
        on_inbound: InboundHandler | None = None,
        bot_factory: Any = None,
    ) -> None:
        super().__init__(
            enabled=enabled,
            approvals=approvals,
            on_decision=on_decision,
            on_inbound=on_inbound,
        )
        self._token = token
        self._bot_factory = bot_factory or self._default_bot_factory
        self._bot: Any = None
        self._task: asyncio.Task[None] | None = None
        self._channel: Any = None

    @staticmethod
    def _default_bot_factory() -> Any:
        # message content is a privileged intent — the user must flip it on
        # in the Discord developer portal too (noted in the README config table)
        intents = discord.Intents.default()
        intents.message_content = True
        return discord.Bot(intents=intents)

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if not self._enabled or not self._token:
            log.info(
                "discord connector not started (enabled=%s, token set=%s)",
                self._enabled,
                bool(self._token),
            )
            return
        bot = self._bot_factory()
        bot.event(self.on_message)
        self._bot = bot
        self._task = asyncio.create_task(self._run_bot(), name="discord-gateway")
        log.info("discord connector starting")

    async def _run_bot(self) -> None:
        try:
            await self._bot.start(self._token)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("discord gateway stopped — check DISCORD_BOT_TOKEN and the privileged intents")

    async def stop(self) -> None:
        bot, self._bot = self._bot, None
        if bot is not None:
            try:
                await bot.close()
            except Exception:
                log.exception("discord close failed")
        task, self._task = self._task, None
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    # -- inbound -----------------------------------------------------------------

    async def on_message(self, message: Any) -> None:
        if message.author.bot or not _is_dm(message.channel) or not message.content:
            return
        self._channel = message.channel
        handle = message.author.name or str(message.author.id)
        await self._emit_inbound(
            InboundMessage(
                surface="discord",
                handle=handle,
                text=message.content,
                chat_ref=str(message.author.id),
            )
        )

    # -- outbound ------------------------------------------------------------------

    async def send_to_user(self, text: str) -> None:
        if self._channel is None:
            log.info("discord: no DM channel yet")
            return
        await self._channel.send(text)

    async def present_approval(self, approval_id: int, tool_name: str, summary: str) -> None:
        if self._channel is None:
            log.info("discord: no DM channel yet — approval visible in the web panel")
            return
        await self._channel.send(
            content=f"Approval needed: **{tool_name}**\n{summary}",
            view=ApprovalView(self._decide, approval_id),
        )
