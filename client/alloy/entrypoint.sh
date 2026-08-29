#!/bin/sh
# Wrapper entrypoint: builds the LLM file-SD target file from LLM_TARGET_1..3
# (blank slots are ignored), then execs the real Alloy binary with the args.
set -eu

dir="${ALLOY_DATA_DIR:-/var/lib/alloy}"
mkdir -p "$dir"
out="$dir/llm-targets.json"

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
json="$json]"
printf '%s\n' "$json" > "$out"

exec alloy "$@"
