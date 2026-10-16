"""Tests for the voice-lifecycle fixes behind the 2026-10-16 dropout.

Covers GuardedVoiceClient (a stale client's cleanup must not evict a newer
one; disconnect is idempotent and stops discord.py's background tasks),
Music.cleanup (stops the player; a stale player can't tear down a newer
session), MusicPlayer.stop / _wait_for_voice / _hold_until_voice (keep the
queue and wait for a rejoin, never reconnect on its own), and MusicSource's
resume offset. discord.py objects are replaced with stand-ins that implement
only what the code under test touches.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import discord

from cogs.music import player as player_mod
from cogs.music.cog import Music
from cogs.music.player import MusicPlayer, MusicQueue
from cogs.music.source import PCM_BYTES_PER_SECOND, MusicSource
from cogs.music.voice import GuardedVoiceClient, settle_voice_client

GUILD_ID = 42


class FakeConnectionState:
    """The two private ConnectionState methods VoiceProtocol.cleanup() uses."""

    def __init__(self):
        self.voice_clients = {}

    def _get_voice_client(self, guild_id):
        return self.voice_clients.get(guild_id)

    def _remove_voice_client(self, guild_id):
        self.voice_clients.pop(guild_id, None)


def guarded_client(state):
    # Skip VoiceClient.__init__ (it wants a real Client and channel); cleanup()
    # only reads .channel and .client.
    vc = GuardedVoiceClient.__new__(GuardedVoiceClient)
    guild = SimpleNamespace(id=GUILD_ID)
    vc.channel = SimpleNamespace(
        guild=guild, _get_voice_client_key=lambda: (GUILD_ID, "guild_id")
    )
    vc.client = SimpleNamespace(_connection=state)
    return vc


class GuardedVoiceClientTest(unittest.TestCase):
    def test_cleanup_removes_itself(self):
        state = FakeConnectionState()
        vc = guarded_client(state)
        state.voice_clients[GUILD_ID] = vc
        vc.cleanup()
        self.assertNotIn(GUILD_ID, state.voice_clients)

    def test_stale_cleanup_keeps_newer_client(self):
        # The incident: the old client's delayed cleanup() ran after a rejoin
        # had registered a new client for the same guild.
        state = FakeConnectionState()
        old, new = guarded_client(state), guarded_client(state)
        state.voice_clients[GUILD_ID] = new
        old.cleanup()
        self.assertIs(state.voice_clients[GUILD_ID], new)

    def test_cleanup_with_empty_slot_is_harmless(self):
        state = FakeConnectionState()
        guarded_client(state).cleanup()
        self.assertEqual(state.voice_clients, {})


class GuardedDisconnectTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_disconnects_send_one_leave(self):
        vc = guarded_client(FakeConnectionState())
        vc._connection = SimpleNamespace(_connector=None, _runner=None)
        release = asyncio.Event()

        async def slow_disconnect(self, *, force=False):
            await release.wait()

        with mock.patch.object(
            discord.VoiceClient,
            "disconnect",
            autospec=True,
            side_effect=slow_disconnect,
        ) as base:
            first = asyncio.ensure_future(vc.disconnect(force=True))
            second = asyncio.ensure_future(vc.disconnect(force=True))
            await asyncio.sleep(0)
            self.assertFalse(second.done())  # waits for the first, doesn't return early
            release.set()
            await asyncio.gather(first, second)

        base.assert_called_once()

    async def test_disconnect_cancels_background_connect_and_reconnect(self):
        vc = guarded_client(FakeConnectionState())
        connector = asyncio.ensure_future(asyncio.sleep(3600))
        runner = asyncio.ensure_future(asyncio.sleep(3600))
        vc._connection = SimpleNamespace(_connector=connector, _runner=runner)

        with mock.patch.object(discord.VoiceClient, "disconnect", mock.AsyncMock()):
            await vc.disconnect(force=True)
        await asyncio.sleep(0)

        self.assertTrue(connector.cancelled())
        self.assertTrue(runner.cancelled())

    async def test_cancelled_caller_does_not_abort_teardown(self):
        vc = guarded_client(FakeConnectionState())
        vc._connection = SimpleNamespace(_connector=None, _runner=None)
        release = asyncio.Event()
        finished = []

        async def slow_disconnect(self, *, force=False):
            await release.wait()
            finished.append(True)

        with mock.patch.object(
            discord.VoiceClient,
            "disconnect",
            autospec=True,
            side_effect=slow_disconnect,
        ):
            caller = asyncio.ensure_future(vc.disconnect(force=True))
            await asyncio.sleep(0)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            release.set()
            await vc._disconnect_task

        self.assertEqual(finished, [True])


class SettleVoiceClientTest(unittest.IsolatedAsyncioTestCase):
    """connect_ must wait for another command's join, not kill it."""

    def _vc(self, *, connected=False, connector=None):
        vc = mock.Mock(spec=GuardedVoiceClient)
        vc.is_connected.return_value = connected
        vc._disconnect_task = None
        vc.channel = "General"
        vc._connection = SimpleNamespace(_connector=connector, _runner=None)
        vc.disconnect = mock.AsyncMock()
        return vc

    async def test_in_progress_connect_is_waited_for_not_killed(self):
        handshake = asyncio.ensure_future(asyncio.sleep(3600))
        vc = self._vc(connector=handshake)
        guild = SimpleNamespace(voice_client=vc)

        async def finish_handshake():
            await asyncio.sleep(0.01)
            vc.is_connected.return_value = True
            handshake.cancel()

        finishing = asyncio.ensure_future(finish_handshake())
        self.assertIs(await settle_voice_client(guild), vc)
        await finishing
        vc.disconnect.assert_not_awaited()

    async def test_dead_client_is_cleared(self):
        vc = self._vc()
        guild = SimpleNamespace(voice_client=vc)

        async def gone(*, force):
            guild.voice_client = None

        vc.disconnect.side_effect = gone
        self.assertIsNone(await settle_voice_client(guild))
        vc.disconnect.assert_awaited_once_with(force=True)

    async def test_stuck_connect_is_cleared_after_timeout(self):
        vc = self._vc(connector=asyncio.ensure_future(asyncio.sleep(3600)))
        guild = SimpleNamespace(voice_client=vc)

        async def gone(*, force):
            guild.voice_client = None

        vc.disconnect.side_effect = gone
        self.assertIsNone(await settle_voice_client(guild, timeout=0.01))
        vc._connection._connector.cancel()

    async def test_failed_teardown_does_not_abort(self):
        vc = self._vc()
        failed = asyncio.get_running_loop().create_future()
        failed.set_exception(RuntimeError("boom"))
        # GuardedVoiceClient's done-callback retrieves this in real use.
        failed.add_done_callback(lambda f: f.exception())
        vc._disconnect_task = failed
        guild = SimpleNamespace(voice_client=vc)
        vc.cleanup.side_effect = lambda: setattr(guild, "voice_client", None)

        self.assertIsNone(await settle_voice_client(guild))
        vc.cleanup.assert_called_once_with()


def fake_guild(vc=None):
    return SimpleNamespace(id=GUILD_ID, voice_client=vc)


def fake_player():
    return mock.Mock(spec=MusicPlayer)


class MusicCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_stops_player_and_disconnects(self):
        cog = Music(bot=None)
        vc = mock.Mock(disconnect=mock.AsyncMock())
        guild = fake_guild(vc)
        player = fake_player()
        cog.players[GUILD_ID] = player

        await cog.cleanup(guild)

        player.stop.assert_called_once_with()
        vc.disconnect.assert_awaited_once()
        self.assertNotIn(GUILD_ID, cog.players)

    async def test_stale_player_cannot_tear_down_newer_session(self):
        cog = Music(bot=None)
        vc = mock.Mock(disconnect=mock.AsyncMock())
        guild = fake_guild(vc)
        stale, current = fake_player(), fake_player()
        cog.players[GUILD_ID] = current

        await cog.cleanup(guild, player=stale)

        stale.stop.assert_called_once_with()
        current.stop.assert_not_called()
        vc.disconnect.assert_not_awaited()
        self.assertIs(cog.players[GUILD_ID], current)

    async def test_destroy_after_cleanup_does_not_disconnect_again(self):
        # leave_ -> cleanup(), then the player's own destroy() lands late: the
        # second call must not issue a second disconnect (that early registry
        # pop is what lost Discord's leave confirmation in prod).
        cog = Music(bot=None)
        vc = mock.Mock(disconnect=mock.AsyncMock())
        guild = fake_guild(vc)
        player = fake_player()
        cog.players[GUILD_ID] = player

        await cog.cleanup(guild)
        await cog.cleanup(guild, player=player)

        vc.disconnect.assert_awaited_once()


def bare_player(guild, *, text_channel=None):
    """A MusicPlayer with its loop not started; only voice-related state set."""
    p = MusicPlayer.__new__(MusicPlayer)
    p._guild = guild
    p._channel = text_channel or mock.Mock(send=mock.AsyncMock())
    p._prefetch_task = None
    p._prefetched = None
    p._alone_task = None
    p.queue = MusicQueue(maxsize=10)
    return p


def connected_vc(channel):
    return mock.Mock(channel=channel, is_connected=mock.Mock(return_value=True))


class PlayerStopTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_loop_and_timers(self):
        p = bare_player(fake_guild())
        p._loop_task = asyncio.ensure_future(asyncio.sleep(3600))
        p._alone_task = asyncio.ensure_future(asyncio.sleep(3600))
        p._prefetch_task = asyncio.ensure_future(asyncio.sleep(3600))
        loop_task, alone_task, prefetch_task = (
            p._loop_task,
            p._alone_task,
            p._prefetch_task,
        )

        p.stop()
        await asyncio.sleep(0)

        self.assertTrue(loop_task.cancelled())
        self.assertTrue(alone_task.cancelled())
        self.assertTrue(prefetch_task.cancelled())
        self.assertIsNone(p._alone_task)
        self.assertIsNone(p._prefetch_task)


def disconnected_vc():
    return mock.Mock(is_connected=mock.Mock(return_value=False))


FAST = {
    "VOICE_POLL_INTERVAL": 0.001,
    "VOICE_SETTLE_TIMEOUT": 0,
    "VOICE_WAIT_TIMEOUT": 0.05,
}


class WaitForVoiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.multiple(player_mod, **FAST)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_connected_returns_at_once_without_a_message(self):
        p = bare_player(fake_guild(connected_vc(None)))
        self.assertTrue(await p._wait_for_voice())
        p._channel.send.assert_not_awaited()

    async def test_rejoin_ends_the_wait(self):
        guild = fake_guild(None)
        p = bare_player(guild)

        async def rejoin():
            await asyncio.sleep(0.01)
            guild.voice_client = connected_vc(None)

        rejoined = asyncio.ensure_future(rejoin())
        self.assertTrue(await p._wait_for_voice())
        await rejoined
        p._channel.send.assert_awaited_once()  # the "lost the voice connection" notice

    async def test_gives_up_after_timeout(self):
        p = bare_player(fake_guild(disconnected_vc()))
        self.assertFalse(await p._wait_for_voice())

    async def test_never_connects_on_its_own(self):
        # The old design reconnected from the player and raced user commands.
        vc = disconnected_vc()
        vc.channel = mock.Mock(connect=mock.AsyncMock())
        p = bare_player(fake_guild(vc))
        await p._wait_for_voice()
        vc.channel.connect.assert_not_awaited()

    async def test_short_gap_is_not_announced(self):
        guild = fake_guild(disconnected_vc())
        p = bare_player(guild)

        async def resume():
            await asyncio.sleep(0.005)
            guild.voice_client = connected_vc(None)

        with mock.patch.object(player_mod, "VOICE_SETTLE_TIMEOUT", 10):
            resumed = asyncio.ensure_future(resume())
            self.assertTrue(await p._wait_for_voice())
            await resumed
        p._channel.send.assert_not_awaited()


class HoldUntilVoiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.multiple(player_mod, **FAST)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_item_goes_back_to_the_front(self):
        guild = fake_guild(None)
        p = bare_player(guild)
        p.queue.put_nowait({"title": "next"})

        async def rejoin():
            await asyncio.sleep(0.01)
            guild.voice_client = connected_vc(None)

        rejoined = asyncio.ensure_future(rejoin())
        self.assertTrue(await p._hold_until_voice({"title": "cut", "resume_at": 61.0}))
        await rejoined
        self.assertEqual([i["title"] for i in p.queue.snapshot()], ["cut", "next"])
        self.assertEqual(p.queue.snapshot()[0]["resume_at"], 61.0)

    async def test_gives_up_and_destroys(self):
        guild = fake_guild(None)
        p = bare_player(guild)
        with mock.patch.object(MusicPlayer, "destroy") as destroy:
            self.assertFalse(await p._hold_until_voice({"title": "cut"}))
        destroy.assert_called_once_with(guild)


class SilentSource(discord.AudioSource):
    def read(self):
        return b"\0" * 3840

    def is_opus(self):
        return False


class ResumeOffsetTest(unittest.TestCase):
    def _make(self, info, start_at):
        with mock.patch("discord.FFmpegPCMAudio", return_value=SilentSource()) as ff:
            src = MusicSource.from_stream_info(info, requester=None, start_at=start_at)
        return src, ff.call_args.kwargs["before_options"]

    def test_resume_seeks_input(self):
        src, before = self._make({"url": "u", "title": "t"}, 754.3)
        self.assertTrue(before.endswith(" -ss 754.3"))
        self.assertEqual(src.start_at, 754.3)

    def test_fresh_play_does_not_seek(self):
        _, before = self._make({"url": "u", "title": "t"}, 0)
        self.assertNotIn("-ss", before)

    def test_live_stream_never_seeks(self):
        src, before = self._make({"url": "u", "title": "t", "is_live": True}, 60)
        self.assertNotIn("-ss", before)
        self.assertEqual(src.start_at, 0)

    def test_position_counts_streamed_audio(self):
        src, _ = self._make({"url": "u", "title": "t"}, 10)
        for _ in range(50):  # 50 x 20ms frames = 1s
            src.read()
        self.assertAlmostEqual(src.position, 11.0)
        self.assertEqual(3840 * 50, PCM_BYTES_PER_SECOND)


if __name__ == "__main__":
    unittest.main()
