# RoboDoze

Self-hosted Discord music bot (discord.py + yt-dlp + ffmpeg) that streams audio into voice
channels. Single container, single replica. It runs on the homelab k3s cluster in namespace
`discord`. The manifests are in `../homelab/apps/discord/`, not here (`DEPLOY.md` documents that flow).

## Commands

```bash
python3 -m unittest discover -s tests -v   # 27 tests, <1s, needs discord.py + yt-dlp importable
ruff check . && ruff format --check .      # config in pyproject.toml; currently clean
basedpyright                               # optional; configured in pyproject (not installed by default)
docker build -t robodoze:local .           # python:3.12-slim + ffmpeg; PyNaCl needs the gcc builder stage
docker compose up --build                  # needs .env with DISCORD_TOKEN (copy .env.example)
```

- The tests insert `bot/` into `sys.path` themselves. The runtime does the same with
  `PYTHONPATH=/app/bot`. Imports are unrooted (`from utils import metrics`, `from cogs.music...`),
  so never add a `bot.` prefix.
- CI gates on these: the `test` job in `.github/workflows/docker.yml` runs `ruff check`,
  `ruff format --check` and the unittest suite on Python 3.12, and `build-and-push` `needs:` it.
  A lint or test failure means no image, so run them locally before pushing.
- Hooks: `.claude/settings.json` provides a ruff-format-on-edit hook and a unittest-on-stop hook,
  so formatting is applied as you edit and the suite runs when you finish a turn. Don't fight them.
- Do not start the bot against real Discord to "check" something. A second live instance with the
  prod token answers every command twice. Playback is only verifiable manually on a live bot.

## Layout

```
bot/main.py              RoboDoze(commands.Bot): intents, loads EXTENSIONS=["cogs.music"], SIGTERM -> close()
bot/health.py            aiohttp server on HEALTH_PORT: /healthz, /readyz (503 until on_ready), /metrics
bot/utils/metrics.py     all Prometheus metrics (robodoze_* names); live gauges bound at scrape time
bot/utils/logging.py     "RoboDoze" logger; LOG_FORMAT=json -> one JSON object per line
bot/cogs/music/cog.py    Music cog: all commands, per-guild players dict, voice-state auto-leave
bot/cogs/music/player.py MusicPlayer (per-guild player_loop task), MusicQueue, loop modes, requeue_finished
bot/cogs/music/source.py yt-dlp/ffmpeg options, MusicSource, MAX_QUEUE_SIZE=500, ALONE_TIMEOUT=60
tests/test_queue.py      pure queue / loop-mode logic
tests/test_alone.py      has_listeners() ("is the bot alone in voice?") with SimpleNamespace fakes
bot/cogs/ask/            rd-ask: questions to dozai (/v1, web on, per-user limit via X-Dozai-End-User).
                         format.py pure (reply chain -> messages, 2000-char splitting, sources), client.py
                         (aiohttp; persona fallback; errors in people's words), cog.py (command + reply listener)
tests/test_ask.py        format helpers + the client against a local fake of dozai's /v1
```

To add a feature area, create a new cog package under `bot/cogs/<name>/` with an `async def setup(bot)`
and add it to `EXTENSIONS` in `main.py`. Earlier `ai` and `video` cogs were tried and removed.

## Config (env vars)

| Var | Default | Prod (homelab configmap) |
|---|---|---|
| `DISCORD_TOKEN` | required. The bot exits if it is unset | from Secret `robodoze-secret` (created out of band, never in git) |
| `COMMAND_PREFIX` | `!dozy` | `rd-` |
| `HEALTH_PORT` | `8080` | `8080` |
| `LOG_LEVEL` | `INFO` | `INFO` |
| `LOG_FORMAT` | `text` | `json` (Loki/Alloy) |
| `DOZAI_TOKEN` | unset = `ask` off | SealedSecret `robodoze-dozai` in homelab (a dozai service client, interactive) |
| `DOZAI_URL` | `http://dozai.dozai.svc.cluster.local:8080` | same |
| `DOZAI_MODEL` | `persona:RoboDoze` | same (a shared dozai persona; falls back to `auto`) |
| `DOZAI_WEB` | `true` | same |
| `DOZAI_CODE` | `true` | same (charts from dozai's code sandbox are posted as files) |

All of these are documented in `README.md` and `.env.example`; keep both in sync with the code
defaults. `LOG_LEVEL` / `LOG_FORMAT` are read in `utils/logging.py`.

## Release -> deploy loop

1. Push to `main`. `.github/workflows/docker.yml` runs the test job, then builds and pushes
   `ghcr.io/mikegio27/robodoze` with the tags `<short-sha>` (no prefix), `main` and `latest`.
   A `[skip ci]` in the commit message skips the whole workflow, which is fine for docs-only changes.
2. Wait for the Actions run to go green (`gh run list -R mikegio27/RoboDoze`).
3. In `../homelab/apps/discord/deployment.yaml`, set `image: ghcr.io/mikegio27/robodoze:<short-sha>`.
   Use the 7-char sha of the commit that was built. Commit and push homelab `main`, and Flux reconciles.
   Pin the sha, never `latest`, because `imagePullPolicy: IfNotPresent`. The `homelab-ship` skill
   automates steps 2-3 (asks before pushing, since a homelab push deploys).
4. The rollout uses `strategy: Recreate`. The old pod dies before the new one starts, so there is a
   short outage and never two bots at once. Keep it that way, along with `replicas: 1`.
5. To verify, `kubectl -n discord logs deploy/robodoze` (JSON lines) and check `/readyz`. Grafana
   dashboard: `../homelab/apps/grafana/dashboards/robodoze-dashboard.json` (uid `robodoze-overview`).

Limits in prod are 500m CPU and 1Gi memory. Every stream is an ffmpeg subprocess, and memory
regressions have happened before (see commit 92803ec, "regather to prevent excessive memory").

## Invariants and gotchas

- **Metric names are a contract.** Renaming or relabeling anything in `utils/metrics.py` breaks
  the homelab Grafana dashboard. Update `../homelab/apps/grafana/dashboards/robodoze-dashboard.json`
  in the same change, and keep the `DEPLOY.md` metrics table accurate. Alloy scrapes via the pod
  annotations `prometheus.io/*` on port 8080.
- **Queue items are raw metadata dicts, never live sources.** Stream URLs from yt-dlp expire, so a
  dict is resolved into a `MusicSource` (which spawns ffmpeg) only at play time. The next track is
  prefetched (`_prefetch_next`) while the current one plays. Loop modes re-queue the *dict*
  (`requeue_finished`). The finished `MusicSource` is already cleaned up.
- **Loop mode is one field** (`LOOP_OFF` / `LOOP_TRACK` / `LOOP_QUEUE`), so the two modes are
  mutually exclusive by construction. Don't split it into booleans.
- `MusicQueue.insert_front` and `append_back` bypass `put_nowait`. They don't wake getters, and
  `append_back` ignores maxsize (a bounded overflow of maxsize+1). Only call them from the
  player-loop tail. Tests cover this, so extend `tests/test_queue.py` when you touch queue
  semantics.
- Under queue-loop the queue never drains, so the 300s idle timeout never fires.
  `_ensure_alone_timer()` re-checks "alone in channel" once per track so the bot can't stream to
  nobody forever. Both it and `cog.on_voice_state_update` must use the shared
  `player.has_listeners()`. If the two paths disagree (they once did about other bots), one starts
  the countdown and the other cancels it. The helper iterates `channel.voice_states` and skips
  self and users the cache knows are bots. Uncached users count as listeners.
- `player_loop` has a last-resort `except Exception` so that one bad track can never kill music for
  a guild. yt-dlp raises an unstable set of exception types, and resolve failures degrade to
  "skip track". The intentional broad catches are marked `# noqa: BLE001` with a reason comment.
  Follow that pattern.
- All yt-dlp calls run on a 4-thread executor with `asyncio.timeout` (30s single, 60s playlist). Never
  call `ytdl.extract_info` on the event loop.
- `FFMPEG_OPTIONS` (the reconnect flags and `aresample=async=1:first_pts=0`) were tuned after
  several commits about start-time drift and live streams. Change them only deliberately.
- `davey` in requirements is the DAVE (Discord voice E2EE) dependency used by discord.py voice. It is
  not imported directly, so don't remove it as "unused".
- When YouTube breaks playback, the fix is usually a `yt-dlp==` bump in `requirements.txt`. History
  shows frequent bumps and one rollback ("fix ver"). Pin exact versions.
- Missing the Embed Links permission raises `discord.Forbidden`. The code falls back to plain text or
  a hint, so keep that fallback when you add new embeds.

## Conventions

- Python 3.12 target (`py312`). Type hints use `X | None`. Log with the shared `logger` and a
  `[{guild}]` prefix. Debug-level logs are verbose on purpose.
- Commits go straight to `main` (no PRs, no semver; the image tag is the commit sha). Older
  history is short and informal ("add loop queue", "upgrade ver"). Newer commits use conventional
  `type(scope): summary` with a short why-body.
- When you add or rename a command, update the command table in `README.md`.
