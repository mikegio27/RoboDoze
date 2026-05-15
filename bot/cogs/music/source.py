import asyncio
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import discord
from discord.ext import commands
from yt_dlp import YoutubeDL

from utils.logging import logger

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ytdl")

MAX_QUEUE_SIZE = 50
ALONE_TIMEOUT = 60  # seconds before auto-leaving an empty voice channel

FFMPEG_OPTIONS = {
    'before_options': '-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -af aresample=async=1:first_pts=0',
}

ytdl_opts = {
    'format': 'bestaudio/best',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'socket_timeout': 15,
}

ytdl = YoutubeDL(ytdl_opts)  # type: ignore[arg-type]

ytdl_flat_opts = {
    **ytdl_opts,
    'noplaylist': False,
    'extract_flat': True,
    'ignoreerrors': True,  # skip unavailable videos instead of aborting
}
ytdl_flat = YoutubeDL(ytdl_flat_opts)  # type: ignore[arg-type]


def is_playlist_url(url: str) -> bool:
    """True only for pure playlist URLs (/playlist?list=...), not video+list combos."""
    try:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        return parsed.path == '/playlist' and 'list' in qs
    except Exception:
        return False


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "LIVE"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02}m {s:02}s" if h else f"{m:02}m {s:02}s"


class VoiceConnectionError(commands.CommandError):
    pass


class InvalidVoiceChannel(VoiceConnectionError):
    pass


class MusicSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester
        self.title = data.get('title')
        self.web_url = data.get('webpage_url')
        self.duration = data.get('duration')
        self.thumbnail = data.get('thumbnail')
        self.is_live: bool = bool(data.get('is_live'))

    def __getitem__(self, item: str):
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, download: bool = False):
        logger.debug(f"[{ctx.guild}] create_source: searching for '{search}' (download={download})")
        to_run = partial(ytdl.extract_info, url=search, download=download)
        try:
            async with asyncio.timeout(30):
                raw = await asyncio.get_running_loop().run_in_executor(_executor, to_run)
        except asyncio.TimeoutError:
            logger.warning(f"[{ctx.guild}] create_source: yt-dlp timed out for '{search}'")
            await ctx.send(embed=discord.Embed(
                title="", description="Song took too long to load, please try again.", color=discord.Color.red()
            ))
            return None

        if raw is None:
            logger.warning(f"[{ctx.guild}] create_source: no results for '{search}'")
            await ctx.send(embed=discord.Embed(title="", description="No results found.", color=discord.Color.red()))
            return None

        data: dict[str, Any] = raw  # type: ignore[assignment]
        if 'entries' in data:
            data = data['entries'][0]

        logger.debug(f"[{ctx.guild}] create_source: resolved '{search}' -> '{data.get('title')}' ({data.get('webpage_url')})")

        if download:
            source = ytdl.prepare_filename(data)  # type: ignore[arg-type]
            return cls(discord.FFmpegPCMAudio(source, **FFMPEG_OPTIONS), data=data, requester=ctx.author)

        return {
            'webpage_url': data['webpage_url'],
            'requester': ctx.author,
            'title': data['title'],
            'thumbnail': data.get('thumbnail'),
            'duration': data.get('duration'),
            'is_live': bool(data.get('is_live')),
        }

    @classmethod
    async def fetch_stream_info(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Fetch a fresh stream URL from yt-dlp without spawning FFmpeg."""
        logger.debug(f"fetch_stream_info: fetching stream for '{data.get('title')}' ({data.get('webpage_url')})")
        to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=False)
        async with asyncio.timeout(30):
            raw = await asyncio.get_running_loop().run_in_executor(_executor, to_run)
        if raw is None:
            raise ValueError(f"No stream data returned for {data['webpage_url']}")
        info: dict[str, Any] = raw  # type: ignore[assignment]
        logger.debug(f"fetch_stream_info: got stream URL for '{data.get('title')}'")
        return info

    @classmethod
    def from_stream_info(cls, info: dict[str, Any], requester) -> "MusicSource":
        """Create a MusicSource from pre-fetched stream info. Spawns FFmpeg subprocess."""
        logger.debug(f"from_stream_info: spawning FFmpeg for '{info.get('title')}'")
        return cls(discord.FFmpegPCMAudio(info['url'], **FFMPEG_OPTIONS), data=info, requester=requester)

    @classmethod
    async def regather_stream(cls, data: dict[str, Any], *, requester=None) -> "MusicSource":
        req = requester or data['requester']
        info = await cls.fetch_stream_info(data)
        return cls.from_stream_info(info, req)

    @classmethod
    async def fetch_playlist_entries(cls, ctx, url: str) -> list[dict[str, Any]]:
        """Fetch all video metadata from a playlist URL using flat extraction."""
        logger.debug(f"[{ctx.guild}] fetch_playlist_entries: fetching '{url}'")

        def _extract():
            raw = ytdl_flat.extract_info(url=url, download=False)
            if raw is None or 'entries' not in raw:
                return None
            # Force-consume any lazy iterator inside the executor thread so the
            # generator's network calls don't bleed back into the event loop.
            return {'title': raw.get('title', url), 'entries': list(raw['entries'])}

        try:
            async with asyncio.timeout(60):
                result = await asyncio.get_running_loop().run_in_executor(_executor, _extract)
        except asyncio.TimeoutError:
            logger.warning(f"[{ctx.guild}] fetch_playlist_entries: timed out for '{url}'")
            await ctx.send(embed=discord.Embed(
                description="Playlist took too long to load, please try again.", color=discord.Color.red()
            ))
            return []

        if not result:
            logger.warning(f"[{ctx.guild}] fetch_playlist_entries: no entries found for '{url}'")
            return []

        entries = []
        for entry in result['entries']:
            if not entry:
                continue
            video_id = entry.get('id')
            if not video_id:
                # yt-dlp flat entries sometimes put just the bare ID in 'url'
                raw_url = entry.get('url', '')
                parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(raw_url).query)
                video_id = (parsed_qs.get('v') or [None])[0] or (raw_url if raw_url and '/' not in raw_url else None)
            if not video_id:
                logger.debug(f"[{ctx.guild}] fetch_playlist_entries: skipping entry with no resolvable ID: {entry}")
                continue
            entries.append({
                'webpage_url': f"https://www.youtube.com/watch?v={video_id}",
                'requester': ctx.author,
                'title': entry.get('title', 'Unknown'),
                'thumbnail': f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
                'duration': entry.get('duration'),
                'is_live': bool(entry.get('is_live')),
            })

        logger.debug(f"[{ctx.guild}] fetch_playlist_entries: found {len(entries)} tracks in '{result['title']}'")
        return entries
