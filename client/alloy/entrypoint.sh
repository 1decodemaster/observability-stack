#!/bin/sh
# Wrapper entrypoint. Two jobs before exec'ing Alloy:
#
#  1. Materialise Prometheus file-SD target files, one per exporter. Only
#     exporters that are actually running get a non-empty file, so a host
#     without a GPU doesn't carry a permanent `up{job="dcgm"}=0` series
#     (which would make any fleet-wide `up == 0` alert useless).
#
#  2. Repoint host.docker.internal at the gateway of the network Alloy is
#     actually attached to. Docker's `host-gateway` always resolves to the
#     DEFAULT bridge (docker0, 172.17.0.1) — but these containers run on a
#     compose-created bridge with a different gateway, and Docker's inter-bridge
#     isolation drops traffic between the two. Anything addressed to
#     host.docker.internal (node-exporter in the host netns, and any
#     LLM_TARGET_* pointing at a published host port) would silently fail to
#     scrape.
#
#  3. Decide where system logs come from. Distros differ: Debian/Ubuntu keep
#     /var/log/syslog, RHEL/Fedora keep /var/log/messages, and Arch/Fedora/
#     openSUSE are journald-only with no plaintext file at all. journald is
#     preferred when present — reading both would duplicate every line.
set -eu

dir="${ALLOY_DATA_DIR:-/var/lib/alloy}"
tdir="$dir/targets"
mkdir -p "$tdir"

# --- 1. exporter targets -----------------------------------------------------

has_profile() {
  case ",${ENABLED_PROFILES:-}," in
    *",$1,"*) return 0 ;;
    *)        return 1 ;;
  esac
}

# write_target <file> <job> <address> <enabled>
write_target() {
  if [ "$4" = "1" ]; then
    printf '[{"targets":["%s"],"labels":{"job":"%s"}}]\n' "$3" "$2" > "$tdir/$1.json"
  else
    printf '[]\n' > "$tdir/$1.json"
  fi
}

# node-exporter runs in the host network namespace, so it is reached through
# the host gateway rather than by compose DNS name.
write_target node     node     "host.docker.internal:9100" 1
write_target cadvisor cadvisor "cadvisor:8080"             1
write_target dcgm     dcgm     "dcgm-exporter:9400"        "$(has_profile gpu   && echo 1 || echo 0)"
write_target ras      ras      "ras-exporter:9108"         "$(has_profile ras   && echo 1 || echo 0)"
write_target smart    smart    "smartctl-exporter:9633"    "$(has_profile smart && echo 1 || echo 0)"

# LLM serving backends: up to three, blank slots skipped.
json="["
n=0
for i in 1 2 3; do
  v="$(printenv "LLM_TARGET_$i" 2>/dev/null || true)"
  if [ -n "$v" ]; then
    [ "$n" -gt 0 ] && json="$json,"
    json="$json{\"targets\":[\"$v\"],\"labels\":{\"job\":\"llm\"}}"
    n=$((n + 1))
  fi
done
printf '%s]\n' "$json" > "$tdir/llm.json"

# --- 2. host gateway -------------------------------------------------------

# Default route from /proc/net/route. The gateway is stored little-endian, so
# the octets come out of the hex string back to front.
host_gw=""
while read -r _iface dest gwhex _rest; do
  [ "$dest" = "00000000" ] || continue
  [ "$gwhex" = "00000000" ] && continue
  [ ${#gwhex} -eq 8 ] || continue
  t="$gwhex"
  o1=$(printf '%d' "0x${t#??????}")
  t="${gwhex%??}";     o2=$(printf '%d' "0x${t#????}")
  t="${gwhex%????}";   o3=$(printf '%d' "0x${t#??}")
  o4=$(printf '%d' "0x${gwhex%??????}")
  host_gw="$o1.$o2.$o3.$o4"
  break
done < /proc/net/route

if [ -n "$host_gw" ]; then
  # /etc/hosts is a bind mount and is writable; rewriting the entry here means
  # existing .env files that say "host.docker.internal:8000" keep working.
  # /etc/hosts is a bind mount, so `sed -i` cannot rename its temp file over
  # it ("Device or resource busy"). Rewrite the contents in place instead.
  hosts="$(grep -v '[[:space:]]host\.docker\.internal$' /etc/hosts 2>/dev/null || true)"
  printf '%s\n%s host.docker.internal\n' "$hosts" "$host_gw" > /etc/hosts
  echo "host gateway: $host_gw"
else
  echo "WARNING: could not determine the host gateway from /proc/net/route;" \
       "node-exporter and host-published LLM targets may not be scrapable."
fi

# --- 3. log transport --------------------------------------------------------

# A directory Alloy can point loki.source.journal at when journald is not the
# chosen transport; it stays empty, so the component reads nothing.
mkdir -p "$dir/no-journal"

nonempty_dir() {
  [ -d "$1" ] || return 1
  [ -n "$(ls -A "$1" 2>/dev/null)" ] || return 1
  return 0
}

journal_dir=""
for p in /var/log/journal /run/log/journal; do
  if nonempty_dir "$p"; then journal_dir="$p"; break; fi
done

syslog_file=""
for p in /var/log/syslog /var/log/messages; do
  if [ -f "$p" ]; then syslog_file="$p"; break; fi
done

case "${LOG_SOURCE:-auto}" in
  journal) use="journal" ;;
  file)    use="file" ;;
  *)       if [ -n "$journal_dir" ]; then use="journal"; else use="file"; fi ;;
esac

if [ "$use" = "journal" ] && [ -n "$journal_dir" ]; then
  JOURNAL_PATH="$journal_dir"
  # Never tail a plaintext file as well: rsyslog on Debian/Ubuntu writes the
  # same records the journal already has.
  SYSLOG_GLOB="$dir/no-journal/.absent"
  echo "log transport: journald ($journal_dir)"
else
  JOURNAL_PATH="$dir/no-journal"
  SYSLOG_GLOB="${syslog_file:-$dir/no-journal/.absent}"
  if [ -n "$syslog_file" ]; then
    echo "log transport: file ($syslog_file)"
  else
    echo "log transport: NONE — no journal directory and no /var/log/syslog" \
         "or /var/log/messages found. Host logs will not be collected;" \
         "container logs still are."
  fi
fi
export JOURNAL_PATH SYSLOG_GLOB

exec alloy "$@"
