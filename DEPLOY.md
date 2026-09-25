# Deployment Guide

## Docker Compose

The simplest way to run RoboDoze locally or on a single host.

### 1. Create your env file

```bash
cp .env.example .env
```

Edit `.env` and set your Discord bot token:

```
DISCORD_TOKEN=your_actual_token_here
```

> **Never commit `.env`.** It is already in `.gitignore`.

### 2. Build and start

```bash
docker compose up --build -d
```

### 3. Verify

```bash
docker compose logs -f
curl http://localhost:8080/healthz   # always 200 while the container is alive
curl http://localhost:8080/readyz    # 200 after Discord on_ready fires
curl http://localhost:8080/metrics   # Prometheus exposition format
```

### Update flow

```bash
docker compose up --build -d
```

Compose will rebuild the image and recreate the container automatically.

### Tear down

```bash
docker compose down
```

---

## Pulling from GHCR

Pre-built images are published to GitHub Container Registry on every merge to `main`. No semantic versioning — tags are:

| Tag | Meaning |
|---|---|
| `latest` | most recent build from `main` |
| `main` | same as `latest`, branch-named |
| `<short-sha>` | exact commit (e.g. `a1b2c3d`) |

```bash
docker pull ghcr.io/mikegio27/robodoze:latest
```

Pin a short-sha tag for anything long-lived; `latest` and `main` move on every push.

CI (`.github/workflows/docker.yml`) runs `ruff check`, `ruff format --check` and the unit
tests first. The image is only built and pushed if they pass.

---

## Production (homelab k3s, Flux GitOps)

The production manifests are not in this repo. They live in the homelab repo at
[`../homelab/apps/discord/`](../homelab/apps/discord/) (namespace `discord`) and are
reconciled by Flux:

| File | Contents |
|---|---|
| `deployment.yaml` | the pinned `image:`, `replicas: 1`, `strategy: Recreate`, probes, `prometheus.io/*` annotations |
| `configmap.yaml` | `COMMAND_PREFIX` (`rd-`), `LOG_LEVEL`, `LOG_FORMAT` (`json`), `DOZAI_URL`, `DOZAI_MODEL` |
| `dozai-sealed-secret.yaml` | `DOZAI_TOKEN`: the dozai service client `robodoze` (interactive), sealed |
| `namespace.yaml`, `kustomization.yaml` | namespace and kustomize wiring |

`DISCORD_TOKEN` comes from the Secret `robodoze-secret`, which is created out of band and
never committed.

### Shipping a new build

1. Push to `main` here and wait for the Actions run to go green.
2. In `../homelab/apps/discord/deployment.yaml`, set
   `image: ghcr.io/mikegio27/robodoze:<short-sha>` (the 7-char sha of the built commit).
   Always pin the sha, never `latest`: the pull policy is `IfNotPresent`.
3. Commit and push homelab `main`. Flux applies it, and the `Recreate` strategy stops the old
   pod before starting the new one, so there is a short outage but never two bots answering
   at once.

The `homelab-ship` Claude Code skill automates steps 1–3 (it finds the latest green build,
bumps the tag, validates the render and commits, asking before it pushes).

### Verify

```bash
kubectl -n discord rollout status deploy/robodoze
kubectl -n discord logs -f deploy/robodoze     # JSON lines (LOG_FORMAT=json)
kubectl -n discord port-forward deploy/robodoze 8080:8080 &
curl http://localhost:8080/readyz
```

---

## Observability (LGTM stack)

The bot exposes Prometheus metrics on the same port as the health server at
`GET /metrics` (default `:8080`). In the homelab, Alloy scrapes it via these pod
annotations (already set in `deployment.yaml`), and the Grafana dashboard lives at
`../homelab/apps/grafana/dashboards/robodoze-dashboard.json`:

```yaml
metadata:
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "8080"
    prometheus.io/path: "/metrics"
```

### Metrics emitted

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `robodoze_guilds` | gauge | — | Servers the bot is in |
| `robodoze_voice_connections_active` | gauge | — | Live voice connections |
| `robodoze_players_active` | gauge | — | Active music players |
| `robodoze_queued_tracks` | gauge | — | Tracks queued across all players |
| `robodoze_commands_total` | counter | `command`, `status` | Commands invoked (success/error) |
| `robodoze_command_duration_seconds` | histogram | `command` | Command handling latency |
| `robodoze_tracks_queued_total` | counter | `kind` (play/playlist) | Tracks added to a queue |
| `robodoze_streams_started_total` | counter | — | Audio streams started |
| `robodoze_audio_bytes_streamed_total` | counter | — | PCM bytes streamed to Discord |
| `robodoze_stream_errors_total` | counter | `stage` (resolve/playback) | Stream failures |
| `robodoze_source_resolve_seconds` | histogram | — | yt-dlp resolution time |
| `robodoze_asks_total` | counter | `status` (ok/error) | `ask` questions answered |
| `robodoze_ask_duration_seconds` | histogram | — | Question to answer (queue, web search, generation) |

Standard `python_*` / `process_*` runtime metrics are included automatically.
