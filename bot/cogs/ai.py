import discord
from discord.ext import commands
import asyncio
from utils.logging import logger

# Example stub — replace this with real LLM logic later
async def run_llm(prompt: str, history: list) -> str:
    """Simulated LLM response generator (replace with real call)."""
    await asyncio.sleep(1)  # Simulate latency
    return f"[RoboDoze AI] You said: '{prompt}' — I remember: {len(history)} messages."


class AI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.memory = {}  # {guild_id: [prompt history list]}

    def get_history(self, guild_id: int) -> list:
        return self.memory.setdefault(guild_id, [])

    @commands.command(name="ask", help="Talk to RoboDoze's AI brain")
    async def ask(self, ctx, *, prompt: str):
        history = self.get_history(ctx.guild.id)
        history.append(prompt)

        try:
            await ctx.typing()
            response = await run_llm(prompt, history)
            await ctx.send(response)
        except Exception as e:
            logger.error(f"AI error: {e}")
            await ctx.send("Something went wrong asking RoboDoze 😢")

    @commands.command(name="resetmemory", help="Forget the conversation so far")
    async def reset_memory(self, ctx):
        self.memory[ctx.guild.id] = []
        await ctx.send("🧠 RoboDoze's memory has been wiped.")

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("AI cog loaded and ready.")


def setup(bot):
    bot.add_cog(AI(bot))
