import asyncio
import collections
import random
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import discord
from discord.ext import commands
from yt_dlp import YoutubeDL

from utils.logging import logger

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ytdl")

FFMPEG_OPTIONS = {
    'before_options': '-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-f s16le -ar 48000 -ac 2 -vn',
}

ytdl_opts = {
    'format': 'bestaudio/best',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'socket_timeout': 15,
}

ytdl = YoutubeDL(ytdl_opts)  # type: ignore[arg-type]


def format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02}m {s:02}s" if h else f"{m:02}m {s:02}s"


class VoiceConnectionError(commands.CommandError):
    pass


class InvalidVoiceChannel(VoiceConnectionError):
    pass


class MusicQueue(asyncio.Queue):
    """asyncio.Queue with safe public methods for inspection and modification."""

    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize)
        # Typed alias for the private deque so subclass methods don't touch _queue.
        self._data: collections.deque = self._queue  # type: ignore[attr-defined]

    def snapshot(self) -> list[Any]:
        return list(self._data)

    def remove_at(self, pos: int) -> Any:
        items = list(self._data)
        removed = items.pop(pos)
        while not self.empty():
            self.get_nowait()
        for item in items:
            self.put_nowait(item)
        return removed

    def remove_last(self) -> Any:
        return self.remove_at(len(self._data) - 1)

    def clear_all(self) -> None:
        while not self.empty():
            self.get_nowait()


class MusicSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester
        self.title = data.get('title')
        self.web_url = data.get('webpage_url')
        self.duration = data.get('duration')

    def __getitem__(self, item: str):
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, download: bool = False):
        to_run = partial(ytdl.extract_info, url=search, download=download)
        try:
            async with asyncio.timeout(30):
                raw = await asyncio.get_running_loop().run_in_executor(_executor, to_run)
        except asyncio.TimeoutError:
            embed = discord.Embed(
                title="",
                description="Song took too long to load, please try again.",
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)
            return None

        if raw is None:
            await ctx.send(embed=discord.Embed(title="", description="No results found.", color=discord.Color.red()))
            return None

        data: dict[str, Any] = raw  # type: ignore[assignment]
        if 'entries' in data:
            data = data['entries'][0]

        embed = discord.Embed(
            title="",
            description=f"Queued [{data['title']}]({data['webpage_url']}) [{ctx.author.mention}]",
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

        if download:
            source = ytdl.prepare_filename(data)  # type: ignore[arg-type]
            return cls(discord.FFmpegPCMAudio(source, **FFMPEG_OPTIONS), data=data, requester=ctx.author)
        return {'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title']}

    @classmethod
    async def regather_stream(cls, data: dict[str, Any], *, requester=None) -> "MusicSource":
        req = requester or data['requester']
        to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=False)
        async with asyncio.timeout(30):
            raw = await asyncio.get_running_loop().run_in_executor(_executor, to_run)
        if raw is None:
            raise ValueError(f"No stream data returned for {data['webpage_url']}")
        info: dict[str, Any] = raw  # type: ignore[assignment]
        return cls(discord.FFmpegPCMAudio(info['url'], **FFMPEG_OPTIONS), data=info, requester=req)


class MusicPlayer:
    __slots__ = (
        'bot', '_guild', '_channel', '_cog',
        'queue', 'next', 'current', 'np', 'volume',
        '_loop', '_prefetch_task', '_prefetched',
    )

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog
        self._loop = asyncio.get_event_loop()

        self.queue = MusicQueue()
        self.next = asyncio.Event()

        self.np = None
        self.volume = 1.0
        self.current = None
        self._prefetch_task = None
        self._prefetched = None

        asyncio.ensure_future(self.player_loop())

    def _after_play(self, error: Exception | None) -> None:
        # Since 2.7.0, FFmpeg errors are properly delivered here instead of being
        # silently discarded. Log them before signalling the next track.
        if error:
            logger.error(f"Playback error in {self._guild}: {error}")
        self._loop.call_soon_threadsafe(self.next.set)

    def _cancel_prefetch(self) -> None:
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        if self._prefetched:
            try:
                self._prefetched.cleanup()
            except Exception:
                pass
        self._prefetch_task = None
        self._prefetched = None

    async def _prefetch_next(self, data: dict[str, Any]) -> None:
        try:
            source = await MusicSource.regather_stream(data)
            self._prefetched = source
            logger.debug(f"Prefetch complete: {data.get('title')}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Prefetch failed: {e}")
            self._prefetched = None
        finally:
            self._prefetch_task = None

    async def _regather_with_retry(self, data: dict):
        try:
            return await MusicSource.regather_stream(data)
        except Exception as e:
            await self._channel.send(f'There was an error processing your song: {e}')
            return None

    async def player_loop(self) -> None:
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                async with asyncio.timeout(300):
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                self.destroy(self._guild)
                return

            if not isinstance(source, MusicSource):
                prefetched_url = self._prefetched.web_url if self._prefetched else None
                queued_url = source.get('webpage_url')

                if self._prefetched is not None and prefetched_url == queued_url:
                    source = self._prefetched
                    self._prefetched = None
                    self._prefetch_task = None
                    logger.debug("Prefetch hit: using pre-fetched stream")
                elif self._prefetch_task and not self._prefetch_task.done():
                    try:
                        async with asyncio.timeout(30):
                            await self._prefetch_task
                        if self._prefetched and self._prefetched.web_url == queued_url:
                            source = self._prefetched
                            self._prefetched = None
                            self._prefetch_task = None
                            logger.debug("Prefetch hit (awaited)")
                        else:
                            self._cancel_prefetch()
                            source = await self._regather_with_retry(source)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        self._cancel_prefetch()
                        source = await self._regather_with_retry(source)
                else:
                    source = await self._regather_with_retry(source)

            if source is None:
                continue

            source.volume = self.volume
            self.current = source

            self._guild.voice_client.play(source, after=self._after_play)

            embed = discord.Embed(
                title="Now playing",
                description=f"[{source.title}]({source.web_url}) [{source.requester.mention}]",
                color=discord.Color.green(),
            )
            self.np = await self._channel.send(embed=embed)

            snapshot = self.queue.snapshot()
            if snapshot and isinstance(snapshot[0], dict):
                self._prefetch_task = asyncio.ensure_future(self._prefetch_next(snapshot[0]))
                logger.debug(f"Prefetch started: {snapshot[0].get('title')}")

            await self.next.wait()
            source.cleanup()
            self.current = None

    def destroy(self, guild) -> asyncio.Task:
        self._cancel_prefetch()
        return self._loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    __slots__ = ('bot', 'players')

    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild) -> None:
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass
        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('This command cannot be used in private messages.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Error connecting to voice channel. Please make sure you are in one.')
        else:
            logger.warning(f"Ignoring exception in command {ctx.command}: {error}")

    def get_player(self, ctx) -> MusicPlayer:
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player
        return player

    @commands.command(name='join', aliases=['connect', 'j'], description="connects to voice")
    async def connect_(self, ctx, *, channel: discord.VoiceChannel = None):
        """Connect to voice."""
        if not channel:
            try:
                channel = ctx.author.voice.channel
            except AttributeError:
                embed = discord.Embed(
                    title="",
                    description="No channel to join. Please call the join command from a voice channel.",
                    color=discord.Color.green(),
                )
                await ctx.send(embed=embed)
                raise InvalidVoiceChannel('No channel to join.')

        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Moving to channel: <{channel}> timed out.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Connecting to channel: <{channel}> timed out.')

        if random.randint(0, 1) == 0:
            await ctx.message.add_reaction('👍')
        await ctx.send(f'**Joined `{channel}`**')

    @commands.command(name='play', aliases=['sing', 'p'], description="streams music")
    async def play_(self, ctx, *, search: str):
        """Request a song and add it to the queue."""
        await ctx.typing()

        vc = ctx.voice_client
        if not vc:
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)
        source = await MusicSource.create_source(ctx, search, download=False)
        if source is not None:
            await player.queue.put(source)

    @commands.command(name='pause', description="pauses music")
    async def pause_(self, ctx):
        """Pause the currently playing song."""
        vc = ctx.voice_client
        if not vc or not vc.is_playing():
            embed = discord.Embed(title="", description="I am currently not playing anything", color=discord.Color.green())
            return await ctx.send(embed=embed)
        elif vc.is_paused():
            return
        vc.pause()
        await ctx.send("Paused ⏸️")

    @commands.command(name='resume', description="resumes music")
    async def resume_(self, ctx):
        """Resume the currently paused song."""
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)
        elif not vc.is_paused():
            return
        vc.resume()
        await ctx.send("Resuming ⏯️")

    @commands.command(name='skip', description="skips to next song in queue")
    async def skip_(self, ctx):
        """Skip the song."""
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)
        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return
        vc.stop()

    @commands.command(name='remove', aliases=['rm', 'rem'], description="removes specified song from queue")
    async def remove_(self, ctx, pos: int | None = None):
        """Removes specified song from queue."""
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)
        if player.queue.empty():
            return await ctx.send("The queue is empty.")

        try:
            if pos is None:
                s = player.queue.remove_last()
            else:
                s = player.queue.remove_at(pos - 1)
            title = s['title'] if isinstance(s, dict) else s.title
            label = "last track" if pos is None else f"track {pos}"
            await ctx.send(f"Removed {label}: {title}")
        except (IndexError, KeyError):
            await ctx.send("Could not find a track at that position.")

    @commands.command(name='clear', aliases=['clr', 'cl', 'cr'], description="clears entire queue")
    async def clear_(self, ctx):
        """Deletes entire queue of upcoming songs."""
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)
        player.queue.clear_all()
        await ctx.send('**Cleared**')

    @commands.command(name='queue', aliases=['q', 'playlist', 'que'], description="shows the queue")
    async def queue_info(self, ctx):
        """Retrieve a basic queue of upcoming songs."""
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)
        current = player.current

        if not current:
            embed = discord.Embed(title="", description="I am currently not playing anything", color=discord.Color.green())
            return await ctx.send(embed=embed)

        upcoming = player.queue.snapshot()
        duration = format_duration(current.duration)
        fmt = '\n'.join(
            f"`{i + 1}.` [{s['title']}]({s['webpage_url']}) | ` Requested by: {s['requester']}`\n"
            for i, s in enumerate(upcoming)
        )
        fmt = (
            f"\n__Now Playing__:\n[{current.title}]({current.web_url}) | "
            f"` Duration {duration} Requested by: {current.requester}`\n\n__Up Next:__\n"
            + fmt
            + f"\n**{len(upcoming)} songs in queue**"
        )
        embed = discord.Embed(title=f'Queue for {ctx.guild.name}', description=fmt, color=discord.Color.green())
        embed.set_footer(text=f"{ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name='np', aliases=['song', 'current', 'currentsong', 'playing'], description="shows the current playing song")
    async def now_playing_(self, ctx):
        """Display information about the currently playing song."""
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)
        current = player.current
        if not current:
            embed = discord.Embed(title="", description="I am currently not playing anything", color=discord.Color.green())
            return await ctx.send(embed=embed)

        duration = format_duration(current.duration)
        embed = discord.Embed(
            title="",
            description=f"[{current.title}]({current.web_url}) [{current.requester.mention}] | `{duration}`",
            color=discord.Color.green(),
        )
        embed.set_author(icon_url=self.bot.user.display_avatar.url, name="Now Playing 🎶")
        await ctx.send(embed=embed)

    @commands.command(name='volume', aliases=['vol', 'v'], description="changes volume")
    async def change_volume(self, ctx, *, vol: float | None = None):
        """Change the player volume (1-100)."""
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I am not currently connected to voice", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)

        if vol is None:
            embed = discord.Embed(title="", description=f"🔊 **{int(player.volume * 100)}%**", color=discord.Color.green())
            return await ctx.send(embed=embed)

        if not 0 < vol < 101:
            embed = discord.Embed(title="", description="Please enter a value between 1 and 100", color=discord.Color.green())
            return await ctx.send(embed=embed)

        if vc.source:
            vc.source.volume = vol / 100
        player.volume = vol / 100
        embed = discord.Embed(
            title="",
            description=f'**`{ctx.author}`** set the volume to **{vol}%**',
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    @commands.command(name='leave', aliases=["stop", "dc", "disconnect", "bye"], description="stops music and disconnects from voice")
    async def leave_(self, ctx):
        """Stop the currently playing song and destroy the player."""
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        if random.randint(0, 1) == 0:
            await ctx.message.add_reaction('👋')
        await ctx.send('**Successfully disconnected**')
        await self.cleanup(ctx.guild)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
