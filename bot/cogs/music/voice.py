import asyncio
import time

import discord

from utils.logging import logger


class GuardedVoiceClient(discord.VoiceClient):
    """discord.py 2.7 VoiceClient with its teardown races closed.

    1. cleanup() never evicts a newer client for its guild. Stock
       VoiceProtocol.cleanup() pops the guild's slot in the voice-client
       registry by guild id without checking that the slot is still this
       client. VoiceClient.disconnect() waits up to 30s for Discord to confirm
       the leave before it runs cleanup(), and that confirmation is itself
       routed through the same registry. So a leave followed by a quick rejoin
       let the old client's late cleanup() delete the *new* client:
       guild.voice_client went None while the new connection kept streaming
       (prod, 2026-10-16).
    2. disconnect() is idempotent. A second call while one is in flight awaits
       the first instead of sending Discord another "leave", whose echo could
       land on the next connection and kill it mid-handshake.
    3. disconnect() first cancels discord.py's own background connect and
       reconnect tasks. VoiceConnectionState.disconnect() leaves them running,
       and a surviving one later times out and sends a guild-wide leave, or
       rejoins the old channel, under whatever connection exists by then.

    Always connect with ``channel.connect(cls=GuardedVoiceClient)``.
    """

    _disconnect_task: asyncio.Future | None = None

    async def disconnect(self, *, force: bool = False) -> None:
        if self._disconnect_task is None:
            self._stop_background_tasks()
            self._disconnect_task = asyncio.ensure_future(
                super().disconnect(force=force)
            )
            # If every awaiting caller was cancelled, nobody retrieves a failure;
            # consume it here so asyncio doesn't log "exception never retrieved".
            self._disconnect_task.add_done_callback(
                lambda t: t.cancelled() or t.exception()
            )
        else:
            logger.debug(
                f"[{self.channel.guild}] voice: disconnect already in progress — awaiting it"
            )
        # Shielded: a caller being cancelled must not abort the teardown half-way
        # (discord.py's disconnect also swallows CancelledError internally, which
        # would otherwise eat the caller's cancellation).
        await asyncio.shield(self._disconnect_task)

    def _stop_background_tasks(self) -> None:
        # Private VoiceConnectionState attributes; the same ones its own
        # connect() and soft_disconnect() cancel.
        state = self._connection
        current = asyncio.current_task()
        for name in ("_connector", "_runner"):
            if name == "_runner" and getattr(state, "_expecting_disconnect", False):
                # The runner is itself mid-way through disconnecting (kick, close
                # code 1000/4021); cancelling it would skip its registry cleanup
                # and leave our own leave unconfirmed for 30s.
                continue
            task = getattr(state, name, None)
            if task is not None and task is not current and not task.done():
                task.cancel()
                setattr(state, name, None)

    def cleanup(self) -> None:
        # Same private key lookup VoiceProtocol.cleanup() itself does.
        key_id, _ = self.channel._get_voice_client_key()
        current = self.client._connection._get_voice_client(key_id)
        if current is not None and current is not self:
            logger.info(
                f"[{self.channel.guild}] voice: skipped stale client cleanup — "
                f"a newer connection owns this guild"
            )
            return
        super().cleanup()


def _connecting(vc) -> bool:
    state = getattr(vc, "_connection", None)
    return any(
        task is not None and not task.done()
        for task in (
            getattr(state, "_connector", None),
            getattr(state, "_runner", None),
        )
    )


async def settle_voice_client(guild, *, timeout: float = 30):
    """Resolve the guild's voice client to a connected one, or None.

    A registered client that isn't connected is in one of three states, and
    only the last may be torn down:
      - tearing down (a GuardedVoiceClient disconnect is in flight): await it;
      - in progress (discord.py's connector or reconnect runner is alive, e.g.
        another command's join is mid-handshake): wait up to ``timeout`` for it;
      - dead: disconnect it so the guild's slot is free for a new connect().
    Killing an in-progress client instead used to cancel the other command's
    connect, silently dropping its song.
    """
    deadline = time.monotonic() + timeout
    while True:
        vc = guild.voice_client
        if vc is None or vc.is_connected():
            return vc
        teardown = getattr(vc, "_disconnect_task", None)
        if teardown is not None:
            # wait(), not await: a failed teardown must not abort the join.
            await asyncio.wait({teardown})
            if guild.voice_client is vc:
                # Teardown finished but the slot wasn't released; release it.
                vc.cleanup()
            continue
        if _connecting(vc) and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            continue
        logger.info(
            f"[{guild}] voice: clearing dead voice client in '{vc.channel}' before joining"
        )
        await vc.disconnect(force=True)
