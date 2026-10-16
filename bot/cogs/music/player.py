import asyncio
import collections
import random
import time
from typing import Any

import discord

from utils import metrics
from utils.logging import logger

from .source import ALONE_TIMEOUT, MAX_QUEUE_SIZE, MusicSource, format_duration

# A voice client that is registered but not connected is usually discord.py
# mid-way through its own resume/reconnect; give it this long before telling the
# channel that voice was lost.
VOICE_SETTLE_TIMEOUT = 10
# How long a player whose voice connection was lost keeps its queue, waiting for
# someone to rejoin it (rd-join / rd-play), before giving up. Same as the idle timeout.
VOICE_WAIT_TIMEOUT = 300
VOICE_POLL_INTERVAL = 1
# A track cut this close to its end counts as finished rather than resumed.
RESUME_MIN_REMAINING = 3

# Loop modes. Mutually exclusive by construction — a single field avoids the
# nonsensical "track loop and queue loop both on" state two booleans would allow.
LOOP_OFF = "off"
LOOP_TRACK = "track"
LOOP_QUEUE = "queue"
LOOP_LABELS = {
    LOOP_TRACK: "🔁 Looping current track",
    LOOP_QUEUE: "🔁 Looping queue",
}


def has_listeners(channel, self_id: int) -> bool:
    """True if anyone other than this bot (and other bots) is in ``channel``.

    The single source of truth for "is the bot alone?", shared by
    MusicPlayer._ensure_alone_timer and Music.on_voice_state_update so the two
    can never disagree (a disagreement makes one start the countdown and the
    other cancel it).

    Iterates channel.voice_states (raw user_id -> VoiceState), which is always
    complete with the voice_states intent, rather than channel.members, which
    is filtered through the member cache and silently drops uncached users.
    A user is only excluded as a bot when the cache positively says so; an
    uncached user counts as a listener, erring towards staying connected.
    """
    for uid in channel.voice_states:
        if uid == self_id:
            continue
        member = channel.guild.get_member(uid)
        if member is not None and member.bot:
            continue
        return True
    return False


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
        """Insert item at the front of the queue so it plays next.

        Only safe to call from the player-loop tail, where no consumer is parked
        in get() — this bypasses put_nowait and so never wakes a waiting getter.
        """
        self._data.appendleft(item)

    def append_back(self, item: Any) -> None:
        """Append item to the back of the queue, bypassing maxsize.

        Used by queue-loop to re-register a finished track. Deliberately not
        put_nowait: the dequeue and this re-append are separated by a whole
        track, so the queue can legitimately be full again by now, and dropping
        the track would silently shrink the rotation. Overflow is bounded at
        maxsize + 1 since exactly one item is appended per completion and each
        completion is preceded by a dequeue.

        Same caller constraint as insert_front: player-loop tail only.
        """
        self._data.append(item)


def requeue_finished(
    queue: MusicQueue,
    current_raw: dict[str, Any] | None,
    mode: str,
    log_prefix: str = "",
) -> bool:
    """Re-register a track that just finished, according to the loop mode.

    Only the raw metadata dict is ever re-queued — the MusicSource has already
    been cleaned up and its ffmpeg process is dead. The dict holds no expiring
    stream URL, so it re-resolves cleanly on the next play.

    Track loop puts it back at the front (plays again immediately); queue loop
    sends it to the back (the rotation cycles). Returns True if re-queued.
    """
    if not current_raw:
        return False
    if mode == LOOP_TRACK:
        queue.insert_front(current_raw)
        logger.debug(
            f"{log_prefix}player_loop: track loop — re-queued "
            f"'{current_raw.get('title')}' at front"
        )
        return True
    if mode == LOOP_QUEUE:
        queue.append_back(current_raw)
        logger.debug(
            f"{log_prefix}player_loop: queue loop — re-queued "
            f"'{current_raw.get('title')}' at back (size={queue.qsize()})"
        )
        return True
    return False


class MusicPlayer:
    __slots__ = (
        "_alone_task",
        "_channel",
        "_cog",
        "_current_raw",
        "_event_loop",
        "_guild",
        "_loop_mode",
        "_loop_task",
        "_prefetch_task",
        "_prefetched",
        "bot",
        "current",
        "next",
        "np",
        "queue",
        "volume",
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
        self._loop_mode = LOOP_OFF
        self._current_raw = None
        self._alone_task = None

        logger.debug(f"[{self._guild}] MusicPlayer created in #{self._channel}")
        self._loop_task = asyncio.ensure_future(self.player_loop())

    def _after_play(self, error: Exception | None) -> None:
        if error:
            metrics.stream_errors_total.labels(stage="playback").inc()
            logger.error(f"[{self._guild}] _after_play: playback error — {error!r}")
        else:
            title = self.current.title if self.current else "<unknown>"
            logger.debug(
                f"[{self._guild}] _after_play: '{title}' finished cleanly, signalling next"
            )
        self._event_loop.call_soon_threadsafe(self.next.set)

    def _cancel_prefetch(self) -> None:
        if self._prefetch_task and not self._prefetch_task.done():
            logger.debug(f"[{self._guild}] Cancelling in-flight prefetch task")
            self._prefetch_task.cancel()
        self._prefetch_task = None
        self._prefetched = None

    def _ensure_alone_timer(self) -> None:
        """Reconcile the auto-leave timer against reality, once per track.

        on_voice_state_update alone is not enough: it never fires a member event
        when an admin moves the bot into an empty channel, or when the bot joins
        an already-empty one. Under queue-loop the queue never empties, so the
        300s idle timeout in player_loop can never fire either — without this
        reconciliation the bot could stream to nobody indefinitely.

        The "alone" test itself lives in has_listeners(), shared with the cog.
        """
        vc = self._guild.voice_client
        if not vc or not vc.channel:
            return

        listeners = has_listeners(vc.channel, self.bot.user.id)
        # Bind to a local so the None-check narrows the type for the .cancel()
        # below; going through an intermediate bool would defeat that.
        task = self._alone_task

        if listeners:
            if task is not None and not task.done():
                logger.debug(
                    f"[{self._guild}] _ensure_alone_timer: listeners present — cancelling auto-leave countdown"
                )
                task.cancel()
                self._alone_task = None
        elif task is None or task.done():
            logger.info(
                f"[{self._guild}] Alone in '{vc.channel}' (reconcile) — starting {ALONE_TIMEOUT}s auto-leave countdown"
            )
            self._alone_task = asyncio.ensure_future(self._alone_leave())

    async def _alone_leave(self) -> None:
        """Disconnect after ALONE_TIMEOUT seconds if the bot is still alone in voice."""
        await asyncio.sleep(ALONE_TIMEOUT)
        vc = self._guild.voice_client
        channel_name = vc.channel.name if vc else "voice"
        logger.info(
            f"[{self._guild}] Auto-leave: alone in '{channel_name}' for {ALONE_TIMEOUT}s — disconnecting"
        )
        try:
            await self._channel.send(
                f"No one's listening — leaving **{channel_name}**. 👋"
            )
        except discord.HTTPException:
            pass
        self.destroy(self._guild)

    async def _prefetch_next(self, data: dict[str, Any]) -> None:
        title = data.get("title", "<unknown>")
        logger.debug(f"[{self._guild}] _prefetch_next: starting prefetch for '{title}'")
        try:
            info = await MusicSource.fetch_stream_info(data)
            self._prefetched = info
            logger.debug(f"[{self._guild}] _prefetch_next: complete for '{title}'")
        except asyncio.CancelledError:
            logger.debug(f"[{self._guild}] _prefetch_next: cancelled for '{title}'")
            raise
        # Prefetch is a pure optimisation — any failure must degrade to a cold
        # regather at play time rather than propagate.
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[{self._guild}] _prefetch_next: failed for '{title}' — {e!r}"
            )
            self._prefetched = None
        finally:
            self._prefetch_task = None

    def _voice_lost(self) -> bool:
        vc = self._guild.voice_client
        return vc is None or not vc.is_connected()

    async def _wait_for_voice(self) -> bool:
        """Wait for the guild's voice connection to be usable again.

        Brief gaps are discord.py resuming or reconnecting on its own. A real
        loss (Discord dropped the bot, a moderator disconnected it) is
        announced, and the player keeps its queue for VOICE_WAIT_TIMEOUT so
        rd-join / rd-play picks up where it left off. The player never
        reconnects by itself: that raced user commands and discord.py's own
        reconnect logic, and would fight a deliberate disconnect.

        Returns False if voice did not come back in time.
        """
        started = time.monotonic()
        announced = False
        while self._voice_lost():
            waited = time.monotonic() - started
            if waited >= VOICE_WAIT_TIMEOUT:
                if announced:
                    metrics.voice_lost_total.labels(result="gave_up").inc()
                logger.warning(
                    f"[{self._guild}] _wait_for_voice: no voice connection after {waited:.0f}s — giving up"
                )
                return False
            if not announced and waited >= VOICE_SETTLE_TIMEOUT:
                announced = True
                logger.warning(
                    f"[{self._guild}] _wait_for_voice: voice connection lost — keeping the queue "
                    f"for {VOICE_WAIT_TIMEOUT}s"
                )
                try:
                    await self._channel.send(
                        "Lost the voice connection. Use `join` or `play` within "
                        f"{VOICE_WAIT_TIMEOUT // 60} minutes to pick up where it left off."
                    )
                except discord.HTTPException:
                    pass
            await asyncio.sleep(VOICE_POLL_INTERVAL)
        if announced:
            metrics.voice_lost_total.labels(result="rejoined").inc()
            logger.info(f"[{self._guild}] _wait_for_voice: voice is back — resuming")
        return True

    async def _hold_until_voice(self, item: Any) -> bool:
        """Voice is gone: put ``item`` back at the front and wait for a rejoin.

        Returns False (after destroying the player) if nobody rejoined.
        """
        # Player-loop tail semantics: this loop is the queue's only consumer and
        # is not parked in get(), so insert_front is safe here.
        self.queue.insert_front(item)
        self._cancel_prefetch()
        # "Alone in the channel" means nothing without a channel; the hold has
        # its own timeout, and a countdown started before the drop would cut it short.
        if self._alone_task and not self._alone_task.done():
            self._alone_task.cancel()
        self._alone_task = None
        if await self._wait_for_voice():
            return True
        try:
            await self._channel.send(
                "Nobody brought me back to voice — dropping the queue. 👋"
            )
        except discord.HTTPException:
            pass
        self.destroy(self._guild)
        return False

    async def _regather_with_retry(self, data: dict):
        title = data.get("title", "<unknown>")
        logger.debug(
            f"[{self._guild}] _regather_with_retry: fetching stream for '{title}'"
        )
        try:
            source = await MusicSource.regather_stream(data)
            logger.debug(f"[{self._guild}] _regather_with_retry: success for '{title}'")
            return source
        # yt-dlp surfaces a wide, unstable set of exception types; any of them
        # must degrade to "skip this track", never kill the player loop.
        except Exception as e:  # noqa: BLE001
            metrics.stream_errors_total.labels(stage="resolve").inc()
            logger.error(
                f"[{self._guild}] _regather_with_retry: failed for '{title}' — {e!r}"
            )
            await self._channel.send(f"There was an error processing your song: {e}")
            return None

    async def player_loop(self) -> None:
        logger.debug(f"[{self._guild}] player_loop: started")
        await self.bot.wait_until_ready()
        logger.debug(f"[{self._guild}] player_loop: bot ready, entering queue loop")

        while not self.bot.is_closed():
            self.next.clear()

            try:
                logger.debug(
                    f"[{self._guild}] player_loop: waiting for next track (timeout=300s, queue_size={self.queue.qsize()})"
                )
                async with asyncio.timeout(300):
                    source = await self.queue.get()
                logger.debug(
                    f"[{self._guild}] player_loop: dequeued item type={type(source).__name__}"
                )
            except TimeoutError:
                logger.info(
                    f"[{self._guild}] player_loop: queue idle for 300s — destroying player"
                )
                try:
                    await self._channel.send(
                        "Queue finished and idle too long — leaving voice. 👋"
                    )
                except discord.HTTPException:
                    pass
                self.destroy(self._guild)
                return

            item = source  # the queue entry as dequeued, for putting it back
            try:
                if self._voice_lost():
                    if await self._hold_until_voice(item):
                        continue
                    return

                if not isinstance(source, MusicSource):
                    queued = source
                    # resume_at is a one-shot offset for a track a voice drop cut
                    # off; a loop re-queue of this track must start from 0 again.
                    self._current_raw = {
                        k: v for k, v in queued.items() if k != "resume_at"
                    }
                    start_at = queued.get("resume_at") or 0
                    queued_url = queued.get("webpage_url")
                    prefetched_url = (
                        self._prefetched.get("webpage_url")
                        if self._prefetched
                        else None
                    )
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
                        source = MusicSource.from_stream_info(
                            info, queued["requester"], start_at=start_at
                        )
                        logger.debug(
                            f"[{self._guild}] player_loop: prefetch hit — FFmpeg spawned immediately"
                        )
                    elif self._prefetch_task and not self._prefetch_task.done():
                        logger.debug(
                            f"[{self._guild}] player_loop: prefetch task in-flight, awaiting (timeout=30s)"
                        )
                        try:
                            async with asyncio.timeout(30):
                                await self._prefetch_task
                            if (
                                self._prefetched
                                and self._prefetched.get("webpage_url") == queued_url
                            ):
                                info = self._prefetched
                                self._prefetched = None
                                self._prefetch_task = None
                                source = MusicSource.from_stream_info(
                                    info, queued["requester"], start_at=start_at
                                )
                                logger.debug(
                                    f"[{self._guild}] player_loop: prefetch hit (awaited)"
                                )
                            else:
                                logger.debug(
                                    f"[{self._guild}] player_loop: prefetch URL mismatch after await — regathering"
                                )
                                self._cancel_prefetch()
                                source = await self._regather_with_retry(queued)
                        except (TimeoutError, asyncio.CancelledError) as e:
                            # A cancelled *prefetch* degrades to a regather, but if
                            # this loop itself is being cancelled (cleanup ->
                            # stop()), the cancellation must propagate.
                            task = asyncio.current_task()
                            if task is not None and task.cancelling():
                                raise
                            logger.warning(
                                f"[{self._guild}] player_loop: prefetch await failed ({type(e).__name__}) — regathering"
                            )
                            self._cancel_prefetch()
                            source = await self._regather_with_retry(queued)
                    else:
                        logger.debug(
                            f"[{self._guild}] player_loop: no prefetch available — regathering"
                        )
                        source = await self._regather_with_retry(queued)

                if source is None:
                    logger.warning(
                        f"[{self._guild}] player_loop: source is None after resolution — "
                        f"skipping track (dropped from loop rotation if looping)"
                    )
                    self._current_raw = None
                    continue

                source.volume = self.volume
                self.current = source

                # No await between here and vc.play(): a cancellation can't strand
                # the FFmpeg process this source has already spawned.
                vc = self._guild.voice_client
                if vc is None or not vc.is_connected():
                    # Voice went away while the track was resolving. Don't hold
                    # FFmpeg (or the stream URL, which expires) during the wait:
                    # drop the source and re-resolve once voice is back.
                    logger.warning(
                        f"[{self._guild}] player_loop: voice lost while resolving "
                        f"'{source.title}' — putting it back"
                    )
                    source.cleanup()
                    self.current = None
                    self._current_raw = None
                    if await self._hold_until_voice(item):
                        continue
                    return

                if vc.is_playing():
                    logger.warning(
                        f"[{self._guild}] player_loop: voice client already playing when starting '{source.title}' — stopping first"
                    )
                    vc.stop()

                duration_str = (
                    "LIVE" if source.is_live else format_duration(source.duration)
                )
                logger.info(
                    f"[{self._guild}] Now playing '{source.title}' | "
                    f"requested by {source.requester} ({source.requester.id}) | "
                    f"duration={duration_str} | channel='{vc.channel}'"
                )
                vc.play(source, after=self._after_play)
                metrics.streams_started_total.inc()
                self._ensure_alone_timer()

                embed = discord.Embed(
                    title="Now playing",
                    description=f"[{source.title}]({source.web_url}) [{source.requester.mention}]",
                    color=discord.Color.green(),
                )
                if source.thumbnail:
                    embed.set_thumbnail(url=source.thumbnail)
                if self._loop_mode != LOOP_OFF:
                    embed.set_footer(text=LOOP_LABELS[self._loop_mode])
                try:
                    self.np = await self._channel.send(embed=embed)
                except discord.Forbidden:
                    logger.warning(
                        f"[{self._guild}] player_loop: missing Embed Links — falling back to plain text now-playing"
                    )
                    try:
                        self.np = await self._channel.send(
                            f"**Now Playing:** {source.title} | `{duration_str}` | {source.requester.mention}\n"
                            f"<{source.web_url}>"
                        )
                    except discord.HTTPException:
                        pass
                except discord.HTTPException as e:
                    logger.error(
                        f"[{self._guild}] player_loop: failed to send now-playing message — {e!r}"
                    )

                snapshot = self.queue.snapshot()
                if snapshot and isinstance(snapshot[0], dict):
                    self._prefetch_task = asyncio.ensure_future(
                        self._prefetch_next(snapshot[0])
                    )
                    logger.debug(
                        f"[{self._guild}] player_loop: prefetch started for next track '{snapshot[0].get('title')}'"
                    )
                else:
                    logger.debug(
                        f"[{self._guild}] player_loop: queue has {len(snapshot)} item(s), no prefetch needed"
                    )

                logger.debug(
                    f"[{self._guild}] player_loop: waiting for track to finish"
                )
                await self.next.wait()
                logger.debug(
                    f"[{self._guild}] player_loop: track ended or skipped, cleaning up source"
                )
                source.cleanup()
                self.current = None

                # A skip or a natural end leaves the voice client connected; a
                # cleanup()/leave cancels this task before it gets here. So a
                # dead voice client now means voice dropped mid-track: put the
                # track back at the front to resume where it was cut. The next
                # iteration finds voice gone and waits for a rejoin.
                resume_at = 0 if source.is_live else source.position
                cut_short = (
                    source.is_live
                    or not source.duration
                    or resume_at < source.duration - RESUME_MIN_REMAINING
                )
                if self._voice_lost() and self._current_raw is not None and cut_short:
                    logger.warning(
                        f"[{self._guild}] player_loop: voice dropped {format_duration(int(resume_at))} "
                        f"into '{source.title}' — re-queued to resume there"
                    )
                    self.queue.insert_front(
                        {**self._current_raw, "resume_at": resume_at}
                    )
                    # Not finished, so loop modes must not re-queue it a second time.
                    self._current_raw = None
                    # The in-flight prefetch is for the track after this one.
                    self._cancel_prefetch()

                requeue_finished(
                    self.queue, self._current_raw, self._loop_mode, f"[{self._guild}] "
                )
                self._current_raw = None

                logger.debug(
                    f"[{self._guild}] player_loop: source cleanup complete, looping"
                )

            # Last line of defence: the player loop must survive anything, or
            # music dies for this guild until the bot restarts.
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    f"[{self._guild}] player_loop: unhandled exception in loop body — {e!r}"
                )
                self.current = None
                self._current_raw = None
                await asyncio.sleep(1)

        logger.info(f"[{self._guild}] player_loop: bot closed, exiting loop")

    def stop(self) -> None:
        """Stop this player for good: its loop, prefetch and auto-leave timer.

        Called by Music.cleanup(). Before this existed, cleanup() only dropped
        the player from the registry and its player_loop kept running; that
        orphan could later destroy() a *newer* session in the same guild.
        Playback itself stops when the voice client disconnects (discord.py
        cleans up the playing source when its AudioPlayer exits).
        """
        self._cancel_prefetch()
        if self._alone_task and not self._alone_task.done():
            self._alone_task.cancel()
        self._alone_task = None
        if not self._loop_task.done():
            self._loop_task.cancel()

    def destroy(self, guild) -> asyncio.Task:
        logger.info(f"[{guild}] destroy() called — scheduling cleanup")
        self._cancel_prefetch()
        if self._alone_task and not self._alone_task.done():
            self._alone_task.cancel()
            self._alone_task = None
        return self._event_loop.create_task(self._cog.cleanup(guild, player=self))
