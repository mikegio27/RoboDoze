import asyncio
import random

import discord
from discord.ext import commands

from utils.logging import logger
from .source import (
    ALONE_TIMEOUT, MAX_QUEUE_SIZE,
    InvalidVoiceChannel, MusicSource, VoiceConnectionError,
    format_duration, is_playlist_url,
)
from .player import MusicPlayer


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
