from discord.ext import commands

class Ping(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def ping(self, ctx):
        """Ping the bot to check if it's alive."""
        await ctx.send("Pong! 🏓")