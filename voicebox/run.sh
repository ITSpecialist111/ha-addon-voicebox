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
POLICY_RECEIPT="${VOICEBOX_POLICY_RECEIPT:-/app/.voicebox-model-policy.json}"
POLICY_VERIFIER="${VOICEBOX_POLICY_VERIFIER:-/usr/local/bin/enforce-model-policy.py}"

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
CPU_PRIORITY="$(get_opt cpu_priority low)"
ALLOW_LARGE_MODEL="$(get_opt allow_large_model false)"

# Guard against a non-numeric value reaching the arithmetic below.
[[ "$MIN_FREE_RAM_MB" =~ ^[0-9]+$ ]] || MIN_FREE_RAM_MB=8192

# Turning on allow_large_model without raising the RAM floor is a trap.
#
# The 1.7B model peaked at 8130 MB here. The default floor is 8192 MB — 62 MB
# of margin, which is 0.8%. That is not headroom, it is a rounding error: the
# preflight would pass and the load would still be OOM-killed, because
# MemAvailable moves by more than 62 MB while the model is loading.
#
# So opting in to the large model also raises the floor. The user asked to be
# ALLOWED to load it, not to be allowed to crash the box trying.
LARGE_MODEL_FLOOR_MB=9600

# Set only when the preflight actually READ the memory and found enough of it.
# Permission to load 1.7B is granted from this, not from the option alone, so
# every path that skips or waives the check also withholds the large model.
LARGE_MODEL_RAM_VERIFIED=false

if [[ "$ALLOW_LARGE_MODEL" == "true" ]] && (( MIN_FREE_RAM_MB == 0 )); then
    warn "allow_large_model is on but min_free_ram_mb is 0, which disables the"
    warn "  RAM check entirely. The two together mean \"load an 8.1 GB model"
    warn "  without looking\", which is how this box was OOM-killed twice."
    warn "  The add-on will start, but 1.7B stays refused. Set min_free_ram_mb"
    warn "  to ${LARGE_MODEL_FLOOR_MB} or more to actually enable it."
fi

if [[ "$ALLOW_LARGE_MODEL" == "true" ]] && (( MIN_FREE_RAM_MB != 0 )) \
   && (( MIN_FREE_RAM_MB < LARGE_MODEL_FLOOR_MB )); then
    warn "allow_large_model is on, but min_free_ram_mb is only ${MIN_FREE_RAM_MB} MB."
    warn "  The 1.7B model peaked at 8130 MB on this hardware, so that leaves"
    warn "  $(( MIN_FREE_RAM_MB - 8130 )) MB of margin — the load would still be OOM-killed."
    warn "  Raising the preflight floor to ${LARGE_MODEL_FLOOR_MB} MB for this start."
    warn "  Set min_free_ram_mb explicitly above ${LARGE_MODEL_FLOOR_MB} to silence this."
    MIN_FREE_RAM_MB="$LARGE_MODEL_FLOOR_MB"
fi

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
# Fail CLOSED when memory cannot be established.
#
# This check is the only thing between an oversized start and the OOM killer,
# and add-ons are killed first (oom_score_adj=200). "Could not measure" is not
# evidence that there is enough; treating it as such would silently disable the
# one safeguard here exactly when something is already wrong.
# allow_low_ram_start remains the deliberate, documented override.
cannot_determine() {
    if [[ "$ALLOW_LOW_RAM" == "true" ]]; then
        warn "$1 — starting anyway because allow_low_ram_start is enabled."
        return 0
    fi
    warn "=========================================================="
    warn "REFUSING TO START — $1"
    warn ""
    warn "  The RAM preflight could not be evaluated, so there is no"
    warn "  evidence this will fit. Add-ons have no memory limit and are"
    warn "  the kernel's first choice under pressure, so starting blind"
    warn "  risks taking down other add-ons, not just this one."
    warn ""
    warn "  To start without the check:"
    warn "    - set 'allow_low_ram_start: true'"
    warn "    - or set 'min_free_ram_mb: 0' to disable it outright"
    warn "=========================================================="
    fail "RAM preflight could not be evaluated — see above."
}

ram_preflight() {
    local avail_kb avail_mb swap_free_kb swap_free_mb

    # An explicit 0 means the user has switched the check off on purpose.
    if (( MIN_FREE_RAM_MB == 0 )); then
        log "RAM preflight disabled (min_free_ram_mb = 0)."
        return 0
    fi

    if [[ ! -r "$MEMINFO" ]]; then
        cannot_determine "$MEMINFO is unreadable"
        return 0
    fi

    avail_kb="$(awk '/^MemAvailable:/ {print $2; exit}' "$MEMINFO" || true)"
    swap_free_kb="$(awk '/^SwapFree:/ {print $2; exit}' "$MEMINFO" || true)"

    if [[ ! "$avail_kb" =~ ^[0-9]+$ ]]; then
        cannot_determine "could not parse MemAvailable from $MEMINFO"
        return 0
    fi
    [[ "$swap_free_kb" =~ ^[0-9]+$ ]] || swap_free_kb=0

    avail_mb=$(( avail_kb / 1024 ))
    swap_free_mb=$(( swap_free_kb / 1024 ))

    log "RAM preflight: ${avail_mb} MB available, ${swap_free_mb} MB free swap, need ${MIN_FREE_RAM_MB} MB"

    if (( avail_mb >= MIN_FREE_RAM_MB )); then
        log "RAM preflight passed."
        # Real numbers, read and compared. This is the ONLY place the large
        # model can earn permission: cannot_determine, min_free_ram_mb=0 and
        # allow_low_ram_start all return without setting it.
        if (( MIN_FREE_RAM_MB >= LARGE_MODEL_FLOOR_MB )); then
            LARGE_MODEL_RAM_VERIFIED=true
        fi
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
    warn "      Assistant at its REST API instead"
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
mkdir -p "$DATA_ROOT/app-data"

# An existing symlink is only good enough if it actually resolves to the
# persistent directory. A broken link, or one left pointing somewhere else by an
# earlier version, would let the add-on start and then fail every write - the
# worst outcome, because it looks healthy while quietly losing profiles.
link_ok=false
if [[ -L "$APP_ROOT/data" ]]; then
    current="$(readlink -f "$APP_ROOT/data" 2>/dev/null || true)"
    expected="$(readlink -f "$DATA_ROOT/app-data" 2>/dev/null || true)"
    if [[ -n "$current" && -n "$expected" && "$current" == "$expected" ]]; then
        link_ok=true
    else
        warn "$APP_ROOT/data resolves to '${current:-<broken>}', expected '$expected' - re-pointing it"
        rm -f "$APP_ROOT/data"
    fi
fi

if [[ "$link_ok" != true ]]; then
    # Preserve anything the image shipped there, once.
    #
    # This used to be `cp ... || true` followed unconditionally by `touch
    # .migrated` and `rm -rf`. A copy that failed part way - out of space, bad
    # permissions - was therefore recorded as migrated and the original deleted,
    # losing profiles and the database silently. Now a failed copy aborts the
    # start with the source still intact.
    if [[ -d "$APP_ROOT/data" && ! -L "$APP_ROOT/data" && ! -e "$DATA_ROOT/app-data/.migrated" ]]; then
        log "migrating existing $APP_ROOT/data into $DATA_ROOT/app-data"

        # Copied entry by entry, deliberately, instead of `cp -an src/. dst/`.
        #
        # BusyBox reads -n as "skip if the destination exists", sees the `.`
        # target directory already there, and copies NOTHING - exiting 0 while
        # it does. The old code took that 0 as success, wrote .migrated, and
        # deleted the source. Reproduced on this host:
        #
        #   $ cp -an /tmp/mt/src/. /tmp/mt/dst/ ; echo $?   -> 0
        #   $ find /tmp/mt/dst                              -> dst only, empty
        #
        # The published image is Debian-based, so its GNU cp does the right
        # thing and no live data was ever at risk. But a step that deletes the
        # user's profiles should not depend on which coreutils the base image
        # happens to ship.
        migrate_failed=""
        for entry in "$APP_ROOT/data"/* "$APP_ROOT/data"/.[!.]*; do
            if [[ ! -e "$entry" ]]; then continue; fi
            entry_name="${entry##*/}"
            # No-clobber: anything already in /data is the user's and wins.
            if [[ -e "$DATA_ROOT/app-data/$entry_name" ]]; then continue; fi
            if ! cp -a "$entry" "$DATA_ROOT/app-data/"; then
                migrate_failed="$entry_name"
                break
            fi
        done

        # Verify rather than trust. The source is about to be deleted, and the
        # bug above was precisely a copy that reported success having done
        # nothing - so confirm each entry actually arrived.
        migrate_missing=""
        for entry in "$APP_ROOT/data"/* "$APP_ROOT/data"/.[!.]*; do
            if [[ ! -e "$entry" ]]; then continue; fi
            entry_name="${entry##*/}"
            if [[ ! -e "$DATA_ROOT/app-data/$entry_name" ]]; then
                migrate_missing="$migrate_missing $entry_name"
            fi
        done

        if [[ -n "$migrate_failed" || -n "$migrate_missing" ]]; then
            warn "=========================================================="
            warn "Could not copy $APP_ROOT/data to $DATA_ROOT/app-data."
            [[ -n "$migrate_failed" ]] && warn "  copy failed on: $migrate_failed"
            [[ -n "$migrate_missing" ]] && warn "  did not arrive:$migrate_missing"
            warn "NOTHING has been deleted - the original data is untouched."
            warn "Check free space and permissions on $DATA_ROOT, then retry."
            warn "=========================================================="
            fail "persistence migration failed - refusing to start."
        fi
        touch "$DATA_ROOT/app-data/.migrated"
    fi
    rm -rf "$APP_ROOT/data"
    ln -sfn "$DATA_ROOT/app-data" "$APP_ROOT/data"
fi

# The build records what this image's model-loading code actually looks like.
# Print it: two fixes have now been aimed at files taken from upstream's GitHub
# that turned out not to exist in the published tag.
if [[ -r /app/.voicebox-inventory.txt ]]; then
    log "--- image inventory (recorded at build time) ---"
    while IFS= read -r inv_line; do log "  $inv_line"; done < /app/.voicebox-inventory.txt
    log "--- end inventory ---"
fi

# Model size policy.
#
# Measured on this hardware, not taken from documentation:
#   0.6B  peaks ~4.2 GB, loaded in ~26 s   -> fits
#   1.7B  peaks ~8.1 GB, OOM-killed twice  -> does NOT fit
#
# Upstream defaults BOTH /models/load and /generate to 1.7B, so the default
# action of the web UI was the one that cannot work here. Worse, creating a
# voice prompt calls the loader with no size at all, which fell through to the
# backend's own 1.7B default - so even an explicit 0.6B request could load the
# wrong model first.
#
# enforce-model-policy.py rewrote those defaults at build time and put a guard
# in load_model_async, the single function every TTS load funnels through. An
# over-large request now returns a clean HTTP 400 instead of inviting the OOM
# killer. The guard reads the policy from the variable exported below.
#
# This check FAILS CLOSED. It previously only warned and carried on, which is
# the wrong way round: the case it is meant to catch — an image whose guard is
# missing or has been tampered with — is exactly the case where starting is
# dangerous. A warning scrolls past in the log and the box gets OOM-killed
# anyway. Refusing to start is visible, harmless and reversible.
#
# It re-runs the verifier rather than merely checking the receipt EXISTS,
# because a stale or planted receipt is precisely what an existence check
# cannot detect.
if [[ ! -r "$POLICY_RECEIPT" ]]; then
    warn "model policy receipt is missing — this image predates the 1.7B guard,"
    warn "  or the build step that installs it did not run."
    fail "refusing to start without the model guard: the 1.7B default needs
  ~8.1 GB here and has been OOM-killed twice. Rebuild the add-on
  (Settings → Add-ons → Voicebox → Rebuild)."
fi

if [[ -r "$POLICY_VERIFIER" ]]; then
    if ! policy_out=$(python3 "$POLICY_VERIFIER" --verify 2>&1); then
        warn "model policy verification FAILED:"
        while IFS= read -r line; do warn "  $line"; done <<< "$policy_out"
        fail "refusing to start: the 1.7B guard is not intact. The image may be
  stale or partially patched. Rebuild the add-on."
    fi
    log "model policy verified — 1.7B cannot be loaded by accident"
else
    warn "model policy verifier is not present in this image."
    fail "refusing to start without a verifiable model guard. Rebuild the add-on."
fi

# Permission is granted from the MEASUREMENT, not from the option.
#
# allow_large_model says "I want 1.7B available". It does not say "I want it
# available on a box that cannot hold it". Three settings would otherwise have
# waved it through unmeasured — min_free_ram_mb=0 (check disabled),
# allow_low_ram_start=true (shortfall downgraded to a warning), and an
# unreadable /proc/meminfo — and each of those is precisely when granting it is
# most dangerous. An OOM here kills Frigate as readily as Voicebox.
#
# So the add-on still starts, honouring the user's choice about STARTING, but
# 1.7B stays refused until the memory has actually been seen.
if [[ "$ALLOW_LARGE_MODEL" == "true" && "$LARGE_MODEL_RAM_VERIFIED" == "true" ]]; then
    export VOICEBOX_ALLOWED_MODEL_SIZES="0.6B,1.7B"
    warn "allow_large_model is ON — the 1.7B model may be loaded."
    warn "  Verified ≥${LARGE_MODEL_FLOOR_MB} MB available before starting."
    warn "  It peaked at ~8.1 GB on this box and was OOM-killed twice. Home"
    warn "  Assistant biases the kernel to kill add-ons first, so this risks"
    warn "  Frigate and every other add-on, not just Voicebox."
elif [[ "$ALLOW_LARGE_MODEL" == "true" ]]; then
    export VOICEBOX_ALLOWED_MODEL_SIZES="0.6B"
    warn "=========================================================="
    warn "allow_large_model is ON, but the ${LARGE_MODEL_FLOOR_MB} MB the 1.7B model needs"
    warn "was never confirmed — the RAM check was disabled, waived or"
    warn "could not read /proc/meminfo. 1.7B stays REFUSED."
    warn ""
    warn "To enable it: set min_free_ram_mb to ${LARGE_MODEL_FLOOR_MB} or above, leave"
    warn "allow_low_ram_start off, and restart. 0.6B is unaffected."
    warn "=========================================================="
else
    export VOICEBOX_ALLOWED_MODEL_SIZES="0.6B"
    log "model policy: 0.6B only (~4.2 GB peak); 1.7B is refused with an HTTP"
    log "  error rather than risking the OOM killer. Set allow_large_model to"
    log "  true only if this host really can spare ~8.1 GB."
fi

log "Voicebox starting — log level ${LOG_LEVEL}"
log "models cached in $DATA_ROOT/cache/huggingface (excluded from backups)"
log "first start downloads several GB of models; this takes a while"

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# CPU priority
# ---------------------------------------------------------------------------
# Supervisor applies no CPU quota to add-ons either: the container-create call
# in supervisor/docker/app.py sets cpu_rt_runtime, which is realtime scheduling
# headroom, not a general limit. So just as with memory, any ceiling has to be
# applied from in here.
#
# This matters when something latency-critical already owns most of the CPU --
# Frigate running camera detection is the usual case. Voicebox inference is
# CPU-bound, and at equal priority the two simply take turns. The visible
# symptom is not slow speech, it is dropped frames on the NVR.
#
# nice costs nothing while the CPU is idle -- it only applies under contention,
# which is exactly when it should. ionice -c 3 (idle) does the same for disk,
# which matters while several GB of models are read in on first start.
CMD=()
case "$CPU_PRIORITY" in
    low)
        if command -v nice >/dev/null 2>&1; then
            CMD+=(nice -n 19)
            command -v ionice >/dev/null 2>&1 && CMD+=(ionice -c 3)
            log "CPU priority: low - yields to other add-ons whenever they need the CPU"
        else
            warn "nice not found in image; starting at normal CPU priority"
        fi
        ;;
    normal)
        log "CPU priority: normal - competes equally with every other add-on"
        ;;
    *)
        warn "unknown cpu_priority '$CPU_PRIORITY'; falling back to normal"
        ;;
esac

# Port 8000 matches the published image and config.yaml's ports mapping.
# exec so uvicorn becomes PID 1 and receives SIGTERM directly on stop.
cd "$APP_ROOT"
CMD+=(uvicorn backend.main:app
      --host 0.0.0.0
      --port 8000
      --log-level "$LOG_LEVEL")
exec "${CMD[@]}"
