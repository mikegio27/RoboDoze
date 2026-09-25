"""dozai's OpenAI-compatible API, as RoboDoze uses it."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import aiohttp


class AskError(Exception):
    """Something to tell the person, in their words."""


@dataclass
class Chart:
    name: str
    data: bytes


@dataclass
class Answer:
    content: str
    model: str
    sources: list[dict] = field(default_factory=list)
    charts: list[Chart] = field(default_factory=list)


class Dozai:
    def __init__(
        self, url: str, token: str, model: str, web: bool, code: bool = False
    ) -> None:
        self.url = url.rstrip("/") + "/v1/chat/completions"
        self.token = token
        self.model = model
        self.web = web
        self.code = code
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Web answers read pages and may wait for a GPU: allow minutes.
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300)
            )
        return self._session

    async def ask(self, messages: list[dict], end_user: str) -> Answer:
        try:
            return await self._ask(messages, end_user, self.model)
        except _NoPersona:
            # The persona hasn't been made (or was renamed): answer anyway.
            return await self._ask(messages, end_user, "auto")

    async def _ask(self, messages: list[dict], end_user: str, model: str) -> Answer:
        body = {"model": model, "messages": messages, "stream": False}
        if self.web:
            body["web"] = True
        if self.code:
            body["code"] = True
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Dozai-End-User": end_user,
        }
        try:
            async with self._http().post(self.url, json=body, headers=headers) as resp:
                data = await resp.json(content_type=None)
                status = resp.status
        except TimeoutError as e:
            raise AskError("That took too long; try a shorter question.") from e
        except aiohttp.ClientError as e:
            raise AskError("dozai isn't reachable right now.") from e
        if status == 200:
            msg = data["choices"][0]["message"]
            charts = [
                Chart(
                    name=f.get("name") or f"chart-{i + 1}.png",
                    data=base64.b64decode(f["data"]),
                )
                for i, f in enumerate(msg.get("files") or [])
                if f.get("mime") in ("image/png", "image/jpeg") and f.get("data")
            ]
            return Answer(
                content=msg.get("content") or "",
                model=data.get("model", ""),
                sources=msg.get("sources") or [],
                charts=charts[:4],
            )
        detail = ((data or {}).get("error") or {}).get("message", "")
        if status == 404 and "no persona named" in detail:
            raise _NoPersona(detail)
        if status == 400 and self.code and "code execution" in detail:
            # The server has no sandbox (or the model can't use tools): ask without it.
            self.code = False
            return await self._ask(messages, end_user, model)
        raise AskError(explain(status, detail))


class _NoPersona(Exception):
    pass


def explain(status: int, detail: str) -> str:
    """dozai's error, as a Discord reply."""
    if status == 429:
        return "You already have a question being answered; wait for that one."
    if status == 503:
        return "No GPU is free to answer right now; try again in a minute."
    if status == 400 and detail:
        return f"dozai couldn't take that: {detail}"
    if status in (401, 403):
        return "RoboDoze isn't allowed to use dozai (its token is wrong or disabled)."
    return "Something went wrong answering that."
