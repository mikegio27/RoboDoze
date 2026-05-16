import asyncio
import collections
import os

from discord.ext import commands
from utils.logging import logger

AI_HISTORY_LIMIT = int(os.getenv("AI_HISTORY_LIMIT", "50"))


async def run_llm(prompt: str, history: list) -> str:
    """Stub — replace with real LLM call."""
    await asyncio.sleep(1)
    return f"[RoboDoze AI] You said: '{prompt}' — I remember: {len(history)} messages."


class AI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.memory: dict[int, collections.deque] = {}

    def get_history(self, guild_id: int) -> collections.deque:
        return self.memory.setdefault(
            guild_id, collections.deque(maxlen=AI_HISTORY_LIMIT)
        )

    @commands.command(name="ask", help="Talk to RoboDoze's AI brain")
    async def ask(self, ctx, *, prompt: str):
        history = self.get_history(ctx.guild.id)
        history.append(prompt)
        try:
            await ctx.typing()
            response = await run_llm(prompt, list(history))
            await ctx.send(response)
        except Exception as e:
            logger.error(f"AI error: {e}")
            await ctx.send("Something went wrong asking RoboDoze.")

    @commands.command(name="resetmemory", help="Forget the conversation so far")
    async def reset_memory(self, ctx):
        self.memory[ctx.guild.id] = collections.deque(maxlen=AI_HISTORY_LIMIT)
        await ctx.send("RoboDoze's memory has been wiped.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AI(bot))
