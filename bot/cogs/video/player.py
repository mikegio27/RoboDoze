import asyncio

import discord

from utils.logging import logger
from cogs.music.player import MusicQueue
from cogs.music.source import format_duration
from .rtp import VideoRTPSender
from .source import ALONE_TIMEOUT, AUDIO_FFMPEG_OPTIONS, MAX_QUEUE_SIZE, VideoAudioSource, VideoInfo


class VideoPlayer:
    __slots__ = (
        'bot', '_guild', '_channel', '_cog',
        'queue', 'next', 'current', 'np', 'volume',
        '_event_loop', '_alone_task', '_current_info', '_rtp_sender',
    )

    def __init__(self, ctx) -> None:
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog
        self._event_loop = asyncio.get_running_loop()

        self.queue: MusicQueue = MusicQueue(maxsize=MAX_QUEUE_SIZE)
        self.next = asyncio.Event()

        self.np = None
        self.volume: float = 1.0
        self.current: VideoAudioSource | None = None
        self._alone_task: asyncio.Task | None = None
        self._current_info: VideoInfo | None = None
        self._rtp_sender: VideoRTPSender | None = None

        logger.debug(f'[{self._guild}] VideoPlayer created in #{self._channel}')
        asyncio.ensure_future(self.player_loop())

    def _after_play(self, error: Exception | None) -> None:
        if error:
            logger.error(f'[{self._guild}] _after_play: playback error — {error!r}')
        else:
            title = self.current.title if self.current else '<unknown>'
            logger.debug(f'[{self._guild}] _after_play: {title!r} finished, signalling next')
        self._event_loop.call_soon_threadsafe(self.next.set)

    async def _alone_leave(self) -> None:
        await asyncio.sleep(ALONE_TIMEOUT)
        vc = self._guild.voice_client
        channel_name = vc.channel.name if vc else 'voice'
        logger.info(f'[{self._guild}] Video auto-leave: alone in {channel_name!r} — disconnecting')
        try:
            await self._channel.send(f"No one's watching — leaving **{channel_name}**. 👋")
        except discord.HTTPException:
            pass
        self.destroy(self._guild)

    async def _build_source(self, info: VideoInfo) -> VideoAudioSource | None:
        try:
            audio_url = await info.regather_audio()
        except Exception as exc:
            logger.error(f'[{self._guild}] _build_source: failed to get audio URL for {info.title!r} — {exc!r}')
            await self._channel.send(f'Could not load audio for **{info.title}**: {exc}')
            return None
        return VideoAudioSource(
            discord.FFmpegPCMAudio(
                audio_url,
                before_options=AUDIO_FFMPEG_OPTIONS['before_options'],
                options=AUDIO_FFMPEG_OPTIONS['options'],
            ),
            info=info,
        )

    async def player_loop(self) -> None:
        logger.debug(f'[{self._guild}] VideoPlayer.player_loop: started')
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                async with asyncio.timeout(300):
                    info: VideoInfo = await self.queue.get()
            except asyncio.TimeoutError:
                music_cog = self.bot.cogs.get('Music')
                if music_cog and self._guild.id in music_cog.players:
                    logger.info(f'[{self._guild}] VideoPlayer: video idle but music player active — exiting video loop only')
                    try:
                        del self._cog.players[self._guild.id]
                    except KeyError:
                        pass
                    return
                logger.info(f'[{self._guild}] VideoPlayer: idle 300s — destroying')
                try:
                    await self._channel.send('Video queue empty too long — leaving. 👋')
                except discord.HTTPException:
                    pass
                self.destroy(self._guild)
                return

            self._current_info = info
            source = await self._build_source(info)
            if source is None:
                self._current_info = None
                continue

            source.volume = self.volume
            self.current = source

            vc = self._guild.voice_client
            if not vc or not vc.is_connected():
                logger.error(f'[{self._guild}] VideoPlayer: no voice client when trying to play {info.title!r}')
                self.destroy(self._guild)
                return

            if vc.is_playing():
                vc.stop()

            duration_str = format_duration(info.duration)
            logger.info(
                f'[{self._guild}] Now playing (video) {info.title!r} | '
                f'{info.source_type} | {duration_str} | requested by {info.requester}'
            )
            # Create RTP sender lazily now that vc is confirmed live
            if self._rtp_sender is None:
                self._rtp_sender = VideoRTPSender(vc)

            try:
                video_url = await info.regather_video()
            except Exception as exc:
                logger.warning(f'[{self._guild}] VideoPlayer: could not resolve video URL for {info.title!r} — {exc!r}')
                video_url = info.video_url

            vc.play(source, after=self._after_play)
            self._rtp_sender.start(video_url)

            embed = discord.Embed(
                title='Now playing',
                description=f'**{info.title}** [{info.requester.mention}]',
                color=discord.Color.blue(),
            )
            embed.set_footer(text=f'{info.source_type} | {duration_str}')
            if info.thumbnail:
                embed.set_thumbnail(url=info.thumbnail)
            try:
                self.np = await self._channel.send(embed=embed)
            except discord.Forbidden:
                try:
                    self.np = await self._channel.send(
                        f'**Now Playing:** {info.title} | `{duration_str}` | {info.requester.mention}'
                    )
                except discord.HTTPException:
                    pass
            except discord.HTTPException as exc:
                logger.error(f'[{self._guild}] VideoPlayer: failed to send now-playing — {exc!r}')

            await self.next.wait()
            if self._rtp_sender:
                self._rtp_sender.stop()
            source.cleanup()
            self.current = None
            self._current_info = None

    def destroy(self, guild) -> asyncio.Task:
        logger.info(f'[{guild}] VideoPlayer.destroy() called')
        if self._rtp_sender:
            self._rtp_sender.stop()
            self._rtp_sender = None
        if self._alone_task and not self._alone_task.done():
            self._alone_task.cancel()
            self._alone_task = None
        return self._event_loop.create_task(self._cog.cleanup(guild))
