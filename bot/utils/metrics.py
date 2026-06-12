"""Prometheus metrics for RoboDoze.

Metrics are exposed on the health server's ``/metrics`` endpoint (see
``bot/health.py``) and scraped by the cluster's Prometheus/Mimir. Counters and
histograms are incremented from the relevant call sites; the live gauges
(guilds, voice connections, players, queued tracks) are bound to the running
bot via :func:`bind_runtime_gauges` and evaluated lazily at scrape time.
"""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# --- Commands ---------------------------------------------------------------
commands_total = Counter(
    "robodoze_commands_total",
    "Total bot commands invoked, by command name and outcome.",
    ["command", "status"],
)
command_duration_seconds = Histogram(
    "robodoze_command_duration_seconds",
    "Time spent handling a command, by command name.",
    ["command"],
)

# --- Music / streaming ------------------------------------------------------
tracks_queued_total = Counter(
    "robodoze_tracks_queued_total",
    "Tracks added to a guild queue, by how they were requested.",
    ["kind"],  # play | playlist
)
streams_started_total = Counter(
    "robodoze_streams_started_total",
    "Audio streams handed to a voice client for playback.",
)
stream_errors_total = Counter(
    "robodoze_stream_errors_total",
    "Errors while resolving or playing a stream, by stage.",
    ["stage"],  # resolve | playback
)
audio_bytes_streamed_total = Counter(
    "robodoze_audio_bytes_streamed_total",
    "Total PCM audio bytes read and streamed to Discord voice.",
)
source_resolve_seconds = Histogram(
    "robodoze_source_resolve_seconds",
    "yt-dlp source/stream resolution time.",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)

# --- Live runtime gauges (bound to the bot, sampled at scrape time) ---------
guilds = Gauge("robodoze_guilds", "Number of guilds (servers) the bot is in.")
voice_connections_active = Gauge(
    "robodoze_voice_connections_active", "Active voice connections."
)
players_active = Gauge("robodoze_players_active", "Active music players.")
queued_tracks = Gauge(
    "robodoze_queued_tracks", "Total tracks queued across all players."
)


def bind_runtime_gauges(bot) -> None:
    """Wire the live gauges to read from the running bot at scrape time."""

    def _players():
        cog = bot.get_cog("Music")
        return cog.players if cog else {}

    guilds.set_function(lambda: len(bot.guilds))
    voice_connections_active.set_function(lambda: len(bot.voice_clients))
    players_active.set_function(lambda: len(_players()))
    queued_tracks.set_function(
        lambda: sum(p.queue.qsize() for p in _players().values())
    )


def render() -> tuple[bytes, str]:
    """Return the metrics exposition payload and its content type."""
    return generate_latest(), CONTENT_TYPE_LATEST
