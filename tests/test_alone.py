"""Tests for has_listeners, the shared "is the bot alone in voice?" check.

Uses plain stand-ins for discord.py's VoiceChannel/Guild/Member: the helper
only touches channel.voice_states, channel.guild.get_member and member.bot.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from cogs.music.player import has_listeners

BOT_ID = 1


def channel(user_ids, cached):
    """A voice channel with voice states for user_ids.

    ``cached`` maps user_id -> is_bot for users present in the member cache;
    anyone missing from it is uncached (get_member returns None).
    """
    members = {
        uid: SimpleNamespace(id=uid, bot=is_bot) for uid, is_bot in cached.items()
    }
    guild = SimpleNamespace(get_member=members.get)
    return SimpleNamespace(
        voice_states={uid: object() for uid in user_ids}, guild=guild
    )


class HasListenersTest(unittest.TestCase):
    def test_empty_channel(self):
        self.assertFalse(has_listeners(channel([], {}), BOT_ID))

    def test_only_self(self):
        self.assertFalse(has_listeners(channel([BOT_ID], {BOT_ID: True}), BOT_ID))

    def test_self_and_cached_human(self):
        ch = channel([BOT_ID, 2], {BOT_ID: True, 2: False})
        self.assertTrue(has_listeners(ch, BOT_ID))

    def test_other_bots_do_not_count(self):
        ch = channel([BOT_ID, 2, 3], {BOT_ID: True, 2: True, 3: True})
        self.assertFalse(has_listeners(ch, BOT_ID))

    def test_uncached_user_counts_as_listener(self):
        # The case channel.members gets wrong: a user with a voice state but
        # no cached Member must still keep the bot connected.
        ch = channel([BOT_ID, 2], {BOT_ID: True})
        self.assertTrue(has_listeners(ch, BOT_ID))

    def test_human_among_bots(self):
        ch = channel([BOT_ID, 2, 3], {BOT_ID: True, 2: True, 3: False})
        self.assertTrue(has_listeners(ch, BOT_ID))


if __name__ == "__main__":
    unittest.main()
