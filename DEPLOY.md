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

Use this image in place of the locally-built `robodoze:local` wherever it appears below. For Kubernetes, update the `image:` field in `k8s/deployment.yaml` to the GHCR reference and set `imagePullPolicy: Always`.

---

## Prerequisites (Kubernetes)

- Docker
- `kubectl` configured for your local cluster (minikube or k3s)
- A Discord bot token

---

## minikube

### 1. Build the image inside minikube's Docker daemon

```bash
eval $(minikube docker-env)
docker build -t robodoze:local .
```

### 2. Set your Discord token

Edit `k8s/secret.yaml` — replace the placeholder with your real base64-encoded token:

```bash
echo -n "your_actual_token_here" | base64
# Paste the output as the value of DISCORD_TOKEN in k8s/secret.yaml
```

> **Never commit this file after editing.** Consider keeping the real secret out of source control entirely and applying it imperatively:
> ```bash
> kubectl create secret generic robodoze-secret \
>   --from-literal=DISCORD_TOKEN=your_actual_token_here
> ```

### 3. Deploy

```bash
kubectl apply -k k8s/
```

### 4. Verify

```bash
kubectl rollout status deployment/robodoze
kubectl logs -f deployment/robodoze
```

### 5. Health check

```bash
kubectl port-forward deployment/robodoze 8080:8080 &
curl http://localhost:8080/healthz   # always 200 while process is alive
curl http://localhost:8080/readyz    # 200 after Discord on_ready fires
```

---

## k3s

### 1. Import the image

```bash
docker build -t robodoze:local .
docker save robodoze:local | sudo k3s ctr images import -
```

Or if using k3d with a local registry:

```bash
k3d registry create myregistry.localhost --port 5000
docker build -t localhost:5000/robodoze:local .
docker push localhost:5000/robodoze:local
# Update image in k8s/deployment.yaml to localhost:5000/robodoze:local
# and set imagePullPolicy: Always
```

### 2–5. Same as minikube steps above.

---

## Update flow

Rebuild the image and restart the deployment. The `Recreate` strategy stops the old pod before starting the new one:

```bash
# (minikube) rebuild inside the daemon
eval $(minikube docker-env)
docker build -t robodoze:local .

kubectl rollout restart deployment/robodoze
kubectl rollout status deployment/robodoze
```

## Tear down

```bash
kubectl delete -k k8s/
```

---

## Observability (LGTM stack)

The bot exposes Prometheus metrics on the same port as the health server at
`GET /metrics` (default `:8080`). Point Prometheus/Mimir at it via pod
annotations:

```yaml
metadata:
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "8080"
    prometheus.io/path: "/metrics"
```

…or, with the Prometheus Operator, a `ServiceMonitor` selecting the bot's
Service on the `8080` port and `/metrics` path.

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

Standard `python_*` / `process_*` runtime metrics are included automatically.
