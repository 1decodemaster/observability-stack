#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Enable RAM (DIMM) temperature collection on this CLIENT machine.
#
# Loads the IPMI kernel modules so /dev/ipmi0 exists, sanity-checks the BMC's
# memory sensors, builds + starts the ipmi-exporter, and verifies it serves
# metrics. Safe to re-run. Requires root (kernel modules + /dev/ipmi0).
#
# Usage:   sudo bash client/enable-ipmi-ramtemp.sh      # from the repo root
# ---------------------------------------------------------------------------
set -euo pipefail

CLIENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> 1/4  IPMI kernel modules (creates /dev/ipmi0)"
modprobe ipmi_si
modprobe ipmi_devintf
mkdir -p /etc/modules-load.d
printf 'ipmi_si\nipmi_devintf\n' > /etc/modules-load.d/ipmi.conf   # persist across reboots
if [[ ! -e /dev/ipmi0 ]]; then
  echo "ERROR: /dev/ipmi0 still missing — BMC not reachable in-band." >&2
  echo "       Check: dmesg | grep -i ipmi" >&2
  echo "       Until fixed, drop 'ipmi' from COMPOSE_PROFILES in client/.env." >&2
  exit 1
fi
echo "    /dev/ipmi0 present"

echo "==> 2/4  BMC memory-temperature sanity check"
# Expect lines like:  Mem 01 Temp | ... | 32 degC | ok | na | na | ... | 85 degC | ... | 95 degC | ...
if ! ipmitool sensor list | grep -iE 'mem|dimm|ram'; then
  echo "WARNING: no memory/DIMM sensors visible via ipmitool."
  echo "         ipmi-exporter will still start, but report ipmi_mem_sensor_count 0."
fi

echo "==> 3/4  build ipmi-exporter + restart the client stack"
cd "$CLIENT_DIR"
docker compose build ipmi-exporter
docker compose up -d          # recreates alloy (config.alloy changed); WAL state preserved

echo "==> 4/4  verify exporter (port 9109 inside the client network)"
sleep 3
for i in 1 2 3 4 5; do
  if out=$(docker exec metrics-stack-client-ipmi-exporter \
        python -c 'import urllib.request as u; print(u.urlopen("http://127.0.0.1:9109/metrics").read().decode())' \
        2>/dev/null); then
    echo "$out" | grep -E '^(ipmi_exporter_up|ipmi_mem_sensor_count|ipmi_mem_temp)' || true
    echo "OK — exporter is serving."
    break
  fi
  echo "    exporter not answering yet ($i/5)..."
  sleep 2
done

cat <<'EOF'

Done on the client. Last step — reload rules + dashboards on the SERVER machine:

    cd <repo>/server
    # sync the changed files first (git pull, or scp):
    #   server/prometheus/rules/ras.yml
    #   server/grafana/dashboards/host.json
    docker compose up -d prometheus grafana

Then check on the server:
    curl -s 'http://localhost:9090/api/v1/query?query=ipmi_mem_temp_celsius'

and open the Host & System Logs dashboard -> Hardware RAS row
(new panels: "RAM (DIMM) temperature", "Hottest DIMM (now)",
 "DIMM temperatures (now)" — yellow at 80C, red at 90C).
EOF
