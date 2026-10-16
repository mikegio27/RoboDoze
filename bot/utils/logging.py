import datetime
import json
import logging
import os
import sys

LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
LOG_FORMAT = os.getenv("LOG_FORMAT", "text").lower()

logger = logging.getLogger("RoboDoze")
logger.setLevel(LOG_LEVEL)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.datetime.fromtimestamp(
                record.created, tz=datetime.UTC
            )
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


if not logger.hasHandlers():
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setLevel(LOG_LEVEL)
    if LOG_FORMAT == "json":
        _handler.setFormatter(_JsonFormatter())
    else:
        _handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
        )
    logger.addHandler(_handler)

    # discord.py only installs its own handler from Client.run(); main.py uses
    # Client.start(), so without this its loggers have no handler and voice close
    # codes / reconnect attempts never reach Loki. Floored at INFO: discord.py's
    # DEBUG output (gateway payloads, every RTP packet batch) would drown ours.
    discord_logger = logging.getLogger("discord")
    discord_logger.setLevel(max(LOG_LEVEL, logging.INFO))
    discord_logger.addHandler(_handler)
    discord_logger.propagate = False


class YtdlLogger:
    """Adapter passed to YoutubeDL(logger=...) so its warnings reach our logs.

    yt-dlp's own "no_warnings" would hide things like "No supported JavaScript
    runtime could be found", which degrades YouTube extraction silently.
    Errors are logged at DEBUG only: yt-dlp raises them as exceptions too, and
    the callers already log those.
    """

    def debug(self, msg: str) -> None:
        logger.debug(f"yt-dlp: {msg}")

    def info(self, msg: str) -> None:
        logger.debug(f"yt-dlp: {msg}")

    def warning(self, msg: str) -> None:
        # YouTube's own notices ("1 unavailable video is hidden") show up on most
        # real playlists and aren't actionable.
        if "YouTube said: INFO -" in msg:
            logger.debug(f"yt-dlp: {msg}")
            return
        logger.warning(f"yt-dlp: {msg}")

    def error(self, msg: str) -> None:
        logger.debug(f"yt-dlp: {msg}")
