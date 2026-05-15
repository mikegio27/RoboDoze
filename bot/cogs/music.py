import asyncio
import collections
import random
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import discord
from discord.ext import commands
from yt_dlp import YoutubeDL

from utils.logging import logger

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ytdl")

MAX_QUEUE_SIZE = 50
ALONE_TIMEOUT = 60  # seconds before auto-leaving an empty voice channel

FFMPEG_OPTIONS = {
    'before_options': '-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -af aresample=async=1:first_pts=0',
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

ytdl_flat_opts = {
    **ytdl_opts,
    'noplaylist': False,
    'extract_flat': True,
    'ignoreerrors': True,  # skip unavailable videos instead of aborting
}
ytdl_flat = YoutubeDL(ytdl_flat_opts)  # type: ignore[arg-type]


def is_playlist_url(url: str) -> bool:
    """True only for pure playlist URLs (/playlist?list=...), not video+list combos."""
    try:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        return parsed.path == '/playlist' and 'list' in qs
    except Exception:
        return False


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "LIVE"
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

    def shuffle(self) -> None:
        items = list(self._data)
        random.shuffle(items)
        self._data.clear()
        self._data.extend(items)

    def insert_front(self, item: Any) -> None:
        """Insert item at the front of the queue so it plays next."""
        self._data.appendleft(item)


class MusicSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester
        self.title = data.get('title')
        self.web_url = data.get('webpage_url')
        self.duration = data.get('duration')
        self.thumbnail = data.get('thumbnail')
        self.is_live: bool = bool(data.get('is_live'))

    def __getitem__(self, item: str):
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, download: bool = False):
        logger.debug(f"[{ctx.guild}] create_source: searching for '{search}' (download={download})")
        to_run = partial(ytdl.extract_info, url=search, download=download)
        try:
            async with asyncio.timeout(30):
                raw = await asyncio.get_running_loop().run_in_executor(_executor, to_run)
        except asyncio.TimeoutError:
            logger.warning(f"[{ctx.guild}] create_source: yt-dlp timed out for '{search}'")
            await ctx.send(embed=discord.Embed(
                title="", description="Song took too long to load, please try again.", color=discord.Color.red()
            ))
            return None

        if raw is None:
            logger.warning(f"[{ctx.guild}] create_source: no results for '{search}'")
            await ctx.send(embed=discord.Embed(title="", description="No results found.", color=discord.Color.red()))
            return None

        data: dict[str, Any] = raw  # type: ignore[assignment]
        if 'entries' in data:
            data = data['entries'][0]

        logger.debug(f"[{ctx.guild}] create_source: resolved '{search}' -> '{data.get('title')}' ({data.get('webpage_url')})")

        if download:
            source = ytdl.prepare_filename(data)  # type: ignore[arg-type]
            return cls(discord.FFmpegPCMAudio(source, **FFMPEG_OPTIONS), data=data, requester=ctx.author)

        return {
            'webpage_url': data['webpage_url'],
            'requester': ctx.author,
            'title': data['title'],
            'thumbnail': data.get('thumbnail'),
            'duration': data.get('duration'),
            'is_live': bool(data.get('is_live')),
        }

    @classmethod
    async def fetch_stream_info(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Fetch a fresh stream URL from yt-dlp without spawning FFmpeg."""
        logger.debug(f"fetch_stream_info: fetching stream for '{data.get('title')}' ({data.get('webpage_url')})")
        to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=False)
        async with asyncio.timeout(30):
            raw = await asyncio.get_running_loop().run_in_executor(_executor, to_run)
        if raw is None:
            raise ValueError(f"No stream data returned for {data['webpage_url']}")
        info: dict[str, Any] = raw  # type: ignore[assignment]
        logger.debug(f"fetch_stream_info: got stream URL for '{data.get('title')}'")
        return info

    @classmethod
    def from_stream_info(cls, info: dict[str, Any], requester) -> "MusicSource":
        """Create a MusicSource from pre-fetched stream info. Spawns FFmpeg subprocess."""
        logger.debug(f"from_stream_info: spawning FFmpeg for '{info.get('title')}'")
        return cls(discord.FFmpegPCMAudio(info['url'], **FFMPEG_OPTIONS), data=info, requester=requester)

    @classmethod
    async def regather_stream(cls, data: dict[str, Any], *, requester=None) -> "MusicSource":
        req = requester or data['requester']
        info = await cls.fetch_stream_info(data)
        return cls.from_stream_info(info, req)

    @classmethod
    async def fetch_playlist_entries(cls, ctx, url: str) -> list[dict[str, Any]]:
        """Fetch all video metadata from a playlist URL using flat extraction."""
        logger.debug(f"[{ctx.guild}] fetch_playlist_entries: fetching '{url}'")

        def _extract():
            raw = ytdl_flat.extract_info(url=url, download=False)
            if raw is None or 'entries' not in raw:
                return None
            # Force-consume any lazy iterator inside the executor thread so the
            # generator's network calls don't bleed back into the event loop.
            return {'title': raw.get('title', url), 'entries': list(raw['entries'])}

        try:
            async with asyncio.timeout(60):
                result = await asyncio.get_running_loop().run_in_executor(_executor, _extract)
        except asyncio.TimeoutError:
            logger.warning(f"[{ctx.guild}] fetch_playlist_entries: timed out for '{url}'")
            await ctx.send(embed=discord.Embed(
                description="Playlist took too long to load, please try again.", color=discord.Color.red()
            ))
            return []

        if not result:
            logger.warning(f"[{ctx.guild}] fetch_playlist_entries: no entries found for '{url}'")
            return []

        entries = []
        for entry in result['entries']:
            if not entry:
                continue
            video_id = entry.get('id')
            if not video_id:
                # yt-dlp flat entries sometimes put just the bare ID in 'url'
                raw_url = entry.get('url', '')
                parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(raw_url).query)
                video_id = (parsed_qs.get('v') or [None])[0] or (raw_url if raw_url and '/' not in raw_url else None)
            if not video_id:
                logger.debug(f"[{ctx.guild}] fetch_playlist_entries: skipping entry with no resolvable ID: {entry}")
                continue
            entries.append({
                'webpage_url': f"https://www.youtube.com/watch?v={video_id}",
                'requester': ctx.author,
                'title': entry.get('title', 'Unknown'),
                'thumbnail': f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
                'duration': entry.get('duration'),
                'is_live': bool(entry.get('is_live')),
            })

        logger.debug(f"[{ctx.guild}] fetch_playlist_entries: found {len(entries)} tracks in '{result['title']}'")
        return entries


class MusicPlayer:
    __slots__ = (
        'bot', '_guild', '_channel', '_cog',
        'queue', 'next', 'current', 'np', 'volume',
        '_event_loop', '_prefetch_task', '_prefetched',
        '_loop', '_current_raw', '_alone_task',
    )

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog
        self._event_loop = asyncio.get_running_loop()

        self.queue = MusicQueue(maxsize=MAX_QUEUE_SIZE)
        self.next = asyncio.Event()

        self.np = None
        self.volume = 1.0
        self.current = None
        self._prefetch_task = None
        self._prefetched = None
        self._loop = False
        self._current_raw = None
        self._alone_task = None

        logger.debug(f"[{self._guild}] MusicPlayer created in #{self._channel}")
        asyncio.ensure_future(self.player_loop())

    def _after_play(self, error: Exception | None) -> None:
        if error:
            logger.error(f"[{self._guild}] _after_play: playback error — {error!r}")
        else:
            title = self.current.title if self.current else "<unknown>"
            logger.debug(f"[{self._guild}] _after_play: '{title}' finished cleanly, signalling next")
        self._event_loop.call_soon_threadsafe(self.next.set)

    def _cancel_prefetch(self) -> None:
        if self._prefetch_task and not self._prefetch_task.done():
            logger.debug(f"[{self._guild}] Cancelling in-flight prefetch task")
            self._prefetch_task.cancel()
        self._prefetch_task = None
        self._prefetched = None

    async def _alone_leave(self) -> None:
        """Disconnect after ALONE_TIMEOUT seconds if the bot is still alone in voice."""
        await asyncio.sleep(ALONE_TIMEOUT)
        vc = self._guild.voice_client
        channel_name = vc.channel.name if vc else "voice"
        logger.info(f"[{self._guild}] Auto-leave: alone in '{channel_name}' for {ALONE_TIMEOUT}s — disconnecting")
        try:
            await self._channel.send(f"No one's listening — leaving **{channel_name}**. 👋")
        except discord.HTTPException:
            pass
        self.destroy(self._guild)

    async def _prefetch_next(self, data: dict[str, Any]) -> None:
        title = data.get('title', '<unknown>')
        logger.debug(f"[{self._guild}] _prefetch_next: starting prefetch for '{title}'")
        try:
            info = await MusicSource.fetch_stream_info(data)
            self._prefetched = info
            logger.debug(f"[{self._guild}] _prefetch_next: complete for '{title}'")
        except asyncio.CancelledError:
            logger.debug(f"[{self._guild}] _prefetch_next: cancelled for '{title}'")
            raise
        except Exception as e:
            logger.warning(f"[{self._guild}] _prefetch_next: failed for '{title}' — {e!r}")
            self._prefetched = None
        finally:
            self._prefetch_task = None

    async def _regather_with_retry(self, data: dict):
        title = data.get('title', '<unknown>')
        logger.debug(f"[{self._guild}] _regather_with_retry: fetching stream for '{title}'")
        try:
            source = await MusicSource.regather_stream(data)
            logger.debug(f"[{self._guild}] _regather_with_retry: success for '{title}'")
            return source
        except Exception as e:
            logger.error(f"[{self._guild}] _regather_with_retry: failed for '{title}' — {e!r}")
            await self._channel.send(f'There was an error processing your song: {e}')
            return None

    async def player_loop(self) -> None:
        logger.debug(f"[{self._guild}] player_loop: started")
        await self.bot.wait_until_ready()
        logger.debug(f"[{self._guild}] player_loop: bot ready, entering queue loop")

        while not self.bot.is_closed():
            self.next.clear()

            try:
                logger.debug(f"[{self._guild}] player_loop: waiting for next track (timeout=300s, queue_size={self.queue.qsize()})")
                async with asyncio.timeout(300):
                    source = await self.queue.get()
                logger.debug(f"[{self._guild}] player_loop: dequeued item type={type(source).__name__}")
            except asyncio.TimeoutError:
                logger.info(f"[{self._guild}] player_loop: queue idle for 300s — destroying player")
                try:
                    await self._channel.send("Queue finished and idle too long — leaving voice. 👋")
                except discord.HTTPException:
                    pass
                self.destroy(self._guild)
                return

            try:
                if not isinstance(source, MusicSource):
                    queued = source
                    self._current_raw = queued
                    queued_url = queued.get('webpage_url')
                    prefetched_url = self._prefetched.get('webpage_url') if self._prefetched else None
                    logger.debug(
                        f"[{self._guild}] player_loop: resolving stream for '{queued.get('title')}' "
                        f"prefetch_ready={self._prefetched is not None} "
                        f"prefetch_url_match={prefetched_url == queued_url} "
                        f"prefetch_task_running={bool(self._prefetch_task and not self._prefetch_task.done())}"
                    )

                    if self._prefetched is not None and prefetched_url == queued_url:
                        info = self._prefetched
                        self._prefetched = None
                        self._prefetch_task = None
                        source = MusicSource.from_stream_info(info, queued['requester'])
                        logger.debug(f"[{self._guild}] player_loop: prefetch hit — FFmpeg spawned immediately")
                    elif self._prefetch_task and not self._prefetch_task.done():
                        logger.debug(f"[{self._guild}] player_loop: prefetch task in-flight, awaiting (timeout=30s)")
                        try:
                            async with asyncio.timeout(30):
                                await self._prefetch_task
                            if self._prefetched and self._prefetched.get('webpage_url') == queued_url:
                                info = self._prefetched
                                self._prefetched = None
                                self._prefetch_task = None
                                source = MusicSource.from_stream_info(info, queued['requester'])
                                logger.debug(f"[{self._guild}] player_loop: prefetch hit (awaited)")
                            else:
                                logger.debug(f"[{self._guild}] player_loop: prefetch URL mismatch after await — regathering")
                                self._cancel_prefetch()
                                source = await self._regather_with_retry(queued)
                        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                            logger.warning(f"[{self._guild}] player_loop: prefetch await failed ({type(e).__name__}) — regathering")
                            self._cancel_prefetch()
                            source = await self._regather_with_retry(queued)
                    else:
                        logger.debug(f"[{self._guild}] player_loop: no prefetch available — regathering")
                        source = await self._regather_with_retry(queued)

                if source is None:
                    logger.warning(f"[{self._guild}] player_loop: source is None after resolution — skipping track")
                    self._current_raw = None
                    continue

                source.volume = self.volume
                self.current = source

                vc = self._guild.voice_client
                if not vc or not vc.is_connected():
                    logger.error(
                        f"[{self._guild}] player_loop: voice client not connected when trying to play "
                        f"'{source.title}' (vc={vc!r}) — destroying player"
                    )
                    self.destroy(self._guild)
                    return

                if vc.is_playing():
                    logger.warning(f"[{self._guild}] player_loop: voice client already playing when starting '{source.title}' — stopping first")
                    vc.stop()

                duration_str = "LIVE" if source.is_live else format_duration(source.duration)
                logger.info(
                    f"[{self._guild}] Now playing '{source.title}' | "
                    f"requested by {source.requester} ({source.requester.id}) | "
                    f"duration={duration_str} | channel='{vc.channel}'"
                )
                vc.play(source, after=self._after_play)

                embed = discord.Embed(
                    title="Now playing",
                    description=f"[{source.title}]({source.web_url}) [{source.requester.mention}]",
                    color=discord.Color.green(),
                )
                if source.thumbnail:
                    embed.set_thumbnail(url=source.thumbnail)
                if self._loop:
                    embed.set_footer(text="🔁 Loop enabled")
                try:
                    self.np = await self._channel.send(embed=embed)
                except discord.Forbidden:
                    logger.warning(f"[{self._guild}] player_loop: missing Embed Links — falling back to plain text now-playing")
                    try:
                        self.np = await self._channel.send(
                            f"**Now Playing:** {source.title} | `{duration_str}` | {source.requester.mention}\n"
                            f"<{source.web_url}>"
                        )
                    except discord.HTTPException:
                        pass
                except discord.HTTPException as e:
                    logger.error(f"[{self._guild}] player_loop: failed to send now-playing message — {e!r}")

                snapshot = self.queue.snapshot()
                if snapshot and isinstance(snapshot[0], dict):
                    self._prefetch_task = asyncio.ensure_future(self._prefetch_next(snapshot[0]))
                    logger.debug(f"[{self._guild}] player_loop: prefetch started for next track '{snapshot[0].get('title')}'")
                else:
                    logger.debug(f"[{self._guild}] player_loop: queue has {len(snapshot)} item(s), no prefetch needed")

                logger.debug(f"[{self._guild}] player_loop: waiting for track to finish")
                await self.next.wait()
                logger.debug(f"[{self._guild}] player_loop: track ended or skipped, cleaning up source")
                source.cleanup()
                self.current = None

                if self._loop and self._current_raw:
                    self.queue.insert_front(self._current_raw)
                    logger.debug(f"[{self._guild}] player_loop: loop mode — re-queued '{self._current_raw.get('title')}' at front")
                else:
                    self._current_raw = None

                logger.debug(f"[{self._guild}] player_loop: source cleanup complete, looping")

            except Exception as e:
                logger.exception(f"[{self._guild}] player_loop: unhandled exception in loop body — {e!r}")
                self.current = None
                self._current_raw = None
                await asyncio.sleep(1)

        logger.info(f"[{self._guild}] player_loop: bot closed, exiting loop")

    def destroy(self, guild) -> asyncio.Task:
        logger.info(f"[{guild}] destroy() called — cancelling prefetch and scheduling cleanup")
        self._cancel_prefetch()
        if self._alone_task and not self._alone_task.done():
            self._alone_task.cancel()
            self._alone_task = None
        return self._event_loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    __slots__ = ('bot', 'players')

    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild) -> None:
        logger.info(f"[{guild}] cleanup() called — disconnecting voice client and removing player")
        try:
            vc = guild.voice_client
            if vc:
                logger.debug(f"[{guild}] cleanup: voice client in '{vc.channel}', disconnecting")
                await vc.disconnect()
            else:
                logger.debug(f"[{guild}] cleanup: no voice client to disconnect")
        except AttributeError:
            logger.debug(f"[{guild}] cleanup: AttributeError on voice client disconnect (already gone)")
        try:
            del self.players[guild.id]
            logger.debug(f"[{guild}] cleanup: player removed from registry")
        except KeyError:
            logger.debug(f"[{guild}] cleanup: player was not in registry (already removed)")

    async def cog_check(self, ctx):
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id:
            guild = before.channel.guild if before.channel else (after.channel.guild if after.channel else None)
            if before.channel and not after.channel:
                logger.warning(f"[{guild}] on_voice_state_update: bot disconnected from voice channel '{before.channel}'")
            elif before.channel and after.channel and before.channel.id != after.channel.id:
                logger.info(f"[{guild}] on_voice_state_update: bot moved from '{before.channel}' to '{after.channel}'")
            elif not before.channel and after.channel:
                logger.debug(f"[{guild}] on_voice_state_update: bot connected to '{after.channel}'")
            return

        guild = member.guild
        vc = guild.voice_client
        if not vc:
            return

        player = self.players.get(guild.id)
        if not player:
            return

        # Member left the bot's voice channel — check if bot is now alone
        if before.channel and before.channel.id == vc.channel.id:
            non_bots = [m for m in vc.channel.members if not m.bot]
            if not non_bots and not (player._alone_task and not player._alone_task.done()):
                logger.info(f"[{guild}] Bot is alone in '{vc.channel}' — starting {ALONE_TIMEOUT}s auto-leave countdown")
                player._alone_task = asyncio.ensure_future(player._alone_leave())

        # Member joined the bot's voice channel — cancel any pending auto-leave
        if after.channel and after.channel.id == vc.channel.id and not member.bot:
            if player._alone_task and not player._alone_task.done():
                logger.info(f"[{guild}] Member joined '{vc.channel}' — cancelling auto-leave countdown")
                player._alone_task.cancel()
                player._alone_task = None

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('This command cannot be used in private messages.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Error connecting to voice channel. Please make sure you are in one.')
        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            if isinstance(original, discord.Forbidden):
                try:
                    await ctx.send(
                        "I'm missing the **Embed Links** permission in this channel. "
                        "Grant it in channel settings and try again."
                    )
                except discord.HTTPException:
                    pass
            else:
                logger.error(f"Command {ctx.command} raised: {original}", exc_info=original)
        else:
            logger.warning(f"Ignoring exception in command {ctx.command}: {error}")

    def get_player(self, ctx) -> MusicPlayer:
        try:
            player = self.players[ctx.guild.id]
            logger.debug(f"[{ctx.guild}] get_player: returning existing player")
        except KeyError:
            logger.debug(f"[{ctx.guild}] get_player: no player found, creating new one")
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
                logger.debug(f"[{ctx.guild}] connect_: already in '{channel}', doing nothing")
                return
            logger.debug(f"[{ctx.guild}] connect_: moving from '{vc.channel}' to '{channel}'")
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Moving to channel: <{channel}> timed out.')
        else:
            logger.debug(f"[{ctx.guild}] connect_: connecting to '{channel}'")
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Connecting to channel: <{channel}> timed out.')

        logger.info(f"[{ctx.guild}] connect_: joined '{channel}'")
        if random.randint(0, 1) == 0:
            await ctx.message.add_reaction('👍')
        await ctx.send(f'**Joined `{channel}`**')

    @commands.command(name='play', aliases=['sing', 'p'], description="streams music")
    async def play_(self, ctx, *, search: str):
        """Request a song and add it to the queue."""
        logger.info(f"[{ctx.guild}] '{search}' requested by {ctx.author} ({ctx.author.id})")
        await ctx.typing()

        vc = ctx.voice_client
        if not vc:
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)
        source = await MusicSource.create_source(ctx, search, download=False)
        if source is None:
            return

        try:
            player.queue.put_nowait(source)
        except asyncio.QueueFull:
            await ctx.send(embed=discord.Embed(
                title="",
                description=f"The queue is full ({MAX_QUEUE_SIZE} songs max). Wait for some tracks to finish.",
                color=discord.Color.red(),
            ))
            return

        position = player.queue.qsize()
        logger.debug(f"[{ctx.guild}] play_: queued '{source.get('title')}' at position #{position}")

        embed = discord.Embed(
            title="",
            description=f"Queued [{source['title']}]({source['webpage_url']}) [{ctx.author.mention}]",
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Position #{position} in queue")
        if source.get('thumbnail'):
            embed.set_thumbnail(url=source['thumbnail'])
        await ctx.send(embed=embed)

    @commands.command(name='playlist', aliases=['pl'], description="queues a playlist URL or comma-separated songs/URLs")
    async def playlist_(self, ctx, *, search: str):
        """Queue a YouTube playlist URL or a comma-separated list of songs/URLs."""
        logger.info(f"[{ctx.guild}] playlist '{search}' requested by {ctx.author} ({ctx.author.id})")
        await ctx.typing()

        vc = ctx.voice_client
        if not vc:
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)

        if is_playlist_url(search):
            await self._handle_playlist(ctx, player, search)
            return

        items = [item.strip() for item in search.split(',') if item.strip()]
        if not items:
            await ctx.send(embed=discord.Embed(description="Nothing to queue.", color=discord.Color.red()))
            return

        status_msg = await ctx.send(embed=discord.Embed(
            description=f"⏳ Loading {len(items)} song(s)...", color=discord.Color.blurple()
        ))

        queued = 0
        failed = 0
        for item in items:
            if player.queue.full():
                break
            source = await MusicSource.create_source(ctx, item, download=False)
            if source is None:
                failed += 1
                continue
            try:
                player.queue.put_nowait(source)
                queued += 1
            except asyncio.QueueFull:
                break

        skipped = len(items) - queued - failed
        logger.info(f"[{ctx.guild}] playlist_: queued {queued}, failed {failed}, skipped {skipped}")

        desc = f"Queued **{queued}** track(s)"
        if failed:
            desc += f"\n⚠️ {failed} not found"
        if skipped:
            desc += f"\n⚠️ {skipped} skipped — queue full ({MAX_QUEUE_SIZE} max)"
        await status_msg.edit(embed=discord.Embed(description=desc, color=discord.Color.green()))

    async def _handle_playlist(self, ctx, player: MusicPlayer, url: str) -> None:
        status_msg = await ctx.send(embed=discord.Embed(
            description="⏳ Loading playlist...", color=discord.Color.blurple()
        ))
        entries = await MusicSource.fetch_playlist_entries(ctx, url)
        if not entries:
            await status_msg.edit(embed=discord.Embed(
                description="No tracks found in that playlist.", color=discord.Color.red()
            ))
            return

        queued = 0
        for entry in entries:
            try:
                player.queue.put_nowait(entry)
                queued += 1
            except asyncio.QueueFull:
                break

        skipped = len(entries) - queued
        logger.info(f"[{ctx.guild}] _handle_playlist: queued {queued} tracks, skipped {skipped} (url={url})")

        desc = f"Queued **{queued}** tracks from playlist"
        if skipped:
            desc += f"\n⚠️ {skipped} tracks skipped — queue full ({MAX_QUEUE_SIZE} max)"
        await status_msg.edit(embed=discord.Embed(description=desc, color=discord.Color.green()))

    @commands.command(name='pause', description="pauses music")
    async def pause_(self, ctx):
        """Pause the currently playing song."""
        vc = ctx.voice_client
        if not vc or not vc.is_playing():
            embed = discord.Embed(title="", description="I am currently not playing anything", color=discord.Color.green())
            return await ctx.send(embed=embed)
        elif vc.is_paused():
            return
        logger.debug(f"[{ctx.guild}] pause_: pausing playback")
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
        logger.debug(f"[{ctx.guild}] resume_: resuming playback")
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
        logger.debug(f"[{ctx.guild}] skip_: stopping current track")
        vc.stop()

    @commands.command(name='loop', aliases=['repeat', 'lp'], description="toggles loop mode for the current track")
    async def loop_(self, ctx):
        """Toggle looping the current track."""
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)
        player._loop = not player._loop
        state = "enabled" if player._loop else "disabled"
        logger.debug(f"[{ctx.guild}] loop_: loop {state} by {ctx.author}")
        embed = discord.Embed(title="", description=f"🔁 Loop **{state}**", color=discord.Color.green())
        await ctx.send(embed=embed)

    @commands.command(name='shuffle', aliases=['sh'], description="shuffles the queue")
    async def shuffle_(self, ctx):
        """Shuffle the upcoming queue."""
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)
        if player.queue.empty():
            embed = discord.Embed(title="", description="There's nothing in the queue to shuffle.", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player.queue.shuffle()
        logger.debug(f"[{ctx.guild}] shuffle_: queue shuffled by {ctx.author}")
        embed = discord.Embed(
            title="",
            description=f"🔀 Queue shuffled ({player.queue.qsize()} tracks)",
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

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
            logger.debug(f"[{ctx.guild}] remove_: removed {label} '{title}'")
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
        logger.debug(f"[{ctx.guild}] clear_: clearing {player.queue.qsize()} queued tracks")
        player.queue.clear_all()
        await ctx.send('**Cleared**')

    @commands.command(name='queue', aliases=['q', 'que'], description="shows the queue")
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
        duration = "🔴 LIVE" if current.is_live else format_duration(current.duration)
        fmt = '\n'.join(
            f"`{i + 1}.` [{s['title']}]({s['webpage_url']}) | ` Requested by: {s['requester']}`\n"
            for i, s in enumerate(upcoming)
        )
        loop_indicator = " | 🔁 Loop ON" if player._loop else ""
        fmt = (
            f"\n__Now Playing__:\n[{current.title}]({current.web_url}) | "
            f"` {duration} Requested by: {current.requester}`{loop_indicator}\n\n__Up Next:__\n"
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

        duration = "🔴 LIVE" if current.is_live else format_duration(current.duration)
        embed = discord.Embed(
            title="",
            description=f"[{current.title}]({current.web_url}) [{current.requester.mention}] | `{duration}`",
            color=discord.Color.green(),
        )
        embed.set_author(icon_url=self.bot.user.display_avatar.url, name="Now Playing 🎶")
        if current.thumbnail:
            embed.set_thumbnail(url=current.thumbnail)
        if player._loop:
            embed.set_footer(text="🔁 Loop enabled")
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
        logger.debug(f"[{ctx.guild}] change_volume: set to {vol}% by {ctx.author}")
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

        logger.info(f"[{ctx.guild}] leave_: disconnect requested by {ctx.author}")
        if random.randint(0, 1) == 0:
            await ctx.message.add_reaction('👋')
        await ctx.send('**Successfully disconnected**')
        await self.cleanup(ctx.guild)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
