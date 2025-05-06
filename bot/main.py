import discord
from discord.ext import commands
from dotenv import load_dotenv
import os

load_dotenv()
token = os.getenv("DISCORD_TOKEN")
initial_extensions = ['cogs.music', 'cogs.ai']
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True

bot = commands.Bot(command_prefix="!dozy", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} - {bot.user.id}")

@bot.command()
async def ping(ctx):
    """Ping the bot to check if it's alive."""
    await ctx.send("Pong! 🏓")



if __name__ == '__main__':
    for ext in initial_extensions:
        bot.load_extension(ext)
    bot.run(token)