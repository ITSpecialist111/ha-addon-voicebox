# Voicebox — Home Assistant add-on repository

A Home Assistant add-on that packages [Voicebox](https://docs.voicebox.sh), a
local AI voice studio: text-to-speech, voice cloning and transcription, running
entirely on your own hardware.

## Add this repository

Settings → Add-ons → Add-on Store → ⋮ → Repositories, then add:

```
https://github.com/ITSpecialist111/ha-addon-voicebox
```

## Add-ons in this repository

| Add-on | Description |
|---|---|
| [Voicebox](voicebox/) | Local AI voice studio — TTS, voice cloning, transcription |

## Before you install: the RAM question

Voicebox states a **minimum of about 8 GB**. Two things make that more serious
here than a typical heavy add-on:

1. **Home Assistant add-ons cannot be given a memory limit.** There is no
   `mem_limit` in the add-on schema, and the Supervisor passes no memory
   argument when creating the container. The cgroup ceiling you would use in a
   plain Docker deployment cannot be expressed here.

2. **Add-ons are created with `oom_score_adj=200`** — a positive bias, meaning
   the kernel picks add-ons *first* when it needs to free memory. An add-on that
   does not fit can therefore cause a *different* add-on to be killed.

So this add-on performs a memory preflight and **refuses to start** if there is
not enough available, explaining exactly what it found. That refusal is a
feature. There is an override, documented and deliberately unattractive.

Check your own figure with `free -m` and read the **available** column, not
`free`.

### Measure it — do not assume, in either direction

The 8192 MB default is upstream's figure for peak use *during inference*, not
what the service occupies while sitting there. Measured on a real 16 GB machine
running 19 add-ons including Frigate, **Voicebox idles at about 614 MB**; models
are loaded on demand.

The same machine also shows why the measurement has to be current:

| | after weeks of uptime | 7 hours after a reboot |
|---|---|---|
| MemAvailable | 6482 MB | **7194 MB** |
| Free swap | 817 MB | **4096 MB** (100%) |
| Pages swapped out | — | **0** |
| Memory pressure (PSI `some avg10`) | 52% | **0.00** |

Nothing was installed or removed between those two readings. The first was an
accumulated degraded state, and a verdict based on it — that the shortfall
exceeded free swap and so could not be satisfied by any means — was simply
wrong. On the second reading the add-on started on a 6000 MB threshold and ran
with zero pages swapped and Frigate unaffected.

If you are close to the line, **reboot and re-measure before deciding.** And if
Voicebox genuinely does not fit alongside Home Assistant, the better pattern is
to run it on a separate machine and point Home Assistant at it over REST — the
service is designed to be used over the network.

### RAM is often not the real constraint — CPU is

On the machine above, memory pressure sat at 0.00 while **CPU** pressure ran at
~19% with 86% of four cores busy. Voicebox inference is CPU-bound and competes
directly with anything latency-sensitive on the same box. With Frigate that
shows up as *silently dropped camera frames*, which is worse than a refused
start because nothing announces it.

The `cpu_priority` option defaults to `low`, which runs Voicebox under `nice 19`
and `ionice -c 3`. That costs nothing while the CPU is idle and makes Voicebox
yield the moment anything else wants it.

## What the add-on adds over the upstream container

- **RAM preflight** — refuses to start rather than triggering the OOM killer,
  with a clear explanation and an opt-in override
- **A working web UI** — the shipped frontend is a desktop-app build that only
  works if the browser is on the same machine as the server. See below
- **The missing TTS engine** — the published image cannot actually generate
  speech without it. See below
- **CPU priority** — yields to Frigate and friends under contention
- **Persistence** — Voicebox writes profiles to `/app/data` inside the image,
  which would be lost on every update. The add-on redirects that onto the
  persistent volume and migrates existing data once, refusing to start rather
  than half-migrate
- **Model cache on the data volume** — the published image runs as root, so the
  cache would otherwise land somewhere ephemeral and re-download several GB on
  every recreate
- **Ingress** — puts the UI behind Home Assistant authentication, which matters
  because Voicebox has none of its own
- **Backups** — profiles included, multi-GB model cache excluded
- **No watchdog, manual boot** — an OOM-killed process that restarts in a loop
  is worse than one that stays down

## Notes on the upstream image

`:latest` and `:dev` are the **same digest**, built 2026-02-03 and unchanged
since. Several things in it are broken or contradict its own source, and the
add-on works around each:

| | Published `:latest` | Upstream Dockerfile |
|---|---|---|
| Port | 8000 | 17493 |
| User | root | non-root `voicebox` |

The add-on targets the **published image**, so the container listens on 8000 and
is mapped to host port 17493 to match upstream's documented port. If you build
from source instead, that mapping needs revisiting.

**The TTS engine is missing.** Downloading any voice model fails with
`No module named 'qwen_tts'`. The in-app provider installer cannot repair it
either — it fetches `https://downloads.voicebox.sh/providers/v1.0.0/...`, which
now returns 404 — and `/providers/start` rejects provider types that
`/providers` itself advertises. The add-on installs the engine at build time,
deliberately without `qwen-tts`'s own dependency pins (they would re-resolve the
ML stack the image is built around) and without `gradio` (an entire web
framework needed only by the project's standalone demo).

**The web UI cannot reach its own server.** The frontend is built for a desktop
app: every API call goes to a hardcoded `http://127.0.0.1:17493`, which resolves
to *the machine running the browser*, and `index.html` references its assets
with absolute paths, which 404 under an ingress sub-path. Unpatched, the UI is
blank under ingress and non-functional over the direct port for any remote
browser. `patch-frontend.py` rewrites both at build time and verifies its own
work, so a future upstream rebuild fails the build rather than silently shipping
a blank page.

**There is no `/mcp` endpoint**, despite the documentation. Verified against the
running container's own `/openapi.json`: 50+ routes, none of them `/mcp`. REST
is the integration path today. This should start working on its own once
upstream republishes the image.

**`POST /shutdown` is unauthenticated**, like the rest of the API — anything
that can reach the port can stop the service. Prefer the ingress panel.

The upstream docs also state prebuilt images are "coming soon" — they exist.

## Testing

The safety-critical parts are tested, and the tests run against the real
`run.sh` and the real `patch-frontend.py` via path seams, not modified copies:

```sh
./test-ram-preflight.sh      # preflight, options parsing, persistence, CPU priority
python test-patch-frontend.py # frontend rewriting, against the real shipped bundle
python check-consistency.py   # cross-file seams no syntax check catches
```

The frontend fixtures are copied verbatim from the running container, so if
upstream changes shape the tests say so.

## Support

Voice generation, model quality, cloning and the UI are upstream concerns:
https://docs.voicebox.sh

The preflight, packaging, ingress, persistence, the frontend fix and
configuration are handled here.

## Licence

The add-on packaging in this repository is provided as-is. Voicebox itself is
licensed by its own authors — see the upstream project.
