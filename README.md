# RoboDoze

A self-hosted Discord music bot. Streams audio from YouTube (and anything yt-dlp supports) directly into voice channels. Runs as a single Docker container, deployable via Docker Compose or Kubernetes.

## Features

- Stream audio from YouTube and other yt-dlp-supported sources
- Queue management with a 50-song cap
- Loop and shuffle modes
- Next-track prefetching to minimize gaps between songs
- Auto-leave when the voice channel is empty (60s timeout)
- Auto-leave when the queue is idle for 5 minutes
- HTTP health endpoints for orchestration liveness/readiness checks

## Commands

Default prefix: `!dozy` (configurable via `COMMAND_PREFIX`)

| Command | Aliases | Description |
|---|---|---|
| `play <query>` | `p`, `sing` | Search and queue a song |
| `join` | `j`, `connect` | Join your voice channel |
| `leave` | `stop`, `dc`, `disconnect`, `bye` | Stop and disconnect |
| `skip` | | Skip the current track |
| `pause` | | Pause playback |
| `resume` | | Resume playback |
| `loop` | `lp`, `repeat` | Toggle loop for the current track |
| `shuffle` | `sh` | Shuffle the upcoming queue |
| `queue` | `q`, `playlist`, `que` | Show the current queue |
| `np` | `song`, `current`, `playing` | Show what's playing now |
| `volume [1-100]` | `v`, `vol` | Get or set volume |
| `remove [pos]` | `rm`, `rem` | Remove a track (defaults to last) |
| `clear` | `clr`, `cl`, `cr` | Clear the entire queue |

## Configuration

Set via environment variables (or a `.env` file when using Docker Compose):

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | — | Discord bot token |
| `COMMAND_PREFIX` | No | `!dozy` | Bot command prefix |
| `HEALTH_PORT` | No | `8080` | Port for the health server |

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

See [DEPLOY.md](DEPLOY.md) for full instructions covering Docker Compose, minikube, k3s, and pulling from GHCR.

## Health endpoints

The bot exposes two HTTP endpoints on port 8080 (configurable):

- `GET /healthz` — 200 while the process is alive
- `GET /readyz` — 200 after Discord `on_ready` fires; 503 before then

## Requirements

- Python 3.12+
- ffmpeg (included in the Docker image)
- discord.py, yt-dlp, aiohttp, PyNaCl
