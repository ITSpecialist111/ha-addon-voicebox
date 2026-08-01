#!/usr/bin/env bash
# Voicebox add-on entrypoint.
#
# This wrapper exists for three reasons:
#
#   1. The upstream image knows nothing about Home Assistant. It does not read
#      /data/options.json, so without this there is no way to configure it.
#
#   2. Home Assistant add-ons have NO memory limit. Verified against the
#      Supervisor source: supervisor/apps/validate.py has no mem_limit key,
#      and the container-create call in supervisor/docker/app.py passes no
#      memory arguments at all. So the cgroup ceiling used in the Docker
#      Compose deployment CANNOT be expressed here. The RAM preflight below is
#      the substitute: refuse to start rather than invoke the OOM killer.
#
#   3. Add-ons are created with oom_score_adj=200 (same file). That is a
#      positive bias, so under memory pressure the kernel picks add-ons FIRST.
#      An over-sized Voicebox does not just fail — it makes every other add-on
#      on the box a more likely casualty.
#
# The base image is Debian trixie with Python 3.12. There is no apk, and bashio
# is not present, so options are parsed with python3 (guaranteed by the image).

set -euo pipefail

# Paths. These are overridable purely so the test suite can exercise this exact
# script against a sandbox instead of testing a modified copy. Nothing sets them
# in production — the defaults are the real container paths.
OPTIONS_FILE="${VOICEBOX_OPTIONS_FILE:-/data/options.json}"
DATA_ROOT="${VOICEBOX_DATA_ROOT:-/data}"
APP_ROOT="${VOICEBOX_APP_ROOT:-/app}"
MEMINFO="${VOICEBOX_MEMINFO:-/proc/meminfo}"

log()  { printf '[%s] %s\n'  "$(date -u '+%H:%M:%S')" "$*"; }
warn() { printf '[%s] WARNING: %s\n' "$(date -u '+%H:%M:%S')" "$*" >&2; }
fail() { printf '[%s] FATAL: %s\n'   "$(date -u '+%H:%M:%S')" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Read options
# ---------------------------------------------------------------------------
# Defaults match config.yaml. If options.json is missing or malformed we still
# start with sane values rather than crash-looping on a config typo.
get_opt() {
    local key="$1" default="$2"
    python3 - "$OPTIONS_FILE" "$key" "$default" <<'PY' 2>/dev/null || printf '%s' "$default"
import json, sys
path, key, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as fh:
        value = json.load(fh).get(key, default)
except Exception:
    value = default
if isinstance(value, bool):
    value = "true" if value else "false"
sys.stdout.write("" if value is None else str(value))
PY
}

LOG_LEVEL="$(get_opt log_level info)"
MIN_FREE_RAM_MB="$(get_opt min_free_ram_mb 8192)"
ALLOW_LOW_RAM="$(get_opt allow_low_ram_start false)"
CORS_ORIGINS="$(get_opt cors_origins '')"

# Guard against a non-numeric value reaching the arithmetic below.
[[ "$MIN_FREE_RAM_MB" =~ ^[0-9]+$ ]] || MIN_FREE_RAM_MB=8192

# ---------------------------------------------------------------------------
# RAM preflight — the reason this wrapper exists
# ---------------------------------------------------------------------------
# MemAvailable is the kernel's own estimate of what can be handed out without
# swapping, which is exactly the question being asked. It is preferred over
# MemFree, which excludes reclaimable page cache and reads alarmingly low on a
# healthy system.
#
# Note this reads the HOST's memory. Containers without lxcfs see host values
# in /proc/meminfo, which is what we want.
ram_preflight() {
    local avail_kb avail_mb swap_free_kb swap_free_mb

    if [[ ! -r "$MEMINFO" ]]; then
        warn "$MEMINFO unreadable — skipping the RAM preflight."
        return 0
    fi

    avail_kb="$(awk '/^MemAvailable:/ {print $2; exit}' "$MEMINFO" || true)"
    swap_free_kb="$(awk '/^SwapFree:/ {print $2; exit}' "$MEMINFO" || true)"

    if [[ ! "$avail_kb" =~ ^[0-9]+$ ]]; then
        warn "could not parse MemAvailable — skipping the RAM preflight."
        return 0
    fi
    [[ "$swap_free_kb" =~ ^[0-9]+$ ]] || swap_free_kb=0

    avail_mb=$(( avail_kb / 1024 ))
    swap_free_mb=$(( swap_free_kb / 1024 ))

    log "RAM preflight: ${avail_mb} MB available, ${swap_free_mb} MB free swap, need ${MIN_FREE_RAM_MB} MB"

    if (( avail_mb >= MIN_FREE_RAM_MB )); then
        log "RAM preflight passed."
        return 0
    fi

    local short=$(( MIN_FREE_RAM_MB - avail_mb ))

    if [[ "$ALLOW_LOW_RAM" == "true" ]]; then
        warn "=========================================================="
        warn "Starting ${short} MB SHORT of the configured minimum."
        warn "allow_low_ram_start is enabled, so this is only a warning."
        warn "Add-ons run with oom_score_adj=200 and have no memory limit,"
        warn "so if this over-commits, the kernel will kill add-ons first —"
        warn "possibly this one, possibly a different one."
        warn "=========================================================="
        return 0
    fi

    printf '\n' >&2
    warn "=========================================================="
    warn "REFUSING TO START — not enough memory."
    warn ""
    warn "  available     ${avail_mb} MB"
    warn "  required      ${MIN_FREE_RAM_MB} MB"
    warn "  short by      ${short} MB"
    warn "  free swap     ${swap_free_mb} MB"
    warn ""
    if (( short > swap_free_mb )); then
        warn "  The shortfall is larger than the free swap, so this cannot"
        warn "  be satisfied by paging either. Starting would mean an"
        warn "  out-of-memory kill, not merely slow performance."
    else
        warn "  This could only be satisfied by swapping heavily, which for"
        warn "  ML inference means thrashing rather than working."
    fi
    warn ""
    warn "  Options, best first:"
    warn "    - run Voicebox on a machine with more RAM and point Home"
    warn "      Assistant at it over REST/MCP instead"
    warn "    - stop other add-ons to free memory, then retry"
    warn "    - lower 'min_free_ram_mb' if you believe your workload needs"
    warn "      less than the 8 GB upstream states"
    warn "    - set 'allow_low_ram_start: true' to override (read DOCS.md)"
    warn "=========================================================="
    printf '\n' >&2

    fail "insufficient memory — see above."
}

ram_preflight

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
# The published image runs as root, so HOME is /root. Pinning the cache to /data
# keeps models on the add-on's persistent volume; otherwise several GB would be
# re-downloaded every time the container is recreated.
export HF_HOME="$DATA_ROOT/cache/huggingface"
export VOICEBOX_MODELS_DIR="$DATA_ROOT/cache/huggingface"
export NUMBA_CACHE_DIR="/tmp/numba_cache"
export LOG_LEVEL="$LOG_LEVEL"
[[ -n "$CORS_ORIGINS" ]] && export VOICEBOX_CORS_ORIGINS="$CORS_ORIGINS"

mkdir -p "$DATA_ROOT/cache/huggingface" "$DATA_ROOT/generations" "$NUMBA_CACHE_DIR"

# Voicebox writes profiles and its database to /app/data. That is inside the
# image and would be lost on update, so redirect it onto /data via a symlink.
if [[ ! -L "$APP_ROOT/data" ]]; then
    mkdir -p "$DATA_ROOT/app-data"
    # Preserve anything the image shipped there, once.
    if [[ -d "$APP_ROOT/data" && ! -e "$DATA_ROOT/app-data/.migrated" ]]; then
        cp -an "$APP_ROOT/data/." "$DATA_ROOT/app-data/" 2>/dev/null || true
        touch "$DATA_ROOT/app-data/.migrated"
    fi
    rm -rf "$APP_ROOT/data"
    ln -sfn "$DATA_ROOT/app-data" "$APP_ROOT/data"
fi

log "Voicebox starting — log level ${LOG_LEVEL}"
log "models cached in $DATA_ROOT/cache/huggingface (excluded from backups)"
log "first start downloads several GB of models; this takes a while"

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
# Port 8000 matches the published image and config.yaml's ports mapping.
# exec so uvicorn becomes PID 1 and receives SIGTERM directly on stop.
cd "$APP_ROOT"
exec uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-level "$LOG_LEVEL"
