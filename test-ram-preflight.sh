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
    printf '#!/usr/bin/env bash\necho "UVICORN_STARTED args:$*"\necho "SIZES=[${VOICEBOX_ALLOWED_MODEL_SIZES:-unset}]"\n' > "$box/bin/uvicorn"
    chmod +x "$box/bin/uvicorn"

    # The model-policy gate fails CLOSED, so a sandbox with no receipt would
    # never reach uvicorn. Satisfy it by default; the scenarios that test the
    # gate itself remove these again via PREP_HOOK.
    printf '{"stub":true}\n' > "$box/app/.voicebox-model-policy.json"
    printf 'import sys\nsys.exit(0)\n' > "$box/bin/policy-verifier.py"

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
        VOICEBOX_POLICY_RECEIPT="$box/app/.voicebox-model-policy.json" \
        VOICEBOX_POLICY_VERIFIER="$box/bin/policy-verifier.py" \
        bash "$RUN_SH" 2>&1
        printf '%s' "$?" > "$RC_FILE"
    )"
    # PREP_HOOK's mirror image. run.sh has finished but the sandbox still
    # exists, so this is the only window in which the RESULT on disk can be
    # inspected - what the symlink points at, what survived a failed
    # migration. Assertions made after run_case returns would be examining a
    # directory that has already been deleted, and would pass or fail for
    # reasons that have nothing to do with run.sh.
    if [[ -n "${POST_HOOK:-}" ]]; then
        eval "$POST_HOOK"
    fi

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
# The model-policy gate. This exists because the 1.7B model needs ~8.1 GB and
# was OOM-killed twice on this box; the guard that prevents it is applied at
# BUILD time, so run.sh must confirm the guard is actually present in the image
# it has been handed. It used to only warn and carry on, which is the wrong way
# round: the case it catches is precisely the case where starting is unsafe.
printf -- '\n--- 4b. model policy receipt MISSING: must refuse ---\n'
PREP_HOOK='rm -f "$box/app/.voicebox-model-policy.json"'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192}')"
PREP_HOOK=
check        "gets past the RAM preflight"  "RAM preflight passed"    "$out"
check        "refuses without the guard"    "refusing to start"       "$out"
check        "says why"                     "predates the 1.7B guard" "$out"
check_absent "never reaches uvicorn"        "UVICORN_STARTED"         "$out"
check_rc     "exits non-zero"               "1"

# ---------------------------------------------------------------------------
printf -- '\n--- 4c. verifier REJECTS the image: must refuse ---\n'
# A receipt that exists but does not describe the image on disk is the exact
# failure a mere existence check cannot see, so the verifier is re-run here.
PREP_HOOK='printf "import sys\nsys.exit(1)\n" > "$box/bin/policy-verifier.py"'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192}')"
PREP_HOOK=
check        "refuses on a failed verify" "guard is not intact" "$out"
check_absent "never reaches uvicorn"      "UVICORN_STARTED"     "$out"
check_rc     "exits non-zero"             "1"

# ---------------------------------------------------------------------------
printf -- '\n--- 4d. verifier ABSENT from the image: must refuse ---\n'
PREP_HOOK='rm -f "$box/bin/policy-verifier.py"'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192}')"
PREP_HOOK=
check        "refuses without a verifier" "verifiable model guard" "$out"
check_absent "never reaches uvicorn"      "UVICORN_STARTED"        "$out"
check_rc     "exits non-zero"             "1"

# ---------------------------------------------------------------------------
printf -- '\n--- 4e. guard intact: says so, and starts ---\n'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192}')"
check    "confirms the guard"   "model policy verified" "$out"
check    "states the effect"    "cannot be loaded by accident" "$out"
check    "starts"               "UVICORN_STARTED"       "$out"
check_rc "exits cleanly"        "0"

# ---------------------------------------------------------------------------
# allow_large_model without a raised floor is a trap: 1.7B peaked at 8130 MB,
# so the default 8192 MB floor leaves 62 MB of margin - a rounding error, not
# headroom. Opting in must therefore also raise the floor.
printf -- '\n--- 4f. allow_large_model raises the RAM floor ---\n'
out="$(run_case 12000 4096 '{"min_free_ram_mb":8192,"allow_large_model":true}')"
check "notices the floor is too low" "min_free_ram_mb is only 8192 MB" "$out"
check "quotes the real margin"       "62 MB of margin"                 "$out"
check "raises it"                    "need 9600 MB"                    "$out"
check "still starts with 12000 MB"   "UVICORN_STARTED"                 "$out"

printf -- '\n--- 4g. allow_large_model + too little RAM: refuses at the NEW floor ---\n'
out="$(run_case 9000 4096 '{"min_free_ram_mb":8192,"allow_large_model":true}')"
check        "would have passed the old floor" "need 9600 MB"     "$out"
check        "refuses at the raised floor"     "REFUSING TO START" "$out"
check_absent "never reaches uvicorn"           "UVICORN_STARTED"   "$out"
check_rc     "exits non-zero"                  "1"

printf -- '\n--- 4h. explicit high floor is respected, not overridden ---\n'
out="$(run_case 12000 4096 '{"min_free_ram_mb":11000,"allow_large_model":true}')"
check_absent "does not warn"        "min_free_ram_mb is only" "$out"
check        "keeps the user value" "need 11000 MB"           "$out"
check        "starts"               "UVICORN_STARTED"         "$out"

# ---------------------------------------------------------------------------
# The floor is only worth having if it cannot be walked around. Three settings
# would otherwise grant 1.7B without the memory ever being looked at. In each
# case the add-on must still START - the user's choice about starting is
# honoured - while 1.7B stays refused.
printf -- '\n--- 4i. allow_large_model + min_free_ram_mb=0: starts, withholds 1.7B ---\n'
out="$(run_case 500 0 '{"min_free_ram_mb":0,"allow_large_model":true}')"
check        "warns the pair is contradictory" "which disables the"        "$out"
check        "skips the check as asked"        "RAM preflight disabled"    "$out"
check        "starts"                          "UVICORN_STARTED"           "$out"
check        "withholds the large model"       "1.7B stays REFUSED"        "$out"
check        "exports 0.6B only"               "SIZES=[0.6B]"              "$out"
check_absent "never grants 1.7B"               "SIZES=[0.6B,1.7B]"         "$out"
check_rc     "exits cleanly"                   "0"

printf -- '\n--- 4j. allow_large_model + allow_low_ram_start: starts, withholds 1.7B ---\n'
out="$(run_case 900 0 '{"allow_large_model":true,"allow_low_ram_start":true}')"
check        "raises the floor"          "need 9600 MB"        "$out"
check        "warns it is short"         "SHORT of the"        "$out"
check        "starts anyway, as asked"   "UVICORN_STARTED"     "$out"
check        "withholds the large model" "1.7B stays REFUSED"  "$out"
check_absent "never grants 1.7B"         "SIZES=[0.6B,1.7B]"   "$out"
check_rc     "exits cleanly"             "0"

printf -- '\n--- 4k. allow_large_model + unmeasurable RAM + override: withholds 1.7B ---\n'
PREP_HOOK='rm -f "$box/meminfo"'
out="$(run_case 12000 4096 '{"allow_large_model":true,"allow_low_ram_start":true}')"
PREP_HOOK=''
check        "starts on the override"    "UVICORN_STARTED"     "$out"
check        "withholds the large model" "1.7B stays REFUSED"  "$out"
check        "exports 0.6B only"         "SIZES=[0.6B]"        "$out"
check_absent "never grants 1.7B"         "SIZES=[0.6B,1.7B]"   "$out"

printf -- '\n--- 4l. measured and sufficient: 1.7B IS granted ---\n'
out="$(run_case 12000 4096 '{"min_free_ram_mb":9600,"allow_large_model":true}')"
check        "verified the memory"  "Verified"          "$out"
check        "grants the model"     "SIZES=[0.6B,1.7B]" "$out"
check_absent "no withholding note"  "stays REFUSED"     "$out"
check        "starts"               "UVICORN_STARTED"   "$out"

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
printf '{"stub":true}\n' > "$box/app/.voicebox-model-policy.json"
printf 'import sys\nsys.exit(0)\n' > "$box/bin/policy-verifier.py"
out="$(PATH="$box/bin:$PATH" VOICEBOX_OPTIONS_FILE="$box/data/options.json" \
     VOICEBOX_DATA_ROOT="$box/data" VOICEBOX_APP_ROOT="$box/app" \
     VOICEBOX_MEMINFO="$box/meminfo" \
     VOICEBOX_POLICY_RECEIPT="$box/app/.voicebox-model-policy.json" \
     VOICEBOX_POLICY_VERIFIER="$box/bin/policy-verifier.py" bash "$RUN_SH" 2>&1)"

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
# The migration DELETES the source once it believes the copy worked, so the
# dangerous case is not a copy that fails - it is a copy that reports success
# having done nothing. That is not hypothetical: BusyBox `cp -an src/. dst/`
# copies nothing and exits 0, which on a BusyBox base would have destroyed the
# user's profiles. Stub cp to behave exactly that way and require run.sh to
# notice by checking the destination rather than trusting the exit code.
printf -- '\n--- 11b. a copy that lies about succeeding: refuse, keep the source ---\n'
box="$(mktemp -d)"
mkdir -p "$box/data" "$box/app/data" "$box/bin"
echo '{"min_free_ram_mb":0}' > "$box/data/options.json"
echo 'precious profile data' > "$box/app/data/profiles.db"
printf 'MemAvailable:   12582912 kB\nSwapFree:        4194300 kB\n' > "$box/meminfo"
printf '#!/usr/bin/env bash\necho UVICORN_STARTED\n' > "$box/bin/uvicorn"; chmod +x "$box/bin/uvicorn"
printf '{"stub":true}\n' > "$box/app/.voicebox-model-policy.json"
printf 'import sys\nsys.exit(0)\n' > "$box/bin/policy-verifier.py"
printf '#!/usr/bin/env bash\nexit 0\n' > "$box/bin/cp"; chmod +x "$box/bin/cp"
out="$(PATH="$box/bin:$PATH" VOICEBOX_OPTIONS_FILE="$box/data/options.json" \
     VOICEBOX_DATA_ROOT="$box/data" VOICEBOX_APP_ROOT="$box/app" \
     VOICEBOX_MEMINFO="$box/meminfo" \
     VOICEBOX_POLICY_RECEIPT="$box/app/.voicebox-model-policy.json" \
     VOICEBOX_POLICY_VERIFIER="$box/bin/policy-verifier.py" bash "$RUN_SH" 2>&1 || true)"
check        "refuses to start"          "persistence migration failed" "$out"
check        "names what never arrived"  "did not arrive"               "$out"
check        "says nothing was deleted"  "NOTHING has been deleted"     "$out"
check_absent "never reaches uvicorn"     "UVICORN_STARTED"              "$out"
[[ -f "$box/app/data/profiles.db" ]] \
    && { printf '  PASS  the original data is still there\n'; PASS=$((PASS+1)); } \
    || { printf '  FAIL  the original data was deleted anyway\n'; FAIL=$((FAIL+1)); }
[[ ! -e "$box/data/app-data/.migrated" ]] \
    && { printf '  PASS  not falsely marked as migrated\n'; PASS=$((PASS+1)); } \
    || { printf '  FAIL  marked migrated despite copying nothing\n'; FAIL=$((FAIL+1)); }
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
    KEEP_BOX="$(mktemp -d)"
    PREP_HOOK='rm -rf "$box/app/data"; ln -s "$box/nonexistent-target" "$box/app/data"'
    POST_HOOK='readlink -f "$box/app/data" > '"$KEEP_BOX"'/got 2>/dev/null || true; readlink -f "$box/data/app-data" > '"$KEEP_BOX"'/want 2>/dev/null || true'
    out="$(run_case 12000 4096 '{"min_free_ram_mb":8192}')"
    PREP_HOOK=''; POST_HOOK=''
    check "notices and repairs it" "re-pointing" "$out"
    check "still starts"           "UVICORN_STARTED" "$out"
    check_rc "exits cleanly"       "0"
    # The wording is not the point - where the link ends up is. Assert that
    # directly, or a reworded warning would masquerade as a working repair.
    got="$(cat "$KEEP_BOX/got" 2>/dev/null || true)"
    want="$(cat "$KEEP_BOX/want" 2>/dev/null || true)"
    [[ -n "$want" && "$got" == "$want" ]] \
        && { printf '  PASS  link now resolves to the persistent directory\n'; PASS=$((PASS+1)); } \
        || { printf '  FAIL  link resolved to "%s", wanted "%s"\n' "$got" "$want"; FAIL=$((FAIL+1)); }
    rm -rf "$KEEP_BOX"
else
    printf '  SKIP  broken-symlink assertion — this filesystem has no symlink support\n'
fi

# ---------------------------------------------------------------------------
# The migration used to mark itself done and delete the source even when the
# copy had failed, which is unrecoverable. It must abort with the source intact.
printf -- '\n--- 21. migration copy fails: aborts with the original intact ---\n'
if [[ "$(id -u)" != "0" ]] && ln -s /tmp "$(mktemp -d)/probe2" 2>/dev/null; then
    KEEP_BOX="$(mktemp -d)"
    # The copy itself must be what fails. Making $box/data unwritable would
    # abort the earlier cache mkdir instead, and never reach the migration.
    PREP_HOOK='echo precious > "$box/app/data/profile.db"; chmod 000 "$box/app/data/profile.db"'
    # Read the on-disk result while the sandbox still exists. Reading it after
    # run_case returns was examining a deleted directory - which only appeared
    # to pass because a chmod 000 file can defeat rm -rf on some platforms.
    POST_HOOK='chmod 600 "$box/app/data/profile.db" 2>/dev/null || true
        : > '"$KEEP_BOX"'/state
        [[ -f "$box/app/data/profile.db" ]]     && printf "SOURCE_INTACT "   >> '"$KEEP_BOX"'/state
        [[ -e "$box/data/app-data/.migrated" ]] && printf "MARKED_MIGRATED " >> '"$KEEP_BOX"'/state
        true'
    out="$(run_case 12000 4096 '{"min_free_ram_mb":8192}')"
    PREP_HOOK=''; POST_HOOK=''
    state="$(cat "$KEEP_BOX/state" 2>/dev/null || true)"
    check        "refuses to start"              "persistence migration failed" "$out"
    check        "says nothing was deleted"      "NOTHING has been deleted"     "$out"
    check_absent "does not reach uvicorn"        "UVICORN_STARTED"              "$out"
    check        "leaves the original in place"  "SOURCE_INTACT"                "$state"
    check_absent "does not mark itself migrated" "MARKED_MIGRATED"              "$state"
    rm -rf "$KEEP_BOX"
else
    printf '  SKIP  migration-failure assertion — running as root, or no symlink support\n'
fi

printf '\n================================================================\n'
printf ' RESULT: %d passed, %d failed\n' "$PASS" "$FAIL"
printf '================================================================\n\n'
[[ "$FAIL" -eq 0 ]]
