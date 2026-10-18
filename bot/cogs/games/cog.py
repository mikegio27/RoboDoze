"""rd-games / rd-game: friends' game servers (dozy-lobby), from Discord.

Only works in the guilds in GAMES_GUILD_IDS (`cog_check` covers every command
here); lobby checks the guild header again. A start is watched in the
background and its message edited as the server comes up. Stopping a server
people are playing on asks the invoker to confirm first.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord
from discord.ext import commands

from utils import metrics
from utils.logging import logger

from .client import Caller, Lobby, LobbyError
from .format import (
    GUILD_ONLY,
    STARTING_PHASES,
    action_text,
    address,
    ambiguous_text,
    confirm_text,
    detail_text,
    list_text,
    not_found_text,
    now,
    resolve,
    start_progress,
    start_timeout_text,
    title,
)

DEFAULT_URL = "http://lobby.dozy-lobby.svc.cluster.local:8081"
DEFAULT_GUILDS = "1554708052593348729,385602741101264896"  # DozyNet, HCH
POLL_INTERVAL = 10.0
START_TIMEOUT = 30 * 60.0  # modpacks take a long time on first start
CONFIRM_TIMEOUT = 60.0
NO_PINGS = discord.AllowedMentions.none()  # server and player names are user text
# Lobby codes worth their own metric result; anything else is "error".
_RESULTS = {"host_full", "already_running", "guild_not_allowed", "players_online"}


def guild_ids(raw: str | None) -> frozenset[int]:
    """GAMES_GUILD_IDS: a comma list; unset or empty means DozyNet and HCH."""
    if not raw or not raw.strip():
        raw = DEFAULT_GUILDS
    return frozenset(int(p) for p in raw.replace(" ", "").split(",") if p.isdigit())


class NotInGamingGuild(commands.CheckFailure):
    pass


def _count(command: str, result: str) -> None:
    metrics.games_commands_total.labels(command=command, result=result).inc()


@dataclass
class _Watch:
    task: asyncio.Task
    message: discord.Message


class ConfirmView(discord.ui.View):
    """Stop anyway / keep it running, when people are playing. Only the
    invoker can press; after CONFIRM_TIMEOUT the buttons go grey."""

    def __init__(
        self,
        owner_id: int,
        action: str,
        busy_text: str,
        kept_text: str,
        run: Callable[[], Awaitable[str]],
        on_keep: Callable[[], None] = lambda: None,
        timeout: float = CONFIRM_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.busy_text = busy_text
        self.kept_text = kept_text
        self.run = run
        self.on_keep = on_keep
        self.message: discord.Message | None = None
        self.confirm.label = f"{action} anyway"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the person who asked can answer this one.", ephemeral=True
        )
        return False

    def _done(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        self.stop()

    @discord.ui.button(label="stop anyway", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._done()
        await interaction.response.edit_message(content=self.busy_text, view=self)
        text = await self.run()
        await interaction.edit_original_response(content=text, view=self)

    @discord.ui.button(label="keep it running", style=discord.ButtonStyle.secondary)
    async def keep(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._done()
        self.on_keep()
        await interaction.response.edit_message(content=self.kept_text, view=self)

    async def on_timeout(self) -> None:
        self._done()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Games(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        lobby: Lobby,
        guilds: frozenset[int],
        poll_interval: float = POLL_INTERVAL,
        start_timeout: float = START_TIMEOUT,
    ) -> None:
        self.bot = bot
        self.lobby = lobby
        self.guilds = guilds
        self.poll_interval = poll_interval
        self.start_timeout = start_timeout
        self._starts: dict[str, _Watch] = {}

    async def cog_unload(self) -> None:
        for w in list(self._starts.values()):
            w.task.cancel()
        await self.lobby.close()

    def _prefix(self) -> str:
        p = getattr(self.bot, "command_prefix", None)
        return p if isinstance(p, str) else "rd-"

    async def cog_check(self, ctx: commands.Context) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride]
        if ctx.guild is None or ctx.guild.id not in self.guilds:
            raise NotInGamingGuild(GUILD_ONLY)
        return True

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        command = ctx.command.name if ctx.command else "unknown"
        if isinstance(error, NotInGamingGuild):
            _count(command, "denied")
            await self._reply(ctx, GUILD_ONLY)
        elif isinstance(error, commands.CommandOnCooldown):
            await self._reply(
                ctx, f"Slow down a little: try again in {error.retry_after:.0f} s."
            )
        elif isinstance(error, commands.CommandInvokeError):
            _count(command, "error")
            logger.error(
                f"[{ctx.guild}] games: {command} failed — {error.original!r}",
                exc_info=error.original,
            )
            await self._reply(ctx, "Something went wrong with that one.")

    # --- Commands -------------------------------------------------------------

    @commands.command(name="games", brief="lists the game servers")
    async def games(self, ctx: commands.Context) -> None:
        await self.do_list(ctx)

    @commands.group(
        name="game",
        invoke_without_command=True,
        case_insensitive=True,
        brief="game server details; start/stop/restart <name>",
        usage="<name> | start <name> | stop <name> | restart <name>",
    )
    async def game(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.do_show(ctx, name)

    @game.command(name="start", brief="starts a game server")
    @commands.cooldown(5, 60, commands.BucketType.user)
    async def game_start(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.do_start(ctx, name)

    @game.command(name="stop", brief="stops a game server (saves the world first)")
    @commands.cooldown(5, 60, commands.BucketType.user)
    async def game_stop(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.do_stop(ctx, name, "stop")

    @game.command(name="restart", brief="restarts a game server")
    @commands.cooldown(5, 60, commands.BucketType.user)
    async def game_restart(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.do_stop(ctx, name, "restart")

    # --- Implementation (called directly by the tests) -------------------------

    async def _reply(self, ctx, text: str, **kwargs) -> discord.Message:
        return await ctx.reply(text, allowed_mentions=NO_PINGS, **kwargs)

    @staticmethod
    def _who(ctx) -> Caller:
        return Caller(
            guild_id=ctx.guild.id,
            user_id=ctx.author.id,
            user_name=ctx.author.display_name,
        )

    async def _fail(self, ctx, command: str, e: LobbyError) -> None:
        _count(command, e.code if e.code in _RESULTS else "error")
        logger.info(
            f"[{ctx.guild}] games: {command} → {e.status} {e.code or '-'} {e.detail}"
        )
        await self._reply(ctx, str(e))

    async def _find(self, ctx, query: str, who: Caller, command: str) -> dict | None:
        """The server `query` names, or None after telling the person why not."""
        p = self._prefix()
        if not query.strip():
            _count(command, "usage")
            sub = "" if command == "game" else f"{command} "
            await self._reply(
                ctx, f"Which server? `{p}game {sub}<name>` (`{p}games` lists them)."
            )
            return None
        servers = await self.lobby.servers(who)
        r = resolve(servers, query)
        if r.server is not None:
            return r.server
        if r.candidates:
            _count(command, "ambiguous")
            await self._reply(ctx, ambiguous_text(query, r.candidates))
        else:
            _count(command, "not_found")
            await self._reply(ctx, not_found_text(query, r.suggestions, p))
        return None

    async def do_list(self, ctx) -> None:
        try:
            servers = await self.lobby.servers(self._who(ctx))
        except LobbyError as e:
            await self._fail(ctx, "games", e)
            return
        await self._reply(ctx, list_text(servers, self._prefix()))
        _count("games", "ok")

    async def do_show(self, ctx, query: str) -> None:
        who = self._who(ctx)
        try:
            server = await self._find(ctx, query, who, "game")
            if server is None:
                return
            # The list entry is enough, but a fresh read has the latest players.
            server = await self.lobby.server(server["name"], who)
        except LobbyError as e:
            await self._fail(ctx, "game", e)
            return
        await self._reply(ctx, detail_text(server, now()))
        _count("game", "ok")

    async def do_start(self, ctx, query: str) -> None:
        who = self._who(ctx)
        try:
            server = await self._find(ctx, query, who, "start")
            if server is None:
                return
            name = server["name"]
            if await self._point_at_watch(ctx, server):
                return
            try:
                server = await self.lobby.start(name, who)
            except LobbyError as e:
                if e.code != "already_running":
                    raise
                # Started from the portal or by someone else: show that one instead.
                server = await self.lobby.server(name, who)
                if server.get("phase") == "Running":
                    addr = address(server)
                    _count("start", "already_running")
                    await self._reply(
                        ctx,
                        f"{title(server)} is already up"
                        + (f" — `{addr}`" if addr else "."),
                    )
                    return
                if server.get("phase") not in STARTING_PHASES:
                    raise
        except LobbyError as e:
            await self._fail(ctx, "start", e)
            return
        text, outcome = start_progress(server)
        if outcome is not None:
            _count("start", "ok" if outcome == "up" else outcome)
            await self._reply(ctx, text)
            return
        if await self._point_at_watch(ctx, server):
            return
        msg = await self._reply(ctx, text)
        self._watch(msg, server, who, str(ctx.guild))

    async def _point_at_watch(self, ctx, server: dict) -> bool:
        w = self._starts.get(server["name"])
        if w is None or w.task.done():
            return False
        _count("start", "pending")
        await self._reply(
            ctx, f"{title(server)} is already starting: {w.message.jump_url}"
        )
        return True

    def _watch(
        self, msg: discord.Message, server: dict, who: Caller, guild: str
    ) -> asyncio.Task:
        name = server["name"]
        task = asyncio.create_task(
            self._poll_start(msg, server, who, guild), name=f"games-start-{name}"
        )
        self._starts[name] = _Watch(task, msg)

        def forget(t: asyncio.Task) -> None:
            w = self._starts.get(name)
            if w is not None and w.task is t:
                del self._starts[name]

        task.add_done_callback(forget)
        return task

    async def _poll_start(
        self, msg: discord.Message, server: dict, who: Caller, guild: str
    ) -> None:
        """Poll lobby until the server is up, failed or stopped (or we give up),
        editing the start message whenever its text changes."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.start_timeout
        last, _ = start_progress(server)
        result = "timeout"
        try:
            while loop.time() < deadline:
                await asyncio.sleep(self.poll_interval)
                try:
                    server = await self.lobby.server(server["name"], who)
                except LobbyError as e:
                    if e.status == 404:
                        result = "failed"
                        await self._edit(msg, f"{title(server)} was deleted.")
                        return
                    logger.debug(f"[{guild}] games: poll {server['name']} — {e}")
                    continue  # lobby restarting or a blip: keep watching
                text, outcome = start_progress(server)
                if text != last:
                    await self._edit(msg, text)
                    last = text
                if outcome is not None:
                    result = "ok" if outcome == "up" else outcome
                    return
            minutes = round(self.start_timeout / 60)
            await self._edit(msg, start_timeout_text(server, minutes, self._prefix()))
        except asyncio.CancelledError:
            result = "cancelled"
            raise
        # A background task has nobody to raise to: log, never die silently.
        except Exception as e:  # noqa: BLE001
            result = "error"
            logger.error(f"[{guild}] games: watching {server['name']} failed — {e!r}")
        finally:
            _count("start", result)
            logger.info(f"[{guild}] games: start {server['name']} → {result}")

    async def _edit(self, msg: discord.Message, text: str) -> None:
        try:
            await msg.edit(content=text, allowed_mentions=NO_PINGS)
        except discord.HTTPException as e:
            logger.debug(f"games: edit failed — {e!r}")  # deleted or no access

    async def do_stop(self, ctx, query: str, action: str) -> ConfirmView | None:
        """Stop or restart. When people are playing, reply with a ConfirmView
        (returned for the tests) whose confirm repeats the call with force."""
        who = self._who(ctx)
        call = self.lobby.stop if action == "stop" else self.lobby.restart
        try:
            server = await self._find(ctx, query, who, action)
            if server is None:
                return None
            try:
                done = await call(server["name"], who, force=False)
            except LobbyError as e:
                if e.code != "players_online":
                    raise
                return await self._confirm(ctx, server, e.players, action, who)
        except LobbyError as e:
            await self._fail(ctx, action, e)
            return None
        _count(action, "ok")
        await self._reply(ctx, action_text(done, action))
        return None

    async def _confirm(
        self, ctx, server: dict, players: list[str], action: str, who: Caller
    ) -> ConfirmView:
        call = self.lobby.stop if action == "stop" else self.lobby.restart

        async def forced() -> str:
            try:
                done = await call(server["name"], who, force=True)
            except LobbyError as e:
                _count(action, e.code if e.code in _RESULTS else "error")
                return str(e)
            _count(action, "forced")
            logger.info(
                f"[{ctx.guild}] games: {action} {server['name']} forced by {who.user_id}"
            )
            return action_text(done, action)

        verb = "Stopping" if action == "stop" else "Restarting"
        view = ConfirmView(
            owner_id=ctx.author.id,
            action=action,
            busy_text=f"{verb} {title(server)}…",
            kept_text=f"OK, {title(server)} keeps running.",
            run=forced,
            on_keep=lambda: _count(action, "cancelled"),
        )
        _count(action, "players_online")
        view.message = await self._reply(
            ctx, confirm_text(server, players, action), view=view
        )
        return view


async def setup(bot: commands.Bot) -> None:
    token = os.getenv("LOBBY_BOT_TOKEN")
    if not token:
        logger.warning("LOBBY_BOT_TOKEN is not set: rd-games is off")
        return
    lobby = Lobby(url=os.getenv("LOBBY_URL", DEFAULT_URL), token=token)
    await bot.add_cog(Games(bot, lobby, guild_ids(os.getenv("GAMES_GUILD_IDS"))))
