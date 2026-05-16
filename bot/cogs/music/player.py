import asyncio
import collections
import random
from typing import Any

import discord

from utils.logging import logger
from .source import ALONE_TIMEOUT, MAX_QUEUE_SIZE, MusicSource, format_duration


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
                video_cog = self.bot.cogs.get('Video')
                if video_cog and self._guild.id in video_cog.players:
                    logger.info(f"[{self._guild}] player_loop: music idle but video player active — exiting music loop only")
                    try:
                        del self._cog.players[self._guild.id]
                    except KeyError:
                        pass
                    return
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
