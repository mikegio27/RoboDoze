"""rd-ask: questions for dozai (the homelab LLM service), answered in Discord.

`rd-ask <question>` answers with a reply. Replying to an answer asks a
follow-up: the reply chain back to the rd-ask message (up to MAX_CHAIN
messages) is sent as the conversation. Nothing is stored, here or in dozai.
Images attached to the question (or a follow-up) go along.
"""

from __future__ import annotations

import base64
import os
import time

import discord
from discord.ext import commands

from utils import metrics
from utils.logging import logger

from .client import AskError, Dozai
from .format import (
    MAX_CHAIN,
    Turn,
    answer_chunks,
    strip_command,
    strip_footer,
    to_messages,
)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 4
NO_PINGS = discord.AllowedMentions.none()  # model output must never @everyone


class Ask(commands.Cog):
    def __init__(self, bot: commands.Bot, dozai: Dozai) -> None:
        self.bot = bot
        self.dozai = dozai

    async def cog_unload(self) -> None:
        await self.dozai.close()

    def _prefix(self) -> str:
        p = self.bot.command_prefix
        return p if isinstance(p, str) else "rd-"

    @commands.command(
        name="ask",
        description="asks dozai (the homelab AI); reply to the answer to follow up",
    )
    @commands.cooldown(5, 60, commands.BucketType.user)
    async def ask(self, ctx: commands.Context, *, question: str = "") -> None:
        turn = await self._turn(
            ctx.message, strip_command(ctx.message.content, self._prefix())
        )
        if not turn.text and not turn.images:
            await ctx.reply(
                f"Ask something: `{self._prefix()}ask what is DNS?`",
                allowed_mentions=NO_PINGS,
            )
            return
        await self._answer(ctx.message, [turn])

    @ask.error
    async def ask_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.reply(
                f"Slow down a little: try again in {error.retry_after:.0f} s.",
                allowed_mentions=NO_PINGS,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """A reply to one of our answers is a follow-up."""
        if (
            message.author.bot
            or not message.reference
            or message.content.startswith(self._prefix())
        ):
            return
        parent = await self._resolve(message)
        if parent is None or not self.bot.user or parent.author.id != self.bot.user.id:
            return
        chain = await self._chain(parent)
        if chain is None:
            return  # a reply to some other bot message (music etc.)
        chain.append(await self._turn(message, message.content.strip()))
        await self._answer(message, chain)

    async def _resolve(self, message: discord.Message) -> discord.Message | None:
        ref = message.reference
        if ref is None or ref.message_id is None:
            return None
        if isinstance(ref.resolved, discord.Message):
            return ref.resolved
        try:
            return await message.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return None

    async def _chain(self, answer: discord.Message) -> list[Turn] | None:
        """Walk up from one of our messages to the rd-ask question; None if the
        chain doesn't start with rd-ask (not an ask conversation)."""
        turns: list[Turn] = []
        msg: discord.Message | None = answer
        me = self.bot.user
        for _ in range(MAX_CHAIN):
            if msg is None:
                return None
            if me and msg.author.id == me.id:
                turns.append(Turn("assistant", strip_footer(msg.content)))
            else:
                text = msg.content.strip()
                is_root = text.lower().startswith(f"{self._prefix()}ask")
                turns.append(
                    await self._turn(
                        msg, strip_command(text, self._prefix()) if is_root else text
                    )
                )
                if is_root:
                    turns.reverse()
                    return turns
            msg = await self._resolve(msg)
        return None

    async def _turn(self, message: discord.Message, text: str) -> Turn:
        images = []
        for a in message.attachments:
            if len(images) >= MAX_IMAGES:
                break
            if (a.content_type or "").startswith(
                "image/"
            ) and a.size <= MAX_IMAGE_BYTES:
                try:
                    data = await a.read()
                except discord.HTTPException:
                    continue
                images.append(
                    f"data:{a.content_type.split(';')[0]};base64,{base64.b64encode(data).decode()}"
                )
        return Turn("user", text, images)

    async def _answer(self, message: discord.Message, turns: list[Turn]) -> None:
        start = time.perf_counter()
        status = "ok"
        try:
            async with message.channel.typing():
                answer = await self.dozai.ask(
                    to_messages(turns), f"discord:{message.author.id}"
                )
            chunks = answer_chunks(answer.content, answer.sources, answer.model)
        except AskError as e:
            status = "error"
            chunks = [str(e)]
        # Each chunk replies to the previous one, so a reply to any of them
        # leads back up the chain to the question.
        target = message
        for chunk in chunks:
            target = await target.reply(
                chunk, allowed_mentions=NO_PINGS, suppress_embeds=True
            )
        metrics.asks_total.labels(status=status).inc()
        metrics.ask_duration_seconds.observe(time.perf_counter() - start)
        logger.info(
            f"ask answered status={status} turns={len(turns)} in {time.perf_counter() - start:.1f}s"
        )


async def setup(bot: commands.Bot) -> None:
    token = os.getenv("DOZAI_TOKEN")
    if not token:
        logger.warning("DOZAI_TOKEN is not set: rd-ask is off")
        return
    dozai = Dozai(
        url=os.getenv("DOZAI_URL", "http://dozai.dozai.svc.cluster.local:8080"),
        token=token,
        model=os.getenv("DOZAI_MODEL", "persona:RoboDoze"),
        web=os.getenv("DOZAI_WEB", "true").lower() not in ("0", "false", "no"),
    )
    await bot.add_cog(Ask(bot, dozai))
