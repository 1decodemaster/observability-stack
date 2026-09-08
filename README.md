# Metrics Stack — Client/Server Observability

Two-deployable observability stack for a fleet of Linux machines (homelab /
small server room). A **client** agent on each machine collects host, container,
GPU, hardware-RAS (memory CE/UE, SMART) and LLM-serving metrics plus system and
container logs, buffers them on local disk, and ships them to a central
**server** (Prometheus + Loki + Grafana + Alertmanager).

```
┌──────────────────── CLIENT (per machine) ────────────────────┐
│ alloy ── scrapes ── node-exporter / cadvisor / dcgm /        │
│   │                 ras / smartctl / LLM backends            │
│   ├── reads ─── systemd journal (or /var/log/{syslog,messages})│
│   ├── reads ─── container logs (Docker API)                  │
│   ├── loki.write ──────────────── WAL on disk ─┐             │
│   └── prometheus.remote_write ─── WAL on disk ─┤             │
└────────────────────────────────────────────────┼─────────────┘
                                                  │ LAN
┌────────────────────────────────────────────────▼─────────────┐
│ SERVER (one machine): Loki · Prometheus · Grafana ·          │
│ Alertmanager — receives everything, keeps 90 days, and       │
│ scrapes ITSELF (host + containers + each component) so a     │
│ full disk is visible instead of silently ending collection   │
└──────────────────────────────────────────────────────────────┘
```

**Crash-durable by design.** Alloy journals every log batch and every unsent
metric sample to local disk (WALs in the `alloy-data` volume) before marking it
shipped. A power loss or hard crash loses nothing: on reboot the agent replays
unsent data to the server. For the replay to be *accepted*, the server sets
`storage.tsdb.out_of_order_time_window: 24h` — without it Prometheus refuses
every sample older than its in-memory head block, and the client's WAL would
buffer faithfully into a bin. Loki accepts backfill up to 14 days old.

## Repository layout

```
client/    copy to every emitter machine (docker compose + .env)
server/    copy to the central machine (docker compose + configs)
```

---

# Installing

## 1. Server (once, on one central machine)

Requirements: Docker + compose plugin. Any Linux box, VM or Proxmox LXC works;
it can be moved later (see "Moving the server").

```bash
git clone https://github.com/1decodemaster/observability-stack.git
cd observability-stack/server
cp .env.example .env
$EDITOR .env                 # GF_ADMIN_PASSWORD is required — there is no default
docker compose up -d
docker compose ps            # loki, prometheus, alertmanager, grafana,
                             # node-exporter, cadvisor all "Up"
```

Endpoints (adjust firewall for these):

| Service      | Port | Purpose                              |
|---|---|---|
| Grafana      | 3000 | dashboards (log in with the credentials from `.env`) |
| Prometheus   | 9090 | metrics query + remote_write receiver (clients push here) |
| Loki         | 3100 | log query + push endpoint (clients push here) |
| Alertmanager | 9093 | alerts (no channel configured until you add one) |
| node-exporter| 9100 | the server's own host metrics |

Verify it's receiving (after at least one client is connected):

```bash
curl -s 'http://localhost:9090/api/v1/query?query=up'      # series per machine/job
curl -s 'http://localhost:3100/loki/api/v1/label/machine/values'  # ["<your machines>"]
```

Notes:

- **The server monitors itself.** It scrapes its own host (node-exporter),
  its own containers (cAdvisor) and each stack component's `/metrics`, all
  labelled `machine="metrics-server"` (rename in `prometheus/prometheus.yml`
  if you prefer the real hostname). This is what makes a filling disk on the
  Proxmox box an alert rather than a mystery outage.
- Retention is bounded **two ways**: 90 days *and* 50 GB
  (`PROM_RETENTION_TIME` / `PROM_RETENTION_SIZE` in `.env`), because time-based
  retention alone cannot stop a growing fleet from filling the volume.
- **Alerts**: `server/alertmanager/alertmanager.yml` starts with a `null`
  receiver (alerts evaluate and are visible in Prometheus `/alerts` + Grafana
  "Alerts"). To notify somewhere, add a receiver (webhook/email/telegram) and
  `docker compose up -d alertmanager`. Grouping and inhibition are already
  configured — a machine going down produces one notification, not twelve.

### Optional: Proxmox VE metrics

Guest (VM/LXC) state and storage/ZFS pool usage, via
`prometheus-pve-exporter`. On the Proxmox host, create a read-only token:

```bash
pveum user add prometheus@pve
pveum aclmod / -user prometheus@pve -role PVEAuditor
pveum user token add prometheus@pve monitoring --privsep 0   # prints the secret once
```

Then on the server:

```bash
cd server
cp pve/pve.yml.example pve/pve.yml && $EDITOR pve/pve.yml         # paste the token
cp prometheus/targets/pve.local.json.example prometheus/targets/pve.local.json
$EDITOR prometheus/targets/pve.local.json                        # your PVE node's address
echo 'COMPOSE_PROFILES=pve' >> .env
docker compose up -d
```

The scrape job reads `prometheus/targets/pve*.json`. The tracked
`prometheus/targets/pve.json` stays `[]`, so the job is inert on a server that
isn't a Proxmox host — no permanent `up=0` series — and your real node list
lives in `pve.local.json`, which is gitignored along with `pve/pve.yml`. Keeping
both out of git is what lets `git pull --ff-only` stay conflict-free forever.

## 2. Client (once per emitter machine)

Requirements: Docker + compose plugin. Optional extras:
- `gpu` profile → NVIDIA driver + NVIDIA container toolkit
- `ras` profile → `rasdaemon` running on the host with `--record`
- `smart` profile → nothing (the exporter runs privileged and reads `/dev`)

```bash
git clone https://github.com/1decodemaster/observability-stack.git
cd observability-stack/client
cp .env.example .env
$EDITOR .env
docker compose up -d
docker compose ps        # alloy, node-exporter, cadvisor (+ profiles)
```

The `.env` knobs:

| Key | Meaning |
|---|---|
| `MACHINE` | Machine name stamped onto every metric sample and log stream. Must be unique per host (e.g. `pensive`, `spark-c081`). |
| `SERVER_LOKI_URL` | `http://<server>:3100/loki/api/v1/push` |
| `SERVER_PROM_URL` | `http://<server>:9090/api/v1/write` |
| `COMPOSE_PROFILES` | Comma-separated: `gpu`, `ras`, `smart`. Also read by Alloy, which only creates scrape jobs for exporters that are actually running. |
| `SCRAPE_INTERVAL` | Default `15s`. **Do not raise above 15s** — the `llm:*` recording rules and several panels use `rate(...[1m])`, which needs at least two samples per minute or it silently returns nothing. |
| `LOG_SOURCE` | `auto` (default), `journal`, or `file`. See "Logs across distros". |
| `LLM_TARGET_1..3` | LLM serving metrics endpoints to scrape, up to 3, blank = off. `host.docker.internal:8000` works for a published host port. |
| `LLM_CONTAINER_1..3` | Docker container **names** of the serving engine(s). Their log streams are tagged `role="serving"`. Blank = off. |
| `DCGM_IMAGE` | Override the dcgm-exporter image/tag if you need a pinned one. |

Use a **stable name** for the server (LAN DNS or Tailscale hostname) — then
moving the server later requires no client changes.

Predefined metric sets:

| Set | What it captures | Enabled by |
|---|---|---|
| host | CPU (per mode), RAM, swap, load, PSI, disk I/O + latency + capacity, network + errors, hwmon temps, fans, RAPL, EDAC | always |
| containers | per-container CPU, memory vs limit, network, disk, restarts, OOM kills (cAdvisor) | always |
| GPU | DCGM: util, VRAM, power, temp, clocks, **ECC SBE/DBE, retired/remapped rows, XID, PCIe replays, throttling** | `gpu` profile |
| RAS | per-DIMM corrected/uncorrected memory errors (rasdaemon), MCE records | `ras` profile |
| disks | SMART/NVMe health, wear, media errors, temperature | `smart` profile |
| LLM | vLLM / llama.cpp throughput, queue depth, KV cache, TTFT/TPOT + serving logs | `LLM_TARGET_*` / `LLM_CONTAINER_*` |
| logs | system journal (or syslog file) + **all** container logs → Loki | always |

Verify on the server after ~1 minute:

```bash
curl -s 'http://<server>:9090/api/v1/query?query=up{machine="<MACHINE>"}'
# expect up=1 for jobs "node", "cadvisor", "alloy" (+ "dcgm"/"ras"/"smart"/"llm")
```

Client debugging: Alloy UI at `http://<client>:12345` (component health, WAL
state). The agent prints its two startup decisions in its first log lines:

```bash
docker logs metrics-stack-client-alloy 2>&1 | head -3
# host gateway: 172.20.0.1
# log transport: journald (/var/log/journal)
```

## 3. Dashboards (server Grafana, provisioned automatically)

- **Fleet Overview** — start here. Every machine at once: who is reporting, who
  went silent, an inventory table (OS, kernel, arch, cores, RAM, uptime), CPU /
  memory / filesystem / temperature pressure, and per-machine shipping rates.
- **Host & System Logs** — one machine in depth: CPU by mode, memory, PSI, disk
  throughput/latency/capacity, network errors, thermals and power, GPU
  (including ECC/XID/throttling), memory RAS (CE/UE per DIMM, EDAC), SMART
  drive health, and the system + container log streams.
- **Containers** — per-container CPU, memory against limit, network, disk,
  restarts and OOM kills, with the matching logs.
- **Collection Health** — is the pipeline itself working. Agent last-seen, WAL
  lag, send failures, dropped log entries, scrape duration, cardinality growth,
  plus the server's own storage and component health.
- **Model Inference** — LLM throughput, queue depth, KV cache occupancy,
  TTFT/TPOT percentiles, GPU, host, and the `role="serving"` log stream.
- **Proxmox VE** — guest state, per-guest CPU/memory/IO, node capacity and
  storage pool usage. Empty unless the `pve` profile is configured.

---

# Logs across distros

Distros disagree about where system logs live, and reading two sources at once
duplicates every line. The agent picks exactly one at startup:

| Host | `LOG_SOURCE=auto` picks |
|---|---|
| Any host with a systemd journal (Debian, Ubuntu, RHEL, Fedora, Arch, openSUSE…) | **journald** — `/var/log/journal`, or `/run/log/journal` if the journal is volatile |
| No journal, Debian/Ubuntu | `/var/log/syslog` |
| No journal, RHEL family | `/var/log/messages` |
| Neither | nothing (container logs still ship; the agent says so in its log) |

journald is preferred wherever it exists: on Debian/Ubuntu, rsyslog writes the
same records the journal already holds, so reading both would double every
line. Force the other way with `LOG_SOURCE=file` if you have a reason to.

Either way the streams arrive labelled `job="syslog"` with a `program` label, so
every dashboard panel, `stage.match` selector and alert works identically on
every distro. A `transport="journal"|"file"` label records which path was taken.

**systemd unit failures** are counted from the log stream
(`syslog_unit_failed_total`), not from node-exporter's `--collector.systemd`.
That collector needs D-Bus access which stock AppArmor on Ubuntu denies to
containers, and it then logs an error on every scrape. To use it anyway,
re-enable the flag and the `/run/dbus/system_bus_socket` mount in
`client/docker-compose.yml`.

---

# Updating

The stack pins no image tags — every service tracks `:latest` — so there are two
independent things to update: **this repo's configuration**, and **the images**.

## Update the configuration (pull the latest commit)

On each machine, from the repo checkout:

```bash
cd observability-stack
git pull --ff-only origin main
```

`.env` files are gitignored and survive the pull. Then apply, on the server:

```bash
cd server
diff <(git show HEAD:server/.env.example) .env.example   # any new knobs to copy in?
docker compose up -d
```

and on each client:

```bash
cd client
diff <(git show HEAD:client/.env.example) .env.example   # any new knobs to copy in?
docker compose up -d
```

`docker compose up -d` recreates only what actually changed. Dashboards and
alert rules are picked up without a restart — Grafana re-reads
`server/grafana/dashboards/` every 10s, and Prometheus reloads rules on
`docker compose up -d prometheus`.

To move to a specific commit or release tag instead of the branch tip:

```bash
git fetch --tags origin
git checkout <tag-or-commit>      # e.g. git checkout v1.2.0
docker compose up -d              # in both server/ and client/
```

Check what you are running, and what changed since:

```bash
git rev-parse --short HEAD                 # the commit this machine runs
git log --oneline HEAD..origin/main        # what a pull would bring in
```

## Update the images

```bash
docker compose pull && docker compose up -d      # in server/, then each client/
docker image prune -f                            # reclaim the superseded layers
```

Data survives both operations: everything durable lives in named volumes
(`prometheus-data`, `loki-data`, `grafana-data`, `alertmanager-data` on the
server; `alloy-data` on each client), and `up -d` never touches them.

## Rolling out to the whole fleet

There is no push mechanism — each client is independent. A loop over SSH is
enough:

```bash
for h in pensive spark-c081 othermachine; do
  ssh "$h" 'cd observability-stack && git pull --ff-only origin main \
            && cd client && docker compose pull && docker compose up -d'
done
```

Update the server first: clients tolerate a server that is briefly away (their
WALs buffer and replay), but a client shipping a metric the server's rules don't
know about yet is harmless, whereas the reverse leaves new panels empty.

## After updating

`Collection Health` answers "did that break anything?" in one screen — every
machine's last-seen time, targets up/down, and WAL lag. Give it two minutes to
refill the rate windows before reading it.

---

# Extending the stack

## Onboarding a new docker service / container

| You have... | Steps |
|---|---|
| **Any container** | Nothing. Logs (`job="docker"`, `container=<name>`) and resource metrics (cAdvisor) are both captured automatically, within ~30s of the container starting. |
| **A vLLM or llama.cpp serving engine** | 1) Publish its metrics port on the host (`-p 8000:8000`). 2) In the client `.env`: `LLM_TARGET_N=host.docker.internal:<port>` and `LLM_CONTAINER_N=<container name>`. 3) `docker compose up -d alloy`. Works out of the box — the recording rules already know both backends' metric families. |
| **A new engine with different metric names** | The steps above, plus add a recording rule in `server/prometheus/rules/llm.yml` mapping its metric families into the `llm:*` namespace, then `docker compose up -d prometheus` on the server. Copy an existing rule — note the `sum by (machine) ... or on(machine) ...` shape: the `or` keeps a machine's value from whichever backend is up, and `by (machine)` keeps hosts separate. |
| **A non-LLM service with custom Prometheus metrics** | 1) Make the endpoint reachable from the client host (published port, or attach the container to the `metrics-stack-client_client-net` network). 2) Add a `discovery.file` + `prometheus.scrape` pair to `client/alloy/config.alloy` with its own `job` label, and have `entrypoint.sh` write its target file (copy the `ras` pattern). 3) `docker compose up -d alloy`. |
| **A new alert** | Add a rule to `server/prometheus/rules/*.yml` (or a new file — everything in that dir is loaded). Multi-host rules group by machine: `sum by (machine) (...)` or `max by (machine) (...)`. `docker compose up -d prometheus alertmanager`. |
| **A new machine** | "Installing → 2. Client". No server-side changes needed. |

After any client `.env` / `config.alloy` change: `docker compose up -d alloy`
(recreates the container; WAL state in the `alloy-data` volume is preserved,
so nothing in-flight is lost).

## Alert rules

| File | Covers |
|---|---|
| `collection.yml` | agent down, machine stopped reporting, remote_write failing, WAL backlog, dropped log entries, host logs stopped |
| `host.yml` | filesystem full + fill forecast, memory pressure, OOM kill, load, swap thrashing, unit failures, reboot, NIC errors |
| `containers.yml` | restart loops, container OOM kills, memory-limit proximity, CPU throttling |
| `ras.yml` | memory UE, CE bursts, hardware temperature |
| `gpu.yml` | GPU DBE/SBE ECC, row-remap failure, XID, overtemp, throttling, VRAM full |
| `disk.yml` | SMART failure, NVMe media errors and wear, reallocated sectors, drive temperature |
| `llm.yml` | backend down, queue backlog, KV cache saturation, stalled serving |
| `server.yml` | stack component down, Alertmanager delivery failing, sample rejection, rule-evaluation failure, Loki discards, TSDB size |
| `proxmox.yml` | guest stopped, storage full + fill forecast, node memory pressure |

`MachineStoppedReporting` deserves a note: `up` disappears five minutes after an
agent's last sample, so `up == 0` can never catch a machine that vanished
entirely. That rule compares "seen in the last 24h" against "present now"
instead, and auto-resolves 24h after the last sample.

## How model-inference monitoring is wired (for reference)

The server is model-agnostic; the per-machine decisions live in the client
`.env`:

1. `LLM_TARGET_1..3` → `client/alloy/entrypoint.sh` materializes the non-blank
   slots into `targets/llm.json` (Prometheus file-SD format) at container start
   → `discovery.file "llm"` → `prometheus.scrape "llm"` (`job="llm"`) →
   `prometheus.remote_write` (adds `machine`).
2. `LLM_CONTAINER_1..3` → relabel rules in `client/alloy/config.alloy` tag those
   containers' log streams with `role="serving"` (a blank slot compiles to a
   regex that matches nothing).
3. `server/prometheus/rules/llm.yml` normalizes backend-specific families
   (`vllm:*`, `llamacpp:*`) into `llm:decode_tokens_per_second`,
   `llm:prompt_tokens_per_second`, `llm:requests_running`,
   `llm:requests_queued`, `llm:tokens_predicted_total`,
   `llm:kv_cache_usage_ratio` and the vLLM-only latency percentiles — per machine.
4. The Model Inference dashboard consumes `llm:*` + DCGM + node metrics (all
   filtered by `$machine`) and the `role="serving"` log stream.

## Moving the server

1. Stop it: `docker compose down` (keep the directory and volumes).
2. Copy the docker volumes to the new host (project name `metrics-stack-server`
   prefixes the volume names):
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

- **node-exporter must use the host network namespace.** `/proc/net/*` is
  namespace-scoped: on a bridge network every `node_network_*`, `netstat`,
  `sockstat` and conntrack metric describes the container's veth rather than the
  host's interfaces. It therefore binds `:9100` on the host directly.
- **`host.docker.internal` is rewritten at startup.** Docker's `host-gateway`
  always resolves to the *default* bridge (docker0, 172.17.0.1), but these
  containers run on a compose-created bridge with a different gateway, and
  inter-bridge traffic is dropped. `entrypoint.sh` reads the real gateway from
  `/proc/net/route` and rewrites `/etc/hosts`, so both node-exporter and any
  `LLM_TARGET_*` pointing at a published host port are reachable.
- **DCGM framebuffer metrics are in MiB**, not bytes — panels multiply by
  1024², they do not divide by 1e9.
- **Duplicates after crash replay**: delivery is at-least-once; a replayed batch
  can produce duplicate log lines in Loki. Loki has no dedup.
- **Loki accepts entries up to 14 days old** (`reject_old_samples_max_age`);
  retention is 90 days (Loki + Prometheus TSDB).
- **Alloy stability**: the client runs Alloy with `--stability.level=experimental`
  to enable the `loki.write` WAL. Everything else used is GA.
- **Alloy runs as root** on the client: reading the journal, `/var/log/syslog`
  (mode 0640 root:adm) and the docker socket all require it.
- **No transport auth**: the client→server traffic is plain HTTP; keep the
  server reachable only from your trusted LAN/tailnet (or add basic_auth to the
  `loki.write` / `prometheus.remote_write` endpoint blocks and matching auth on
  the server later).
- **`node_uname_info` label collision**: that metric ships its own
  `machine="<cpu arch>"` label, which wins over the external label and would
  make the series useless for inventory. A relabel rule in `config.alloy` moves
  it to `arch` and restores the real machine name — which is what lets the Fleet
  Overview inventory table work on a mixed x86_64/aarch64 fleet.
- **Proxmox LXC caveat**: if the server runs in an unprivileged LXC without
  lxcfs, its node-exporter reports the *host's* CPU and memory rather than the
  container's. The `pve` profile's per-guest metrics are the accurate source in
  that case.
- Alloy agent UI/debug: http://<client>:12345 (component health, WAL state).
- To inspect hardware errors on a client directly: `sudo ras-mc-ctl --summary`.
