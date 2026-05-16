import asyncio
import os

import discord
from discord.ext import commands

from utils.logging import logger
from cogs.music.source import InvalidVoiceChannel, VoiceConnectionError, format_duration
from .jellyfin import JellyfinClient
from .local import LocalLibrary
from .player import VideoPlayer
from .source import MAX_QUEUE_SIZE, VideoInfo


class Video(commands.Cog):
    __slots__ = ('bot', 'players', '_library', '_jellyfin')

    def __init__(self, bot) -> None:
        self.bot = bot
        self.players: dict[int, VideoPlayer] = {}

        video_path = os.getenv('VIDEO_LOCAL_PATH')
        self._library: LocalLibrary | None = LocalLibrary(video_path) if video_path else None

        jelly_url = os.getenv('JELLYFIN_URL')
        jelly_key = os.getenv('JELLYFIN_API_KEY', '')
        self._jellyfin: JellyfinClient | None = JellyfinClient(jelly_url, jelly_key) if jelly_url else None

    async def cleanup(self, guild) -> None:
        logger.info(f'[{guild}] Video.cleanup(): disconnecting and removing player')
        try:
            vc = guild.voice_client
            if vc:
                await vc.disconnect()
        except AttributeError:
            pass
        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def cog_check(self, ctx) -> bool:
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        if member.id == self.bot.user.id:
            return

        guild = member.guild
        vc = guild.voice_client
        if not vc:
            return

        player = self.players.get(guild.id)
        if not player:
            return

        from .source import ALONE_TIMEOUT
        if before.channel and before.channel.id == vc.channel.id:
            non_bots = [m for m in vc.channel.members if not m.bot]
            if not non_bots and not (player._alone_task and not player._alone_task.done()):
                logger.info(f'[{guild}] Video: alone in {vc.channel!r} — starting {ALONE_TIMEOUT}s auto-leave')
                player._alone_task = asyncio.ensure_future(player._alone_leave())

        if after.channel and after.channel.id == vc.channel.id and not member.bot:
            if player._alone_task and not player._alone_task.done():
                logger.info(f'[{guild}] Video: member joined — cancelling auto-leave')
                player._alone_task.cancel()
                player._alone_task = None

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error) -> None:
        if isinstance(error, commands.NoPrivateMessage):
            try:
                await ctx.send('This command cannot be used in private messages.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Error connecting to voice channel. Make sure you are in one.')
        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            if isinstance(original, discord.Forbidden):
                try:
                    await ctx.send("I'm missing the **Embed Links** permission in this channel.")
                except discord.HTTPException:
                    pass
            else:
                logger.error(f'Command {ctx.command} raised: {original}', exc_info=original)
        else:
            logger.warning(f'Ignoring exception in command {ctx.command}: {error}')

    def get_player(self, ctx) -> VideoPlayer:
        player = self.players.get(ctx.guild.id)
        if not player:
            player = VideoPlayer(ctx)
            self.players[ctx.guild.id] = player
        return player

    async def _ensure_voice(self, ctx, channel: discord.VoiceChannel | None = None) -> None:
        """Join the author's voice channel, moving if already connected elsewhere."""
        if not channel:
            try:
                channel = ctx.author.voice.channel
            except AttributeError:
                channel = None
            if not channel:
                await ctx.send(embed=discord.Embed(
                    description='Join a voice channel first.',
                    color=discord.Color.red(),
                ))
                raise InvalidVoiceChannel('No channel to join.')

        vc = ctx.voice_client
        if vc:
            if vc.channel and vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Moving to {channel} timed out.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Connecting to {channel} timed out.')
        logger.info(f'[{ctx.guild}] Video: joined {channel!r}')

    def _check_music_conflict(self, ctx) -> bool:
        """True if the music cog currently has an active player in this guild."""
        music_cog = self.bot.cogs.get('Music')
        if not music_cog:
            return False
        return ctx.guild.id in music_cog.players

    async def _queue_info(self, ctx, info: VideoInfo) -> None:
        """Add info to queue and send a queued embed."""
        player = self.get_player(ctx)
        try:
            player.queue.put_nowait(info)
        except asyncio.QueueFull:
            await ctx.send(embed=discord.Embed(
                description=f'Queue is full ({MAX_QUEUE_SIZE} max).',
                color=discord.Color.red(),
            ))
            return

        pos = player.queue.qsize()
        embed = discord.Embed(
            description=f'Queued **{info.title}** [{ctx.author.mention}]',
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f'{info.source_type} | position #{pos}')
        if info.thumbnail:
            embed.set_thumbnail(url=info.thumbnail)
        await ctx.send(embed=embed)

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.command(name='vplay', aliases=['vp'], description='stream a YouTube video')
    async def vplay_(self, ctx, *, search: str) -> None:
        """Queue a YouTube video URL or search term."""
        if self._check_music_conflict(ctx):
            return await ctx.send(embed=discord.Embed(
                description='Music is currently playing. Use `!leave` first.',
                color=discord.Color.red(),
            ))

        await ctx.typing()
        await self._ensure_voice(ctx)

        info = await VideoInfo.from_youtube(search, ctx.author)
        if not info:
            return await ctx.send(embed=discord.Embed(
                description='No results found.', color=discord.Color.red(),
            ))

        logger.info(f'[{ctx.guild}] vplay: {info.title!r} queued by {ctx.author}')
        await self._queue_info(ctx, info)

    @commands.command(name='vlocal', aliases=['vl'], description='play a file from local storage')
    async def vlocal_(self, ctx, *, search: str) -> None:
        """Search the local video library and queue the best match."""
        if not self._library:
            return await ctx.send(embed=discord.Embed(
                description='Local video storage is not configured (`VIDEO_LOCAL_PATH` unset).',
                color=discord.Color.red(),
            ))
        if self._check_music_conflict(ctx):
            return await ctx.send(embed=discord.Embed(
                description='Music is currently playing. Use `!leave` first.',
                color=discord.Color.red(),
            ))

        await ctx.typing()
        results = self._library.search(search)
        if not results:
            return await ctx.send(embed=discord.Embed(
                description=f'No local videos matching **{search}**.', color=discord.Color.red(),
            ))

        await self._ensure_voice(ctx)
        path = results[0]
        info = VideoInfo.from_local(path, ctx.author)
        logger.info(f'[{ctx.guild}] vlocal: {info.title!r} queued by {ctx.author}')
        await self._queue_info(ctx, info)

        if len(results) > 1:
            others = ', '.join(f'`{p.stem}`' for p in results[1:])
            await ctx.send(f'Other matches: {others}')

    @commands.command(name='vjelly', aliases=['vj'], description='play from Jellyfin')
    async def vjelly_(self, ctx, *, search: str) -> None:
        """Search Jellyfin and queue the best match."""
        if not self._jellyfin:
            return await ctx.send(embed=discord.Embed(
                description='Jellyfin is not configured (`JELLYFIN_URL` unset).',
                color=discord.Color.red(),
            ))
        if self._check_music_conflict(ctx):
            return await ctx.send(embed=discord.Embed(
                description='Music is currently playing. Use `!leave` first.',
                color=discord.Color.red(),
            ))

        await ctx.typing()
        items = await self._jellyfin.search(search)
        if not items:
            return await ctx.send(embed=discord.Embed(
                description=f'No Jellyfin results for **{search}**.', color=discord.Color.red(),
            ))

        await self._ensure_voice(ctx)
        item = items[0]
        item_id = item['Id']
        info = VideoInfo.from_jellyfin(
            item,
            audio_url=self._jellyfin.audio_url(item_id),
            video_url=self._jellyfin.stream_url(item_id),
            requester=ctx.author,
        )
        logger.info(f'[{ctx.guild}] vjelly: {info.title!r} queued by {ctx.author}')
        await self._queue_info(ctx, info)

        if len(items) > 1:
            others = ', '.join(f'`{i["Name"]}`' for i in items[1:])
            await ctx.send(f'Other matches: {others}')

    @commands.command(name='vqueue', aliases=['vq'], description='show the video queue')
    async def vqueue_(self, ctx) -> None:
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send(embed=discord.Embed(
                description="I'm not connected to a voice channel.", color=discord.Color.blue(),
            ))

        player = self.players.get(ctx.guild.id)
        if not player or not player.current:
            return await ctx.send(embed=discord.Embed(
                description='Nothing is playing.', color=discord.Color.blue(),
            ))

        current = player.current
        duration = format_duration(current.duration)
        upcoming = player.queue.snapshot()
        fmt = '\n'.join(
            f'`{i + 1}.` **{v.title}** | `{v.source_type}` | {v.requester.mention}'
            for i, v in enumerate(upcoming)
        )
        desc = (
            f'__Now Playing__:\n**{current.title}** | `{duration}` | {current.requester.mention}'
            + ('\n\n__Up Next:__\n' + fmt if upcoming else '')
            + f'\n\n**{len(upcoming)} video(s) in queue**'
        )
        await ctx.send(embed=discord.Embed(
            title=f'Video queue for {ctx.guild.name}',
            description=desc,
            color=discord.Color.blue(),
        ))

    @commands.command(name='vnp', aliases=['vcurrent'], description='show now playing video')
    async def vnp_(self, ctx) -> None:
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send(embed=discord.Embed(
                description="I'm not connected to a voice channel.", color=discord.Color.blue(),
            ))

        player = self.players.get(ctx.guild.id)
        if not player or not player.current:
            return await ctx.send(embed=discord.Embed(
                description='Nothing is playing.', color=discord.Color.blue(),
            ))

        current = player.current
        duration = format_duration(current.duration)
        embed = discord.Embed(
            description=f'**{current.title}** | {current.requester.mention} | `{duration}`',
            color=discord.Color.blue(),
        )
        embed.set_author(name='Now Playing 🎬', icon_url=self.bot.user.display_avatar.url)
        if current.thumbnail:
            embed.set_thumbnail(url=current.thumbnail)
        await ctx.send(embed=embed)

    @commands.command(name='vskip', aliases=['vs'], description='skip the current video')
    async def vskip_(self, ctx) -> None:
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send(embed=discord.Embed(
                description="I'm not connected to a voice channel.", color=discord.Color.blue(),
            ))
        if not vc.is_playing() and not vc.is_paused():
            return
        logger.debug(f'[{ctx.guild}] vskip: skipping by {ctx.author}')
        vc.stop()

    @commands.command(name='vpause', description='pause the current video')
    async def vpause_(self, ctx) -> None:
        vc = ctx.voice_client
        if not vc or not vc.is_playing():
            return await ctx.send(embed=discord.Embed(
                description='Nothing is playing.', color=discord.Color.blue(),
            ))
        vc.pause()
        await ctx.send('Paused ⏸️')

    @commands.command(name='vresume', description='resume the paused video')
    async def vresume_(self, ctx) -> None:
        vc = ctx.voice_client
        if not vc or not vc.is_paused():
            return
        vc.resume()
        await ctx.send('Resuming ⏯️')

    @commands.command(name='vleave', aliases=['vstop', 'vdc'], description='stop video and disconnect')
    async def vleave_(self, ctx) -> None:
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send(embed=discord.Embed(
                description="I'm not in a voice channel.", color=discord.Color.blue(),
            ))
        logger.info(f'[{ctx.guild}] vleave: disconnect requested by {ctx.author}')
        await ctx.send('**Disconnected**')
        await self.cleanup(ctx.guild)
