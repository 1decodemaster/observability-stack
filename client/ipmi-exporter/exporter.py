#!/usr/bin/env python3
"""Prometheus exporter for BMC (IPMI) memory / DIMM temperature sensors.

Parses `ipmitool sensor list` for temperature sensors whose names indicate
RAM (mem/dimm/ram/memory/ddr...) and exposes, per sensor:

  ipmi_mem_temp_celsius                    current reading
  ipmi_mem_temp_upper_non_crit_celsius     BMC upper non-critical threshold
  ipmi_mem_temp_upper_crit_celsius         BMC upper critical threshold

ipmitool's modern (1.8.x) sensor-list format is pipe-separated, one line per
sensor:

  TEMP_CPU1_DDR4A | 59.000 | degrees C | ok | na | na | na | 84.000 | 85.000 | na
   name            reading   unit        stat  lNR   lCR   lNC   uNC    uCR   uNR

Thresholds are exported so the server-side alerts can fire on each sensor's
*own* thresholds, with a conservative constant fallback (85C / 95C) when the
BMC does not publish them. Stdlib only; `ipmitool` must be present in the
container and the host's in-band BMC device (/dev/ipmi0) mounted in.
"""

import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("IPMI_EXPORTER_PORT", "9109"))
# Re-reading the BMC is relatively slow (SDB download); cache between scrapes.
CACHE_TTL = 15.0
# Sensor names that indicate a RAM / DIMM temperature. "ddr" is matched
# without word boundaries because names like TEMP_CPU1_DDR4A have no word
# boundary around the token (underscores are word chars).
_MEM_RE = re.compile(r"(?i)(ddr|\bmem\w*\b|\bdimm\w*\b|\bram\b|\bmemory\b)")
_LABEL_RE = re.compile(r"[^a-zA-Z0-9_:]")


def _safe_label(value):
    return _LABEL_RE.sub("_", str(value or "unknown"))[:60]


def _parse_num(value):
    """'84.000' -> 84.0; 'na'/''/garbage -> None."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_sensor_list(text):
    """Return [(name, reading, upper_non_crit, upper_crit), ...]."""
    sensors = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(" | ")]
        if len(parts) < 8:
            continue
        name, reading_raw, unit = parts[0], parts[1], parts[2]
        if "deg" not in unit.lower():
            continue  # temperature sensors only (skip Volts / Amps / RPM ...)
        if not _MEM_RE.search(name):
            continue
        # "na" readings = empty DIMM slot or sensor the BMC cannot read.
        # Over-threshold statuses ("upper critical going high" etc.) still
        # carry a valid reading and are kept.
        reading = _parse_num(reading_raw)
        if reading is None:
            continue
        if "f" in unit.lower() and "c" not in unit.lower():
            reading = (reading - 32) * 5 / 9
        # Threshold columns: 10-col layout = [name,reading,unit,status,
        # lNR,lCR,lNC,uNC,uCR,uNR]; 9-col (older ipmitool) has no uNR.
        if len(parts) >= 10:
            upper_non_crit, upper_crit = _parse_num(parts[7]), _parse_num(parts[8])
        else:
            upper_non_crit, upper_crit = _parse_num(parts[6]), _parse_num(parts[7])
        sensors.append((name, reading, upper_non_crit, upper_crit))
    return sensors


class State:
    def __init__(self):
        self.sensors = None  # parse_sensor_list() result; None until first success
        self.last_error = None
        self.last_try = 0.0

    def refresh(self):
        # Avoid hammering the BMC on lock/timeout; retry at most once per second.
        now = time.monotonic()
        if self.last_error and now - self.last_try < 1.0:
            return
        self.last_try = now
        try:
            out = subprocess.run(
                ["ipmitool", "sensor", "list"],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "LC_ALL": "C"},
            )
            if out.returncode != 0:
                raise RuntimeError(out.stderr.strip() or out.stdout.strip()
                                   or "ipmitool exited %d" % out.returncode)
            self.sensors = parse_sensor_list(out.stdout)
            self.last_error = None
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)


STATE = State()


def render_metrics():
    STATE.refresh()
    lines = [
        "# HELP ipmi_exporter_up Whether the ipmi-exporter can read the BMC (1) or not (0).",
        "# TYPE ipmi_exporter_up gauge",
    ]
    if STATE.last_error is not None or STATE.sensors is None:
        lines.append("ipmi_exporter_up 0")
        if STATE.last_error:
            lines.append("# last error: %s" % STATE.last_error.replace("\n", " "))
        return "\n".join(lines) + "\n"

    lines.append("ipmi_exporter_up 1")

    lines += [
        "# HELP ipmi_mem_sensor_count Number of readable memory-temperature sensors the BMC exposes.",
        "# TYPE ipmi_mem_sensor_count gauge",
        "ipmi_mem_sensor_count %d" % len(STATE.sensors),
    ]

    lines += [
        "# HELP ipmi_mem_temp_celsius Current memory (DIMM) temperature from the BMC, per sensor.",
        "# TYPE ipmi_mem_temp_celsius gauge",
        "# HELP ipmi_mem_temp_upper_non_crit_celsius BMC upper non-critical threshold for the sensor (NaN if unpublished).",
        "# TYPE ipmi_mem_temp_upper_non_crit_celsius gauge",
        "# HELP ipmi_mem_temp_upper_crit_celsius BMC upper critical threshold for the sensor (NaN if unpublished).",
        "# TYPE ipmi_mem_temp_upper_crit_celsius gauge",
    ]
    for name, reading, upper_non_crit, upper_crit in STATE.sensors:
        label = '{sensor="%s"}' % _safe_label(name)
        lines.append("ipmi_mem_temp_celsius%s %.1f" % (label, reading))
        lines.append("ipmi_mem_temp_upper_non_crit_celsius%s %s"
                     % (label, "NaN" if upper_non_crit is None else "%.1f" % upper_non_crit))
        lines.append("ipmi_mem_temp_upper_crit_celsius%s %s"
                     % (label, "NaN" if upper_crit is None else "%.1f" % upper_crit))
    if not STATE.sensors:
        # Keep the metric family visible even when the BMC publishes no
        # memory-temperature sensors (a "0" count is itself useful).
        lines.append('ipmi_mem_temp_celsius{sensor="none"} NaN')
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "ipmi-exporter/1.0"

    def do_GET(self):  # noqa: N802
        if self.path == "/metrics":
            body = render_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/health", "/-/healthy", "/-/ready"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence default logging
        pass


def main():
    STATE.refresh()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("ipmi-exporter listening on :%d" % PORT, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
