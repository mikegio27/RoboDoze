import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Literal

import discord
from utils.logging import logger
from yt_dlp import YoutubeDL

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="video")

ALONE_TIMEOUT = 60
MAX_QUEUE_SIZE = 20

AUDIO_FFMPEG_OPTIONS = {
    "before_options": "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -af aresample=async=1:first_pts=0",
}

_ytdl_opts: dict[str, Any] = {
    "format": "bestaudio/best",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "socket_timeout": 15,
}
_ytdl = YoutubeDL(_ytdl_opts)  # type: ignore[arg-type]

# Separate instance for video-only stream resolution.
# Prefers VP9/webm since FFmpeg can transcode it to VP8; falls back to any video.
_ytdl_video_opts: dict[str, Any] = {
    **_ytdl_opts,
    "format": "bestvideo[height<=720][ext=webm]/bestvideo[height<=720]/bestvideo",
}
_ytdl_video = YoutubeDL(_ytdl_video_opts)  # type: ignore[arg-type]


@dataclass
class VideoInfo:
    title: str
    source_type: Literal["local", "jellyfin", "youtube"]
    audio_url: str
    video_url: str
    duration: int | None
    thumbnail: str | None
    requester: discord.Member
    webpage_url: str | None = field(default=None)
    item_id: str | None = field(default=None)

    @classmethod
    def from_local(cls, path: Path, requester: discord.Member) -> "VideoInfo":
        return cls(
            title=path.stem,
            source_type="local",
            audio_url=str(path),
            video_url=str(path),
            duration=None,
            thumbnail=None,
            requester=requester,
        )

    @classmethod
    def from_jellyfin(
        cls,
        item: dict,
        audio_url: str,
        video_url: str,
        requester: discord.Member,
    ) -> "VideoInfo":
        ticks = item.get("RunTimeTicks")
        return cls(
            title=item.get("Name", "Unknown"),
            source_type="jellyfin",
            audio_url=audio_url,
            video_url=video_url,
            duration=ticks // 10_000_000 if ticks else None,
            thumbnail=None,
            requester=requester,
            item_id=item.get("Id"),
        )

    @classmethod
    async def from_youtube(
        cls, search: str, requester: discord.Member
    ) -> "VideoInfo | None":
        to_run = partial(_ytdl.extract_info, url=search, download=False)
        try:
            async with asyncio.timeout(30):
                raw = await asyncio.get_running_loop().run_in_executor(
                    _executor, to_run
                )
        except asyncio.TimeoutError:
            logger.warning(f"from_youtube: timed out for {search!r}")
            return None

        if raw is None:
            return None

        info: Any = raw
        data: Any = info["entries"][0] if "entries" in info else info

        return cls(
            title=data.get("title", "Unknown"),
            source_type="youtube",
            audio_url=data["url"],
            video_url=data.get("webpage_url", data["url"]),
            duration=data.get("duration"),
            thumbnail=data.get("thumbnail"),
            requester=requester,
            webpage_url=data.get("webpage_url"),
        )

    async def regather_audio(self) -> str:
        """Return a fresh audio stream URL. Only re-fetches for YouTube (URLs expire)."""
        if self.source_type != "youtube" or not self.webpage_url:
            return self.audio_url
        to_run = partial(_ytdl.extract_info, url=self.webpage_url, download=False)
        async with asyncio.timeout(30):
            raw = await asyncio.get_running_loop().run_in_executor(_executor, to_run)
        if raw is None:
            raise ValueError(f"No stream data for {self.webpage_url}")
        info: Any = raw
        return info.get("url", "")

    async def regather_video(self) -> str:
        """Return a direct video CDN URL suitable for FFmpeg.
        For YouTube this re-fetches using a video-only format selector so FFmpeg
        gets a real stream URL rather than the webpage URL."""
        if self.source_type != "youtube" or not self.webpage_url:
            return self.video_url
        to_run = partial(_ytdl_video.extract_info, url=self.webpage_url, download=False)
        async with asyncio.timeout(30):
            raw = await asyncio.get_running_loop().run_in_executor(_executor, to_run)
        if raw is None:
            raise ValueError(f"No video stream data for {self.webpage_url}")
        info: Any = raw
        url: str = info.get("url", "")
        if not url:
            raise ValueError(f"yt-dlp returned no video URL for {self.webpage_url}")
        logger.debug(f"regather_video: resolved video stream for {self.title!r}")
        return url


class VideoAudioSource(discord.PCMVolumeTransformer):
    """PCMVolumeTransformer wrapping an FFmpeg audio pipeline for a VideoInfo."""

    def __init__(self, source: discord.AudioSource, *, info: VideoInfo) -> None:
        super().__init__(source)
        self.info = info
        self.title = info.title
        self.requester = info.requester
        self.thumbnail = info.thumbnail
        self.duration = info.duration
        self.is_live: bool = info.duration is None and info.source_type == "youtube"
