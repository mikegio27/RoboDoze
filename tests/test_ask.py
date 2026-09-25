"""Tests for rd-ask: building the conversation, fitting answers into Discord
messages, and talking to dozai (against a local fake of its /v1 API)."""

import sys
import unittest
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from cogs.ask.client import AskError, Dozai, explain
from cogs.ask.format import (
    Turn,
    answer_chunks,
    split_message,
    strip_command,
    strip_footer,
    to_messages,
)


class FormatTests(unittest.TestCase):
    def test_strip_command(self):
        self.assertEqual(strip_command("rd-ask  what is DNS?", "rd-"), "what is DNS?")
        self.assertEqual(strip_command("RD-ASK\nline two", "rd-"), "line two")
        self.assertEqual(strip_command("a follow-up", "rd-"), "a follow-up")

    def test_strip_footer(self):
        text = "DNS maps names [1].\n-# [1] Wiki <https://x>\n-# dozai · qwen3.5:9b"
        self.assertEqual(strip_footer(text), "DNS maps names [1].")

    def test_to_messages_merges_split_answers_and_limits_images(self):
        turns = [
            Turn("user", "look", ["data:a", "data:b", "data:c"]),
            Turn("assistant", "part 1"),
            Turn("assistant", "part 2"),
            Turn("user", "and these?", ["data:d", "data:e"]),
        ]
        msgs = to_messages(turns, limit=4)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(msgs[1]["content"], "part 1\n\npart 2")
        # Newest images first: both of the follow-up's, then 2 of the first 3.
        self.assertEqual(len(msgs[2]["content"]) - 1, 2)
        self.assertEqual(len(msgs[0]["content"]) - 1, 2)
        self.assertEqual(msgs[0]["content"][0], {"type": "text", "text": "look"})

    def test_plain_text_when_no_images(self):
        self.assertEqual(
            to_messages([Turn("user", "hi")]), [{"role": "user", "content": "hi"}]
        )

    def test_split_keeps_code_fences_balanced(self):
        code = "```python\n" + "\n".join(f"print({i})" for i in range(400)) + "\n```"
        parts = split_message("Intro.\n\n" + code, size=500)
        self.assertGreater(len(parts), 3)
        for p in parts:
            self.assertLessEqual(len(p), 520)
            self.assertEqual(p.count("```") % 2, 0, p)
        self.assertTrue(parts[1].startswith("```python"))

    def test_answer_chunks_footer_and_sources(self):
        chunks = answer_chunks(
            "Go 1.26 [1].",
            [
                {
                    "n": 1,
                    "title": "Release\nHistory",
                    "url": "https://go.dev/doc/devel/release",
                }
            ],
            "qwen3.5:9b",
        )
        self.assertEqual(
            chunks,
            [
                "Go 1.26 [1].\n-# [1] Release History <https://go.dev/doc/devel/release>\n-# dozai · qwen3.5:9b"
            ],
        )
        self.assertEqual(
            answer_chunks("Go 1.23 [1].", [], "m")[0], "Go 1.23.\n-# dozai · m"
        )
        long = answer_chunks("x " * 990, [], "m")
        self.assertTrue(all(len(c) <= 2000 for c in long))
        self.assertTrue(long[-1].endswith("-# dozai · m"))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []

        async def completions(request):
            body = await request.json()
            self.seen.append((body, dict(request.headers)))
            if body["model"] == "persona:Missing":
                return web.json_response(
                    {"error": {"message": 'no persona named "Missing"'}}, status=404
                )
            if body["messages"][-1]["content"] == "busy":
                return web.json_response(
                    {"error": {"message": "already answering"}}, status=429
                )
            if body.get("code") and body["messages"][-1]["content"] == "no sandbox":
                return web.json_response(
                    {
                        "error": {
                            "message": "code execution isn't configured on this server"
                        }
                    },
                    status=400,
                )
            return web.json_response(
                {
                    "model": "qwen3.5:9b",
                    "choices": [
                        {
                            "message": {
                                "content": "hello",
                                "sources": [{"n": 1, "url": "https://x"}],
                                "files": [
                                    {
                                        "name": "figure-1.png",
                                        "mime": "image/png",
                                        "data": "iVBORw==",
                                    }
                                ],
                            }
                        }
                    ],
                }
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", completions)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # pyright: ignore
        self.url = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_ask(self):
        d = Dozai(self.url, "tok", "persona:RoboDoze", web=True)
        a = await d.ask([{"role": "user", "content": "hi"}], "discord:42")
        await d.close()
        self.assertEqual(
            (a.content, a.model, a.sources[0]["url"]),
            ("hello", "qwen3.5:9b", "https://x"),
        )
        body, headers = self.seen[0]
        self.assertTrue(body["web"])
        self.assertFalse(body["stream"])
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(headers["X-Dozai-End-User"], "discord:42")

    async def test_charts_and_code_fallback(self):
        d = Dozai(self.url, "tok", "auto", web=False, code=True)
        a = await d.ask([{"role": "user", "content": "plot"}], "discord:1")
        self.assertEqual(
            [(c.name, c.data) for c in a.charts], [("figure-1.png", b"\x89PNG")]
        )
        self.assertTrue(self.seen[0][0]["code"])
        # A server without a sandbox: the bot asks again without code, and stops asking.
        b = await d.ask([{"role": "user", "content": "no sandbox"}], "discord:1")
        await d.close()
        self.assertEqual(b.content, "hello")
        self.assertNotIn("code", self.seen[-1][0])
        self.assertFalse(d.code)

    async def test_missing_persona_falls_back_to_auto(self):
        d = Dozai(self.url, "tok", "persona:Missing", web=False)
        a = await d.ask([{"role": "user", "content": "hi"}], "discord:1")
        await d.close()
        self.assertEqual(a.content, "hello")
        self.assertEqual(
            [b["model"] for b, _ in self.seen], ["persona:Missing", "auto"]
        )
        self.assertNotIn("web", self.seen[1][0])

    async def test_errors_are_explained(self):
        d = Dozai(self.url, "tok", "auto", web=False)
        with self.assertRaises(AskError) as cm:
            await d.ask([{"role": "user", "content": "busy"}], "discord:1")
        await d.close()
        self.assertIn("already have a question", str(cm.exception))
        self.assertIn("No GPU", explain(503, ""))
        d2 = Dozai("http://127.0.0.1:1", "tok", "auto", web=False)
        with self.assertRaises(AskError) as cm:
            await d2.ask([{"role": "user", "content": "x"}], "discord:1")
        await d2.close()
        self.assertIn("isn't reachable", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
