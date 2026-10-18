"""Pure formatting and name resolution for the game-server commands.

Everything here takes lobby `Server` dicts and returns Discord text, so it is
tested without Discord or the network.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from discord.utils import escape_markdown

PORTAL_URL = "https://home.dozydelta.com/games"
GUILD_ONLY = "Game servers can only be managed from the DozyNet or HCH Discord."
MAX_MESSAGE = 2000

STARTING_PHASES = {"Pending", "Provisioning", "Starting"}

_STATUS = {
    "Running": ("🟢", "up"),
    "Pending": ("🟡", "starting"),
    "Provisioning": ("🟡", "starting"),
    "Starting": ("🟡", "starting"),
    "Stopping": ("🟠", "stopping"),
    "Stopped": ("⚫", "off"),
    "Archiving": ("📦", "archiving"),
    "Archived": ("📦", "archived"),
    "Failed": ("🔴", "failed"),
    "Deleting": ("🗑️", "being deleted"),
}

# What a slow start is busy with, for the "Starting X…" message.
_STARTING_WHAT = {
    "Pending": "waiting for its host",
    "Provisioning": "setting up (downloading the game can take a while)",
    "Starting": "the game is loading",
}


def esc(text: str) -> str:
    return escape_markdown(text or "")


def title(server: dict) -> str:
    return f"**{esc(server.get('displayName') or server.get('name', '?'))}**"


def game_name(server: dict) -> str:
    return (server.get("game") or {}).get("displayName") or server.get("definition", "")


def status(server: dict) -> tuple[str, str]:
    phase = server.get("phase") or server.get("state") or ""
    return _STATUS.get(phase, ("❔", phase.lower() or "unknown"))


def address(server: dict) -> str:
    return server.get("connect") or server.get("address") or ""


def players_text(server: dict) -> str:
    """Who's on: names, else a count, else "players unknown"."""
    p = server.get("players") or {}
    if not p.get("known"):
        return "players unknown"
    online = p.get("online") or 0
    names = p.get("names") or []
    if online == 0:
        return "nobody on"
    if names:
        shown = ", ".join(esc(n) for n in names[:10])
        more = online - min(len(names), 10)
        return shown + (f" +{more}" if more > 0 else "")
    count = f"{online}/{p['max']}" if p.get("max") else str(online)
    return f"{count} on"


def parse_time(value: str | None) -> datetime | None:
    """RFC 3339 from Go (up to nanoseconds, `Z`) as an aware datetime."""
    if not value:
        return None
    # Python before 3.13 rejects more than 6 fractional digits.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} days"


def ago(value: str | None, now: datetime) -> str:
    dt = parse_time(value)
    if dt is None:
        return ""
    seconds = (now - dt).total_seconds()
    return "just now" if seconds < 60 else f"{_span(seconds)} ago"


def idle_text(server: dict, now: datetime) -> str:
    idle = server.get("idle") or {}
    if idle.get("never"):
        return "never stops on its own"
    stops = parse_time(idle.get("stopsAt"))
    if stops is not None:
        left = (stops - now).total_seconds()
        if left <= 60:
            return "nobody's on: stopping any moment now"
        return f"nobody's on: stops in {_span(left)}"
    minutes = idle.get("minutes")
    if minutes:
        return f"stops after {minutes} min with nobody on"
    return ""


def list_text(servers: list[dict], prefix: str = "rd-") -> str:
    """rd-games: one line per server, archived ones summarized at the end."""
    if not servers:
        return f"No game servers yet. Make one at <{PORTAL_URL}>."
    live = [s for s in servers if s.get("state") != "Archived"]
    archived = [s for s in servers if s.get("state") == "Archived"]
    lines = [server_line(s) for s in live]
    if archived:
        names = ", ".join(title(s) for s in archived)
        lines.append(
            f"📦 Archived: {names} (`{prefix}game start <name>` brings one back; it re-downloads first)"
        )
    footer = f"-# New servers and settings: <{PORTAL_URL}>"
    return fit(lines, footer)


def server_line(server: dict) -> str:
    emoji, word = status(server)
    parts = [f"{emoji} {title(server)}", esc(game_name(server))]
    phase = server.get("phase")
    if phase == "Running":
        parts.append(players_text(server))
        if address(server):
            parts.append(f"`{address(server)}`")
    else:
        parts.append(word)
    return " · ".join(p for p in parts if p)


def fit(lines: list[str], footer: str) -> str:
    """Join lines under Discord's 2000-char limit, dropping the tail if needed."""
    out: list[str] = []
    used = len(footer) + 40
    for i, line in enumerate(lines):
        if used + len(line) + 1 > MAX_MESSAGE:
            out.append(f"…and {len(lines) - i} more")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join([*out, footer])


def detail_text(server: dict, now: datetime) -> str:
    """rd-game <name>: everything a player wants to know about one server."""
    emoji, word = status(server)
    head = f"{emoji} {title(server)} · {esc(game_name(server))}"
    if server.get("hostDisplayName") or server.get("host"):
        head += f" · on {esc(server.get('hostDisplayName') or server.get('host', ''))}"
    lines = [head, f"Status: {word}"]
    if server.get("message") and server.get("phase") != "Running":
        lines[-1] += f" ({esc(server['message'])})"
    if address(server):
        lines.append(f"Address: `{address(server)}`")
    phase = server.get("phase")
    if phase == "Running":
        p = server.get("players") or {}
        who = players_text(server)
        if p.get("known") and p.get("max"):
            who = f"{p.get('online') or 0}/{p['max']}" + (
                f": {who}" if p.get("names") else ""
            )
        lines.append(f"Players: {who}")
        idle = idle_text(server, now)
        if idle:
            lines.append(f"Idle: {idle}")
    played = ago(server.get("lastPlayedAt"), now)
    if played:
        lines.append(f"Last played: {played}")
    lines.append(f"-# <{PORTAL_URL}>")
    return "\n".join(lines)


# --- Starting -----------------------------------------------------------------


def start_progress(server: dict) -> tuple[str, str | None]:
    """The start message for this poll, and the outcome once it is final:
    "up", "failed" or "stopped" (None while it is still starting)."""
    phase = server.get("phase") or ""
    name = title(server)
    if phase == "Running":
        addr = address(server)
        return (f"{name} is up — `{addr}`" if addr else f"{name} is up."), "up"
    if phase == "Failed":
        why = esc(server.get("message") or "no reason given")
        return f"{name} didn't start: {why}", "failed"
    if phase in STARTING_PHASES or phase == "":
        what = _STARTING_WHAT.get(phase, "")
        msg = server.get("message")
        if msg:
            what = esc(msg)
        return f"Starting {name}…" + (f" {what}" if what else ""), None
    why = server.get("message")
    return (
        f"{name} stopped before it came up" + (f": {esc(why)}" if why else "."),
        "stopped",
    )


def start_timeout_text(server: dict, minutes: int, prefix: str = "rd-") -> str:
    return (
        f"{title(server)} is still starting after {minutes} min. "
        f"I've stopped watching; check `{prefix}game {server.get('name')}` or <{PORTAL_URL}>."
    )


# --- Stopping -----------------------------------------------------------------


def confirm_text(server: dict, players: list[str], action: str) -> str:
    n = len(players)
    who = (
        f"{n} {'person is' if n == 1 else 'people are'} playing: "
        + ", ".join(esc(p) for p in players)
        if players
        else "People are playing"
    )
    verb = "Stopping" if action == "stop" else "Restarting"
    return f"{who}. {verb} {title(server)} saves the world and kicks them."


def action_text(server: dict, action: str) -> str:
    if action == "restart":
        return f"Restarting {title(server)}. It'll be back in a minute or two."
    return f"Stopping {title(server)}. The world is saved first."


# --- Name resolution ----------------------------------------------------------


@dataclass
class Resolution:
    server: dict | None = None
    candidates: list[dict] = field(default_factory=list)  # ambiguous matches
    suggestions: list[dict] = field(default_factory=list)  # near misses


def resolve(servers: list[dict], query: str) -> Resolution:
    """Exact `name`, else a case-insensitive `name`/`displayName` match, else a
    unique prefix of either."""
    q = query.strip()
    for s in servers:
        if s.get("name") == q:
            return Resolution(server=s)
    ql = q.casefold()
    if not ql:
        return Resolution()

    def keys(s: dict) -> list[str]:
        return [k.casefold() for k in (s.get("name"), s.get("displayName")) if k]

    same = [s for s in servers if ql in keys(s)]
    if len(same) == 1:
        return Resolution(server=same[0])
    if same:
        return Resolution(candidates=same)
    prefixed = [s for s in servers if any(k.startswith(ql) for k in keys(s))]
    if len(prefixed) == 1:
        return Resolution(server=prefixed[0])
    if prefixed:
        return Resolution(candidates=prefixed)
    near = [s for s in servers if any(ql in k for k in keys(s))]
    by_key = {k: s for s in servers for k in keys(s)}
    for k in difflib.get_close_matches(ql, list(by_key), n=5, cutoff=0.5):
        if by_key[k] not in near:
            near.append(by_key[k])
    return Resolution(suggestions=near[:5])


def _names(servers: list[dict]) -> str:
    return ", ".join(
        f"`{s.get('name')}` ({esc(s.get('displayName') or '')})" for s in servers[:10]
    )


def _code(text: str) -> str:
    return "`" + text.replace("`", "'")[:100] + "`"


def ambiguous_text(query: str, candidates: list[dict]) -> str:
    return f"{_code(query)} could be {_names(candidates)}. Which one?"


def not_found_text(query: str, suggestions: list[dict], prefix: str = "rd-") -> str:
    text = f"There's no game server called {_code(query)}."
    if suggestions:
        text += f" Did you mean {_names(suggestions)}?"
    else:
        text += f" `{prefix}games` lists them."
    return text


def now() -> datetime:
    return datetime.now(UTC)
