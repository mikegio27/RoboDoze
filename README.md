# RoboDoze

A self-hosted Discord music bot. Streams audio from YouTube (and anything yt-dlp supports) directly into voice channels. Runs as a single Docker container, deployable via Docker Compose or Kubernetes.

## Features

- Stream audio from YouTube and other yt-dlp-supported sources
- Queue management with a 500-song cap
- Loop and shuffle modes
- Next-track prefetching to minimize gaps between songs
- Auto-leave when the voice channel is empty (60s timeout)
- Auto-leave when the queue is idle for 5 minutes
- HTTP health endpoints for orchestration liveness/readiness checks

## Commands

Default prefix: `!dozy` (configurable via `COMMAND_PREFIX`; the production deployment uses `rd-`, e.g. `rd-play`)

| Command | Aliases | Description |
|---|---|---|
| `play <query>` | `p`, `sing` | Search and queue a single song |
| `playlist <input>` | `pl` | Queue a YouTube playlist URL, or a comma-separated list of songs/URLs (e.g. `song1, song2, url3`) |
| `join` | `j`, `connect` | Join your voice channel |
| `leave` | `stop`, `dc`, `disconnect`, `bye` | Stop and disconnect |
| `skip` | | Skip the current track |
| `pause` | | Pause playback |
| `resume` | | Resume playback |
| `loop` | `lp`, `repeat` | Toggle repeat for the **current track** |
| `loopqueue` | `lq`, `loopall`, `repeatqueue` | Toggle repeat for the **whole queue** — finished tracks return to the end |
| `shuffle` | `sh` | Shuffle the upcoming queue |
| `queue` | `q`, `que` | Show the current queue |
| `np` | `song`, `current`, `playing` | Show what's playing now |
| `volume [1-100]` | `v`, `vol` | Get or set volume |
| `remove [pos]` | `rm`, `rem` | Remove a track (defaults to last) |
| `clear` | `clr`, `cl`, `cr` | Clear the entire queue |
| `ask <question>` | | Ask dozai, the homelab AI (it can search the web; attach images to show it something). **Reply to its answer to follow up** |

`loop` and `loopqueue` are mutually exclusive — enabling one turns the other off.
While `loopqueue` is on, the queue never drains: `remove` can only drop upcoming
tracks, since the playing track returns to the end of the rotation. Use `clear` to
empty the rotation, or `loopqueue` again to turn it off.

## Configuration

Set via environment variables (or a `.env` file when using Docker Compose):

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | — | Discord bot token |
| `COMMAND_PREFIX` | No | `!dozy` | Bot command prefix |
| `HEALTH_PORT` | No | `8080` | Port for the health server (`/healthz`, `/readyz`, `/metrics`) |
| `LOG_LEVEL` | No | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`, ...). `DEBUG` is very verbose |
| `LOG_FORMAT` | No | `text` | `text` for human-readable lines, `json` for one JSON object per line (used in prod for Loki) |
| `DOZAI_TOKEN` | No | — | dozai service token (`dozai client create robodoze -interactive`). Unset = `ask` is off |
| `DOZAI_URL` | No | `http://dozai.dozai.svc.cluster.local:8080` | dozai's address |
| `DOZAI_MODEL` | No | `persona:RoboDoze` | Model for `ask`: a dozai persona (its instructions are the bot's voice), `auto`, or a model name. A missing persona falls back to `auto` |
| `DOZAI_WEB` | No | `true` | Let `ask` search the web |
| `DOZAI_CODE` | No | `true` | Let `ask` run Python in dozai's sandbox; charts are posted with the answer |

## Docker image

The image is published to GitHub Container Registry on every push to `main`:

```
ghcr.io/mikegio27/robodoze:latest
ghcr.io/mikegio27/robodoze:main
ghcr.io/mikegio27/robodoze:<short-sha>
```

Tags are managed by the CI workflow — there is no semantic versioning.

## Running locally

```bash
cp .env.example .env   # add your DISCORD_TOKEN
docker compose up --build
```

## Deployment

Production runs on a homelab k3s cluster via Flux; the manifests live in the separate homelab
repo (`apps/discord/`), and a deploy is a pinned image-tag bump there. See [DEPLOY.md](DEPLOY.md)
for Docker Compose, GHCR tags, the production deploy flow, and the metrics reference.

## Health endpoints

The bot exposes two HTTP endpoints on port 8080 (configurable):

- `GET /healthz` — 200 while the process is alive
- `GET /readyz` — 200 after Discord `on_ready` fires; 503 before then

## Development

Lint and format (config in `pyproject.toml`):

```bash
ruff check .
ruff format .
```

Run the tests — stdlib `unittest`, no extra dependencies:

```bash
python3 -m unittest discover -s tests -v
```

They cover the queue and loop-mode re-queueing logic and the "alone in voice"
auto-leave check, none of which need a Discord connection. CI runs ruff and the
tests on every push and only builds the image if they pass. Playback itself is not covered — verify that against a live bot.

## Requirements

- Python 3.12+
- ffmpeg (included in the Docker image)
- discord.py, yt-dlp, aiohttp, PyNaCl
