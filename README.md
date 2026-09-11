# Metrics Stack — Client/Server Observability

Two-deployable observability stack for a fleet of Linux machines (homelab /
small server room). A **client** agent on each machine collects host, GPU,
hardware-RAS (memory CE/UE) and LLM-serving metrics plus syslog and container
logs, buffers them on local disk, and ships them to a central **server**
(Prometheus + Loki + Grafana + Alertmanager).

```
┌──────────────────── CLIENT (per machine) ────────────────────┐
│ alloy ── scrapes ── node-exporter / dcgm-exporter /          │
│   │                   ras-exporter / LLM backends            │
│   ├── tails ── /var/log/syslog (pipeline: labels + counters) │
│   ├── reads ── container logs (Docker API)                   │
│   ├── loki.write ──────────────── WAL on disk ─┐             │
│   └── prometheus.remote_write ─── WAL on disk ─┤             │
└────────────────────────────────────────────────┼─────────────┘
                                                  │ LAN
┌────────────────────────────────────────────────▼─────────────┐
│ SERVER (one machine): Loki · Prometheus · Grafana ·          │
│ Alertmanager — receives everything, keeps 90 days            │
└──────────────────────────────────────────────────────────────┘
```

**Crash-durable by design.** Alloy journals every log batch and every unsent
metric sample to local disk (WALs in the `alloy-data` volume) before marking
it shipped. A power loss or hard crash loses nothing: on reboot the agent
replays unsent data to the server. The raw logs also remain on the client's
disk, so the machine can be inspected even while the rest of it is down.

## Repository layout

```
client/    copy to every emitter machine (docker compose + .env)
server/    copy to the central machine (docker compose + configs)
```

---

# Installing

## 1. Server (once, on one central machine)

Requirements: Docker + compose plugin. Any Linux box, VM or Proxmox LXC
works; it can be moved later (see "Moving the server").

```bash
# copy the repo (or just server/) to the central machine, then:
cd server
docker compose up -d
docker compose ps        # loki, prometheus, alertmanager, grafana all "Up"
```

Endpoints (adjust firewall for these):

| Service      | Port | Purpose                              |
|---|---|---|
| Grafana      | 3000 | dashboards (log in as `admin` — set a new password in the UI) |
| Prometheus   | 9090 | metrics query + remote_write receiver (clients push here) |
| Loki         | 3100 | log query + push endpoint (clients push here) |
| Alertmanager | 9093 | alerts (no channel configured until you add one) |

Verify it's receiving (after at least one client is connected):

```bash
curl -s 'http://localhost:9090/api/v1/query?query=up'      # series per machine/job
curl -s 'http://localhost:3100/loki/api/v1/label/machine/values'  # ["<your machines>"]
```

Notes:

- The server has **no scrape jobs** — it only receives, stores, alerts and
  visualises. All data arrives stamped with a `machine` label set by the client.
- Retention is 90 days (Prometheus TSDB + Loki compactor); Loki accepts
  backfilled entries up to 14 days old.
- **Alerts**: `server/alertmanager/alertmanager.yml` starts with a `null`
  receiver (alerts evaluate and are visible in Prometheus `/alerts` + Grafana
  "Alerts"). To notify somewhere, add a receiver (webhook/email/telegram) and
  `docker compose up -d alertmanager`.

## 2. Client (once per emitter machine)

Requirements: Docker + compose plugin. Optional extras:
- `gpu` profile → NVIDIA driver + docker `nvidia` runtime
- `ras` profile → `rasdaemon` running on the host (`apt install rasdaemon`)

```bash
# copy the repo (or just client/) to the machine, then:
cd client
cp .env.example .env
$EDITOR .env
docker compose up -d
docker compose ps        # alloy + node-exporter (+ dcgm/ras per profiles)
```

The `.env` knobs:

| Key | Meaning |
|---|---|
| `MACHINE` | Machine name stamped onto every metric sample and log stream. Must be unique per host (e.g. `pensive`, `spark-c081`). |
| `SERVER_LOKI_URL` | `http://<server>:3100/loki/api/v1/push` |
| `SERVER_PROM_URL` | `http://<server>:9090/api/v1/write` |
| `COMPOSE_PROFILES` | Comma-separated: `gpu` (NVIDIA), `ras` (AMD/Intel RAS via rasdaemon) |
| `LLM_TARGET_1..3` | LLM serving metrics endpoints to scrape, up to 3, blank = off. Reachable from the host, e.g. `host.docker.internal:8000` (a published container port) |
| `LLM_CONTAINER_1..3` | Docker container **names** of the serving engine(s), up to 3. Their log streams are tagged `role="serving"` and shown in the Model Inference dashboard. Blank = off. |

Use a **stable name** for the server (LAN DNS or Tailscale hostname) — then
moving the server later requires no client changes.

Predefined metric sets:

| Set | What it captures | Enabled by |
|---|---|---|
| host | CPU, RAM, load, disk, network, hwmon temps, EDAC, systemd unit states | always |
| GPU | DCGM: util, VRAM, power, temp, clocks | `gpu` profile + NVIDIA driver |
| RAS | per-DIMM corrected/uncorrected memory errors (rasdaemon), MCE records | `ras` profile + rasdaemon installed |
| LLM | vLLM / llama.cpp serving metrics + serving logs (`role="serving"`) | `LLM_TARGET_1..3` / `LLM_CONTAINER_1..3` |
| logs | host syslog + **all** container logs → Loki | always |

Verify on the server after ~1 minute:

```bash
curl -s 'http://<server>:9090/api/v1/query?query=up{machine="<MACHINE>"}'
# expect up=1 for job "node" (+ "dcgm", "ras", "llm", "alloy" if enabled)
```

Client debugging: Alloy UI at `http://<client>:12345` (component health, WAL
state); hardware errors directly via `sudo ras-mc-ctl --summary`.

## 3. Dashboards (server Grafana, provisioned automatically)

- **Host & System Logs** — per-machine ($machine variable): uptime/load/CPU/RAM,
  hardware RAS (CE/UE totals, CE rate, per-DIMM breakdown, CPU Tctl, EDAC),
  syslog error counters, system/container logs, kernel MCE/hardware-error lines.
- **Model Inference** — per-machine LLM throughput, GPU, host, serving logs
  (the `role="serving"` container only).

---

# Extending the stack

## Onboarding a new docker service / container

| You have... | Steps |
|---|---|
| **Any container, want its logs** | Nothing. All containers on a client machine are captured automatically (`job="docker"`, `container=<name>` label, picked up within ~60s of starting). |
| **A vLLM or llama.cpp serving engine** | 1) Publish its metrics port on the host (`-p 8000:8000`). 2) In the client `.env`: `LLM_TARGET_N=host.docker.internal:<port>` and `LLM_CONTAINER_N=<container name>`. 3) `docker compose up -d alloy`. Works out of the box — the recording rules already know vLLM and llama.cpp metric families. |
| **A new engine with different metric names** | The steps above, plus add a recording rule in `server/prometheus/rules/llm.yml` mapping its metric families into the `llm:*` namespace, then `docker compose up -d prometheus` on the server. Copy an existing rule — note the `sum by (machine) ... or on(machine) ...` shape: the `or` keeps a machine's value from whichever backend is up, and `by (machine)` keeps hosts separate. |
| **A non-LLM service with custom Prometheus metrics** | 1) Make the endpoint reachable from the client host (published port, or attach the container to the `metrics-stack-client_client-net` network). 2) Add a `prometheus.scrape "<name>"` block to `client/alloy/config.alloy` with its own `job` label (copy the `prometheus.scrape "ras"` block as a template). 3) `docker compose up -d alloy`. 4) Optionally add panels (Grafana UI, or edit `server/grafana/dashboards/*.json`) and alert rules (below). |
| **A new alert** | Add a rule to `server/prometheus/rules/*.yml` (or a new file — everything in that dir is loaded). Multi-host rules group by machine: `sum by (machine) (...)` or `max by (machine) (...)`. `docker compose up -d prometheus alertmanager`. |
| **A new machine** | "Installing → 2. Client". No server-side changes needed. |

After any client `.env` / `config.alloy` change: `docker compose up -d alloy`
(recreates the container; WAL state in the `alloy-data` volume is preserved,
so nothing in-flight is lost).

## How model-inference monitoring is wired (for reference)

The server is model-agnostic; the per-machine decisions live in the client
`.env`:

1. `LLM_TARGET_1..3` → `client/alloy/entrypoint.sh` materializes the
   non-blank slots into `llm-targets.json` (Prometheus file-SD format) at
   container start → `discovery.file "llm"` → `prometheus.scrape "llm"`
   (`job="llm"`) → `prometheus.remote_write` (adds `machine`).
2. `LLM_CONTAINER_1..3` → relabel rules in `client/alloy/config.alloy` tag
   those containers' log streams with `role="serving"` (a blank slot compiles
   to a regex that matches nothing).
3. `server/prometheus/rules/llm.yml` normalizes backend-specific families
   (`vllm:*`, `llamacpp:*`) into `llm:decode_tokens_per_second`,
   `llm:prompt_tokens_per_second`, `llm:requests_running`,
   `llm:requests_queued`, `llm:tokens_predicted_total` — per machine.
4. The Model Inference dashboard consumes `llm:*` + DCGM + node metrics
   (all filtered by `$machine`) and the `role="serving"` log stream.

## Moving the server

1. Stop it: `docker compose down` (keep the directory and volumes).
2. Copy the docker volumes to the new host (project name
   `metrics-stack-server` prefixes the volume names):
   ```bash
   # on the OLD host: tar each volume out, then scp /tmp/mst/*.tar.gz over
   for v in loki-data prometheus-data grafana-data alertmanager-data; do
     docker run --rm -v metrics-stack-server_$v:/data:ro -v /tmp/mst:/out alpine \
       tar czf /out/$v.tar.gz -C /data .
   done

   # on the NEW host: create the volumes and unpack into them
   for v in loki-data prometheus-data grafana-data alertmanager-data; do
     docker volume create metrics-stack-server_$v
     docker run --rm -v metrics-stack-server_$v:/data -v /tmp/mst:/in alpine \
       tar xzf /in/$v.tar.gz -C /data
   done
   ```
3. `cd server && docker compose up -d`.
4. If the server URL changed (no stable hostname), update `SERVER_LOKI_URL` /
   `SERVER_PROM_URL` in each client's `.env` and `docker compose up -d alloy`.

---

# Notes & trade-offs

- **Duplicates after crash replay**: delivery is at-least-once; a replayed
  batch can produce duplicate log lines in Loki. Loki has no dedup.
- **Loki accepts entries up to 14 days old** (`reject_old_samples_max_age`);
  retention is 90 days (Loki + Prometheus TSDB).
- **Alloy stability**: the client runs Alloy with `--stability.level=experimental`
  to enable the `loki.write` WAL. Everything else used is GA.
- **No transport auth**: the client→server traffic is plain HTTP; keep the
  server reachable only from your trusted LAN/tailnet (or add basic_auth to
  the `loki.write` / `prometheus.remote_write` endpoint blocks and matching
  auth on the server later).
- **node_exporter label collision**: `node_uname_info` carries its own
  `machine="x86_64"` label (CPU arch), which the `machine` external label can
  not override. A `prometheus.relabel "host"` component in the client config
  drops the exporter's `machine` label before shipping, so the stamped machine
  wins everywhere (dropdown included).
- **systemd collector requirements**: node-exporter runs with
  `--collector.systemd` and reaches the host D-Bus via
  `DBUS_SYSTEM_BUS_ADDRESS=unix:path=/host/run/dbus/system_bus_socket` (the
  socket is already visible through the `/host` read-only bind — no extra
  mount). Ubuntu ≥ 23.10 dbus-daemon enforces AppArmor mediation that rejects
  Docker-confined clients, so the exporter runs with
  `security_opt: apparmor:unconfined` (ignored on non-AppArmor kernels). On
  distros without systemd/dbus the collector logs one error and the rest of
  node-exporter works normally — check
  `node_scrape_collector_success{collector="systemd"}` to verify per machine.
- **Pinned versions**: all images are pinned (client: alloy v1.19.2,
  node-exporter v1.12.1, dcgm-exporter 4.8.3, python 3.12.14-slim; server:
  Loki 3.7.7, Prometheus v3.14.0, Grafana 13.2.1, Alertmanager v0.34.0).
  Upgrade = bump the tag + `docker compose up -d`; `:latest` drifts silently
  across a fleet deployed over months.
- Alloy agent UI/debug: http://<client>:12345 (component health, WAL state).
- To inspect hardware errors on a client directly: `sudo ras-mc-ctl --summary`.
