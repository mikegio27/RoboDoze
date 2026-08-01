"""Tests for the music queue and loop-mode re-queueing.

Run with:  PYTHONPATH=bot python3 -m unittest discover -s tests -v

Scope is deliberately narrow: MusicQueue and requeue_finished are pure,
deterministic logic with no Discord or network I/O, and they hold the subtle
invariants (ordering, maxsize overflow, requester preservation). The player
loop and command handlers are not covered here — they need extensive
discord.py voice/ffmpeg mocking for very little return.
"""

import asyncio
import sys
import unittest
from pathlib import Path

# The runtime puts bot/ on sys.path (Dockerfile: PYTHONPATH=/app/bot).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from cogs.music.player import (
    LOOP_OFF,
    LOOP_QUEUE,
    LOOP_TRACK,
    MusicQueue,
    requeue_finished,
)
from cogs.music.source import MAX_QUEUE_SIZE


def track(title):
    """A queue item in the shape create_source(download=False) produces."""
    return {
        "webpage_url": f"https://example.invalid/{title}",
        "requester": f"user-{title}",
        "title": title,
        "thumbnail": None,
        "duration": 10,
        "is_live": False,
    }


def titles(queue):
    return [item["title"] for item in queue.snapshot()]


def make_queue(*names):
    queue = MusicQueue(maxsize=MAX_QUEUE_SIZE)
    for name in names:
        queue.put_nowait(track(name))
    return queue


class TestMusicQueue(unittest.TestCase):
    def test_snapshot_does_not_consume(self):
        queue = make_queue("A", "B")
        self.assertEqual(titles(queue), ["A", "B"])
        self.assertEqual(queue.qsize(), 2)

    def test_insert_front(self):
        queue = make_queue("A", "B")
        queue.insert_front(track("Z"))
        self.assertEqual(titles(queue), ["Z", "A", "B"])

    def test_append_back(self):
        queue = make_queue("A", "B")
        queue.append_back(track("Z"))
        self.assertEqual(titles(queue), ["A", "B", "Z"])

    def test_clear_all(self):
        queue = make_queue("A", "B", "C")
        queue.clear_all()
        self.assertEqual(titles(queue), [])
        self.assertTrue(queue.empty())

    def test_remove_at_and_remove_last(self):
        queue = make_queue("A", "B", "C")
        self.assertEqual(queue.remove_at(1)["title"], "B")
        self.assertEqual(titles(queue), ["A", "C"])
        self.assertEqual(queue.remove_last()["title"], "C")
        self.assertEqual(titles(queue), ["A"])

    def test_remove_at_out_of_range_raises(self):
        queue = make_queue("A")
        with self.assertRaises(IndexError):
            queue.remove_at(5)

    def test_shuffle_preserves_membership(self):
        queue = make_queue(*[str(i) for i in range(20)])
        before = sorted(titles(queue))
        queue.shuffle()
        self.assertEqual(sorted(titles(queue)), before)
        self.assertEqual(queue.qsize(), 20)


class TestQueueOverflow(unittest.TestCase):
    """append_back must never drop a looped track, even at maxsize."""

    def _full_queue(self):
        return make_queue(*[f"t{i}" for i in range(MAX_QUEUE_SIZE)])

    def test_put_nowait_rejects_when_full(self):
        queue = self._full_queue()
        self.assertTrue(queue.full())
        with self.assertRaises(asyncio.QueueFull):
            queue.put_nowait(track("extra"))

    def test_append_back_overflows_by_one_rather_than_dropping(self):
        queue = self._full_queue()
        queue.append_back(track("looped"))
        self.assertEqual(queue.qsize(), MAX_QUEUE_SIZE + 1)
        self.assertEqual(titles(queue)[-1], "looped")

    def test_remove_at_survives_the_overflowed_state(self):
        # remove_at rebuilds via put_nowait; maxsize + 1 is the hard ceiling and
        # must not lose items on the way back in.
        queue = self._full_queue()
        queue.append_back(track("looped"))
        removed = queue.remove_at(0)
        self.assertEqual(removed["title"], "t0")
        self.assertEqual(queue.qsize(), MAX_QUEUE_SIZE)
        self.assertEqual(titles(queue)[0], "t1")
        self.assertEqual(titles(queue)[-1], "looped")


class TestRequeueFinished(unittest.TestCase):
    def test_loop_off_drops_the_track(self):
        queue = make_queue("B")
        self.assertFalse(requeue_finished(queue, track("A"), LOOP_OFF))
        self.assertEqual(titles(queue), ["B"])

    def test_track_loop_goes_to_front(self):
        queue = make_queue("B")
        self.assertTrue(requeue_finished(queue, track("A"), LOOP_TRACK))
        self.assertEqual(titles(queue), ["A", "B"])

    def test_queue_loop_goes_to_back(self):
        queue = make_queue("B")
        self.assertTrue(requeue_finished(queue, track("A"), LOOP_QUEUE))
        self.assertEqual(titles(queue), ["B", "A"])

    def test_no_current_track_is_a_noop(self):
        queue = make_queue("B")
        self.assertFalse(requeue_finished(queue, None, LOOP_QUEUE))
        self.assertEqual(titles(queue), ["B"])

    def test_unknown_mode_does_not_requeue(self):
        queue = make_queue("B")
        self.assertFalse(requeue_finished(queue, track("A"), "bogus"))
        self.assertEqual(titles(queue), ["B"])


class TestRotation(unittest.TestCase):
    """Drive the real dequeue/re-queue cycle the player loop performs."""

    def _cycle(self, queue, current, mode, rounds):
        played = []
        for _ in range(rounds):
            played.append(current["title"])
            requeue_finished(queue, current, mode)
            current = queue.get_nowait()
        return played, current

    def test_queue_loop_cycles_indefinitely(self):
        queue = make_queue("B", "C")
        played, _ = self._cycle(queue, track("A"), LOOP_QUEUE, 7)
        self.assertEqual(played, list("ABCABCA"))

    def test_queue_loop_never_drains(self):
        queue = make_queue("B", "C")
        _, _ = self._cycle(queue, track("A"), LOOP_QUEUE, 10)
        self.assertEqual(queue.qsize(), 2)

    def test_track_loop_repeats_and_keeps_rest_pending(self):
        queue = make_queue("B")
        played, _ = self._cycle(queue, track("A"), LOOP_TRACK, 3)
        self.assertEqual(played, list("AAA"))
        self.assertEqual(titles(queue), ["B"])

    def test_queue_loop_with_empty_queue_repeats_single_track(self):
        queue = make_queue()
        played, _ = self._cycle(queue, track("A"), LOOP_QUEUE, 3)
        self.assertEqual(played, list("AAA"))

    def test_requester_survives_rotation(self):
        # regather_stream reads data['requester'] with no fallback, so losing it
        # would raise KeyError on every wrap.
        queue = make_queue("B")
        _, current = self._cycle(queue, track("A"), LOOP_QUEUE, 4)
        self.assertEqual(current["requester"], f"user-{current['title']}")

    def test_clear_under_queue_loop_leaves_queue_empty(self):
        # cog.clear_ empties the queue and drops _current_raw under LOOP_QUEUE,
        # so the playing track must not resurrect at the back.
        queue = make_queue("B", "C")
        queue.clear_all()
        requeue_finished(queue, None, LOOP_QUEUE)
        self.assertEqual(titles(queue), [])


if __name__ == "__main__":
    unittest.main()
