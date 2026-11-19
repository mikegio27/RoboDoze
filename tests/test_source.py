"""Tests for stream resolution in cogs.music.source.

Covers the available_at wait (YouTube 403s a googlevideo URL fetched before
the time yt-dlp reports, 2026-11) and create_source's handling of a search
that matches nothing. yt-dlp itself is never called: extract_info is patched.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from cogs.music import source as source_mod
from cogs.music.source import (
    MAX_AVAILABLE_AT_WAIT,
    MusicSource,
    seconds_until_available,
)

NOW = 1_795_062_174.0


class SecondsUntilAvailableTest(unittest.TestCase):
    def test_no_available_at_means_no_wait(self):
        self.assertEqual(seconds_until_available({"url": "u"}, now=NOW), 0.0)
        self.assertEqual(seconds_until_available({"available_at": None}, now=NOW), 0)

    def test_future_timestamp_waits_the_difference(self):
        info = {"available_at": int(NOW) + 4}
        self.assertAlmostEqual(seconds_until_available(info, now=NOW), 4.0)

    def test_past_timestamp_does_not_wait(self):
        info = {"available_at": int(NOW) - 30}
        self.assertEqual(seconds_until_available(info, now=NOW), 0.0)

    def test_wait_is_capped(self):
        info = {"available_at": int(NOW) + 3600}
        self.assertEqual(
            seconds_until_available(info, now=NOW), float(MAX_AVAILABLE_AT_WAIT)
        )

    def test_merged_formats_use_the_latest(self):
        info = {
            "available_at": int(NOW) + 1,
            "requested_formats": [
                {"available_at": int(NOW) + 2},
                {"available_at": int(NOW) + 5},
                {},
            ],
        }
        self.assertAlmostEqual(seconds_until_available(info, now=NOW), 5.0)


def _patch_extract(result):
    return mock.patch.object(
        source_mod.ytdl, "extract_info", mock.Mock(return_value=result)
    )


class FetchStreamInfoTest(unittest.IsolatedAsyncioTestCase):
    async def test_sleeps_until_available(self):
        info = {"url": "https://example/videoplayback", "available_at": 123}
        sleep = mock.AsyncMock()
        with (
            _patch_extract(info),
            mock.patch.object(source_mod, "seconds_until_available", return_value=4.5),
            mock.patch.object(source_mod.asyncio, "sleep", sleep),
        ):
            got = await MusicSource.fetch_stream_info(
                {"webpage_url": "w", "title": "t"}
            )
        self.assertIs(got, info)
        sleep.assert_awaited_once_with(4.5)

    async def test_no_sleep_when_already_available(self):
        sleep = mock.AsyncMock()
        with (
            _patch_extract({"url": "u"}),
            mock.patch.object(source_mod.asyncio, "sleep", sleep),
        ):
            await MusicSource.fetch_stream_info({"webpage_url": "w", "title": "t"})
        sleep.assert_not_awaited()

    async def test_wait_is_cancellable(self):
        """A prefetch cancelled mid-wait must not linger or spawn anything."""
        with (
            _patch_extract({"url": "u"}),
            mock.patch.object(source_mod, "seconds_until_available", return_value=10),
        ):
            task = asyncio.ensure_future(
                MusicSource.fetch_stream_info({"webpage_url": "w", "title": "t"})
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class CreateSourceEmptySearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_search_result_is_no_results_not_a_crash(self):
        ctx = SimpleNamespace(guild="g", author="someone", send=mock.AsyncMock())
        with _patch_extract({"_type": "playlist", "entries": []}):
            got = await MusicSource.create_source(ctx, "rd-playlist https://x")
        self.assertIsNone(got)
        ctx.send.assert_awaited_once()
        self.assertIn("No results", ctx.send.await_args.kwargs["embed"].description)

    async def test_first_non_empty_entry_is_used(self):
        ctx = SimpleNamespace(guild="g", author="someone", send=mock.AsyncMock())
        entry = {"webpage_url": "w", "title": "t", "duration": 3}
        with _patch_extract({"entries": [None, entry]}):
            got = await MusicSource.create_source(ctx, "query")
        self.assertEqual(got["webpage_url"], "w")
        ctx.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
