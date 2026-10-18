"""lobby's bot API (dozy-lobby's control plane), as RoboDoze uses it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

from .format import GUILD_ONLY


@dataclass
class Caller:
    """Who asked, sent on every request so lobby can check the guild and log the user."""

    guild_id: int
    user_id: int
    user_name: str


class LobbyError(Exception):
    """Something to tell the person, in their words. `code` is lobby's problem code."""

    def __init__(
        self,
        message: str,
        status: int = 0,
        code: str = "",
        detail: str = "",
        players: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail
        self.players = players or []


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class Lobby:
    def __init__(self, url: str, token: str, timeout: float = 20) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def servers(self, who: Caller) -> list[dict]:
        data = await self._call("GET", "/bot/servers", who)
        return data.get("items") or []

    async def server(self, name: str, who: Caller) -> dict:
        return await self._call("GET", f"/bot/servers/{quote(name, safe='')}", who)

    async def start(self, name: str, who: Caller) -> dict:
        return await self._call(
            "POST", f"/bot/servers/{quote(name, safe='')}/start", who
        )

    async def stop(self, name: str, who: Caller, force: bool = False) -> dict:
        return await self._call(
            "POST", f"/bot/servers/{quote(name, safe='')}/stop", who, {"force": force}
        )

    async def restart(self, name: str, who: Caller, force: bool = False) -> dict:
        return await self._call(
            "POST",
            f"/bot/servers/{quote(name, safe='')}/restart",
            who,
            {"force": force},
        )

    async def _call(
        self, method: str, path: str, who: Caller, body: dict | None = None
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Discord-Guild-Id": str(who.guild_id),
            "X-Discord-User-Id": str(who.user_id),
            # Display names are free text: no control characters in a header.
            "X-Discord-User-Name": _CONTROL.sub("", who.user_name)[:100],
        }
        try:
            async with self._http().request(
                method, self.url + path, json=body, headers=headers
            ) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = None
        except TimeoutError as e:
            raise LobbyError("The game lobby took too long to answer.") from e
        except aiohttp.ClientError as e:
            raise LobbyError("The game lobby isn't reachable right now.") from e
        if 200 <= status < 300 and isinstance(data, dict):
            return data
        p = data if isinstance(data, dict) else {}
        code = str(p.get("code") or "")
        detail = str(p.get("detail") or "")
        raise LobbyError(
            explain(status, code, detail),
            status=status,
            code=code,
            detail=detail,
            players=[str(x) for x in p.get("players") or []],
        )


def explain(status: int, code: str, detail: str) -> str:
    """lobby's error, as a Discord reply."""
    if code == "guild_not_allowed":
        return GUILD_ONLY
    if code == "unauthorized" or status == 401:
        return "RoboDoze isn't allowed to use the game lobby (its token is wrong or disabled)."
    if code == "host_full":
        return (
            "There's no room on that server's machine right now. "
            "Stop another server there first, or ask an admin to move it."
        )
    if code == "already_running":
        return "That server is already running."
    if code == "players_online":
        return "People are playing on it right now."
    if status == 404:
        return "There's no game server by that name."
    if status == 403:
        return "The game lobby says you can't do that."
    if 400 <= status < 500 and detail:
        return f"The game lobby said no: {detail}"
    if status >= 500 or status == 0:
        return "Something went wrong in the game lobby; try again in a bit."
    return "Something went wrong talking to the game lobby."
