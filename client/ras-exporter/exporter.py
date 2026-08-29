#!/usr/bin/env python3
"""Prometheus exporter for rasdaemon's RAS error database.

Reads the (world-readable) rasdaemon SQLite DB and exposes memory-controller
error counters, MCE record counts and the last-event timestamp as Prometheus
metrics. Stdlib-only.
"""

import os
import re
import sqlite3
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get("RAS_DB_PATH", "/var/lib/rasdaemon/ras-mc_event.db")
PORT = int(os.environ.get("RAS_EXPORTER_PORT", "9108"))
# Prometheus label values must match [a-zA-Z0-9_:]
_LABEL_RE = re.compile(r"[^a-zA-Z0-9_:]")


def _safe_label(value):
    return _LABEL_RE.sub("_", str(value or "unknown"))


def _parse_ts(ts):
    """DB timestamps look like '2026-08-21 07:52:49 +0000' -> epoch float."""
    if not ts:
        return 0.0
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return 0.0


class State:
    def __init__(self):
        self.last_good = None
        self.last_error = None
        self.last_try = 0.0

    def refresh(self):
        # Avoid hammering the DB on lock; retry at most once per second.
        now = time.monotonic()
        if self.last_error and now - self.last_try < 1.0:
            return
        self.last_try = now
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=2.0)
            cur = conn.cursor()

            mc = cur.execute(
                "SELECT label, err_type, COUNT(*) FROM mc_event GROUP BY label, err_type"
            ).fetchall()
            mce_total = cur.execute("SELECT COUNT(*) FROM mce_record").fetchone()[0]
            last_ts = cur.execute("SELECT MAX(timestamp) FROM mc_event").fetchone()[0]
            conn.close()

            self.last_good = {
                "mc": [(_safe_label(l), _safe_type(t), int(c)) for l, t, c in mc],
                "mce_total": int(mce_total),
                "last_mc_ts": _parse_ts(last_ts),
            }
            self.last_error = None
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)


def _safe_type(t):
    return _safe_label({"Corrected": "corrected", "Uncorrected": "uncorrected"}.get(t, t))


STATE = State()


def render_metrics():
    STATE.refresh()
    lines = [
        "# HELP ras_exporter_up Whether the ras-exporter is running (1) or not (0).",
        "# TYPE ras_exporter_up gauge",
    ]
    if STATE.last_good is None:
        lines.append("ras_exporter_up 0")
        return "\n".join(lines) + "\n"

    lines.append("ras_exporter_up 1")

    lines += [
        "# HELP ras_mc_event_total Corrected/uncorrected memory-controller errors per DIMM "
        "(cumulative, from rasdaemon).",
        "# TYPE ras_mc_event_total counter",
    ]
    for label, etype, count in STATE.last_good["mc"]:
        lines.append(
            'ras_mc_event_total{label="%s",err_type="%s"} %d' % (label, etype, count)
        )
    # Ensure the metric exists even with zero rows (avoid missing-series confusion).
    if not STATE.last_good["mc"]:
        lines.append('ras_mc_event_total{label="none",err_type="corrected"} 0')

    lines += [
        "# HELP ras_mce_record_total Total MCE records logged by rasdaemon.",
        "# TYPE ras_mce_record_total counter",
        "ras_mce_record_total %d" % STATE.last_good["mce_total"],
        "# HELP ras_last_mc_event_timestamp_seconds Unix time of the most recent memory event.",
        "# TYPE ras_last_mc_event_timestamp_seconds gauge",
        "ras_last_mc_event_timestamp_seconds %s"
        % ("NaN" if STATE.last_good["last_mc_ts"] == 0.0 else "%.6f" % STATE.last_good["last_mc_ts"]),
    ]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "ras-exporter/1.0"

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
    print("ras-exporter listening on :%d, db=%s" % (PORT, DB_PATH), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
