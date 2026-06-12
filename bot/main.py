import asyncio
import os
import signal
import sys
import time

import discord
from discord.ext import commands
from utils import metrics
from utils.logging import logger
from health import start_health_server

token = os.getenv("DISCORD_TOKEN")
if not token:
    sys.exit("DISCORD_TOKEN environment variable is not set.")

EXTENSIONS = ['cogs.music', 'cogs.ai']

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True


class RoboDoze(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=os.getenv("COMMAND_PREFIX", "!dozy"),
            intents=intents,
        )
        self._health_runner = None

    async def setup_hook(self) -> None:
        self.before_invoke(self._metrics_before_invoke)
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        self._health_runner = await start_health_server(
            self, port=int(os.getenv("HEALTH_PORT", "8080"))
        )

    async def on_ready(self) -> None:
        logger.info(f"Logged in as {self.user.name} ({self.user.id})")

    async def _metrics_before_invoke(self, ctx: commands.Context) -> None:
        ctx._metrics_start = time.perf_counter()

    async def on_command_completion(self, ctx: commands.Context) -> None:
        name = ctx.command.qualified_name if ctx.command else "unknown"
        start = getattr(ctx, "_metrics_start", None)
        if start is not None:
            metrics.command_duration_seconds.labels(command=name).observe(
                time.perf_counter() - start
            )
        metrics.commands_total.labels(command=name, status="success").inc()

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        name = ctx.command.qualified_name if ctx.command else "unknown"
        metrics.commands_total.labels(command=name, status="error").inc()

    async def close(self) -> None:
        for vc in list(self.voice_clients):
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
        if self._health_runner:
            try:
                await self._health_runner.cleanup()
            except Exception:
                pass
        await super().close()


async def main() -> None:
    bot = RoboDoze()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(bot.close()))
    async with bot:
        await bot.start(token)


if __name__ == '__main__':
    asyncio.run(main())
