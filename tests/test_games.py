"""Tests for rd-games / rd-game: formatting, name resolution, the guild check,
and the commands against a local fake of lobby's bot API."""

import asyncio
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from cogs.games import cog as games_cog
from cogs.games.client import Caller, Lobby, LobbyError, explain
from cogs.games.cog import ConfirmView, Games, NotInGamingGuild, guild_ids
from cogs.games.format import (
    GUILD_ONLY,
    detail_text,
    list_text,
    parse_time,
    players_text,
    resolve,
    start_progress,
)

DOZYNET, HCH = 1554708052593348729, 385602741101264896
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def server(name, display=None, phase="Running", players=None, **extra):
    state = {"Archived": "Archived", "Stopped": "Stopped"}.get(phase, "Running")
    s = {
        "name": name,
        "displayName": display or name.title(),
        "game": {"displayName": "Minecraft", "icon": "minecraft"},
        "state": state,
        "phase": phase,
        "address": f"games.dozydelta.com:{25565 + len(name)}",
        "players": players or {"known": True, "online": 0, "names": []},
        "idle": {"minutes": 60, "never": False},
        "host": "dozy-games",
        "hostDisplayName": "Dozy Games",
        "can": {"start": True, "stop": True},
    }
    s.update(extra)
    return s


class FormatTests(unittest.TestCase):
    def test_players(self):
        self.assertEqual(
            players_text(
                server("a", players={"known": True, "online": 2, "names": ["al", "bo"]})
            ),
            "al, bo",
        )
        self.assertEqual(
            players_text(
                server(
                    "a", players={"known": True, "online": 3, "max": 10, "names": []}
                )
            ),
            "3/10 on",
        )
        self.assertEqual(
            players_text(server("a", players={"known": False, "online": 0})),
            "players unknown",
        )
        self.assertEqual(players_text(server("a")), "nobody on")
        # Markdown in names is escaped.
        self.assertEqual(
            players_text(
                server("a", players={"known": True, "online": 1, "names": ["x_y_"]})
            ),
            r"x\_y\_",
        )

    def test_list(self):
        text = list_text(
            [
                server(
                    "mc",
                    "Survival",
                    players={"known": True, "online": 1, "names": ["al"]},
                    connect="mc.dozydelta.com",
                ),
                server("val", "Valheim", phase="Stopped"),
                server("sat", "Satisfactory", players={"known": False, "online": 0}),
                server("old", "Old World", phase="Archived"),
                server("older", "Older", phase="Archived"),
            ]
        )
        lines = text.splitlines()
        self.assertEqual(
            lines[0], "🟢 **Survival** · Minecraft · al · `mc.dozydelta.com`"
        )
        self.assertEqual(lines[1], "⚫ **Valheim** · Minecraft · off")
        self.assertIn("players unknown", lines[2])
        self.assertIn("`games.dozydelta.com:", lines[2])  # no connect: the address
        self.assertTrue(lines[3].startswith("📦 Archived: **Old World**, **Older**"))
        self.assertIn("https://home.dozydelta.com/games", lines[-1])
        self.assertEqual(len(lines), 5)
        self.assertIn("Make one at", list_text([]))

    def test_list_fits_in_one_message(self):
        text = list_text([server(f"s{i:03d}", "x" * 60) for i in range(100)])
        self.assertLessEqual(len(text), 2000)
        self.assertIn("more", text)

    def test_detail(self):
        s = server(
            "mc",
            "Survival",
            players={"known": True, "online": 0, "max": 20, "names": []},
            idle={
                "minutes": 60,
                "never": False,
                "since": iso(NOW - timedelta(minutes=37)),
                "stopsAt": iso(NOW + timedelta(minutes=23)),
            },
            lastPlayedAt=iso(NOW - timedelta(hours=3)),
        )
        text = detail_text(s, NOW)
        self.assertIn("🟢 **Survival** · Minecraft · on Dozy Games", text)
        self.assertIn("Players: 0/20", text)
        self.assertIn("Idle: nobody's on: stops in 23 min", text)
        self.assertIn("Last played: 3 h ago", text)
        s["idle"] = {"minutes": 60, "never": True}
        self.assertIn("never stops on its own", detail_text(s, NOW))
        stopped = server("v", phase="Stopped", message="stopped: idle")
        self.assertIn("Status: off (stopped: idle)", detail_text(stopped, NOW))

    def test_parse_go_time(self):
        self.assertEqual(
            parse_time("2026-10-17T12:00:00.123456789Z"),
            datetime(2026, 10, 17, 12, 0, 0, 123456, tzinfo=UTC),
        )
        self.assertIsNone(parse_time("nope"))
        self.assertIsNone(parse_time(None))

    def test_start_progress(self):
        self.assertEqual(
            start_progress(server("mc", "Survival", connect="mc.example")),
            ("**Survival** is up — `mc.example`", "up"),
        )
        text, outcome = start_progress(server("mc", phase="Provisioning"))
        self.assertIsNone(outcome)
        self.assertTrue(text.startswith("Starting **Mc**…"))
        self.assertEqual(
            start_progress(server("mc", phase="Failed", message="image not found")),
            ("**Mc** didn't start: image not found", "failed"),
        )
        self.assertEqual(start_progress(server("mc", phase="Stopped"))[1], "stopped")

    def test_resolve(self):
        servers = [
            server("mc-survival", "Survival"),
            server("mc-creative", "Creative"),
            server("valheim", "Viking Time"),
            server("Valheim2", "Other"),
        ]
        self.assertEqual(resolve(servers, "valheim").server["name"], "valheim")
        self.assertEqual(resolve(servers, "VALHEIM2").server["name"], "Valheim2")
        self.assertEqual(resolve(servers, "viking time").server["name"], "valheim")
        self.assertEqual(resolve(servers, "surv").server["name"], "mc-survival")
        self.assertEqual(resolve(servers, "Cre").server["name"], "mc-creative")
        amb = resolve(servers, "mc")
        self.assertIsNone(amb.server)
        self.assertEqual(
            {s["name"] for s in amb.candidates}, {"mc-survival", "mc-creative"}
        )
        miss = resolve(servers, "valhiem")
        self.assertIsNone(miss.server)
        self.assertEqual(miss.candidates, [])
        self.assertIn("valheim", [s["name"] for s in miss.suggestions])
        self.assertIsNone(resolve(servers, "  ").server)


class GuildCheckTests(unittest.IsolatedAsyncioTestCase):
    def ctx(self, guild):
        return SimpleNamespace(
            guild=SimpleNamespace(id=guild) if guild else None,
            author=SimpleNamespace(id=1, display_name="x"),
        )

    async def test_only_the_gaming_guilds(self):
        cog = Games(None, Lobby("http://127.0.0.1:1", "t"), guild_ids(None))  # pyright: ignore
        self.assertTrue(await cog.cog_check(self.ctx(DOZYNET)))
        self.assertTrue(await cog.cog_check(self.ctx(HCH)))
        for ctx in (self.ctx(123), self.ctx(None)):
            with self.assertRaises(NotInGamingGuild) as cm:
                await cog.cog_check(ctx)
            self.assertEqual(str(cm.exception), GUILD_ONLY)

    async def test_denied_reply(self):
        cog = Games(None, Lobby("http://127.0.0.1:1", "t"), guild_ids(None))  # pyright: ignore
        ctx = self.ctx(123)
        ctx.command = SimpleNamespace(name="start")
        ctx.reply = AsyncMock()
        await cog.cog_command_error(ctx, NotInGamingGuild(GUILD_ONLY))
        self.assertEqual(ctx.reply.await_args.args[0], GUILD_ONLY)

    def test_guild_ids(self):
        self.assertEqual(guild_ids(None), {DOZYNET, HCH})
        self.assertEqual(guild_ids(""), {DOZYNET, HCH})
        self.assertEqual(guild_ids("7, 8,junk"), {7, 8})

    async def test_setup_reads_env(self):
        bot = SimpleNamespace(add_cog=AsyncMock())
        with patch.dict(os.environ, {"GAMES_GUILD_IDS": "7"}, clear=False):
            os.environ.pop("LOBBY_BOT_TOKEN", None)
            await games_cog.setup(bot)  # pyright: ignore
            bot.add_cog.assert_not_awaited()
            os.environ["LOBBY_BOT_TOKEN"] = "tok"
            await games_cog.setup(bot)  # pyright: ignore
        cog = bot.add_cog.await_args.args[0]
        self.assertEqual(cog.guilds, {7})
        self.assertEqual(cog.lobby.url, games_cog.DEFAULT_URL)
        await cog.lobby.close()


class FakeLobby:
    """lobby's /bot API, enough for the commands."""

    def __init__(self):
        self.seen = []  # (method, path, body, headers)
        self.servers = {
            "mc": server(
                "mc",
                "Survival",
                players={"known": True, "online": 2, "names": ["al", "bo"]},
                connect="mc.dozydelta.com",
            ),
            "val": server("val", "Valheim", phase="Stopped"),
            "sat": server("sat", "Satisfactory", phase="Stopped"),
        }
        self.phases = {}  # name -> phases the next GETs walk through
        self.start_code = {}  # name -> 409 code for POST start

    def app(self):
        app = web.Application()
        app.router.add_get("/bot/servers", self.list)
        app.router.add_get("/bot/servers/{name}", self.get)
        app.router.add_post("/bot/servers/{name}/{action}", self.act)
        return app

    def problem(self, status, code, **extra):
        return web.json_response(
            {"status": status, "title": "x", "detail": f"{code}!", "code": code}
            | extra,
            status=status,
            content_type="application/problem+json",
        )

    async def auth(self, request):
        body = await request.json() if request.can_read_body else None
        self.seen.append((request.method, request.path, body, dict(request.headers)))
        if request.headers.get("Authorization") != "Bearer tok":
            return self.problem(401, "unauthorized")
        if request.headers.get("X-Discord-Guild-Id") not in (str(DOZYNET), str(HCH)):
            return self.problem(403, "guild_not_allowed")
        return None

    async def list(self, request):
        if err := await self.auth(request):
            return err
        return web.json_response({"items": list(self.servers.values())})

    async def get(self, request):
        if err := await self.auth(request):
            return err
        name = request.match_info["name"]
        if name not in self.servers:
            return self.problem(404, "not_found")
        if self.phases.get(name):
            phase, message = self.phases[name].pop(0)
            self.servers[name].update(phase=phase, message=message)
        return web.json_response(self.servers[name])

    async def act(self, request):
        if err := await self.auth(request):
            return err
        name, action = request.match_info["name"], request.match_info["action"]
        s = self.servers.get(name)
        if s is None:
            return self.problem(404, "not_found")
        if action == "start":
            if code := self.start_code.get(name):
                return self.problem(409, code)
            s.update(phase="Pending", state="Running", message="")
            return web.json_response(s)
        body = self.seen[-1][2] or {}
        online = s["players"]
        if online.get("online") and not body.get("force"):
            return self.problem(409, "players_online", players=online["names"])
        s.update(phase="Stopping")
        return web.json_response(s)


class LobbyTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeLobby()
        self.runner = web.AppRunner(self.fake.app())
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # pyright: ignore
        self.url = f"http://127.0.0.1:{port}"
        self.lobby = Lobby(self.url, "tok")

    async def asyncTearDown(self):
        await self.lobby.close()
        await self.runner.cleanup()


class ClientTests(LobbyTestCase):
    async def test_headers(self):
        items = await self.lobby.servers(Caller(DOZYNET, 42, "Mike ✨\n"))
        self.assertEqual(len(items), 3)
        _, path, _, headers = self.fake.seen[0]
        self.assertEqual(path, "/bot/servers")
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(headers["X-Discord-Guild-Id"], str(DOZYNET))
        self.assertEqual(headers["X-Discord-User-Id"], "42")
        # Sent as raw UTF-8, control characters dropped.
        self.assertEqual(headers["X-Discord-User-Name"], "Mike ✨")

    async def test_stop_body_and_players_online(self):
        who = Caller(HCH, 1, "a")
        with self.assertRaises(LobbyError) as cm:
            await self.lobby.stop("mc", who)
        self.assertEqual(cm.exception.code, "players_online")
        self.assertEqual(cm.exception.players, ["al", "bo"])
        self.assertEqual(self.fake.seen[-1][2], {"force": False})
        done = await self.lobby.restart("mc", who, force=True)
        self.assertEqual(done["phase"], "Stopping")
        self.assertEqual(self.fake.seen[-1][1], "/bot/servers/mc/restart")
        self.assertEqual(self.fake.seen[-1][2], {"force": True})

    async def test_errors(self):
        with self.assertRaises(LobbyError) as cm:
            await self.lobby.servers(Caller(123, 1, "a"))
        self.assertEqual(str(cm.exception), GUILD_ONLY)
        wrong = Lobby(self.url, "wrong")
        with self.assertRaises(LobbyError) as cm:
            await wrong.servers(Caller(DOZYNET, 1, "a"))
        await wrong.close()
        self.assertIn("token", str(cm.exception))
        with self.assertRaises(LobbyError) as cm:
            await self.lobby.server("nope", Caller(DOZYNET, 1, "a"))
        self.assertEqual(cm.exception.status, 404)
        down = Lobby("http://127.0.0.1:1", "tok")
        with self.assertRaises(LobbyError) as cm:
            await down.servers(Caller(DOZYNET, 1, "a"))
        await down.close()
        self.assertIn("isn't reachable", str(cm.exception))
        self.assertIn("no room", explain(409, "host_full", "x"))
        self.assertIn("already running", explain(409, "already_running", ""))
        self.assertIn("try again", explain(502, "", ""))
        self.assertIn("said no: bad", explain(422, "", "bad"))


class FakeMessage:
    def __init__(self, n):
        self.jump_url = f"https://discord.com/channels/1/2/{n}"
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeCtx:
    def __init__(self, guild=DOZYNET, user=42):
        self.guild = SimpleNamespace(id=guild)
        self.author = SimpleNamespace(id=user, display_name="Mike")
        self.replies = []  # (text, kwargs, message)

    async def reply(self, text, **kwargs):
        msg = FakeMessage(len(self.replies))
        self.replies.append((text, kwargs, msg))
        return msg

    @property
    def last(self):
        return self.replies[-1][0]


def interaction(user=42):
    return SimpleNamespace(
        user=SimpleNamespace(id=user),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


class CogTests(LobbyTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        bot = SimpleNamespace(command_prefix="rd-")
        self.cog = Games(bot, self.lobby, guild_ids(None), poll_interval=0.01)  # pyright: ignore

    async def asyncTearDown(self):
        for w in list(self.cog._starts.values()):
            w.task.cancel()
        await super().asyncTearDown()

    async def test_list_and_show(self):
        ctx = FakeCtx()
        await self.cog.do_list(ctx)
        self.assertIn("**Survival** · Minecraft · al, bo", ctx.last)
        await self.cog.do_show(ctx, "survival")
        self.assertIn("Players: al, bo", ctx.last)
        self.assertIn("Address: `mc.dozydelta.com`", ctx.last)
        self.assertEqual(self.fake.seen[-1][1], "/bot/servers/mc")
        await self.cog.do_show(ctx, "")
        self.assertIn("Which server?", ctx.last)

    async def test_unknown_and_ambiguous(self):
        ctx = FakeCtx()
        await self.cog.do_start(ctx, "valhiem")
        self.assertIn("no game server called `valhiem`", ctx.last)
        self.assertIn("`val` (Valheim)", ctx.last)
        self.fake.servers["sat2"] = server("sat2", "Satisfactory Two")
        await self.cog.do_start(ctx, "satisf")
        self.assertIn("could be", ctx.last)
        self.assertFalse([s for s in self.fake.seen if s[0] == "POST"])

    async def test_stop_asks_when_people_are_playing(self):
        ctx = FakeCtx()
        view = await self.cog.do_stop(ctx, "mc", "stop")
        self.assertIsInstance(view, ConfirmView)
        text, kwargs, _ = ctx.replies[-1]
        self.assertEqual(
            text,
            "2 people are playing: al, bo. Stopping **Survival** saves the world and kicks them.",
        )
        self.assertIs(kwargs["view"], view)
        self.assertEqual(view.confirm.label, "stop anyway")
        # Someone else can't press it.
        other = interaction(user=7)
        self.assertFalse(await view.interaction_check(other))  # pyright: ignore
        other.response.send_message.assert_awaited()
        self.assertTrue(await view.interaction_check(interaction()))  # pyright: ignore
        # The invoker confirms: the stop is repeated with force.
        press = interaction()
        await view.confirm.callback(press)  # pyright: ignore
        self.assertEqual(
            self.fake.seen[-1][1:3], ("/bot/servers/mc/stop", {"force": True})
        )
        press.response.edit_message.assert_awaited()
        final = press.edit_original_response.await_args.kwargs
        self.assertIn("Stopping **Survival**", final["content"])
        self.assertTrue(all(b.disabled for b in view.children))  # pyright: ignore

    async def test_keep_it_running(self):
        ctx = FakeCtx()
        view = await self.cog.do_stop(ctx, "mc", "restart")
        assert view is not None
        self.assertEqual(view.confirm.label, "restart anyway")
        posts = len([s for s in self.fake.seen if s[0] == "POST"])
        press = interaction()
        await view.keep.callback(press)  # pyright: ignore
        self.assertIn(
            "keeps running", press.response.edit_message.await_args.kwargs["content"]
        )
        self.assertEqual(len([s for s in self.fake.seen if s[0] == "POST"]), posts)

    async def test_stop_nobody_on(self):
        ctx = FakeCtx()
        self.assertIsNone(await self.cog.do_stop(ctx, "sat", "stop"))
        self.assertIn("Stopping **Satisfactory**", ctx.last)
        self.assertEqual(self.fake.seen[-1][2], {"force": False})

    async def test_start_until_running(self):
        self.fake.servers["val"]["connect"] = "val.dozydelta.com:2456"
        self.fake.phases["val"] = [
            ("Provisioning", ""),
            ("Provisioning", ""),
            ("Starting", "loading world"),
            ("Running", ""),
        ]
        ctx = FakeCtx()
        await self.cog.do_start(ctx, "Valheim")
        self.assertTrue(ctx.last.startswith("Starting **Valheim**…"))
        first = ctx.replies[-1][2]
        # A second start while it's pending points at the first message.
        await self.cog.do_start(ctx, "val")
        self.assertIn(first.jump_url, ctx.last)
        await asyncio.wait_for(self.cog._starts["val"].task, 5)
        contents = [e["content"] for e in first.edits]
        self.assertEqual(contents[-1], "**Valheim** is up — `val.dozydelta.com:2456`")
        self.assertIn("Starting **Valheim**… loading world", contents)
        self.assertEqual(len(contents), len(set(contents)))  # edits only on change
        self.assertEqual(len([s for s in self.fake.seen if s[1].endswith("/start")]), 1)
        self.assertNotIn("val", self.cog._starts)

    async def test_start_failed(self):
        self.fake.phases["sat"] = [("Starting", ""), ("Failed", "out of memory")]
        ctx = FakeCtx()
        await self.cog.do_start(ctx, "sat")
        msg = ctx.replies[-1][2]
        await asyncio.wait_for(self.cog._starts["sat"].task, 5)
        self.assertEqual(
            msg.edits[-1]["content"], "**Satisfactory** didn't start: out of memory"
        )

    async def test_start_gives_up(self):
        self.cog.start_timeout = 0.05
        self.fake.phases["sat"] = [("Provisioning", "")] * 100
        ctx = FakeCtx()
        await self.cog.do_start(ctx, "sat")
        msg = ctx.replies[-1][2]
        await asyncio.wait_for(self.cog._starts["sat"].task, 5)
        self.assertIn("still starting", msg.edits[-1]["content"])

    async def test_start_errors(self):
        ctx = FakeCtx()
        self.fake.start_code["val"] = "host_full"
        await self.cog.do_start(ctx, "val")
        self.assertIn("no room", ctx.last)
        self.fake.start_code["mc"] = "already_running"
        await self.cog.do_start(ctx, "mc")
        self.assertEqual(ctx.last, "**Survival** is already up — `mc.dozydelta.com`")
        down = Lobby("http://127.0.0.1:1", "tok")
        await Games(None, down, guild_ids(None)).do_list(ctx)  # pyright: ignore
        await down.close()
        self.assertIn("isn't reachable", ctx.last)


if __name__ == "__main__":
    unittest.main()
