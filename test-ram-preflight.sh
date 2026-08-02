#!/usr/bin/env bash
# Test harness for the Voicebox add-on RAM preflight.
#
# The preflight is the only thing standing between an over-sized Voicebox and
# the OOM killer, because Home Assistant add-ons cannot be given a memory limit.
# It therefore needs to be tested rather than assumed.
#
# This runs the REAL run.sh — not a rewritten copy — by pointing its path seams
# (VOICEBOX_OPTIONS_FILE / VOICEBOX_DATA_ROOT / VOICEBOX_APP_ROOT /
# VOICEBOX_MEMINFO) at a sandbox. A stub `uvicorn` on PATH catches the exec.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_SH="$HERE/voicebox/run.sh"
PASS=0; FAIL=0

check() {
    local label="$1" expected="$2" haystack="$3"
    if [[ "$haystack" == *"$expected"* ]]; then
        printf '  PASS  %s\n' "$label"; PASS=$((PASS+1))
    else
        printf '  FAIL  %s\n        wanted substring: %s\n' "$label" "$expected"; FAIL=$((FAIL+1))
    fi
}

check_absent() {
    local label="$1" needle="$2" haystack="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        printf '  PASS  %s\n' "$label"; PASS=$((PASS+1))
    else
        printf '  FAIL  %s: unexpectedly found "%s"\n' "$label" "$needle"; FAIL=$((FAIL+1))
    fi
}

check_rc() {
    # Reads the exit code from RC_FILE rather than taking it as an argument.
    # run_case is invoked inside a command substitution, so any variable it
    # assigns is lost with the subshell — the file is the only channel out.
    local label="$1" expected="$2" actual
    actual="$(cat "$RC_FILE" 2>/dev/null)"
    if [[ "$actual" == "$expected" ]]; then
        printf '  PASS  %s (rc=%s)\n' "$label" "$actual"; PASS=$((PASS+1))
    else
        printf '  FAIL  %s: wanted rc=%s got rc=%s\n' "$label" "$expected" "$actual"; FAIL=$((FAIL+1))
    fi
}

run_case() {
    local avail_mb="$1" swap_mb="$2" opts_json="$3" stub_nice="${4:-}"
    local box; box="$(mktemp -d)"

    mkdir -p "$box/data" "$box/app/data" "$box/bin"

    # nice/ionice are optional stubs. They announce themselves, drop their own
    # flag pair, then exec the rest, so the whole wrapper chain shows up in the
    # captured output exactly as it would be assembled at runtime.
    if [[ -n "$stub_nice" ]]; then
        printf '#!/usr/bin/env bash\necho "NICE_CALLED args:$*"\n[[ "$1" == "-n" ]] && shift 2\nexec "$@"\n' > "$box/bin/nice"
        printf '#!/usr/bin/env bash\necho "IONICE_CALLED args:$*"\n[[ "$1" == "-c" ]] && shift 2\nexec "$@"\n' > "$box/bin/ionice"
        chmod +x "$box/bin/nice" "$box/bin/ionice"
    fi
    printf '%s' "$opts_json" > "$box/data/options.json"

    cat > "$box/meminfo" <<EOF
MemTotal:       16282472 kB
MemFree:          145678 kB
MemAvailable:   $(( avail_mb * 1024 )) kB
SwapTotal:       4194300 kB
SwapFree:       $(( swap_mb * 1024 )) kB
EOF

    # Stub uvicorn so the final exec is observable instead of fatal.
    printf '#!/usr/bin/env bash\necho "UVICORN_STARTED args:$*"\n' > "$box/bin/uvicorn"
    chmod +x "$box/bin/uvicorn"

    # Optional hook so a scenario can damage the sandbox — remove meminfo,
    # pre-place a broken symlink, make a directory unwritable — before run.sh
    # sees it. "$box" is in scope. Cleared by the caller after use.
    if [[ -n "${PREP_HOOK:-}" ]]; then
        eval "$PREP_HOOK"
    fi

    # The exit code has to travel out of the command substitution, which is a
    # subshell — a plain global assignment inside would be discarded. Write it
    # to a file in the caller's scope instead.
    local out
    out="$(
        PATH="$box/bin:$PATH" \
        VOICEBOX_OPTIONS_FILE="$box/data/options.json" \
        VOICEBOX_DATA_ROOT="$box/data" \
        VOICEBOX_APP_ROOT="$box/app" \
        VOICEBOX_MEMINFO="$box/meminfo" \
        bash "$RUN_SH" 2>&1
        printf '%s' "$?" > "$RC_FILE"
    )"
    rm -rf "$box"
    printf '%s' "$out"
}
RC_FILE="$(mktemp)"
trap 'rm -f "$RC_FILE"' EXIT

printf '\n=== Voicebox add-on RAM preflight ===\n\n'

# ---------------------------------------------------------------------------
printf -- '--- 1. plenty of RAM: should start ---\n'
out="$(run_case 12000 4096 '{"log_level":"info","min_free_ram_mb":8192,"allow_low_ram_start":false}')"
check    "preflight passes"        "RAM preflight passed" "$out"
check    "hands off to uvicorn"    "UVICORN_STARTED"      "$out"
check    "binds port 8000"         "--port 8000"          "$out"
check    "passes the log level"    "--log-level info"     "$out"
check_rc "exits cleanly"           "0"

# ---------------------------------------------------------------------------
printf -- '\n--- 2. this OptiPlex today (6482 MB avail, 817 MB swap): must refuse ---\n'
out="$(run_case 6482 817 '{"log_level":"info","min_free_ram_mb":8192,"allow_low_ram_start":false}')"
check        "refuses to start"        "REFUSING TO START"          "$out"
check        "reports the shortfall"   "1710 MB"                    "$out"
check        "spots swap cannot cover" "larger than the free swap"  "$out"
check_absent "never reaches uvicorn"   "UVICORN_STARTED"            "$out"
check_rc     "exits non-zero"          "1"

# ---------------------------------------------------------------------------
printf -- '\n--- 3. override enabled: warns loudly but proceeds ---\n'
out="$(run_case 6482 817 '{"min_free_ram_mb":8192,"allow_low_ram_start":true}')"
check    "warns about the shortfall" "1710 MB SHORT"       "$out"
check    "explains the OOM bias"     "oom_score_adj"       "$out"
check    "still starts"              "UVICORN_STARTED"     "$out"
check_rc "exits cleanly"             "0"

# ---------------------------------------------------------------------------
printf -- '\n--- 4. short but swap could absorb it: different advice ---\n'
out="$(run_case 7000 4000 '{"min_free_ram_mb":8192,"allow_low_ram_start":false}')"
check        "still refuses"          "REFUSING TO START"             "$out"
check        "warns about thrashing"  "thrashing rather than working" "$out"
check_absent "never reaches uvicorn"  "UVICORN_STARTED"               "$out"
check_rc     "exits non-zero"         "1"

# ---------------------------------------------------------------------------
printf -- '\n--- 5. malformed options.json: defaults, no crash-loop ---\n'
out="$(run_case 12000 4096 'this is not json{{{')"
check    "falls back to 8192"      "need 8192 MB"    "$out"
check    "still starts"            "UVICORN_STARTED" "$out"
check_rc "exits cleanly"           "0"

# ---------------------------------------------------------------------------
printf -- '\n--- 6. empty options.json ---\n'
out="$(run_case 12000 4096 '{}')"
check    "falls back to 8192"      "need 8192 MB"    "$out"
check    "still starts"            "UVICORN_STARTED" "$out"
check_rc "exits cleanly"           "0"

# ---------------------------------------------------------------------------
printf -- '\n--- 7. non-numeric min_free_ram_mb: coerced, no arithmetic crash ---\n'
out="$(run_case 12000 4096 '{"min_free_ram_mb":"lots"}')"
check    "falls back to 8192"      "need 8192 MB"    "$out"
check    "still starts"            "UVICORN_STARTED" "$out"
check_rc "exits cleanly"           "0"

# ---------------------------------------------------------------------------
printf -- '\n--- 8. min_free_ram_mb=0 disables the check ---\n'
out="$(run_case 100 0 '{"min_free_ram_mb":0}')"
check        "reports the check is off" "RAM preflight disabled"  "$out"
check_absent "does not claim it passed" "RAM preflight passed"   "$out"
check    "still starts"            "UVICORN_STARTED"      "$out"
check_rc "exits cleanly"           "0"

# ---------------------------------------------------------------------------
printf -- '\n--- 9. exactly on the boundary: should pass, not refuse ---\n'
out="$(run_case 8192 4096 '{"min_free_ram_mb":8192}')"
check        "boundary is inclusive" "RAM preflight passed" "$out"
check_absent "does not refuse"       "REFUSING TO START"    "$out"
check_rc     "exits cleanly"         "0"

# ---------------------------------------------------------------------------
printf -- '\n--- 10. one MB under: must refuse ---\n'
out="$(run_case 8191 4096 '{"min_free_ram_mb":8192}')"
check    "refuses"          "REFUSING TO START" "$out"
check_rc "exits non-zero"   "1"

# ---------------------------------------------------------------------------
printf -- '\n--- 11. persistence: /app/data redirected onto /data ---\n'
# Git-bash on Windows fakes `ln -s` with a copy unless winsymlinks is enabled,
# so probe the filesystem first rather than reporting a phantom failure. The
# add-on only ever runs on Linux, where this is a genuine symlink.
symlink_probe="$(mktemp -d)"
touch "$symlink_probe/target"
ln -s "$symlink_probe/target" "$symlink_probe/link" 2>/dev/null || true
SYMLINKS_WORK=0
[[ -L "$symlink_probe/link" ]] && SYMLINKS_WORK=1
rm -rf "$symlink_probe"

box="$(mktemp -d)"
mkdir -p "$box/data" "$box/app/data" "$box/bin"
echo '{"min_free_ram_mb":0}' > "$box/data/options.json"
echo 'pre-existing profile' > "$box/app/data/profiles.db"
printf 'MemAvailable:   12582912 kB\nSwapFree:        4194300 kB\n' > "$box/meminfo"
printf '#!/usr/bin/env bash\necho UVICORN_STARTED\n' > "$box/bin/uvicorn"; chmod +x "$box/bin/uvicorn"
out="$(PATH="$box/bin:$PATH" VOICEBOX_OPTIONS_FILE="$box/data/options.json" \
     VOICEBOX_DATA_ROOT="$box/data" VOICEBOX_APP_ROOT="$box/app" \
     VOICEBOX_MEMINFO="$box/meminfo" bash "$RUN_SH" 2>&1)"

if (( SYMLINKS_WORK )); then
    [[ -L "$box/app/data" ]] \
        && { printf '  PASS  /app/data became a symlink\n'; PASS=$((PASS+1)); } \
        || { printf '  FAIL  /app/data is not a symlink\n'; FAIL=$((FAIL+1)); }
else
    printf '  SKIP  symlink assertion — this filesystem has no symlink support\n'
    printf '        (Git-bash on Windows; the add-on itself only runs on Linux)\n'
fi
[[ -f "$box/data/app-data/profiles.db" ]] \
    && { printf '  PASS  shipped data migrated, not lost\n'; PASS=$((PASS+1)); } \
    || { printf '  FAIL  shipped data was lost on migration\n'; FAIL=$((FAIL+1)); }
[[ -d "$box/data/cache/huggingface" ]] \
    && { printf '  PASS  model cache dir created under /data\n'; PASS=$((PASS+1)); } \
    || { printf '  FAIL  model cache dir missing\n'; FAIL=$((FAIL+1)); }
[[ "$out" == *UVICORN_STARTED* ]] \
    && { printf '  PASS  still reaches startup after migration\n'; PASS=$((PASS+1)); } \
    || { printf '  FAIL  migration path blocked startup\n'; FAIL=$((FAIL+1)); }
rm -rf "$box"

# ---------------------------------------------------------------------------
printf -- '\n--- 12. cpu_priority=low: runs under nice + ionice ---\n'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192,"cpu_priority":"low"}' stub)"
check    "announces low priority"  "CPU priority: low"       "$out"
check    "wraps in nice"           "NICE_CALLED args:-n 19"  "$out"
check    "wraps in ionice"         "IONICE_CALLED args:-c 3" "$out"
check    "still reaches uvicorn"   "UVICORN_STARTED"         "$out"
check    "still binds 8000"        "--port 8000"             "$out"
check_rc "exits cleanly"           "0"

# ---------------------------------------------------------------------------
printf -- '\n--- 13. cpu_priority=normal: no nice, even though it is available ---\n'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192,"cpu_priority":"normal"}' stub)"
check        "announces normal"     "CPU priority: normal" "$out"
check_absent "does not nice"        "NICE_CALLED"          "$out"
check_absent "does not ionice"      "IONICE_CALLED"        "$out"
check        "reaches uvicorn"      "UVICORN_STARTED"      "$out"
check_rc     "exits cleanly"        "0"

# ---------------------------------------------------------------------------
printf -- '\n--- 14. option absent: defaults to low, matching config.yaml ---\n'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192}' stub)"
check    "defaults to low"      "CPU priority: low"      "$out"
check    "wraps in nice"        "NICE_CALLED args:-n 19" "$out"
check_rc "exits cleanly"        "0"

# ---------------------------------------------------------------------------
printf -- '\n--- 15. garbage cpu_priority: warns but must still start ---\n'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192,"cpu_priority":"turbo"}' stub)"
check        "warns"            "unknown cpu_priority" "$out"
check_absent "does not nice"    "NICE_CALLED"          "$out"
check        "still starts"     "UVICORN_STARTED"      "$out"
check_rc     "exits cleanly"    "0"

# ---------------------------------------------------------------------------
printf -- '\n--- 16. cpu_priority=low but nice absent: degrades, does not crash ---\n'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192,"cpu_priority":"low"}')"
check    "still reaches uvicorn" "UVICORN_STARTED" "$out"
check_rc "exits cleanly"         "0"
# ---------------------------------------------------------------------------
# The next three cover failing CLOSED. Previously an unreadable or malformed
# meminfo let the start proceed, which disabled the safeguard precisely when
# something was already wrong. "Could not measure" is not "there is enough".
printf -- '\n--- 17. meminfo missing: must refuse, not assume ---\n'
PREP_HOOK='rm -f "$box/meminfo"'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192}')"
PREP_HOOK=''
check        "refuses"              "REFUSING TO START"  "$out"
check        "says why"             "unreadable"         "$out"
check_absent "never reaches uvicorn" "UVICORN_STARTED"   "$out"
check_rc     "exits non-zero"       "1"

# ---------------------------------------------------------------------------
printf -- '\n--- 18. meminfo present but unparseable: must refuse ---\n'
PREP_HOOK='printf "garbage\nnot a meminfo\n" > "$box/meminfo"'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192}')"
PREP_HOOK=''
check        "refuses"              "REFUSING TO START"        "$out"
check        "names the field"      "could not parse MemAvailable" "$out"
check_absent "never reaches uvicorn" "UVICORN_STARTED"         "$out"
check_rc     "exits non-zero"       "1"

# ---------------------------------------------------------------------------
printf -- '\n--- 19. unmeasurable + override: proceeds, because that is the opt-out ---\n'
PREP_HOOK='rm -f "$box/meminfo"'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192,"allow_low_ram_start":true}')"
PREP_HOOK=''
check        "warns rather than refusing" "starting anyway"   "$out"
check_absent "does not refuse"            "REFUSING TO START" "$out"
check        "reaches uvicorn"            "UVICORN_STARTED"   "$out"
check_rc     "exits cleanly"              "0"

# ---------------------------------------------------------------------------
# A dangling symlink at /app/data looks fine to `[ -L ]` but every write
# through it fails. Accepting one produced an add-on that reported healthy and
# quietly lost data, so it has to be validated by target, not by type.
printf -- '\n--- 20. broken symlink at /app/data: repaired, not trusted ---\n'
if ln -s /tmp "$(mktemp -d)/probe" 2>/dev/null; then
    PREP_HOOK='rm -rf "$box/app/data"; ln -s "$box/nonexistent-target" "$box/app/data"'
    out="$(run_case 12000 4096 '{"min_free_ram_mb":8192}')"
    PREP_HOOK=''
    check "notices and repairs it" "re-pointing" "$out"
    check "still starts"           "UVICORN_STARTED" "$out"
    check_rc "exits cleanly"       "0"
else
    printf '  SKIP  broken-symlink assertion — this filesystem has no symlink support\n'
fi

# ---------------------------------------------------------------------------
# The migration used to mark itself done and delete the source even when the
# copy had failed, which is unrecoverable. It must abort with the source intact.
printf -- '\n--- 21. migration copy fails: aborts with the original intact ---\n'
if [[ "$(id -u)" != "0" ]] && ln -s /tmp "$(mktemp -d)/probe2" 2>/dev/null; then
    KEEP_BOX="$(mktemp -d)"
    PREP_HOOK='echo precious > "$box/app/data/profile.db"; chmod 500 "$box/data"; echo "$box" > '"$KEEP_BOX"'/where'
    out="$(run_case 12000 4096 '{"min_free_ram_mb":8192}')"
    PREP_HOOK=''
    check        "does not claim success" "migrated" "$out"
    check_absent "does not reach uvicorn" "UVICORN_STARTED" "$out"
    rm -rf "$KEEP_BOX"
else
    printf '  SKIP  migration-failure assertion — running as root, or no symlink support\n'
fi

printf '\n================================================================\n'
printf ' RESULT: %d passed, %d failed\n' "$PASS" "$FAIL"
printf '================================================================\n\n'
[[ "$FAIL" -eq 0 ]]
