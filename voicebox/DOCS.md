# Voicebox

Local AI voice studio for Home Assistant — text-to-speech, voice cloning
and transcription, running entirely on your own hardware.

## Read this first: will it fit?

**It depends entirely on which model you use, and upstream defaults to the one
that does not fit.**

Voicebox ships two text-to-speech models. These are measured figures from a
16 GB Home Assistant box running Frigate and 18 other add-ons, not numbers
copied from documentation:

| Model | Peak RAM | Result |
|---|---|---|
| `0.6B` | **4220 MB** | loaded in 26 s — works |
| `1.7B` | **~8130 MB** | OOM-killed, twice |

Idle usage, with no model loaded, is about **600 MB**. The large figures are
peaks *during a model load*, not resident usage.

Upstream defaults every entry point to `1.7B` — the `/generate` request body,
its internal fallback, the `/models/load` query parameter, and the backend
constructor that an unqualified load falls through to. So out of the box, the
default action of the web UI is the one that cannot work on a machine this
size. **This add-on rewrites those defaults to `0.6B` at build time and refuses
the large model with a clear HTTP error** rather than letting it invite the OOM
killer. See [`allow_large_model`](#allow_large_model) if your host really can
spare 8 GB.

Home Assistant add-ons **cannot be given a memory limit**. There is no such
option in the add-on schema, and the Supervisor does not pass any memory
argument when it creates the container. So unlike a normal Docker deployment,
there is nothing to stop Voicebox taking whatever it wants.

Worse, the Supervisor creates every add-on with `oom_score_adj=200`. That is a
*positive* bias: under memory pressure the Linux OOM killer targets add-ons
before other processes. An oversized Voicebox does not just fail on its own —
it makes every other add-on on the machine a more likely casualty. If you run
Frigate as an NVR, that is your camera recording.

Because of that, this add-on **refuses to start** if there is not enough memory
available, rather than starting and hoping. That check is the main thing this
wrapper adds over running the upstream container directly.

### Check your own numbers

Settings → System → Hardware, or from a terminal:

```sh
free -m
```

Compare the **available** column against 8192 MB. Not "free" — `available` is
the kernel's own estimate of what can be handed out without swapping, which is
exactly the question being asked. `free` looks alarmingly low on a healthy
system because it excludes reclaimable page cache.

Two readings from the *same* real 16 GB machine — an OptiPlex 3040 running 19
add-ons including Frigate — taken weeks apart:

```
                        after weeks up     7 h after a reboot
Voicebox minimum           8192 MB             8192 MB
Actually available         6482 MB             7194 MB
Short by                   1710 MB              998 MB
Free swap                   817 MB             4096 MB
verdict                    REFUSE              START
```

Nothing was installed or removed between those two readings. The machine had
simply been up long enough to accumulate 3.2 GB of paged-out memory, and the
reboot returned it. In the first reading the shortfall exceeded free swap, so
the allocation could not be satisfied by any means. In the second it is covered
several times over.

The lesson: **measure, do not assume, and prefer a recently rebooted reading.**
A long-uptime figure describes accumulated drift, not the actual requirement. If
you are close to the line, reboot and measure again before concluding it will
not fit.

**16 GB is not automatically enough**, and it is not automatically insufficient
either. It depends entirely on what else you are running. Frigate alone can use
4–5 GB.

### RAM is often not the real constraint — CPU is

On the machine above, memory pressure sat at **0.00** while CPU sat at **86 %
busy across 4 cores**, with CPU pressure at 18 %. There was enough RAM and not
enough CPU.

That matters because Voicebox inference is CPU-bound, and the other big consumer
was Frigate doing camera detection. At equal priority the two simply take turns,
and the visible symptom is not slow speech — it is **dropped frames on the
NVR**. That is a much worse failure than a refused start, because it is silent.

This is what `cpu_priority` is for, and why it defaults to `low`. See below.

## Installation

1. Settings → Add-ons → Add-on Store
2. ⋮ (top right) → Repositories
3. Add `https://github.com/ITSpecialist111/ha-addon-voicebox`
4. Find **Voicebox** in the store and click Install

The first install builds the add-on locally and pulls a large upstream image
(around 4.4 GB compressed, roughly 11 GB on disk). Budget time and disk space.

The first *start* then downloads several GB of ML models. This is slow and it
is normal — the add-on will look unresponsive for a while. Models are cached on
the persistent volume, so this happens once.

**Disk:** allow around 20 GB total for the image plus models.

## Configuration

```yaml
log_level: info
min_free_ram_mb: 8192
allow_low_ram_start: false
cors_origins: ""
```

### `log_level`

`debug` | `info` | `warning` | `error`. Passed to uvicorn. Use `debug` if
something is not behaving and you want detail in the add-on log.

### `min_free_ram_mb`

How much RAM must actually be available before the add-on will start.
Default 8192, which is what upstream states as the minimum for a single engine.

- **Lower it** if you have measured your workload and know it needs less — for
  example, if you only use one small TTS engine and never load a second.
- **Set it to `0`** to disable the check entirely. Only sensible if you are
  running on a machine with plenty of headroom and find the check noise.
- **Raise it** to around 16384 if you plan to run multiple engines
  simultaneously, which is what upstream recommends.

### `allow_low_ram_start`

Default `false`. When `true`, a failed RAM check becomes a warning instead of a
hard stop, and the add-on starts anyway.

**Think carefully before enabling this.** The check is not being cautious for
its own sake. If it says you are short, then starting Voicebox means one of:

- Voicebox is OOM-killed shortly after start, or
- something else on the machine is OOM-killed instead — and because add-ons
  carry `oom_score_adj=200`, an add-on is the likely victim, or
- the machine begins swapping heavily and everything becomes slow enough to be
  effectively broken.

Enable it if you are testing, or if you genuinely know better than the estimate.
Do not enable it to make an error message go away on a machine that is already
tight.

### `allow_large_model`

Default `false`. Leave it there unless you have measured your own headroom.

`false` permits only the `0.6B` model. Any attempt to load `1.7B` — from the
UI, from the REST API, or indirectly while building a voice prompt — returns
HTTP 400 with an explanation, instead of allocating ~8 GB and being killed.

`true` permits both. Only do this if the host genuinely has ~8 GB spare. An
out-of-memory event here is not contained to Voicebox: add-ons carry
`oom_score_adj=200`, so the kernel prefers them as victims, and the practical
casualty is whatever else is large — usually Frigate.

Turning this on also **raises the memory floor**. The 1.7B model peaked at
8130 MB here, and the default `min_free_ram_mb` is 8192 MB — 62 MB of margin,
which is a rounding error rather than headroom, since `MemAvailable` moves by
more than that while a model is loading. So if you enable `allow_large_model`
and leave `min_free_ram_mb` below 9600, the add-on raises it to 9600 for that
start and says so. Set it explicitly above 9600 to take control back.

The enforcement is not advisory. At build time the image is patched at the one
function every model load passes through, and the build fails if that function
has moved. See `enforce-model-policy.py`.

Three things back it up, because a guard that can be bypassed is not a guard:

- **The external-provider subsystem is refused too.** Voicebox can download and
  run a TTS engine as a *separate process*; its client defaults to 1.7B and
  reports the size to that process rather than loading it here — so the
  in-process guard would never see it, and neither would this add-on's memory
  accounting. `POST /providers/start` and `POST /providers/download` therefore
  return HTTP 403 unless `allow_large_model` is on. Stopping, deleting and
  listing providers are unaffected.
- **The add-on refuses to start if the guard is missing.** Not a warning — it
  re-runs the verifier at startup and exits. An unguarded image never runs.
- **The upstream image is pinned by digest**, not by the mutable `latest` tag,
  so upstream cannot change the code underneath the patch.

### `cpu_priority`

`low` (default) or `normal`.

Home Assistant applies **no CPU quota** to add-ons, in the same way it applies no
memory limit — the Supervisor's container-create call sets `cpu_rt_runtime`,
which is realtime scheduling headroom, not a general cap. So if a ceiling is
wanted, the add-on has to impose it on itself.

`low` starts Voicebox under `nice -n 19` and `ionice -c 3`. Both are free when
the machine is idle: they only take effect when something else actually wants
the resource. The practical effect is that under load Voicebox generation gets
slower, instead of Frigate dropping frames or the UI going sluggish.

Leave this on `low` unless Voicebox is the most important thing on the box. The
only cost is generation latency while the machine is busy.

### `cors_origins`

Optional comma-separated list of allowed origins, if you want to call the
Voicebox API from a browser page served from somewhere else. Leave empty unless
you need it.

## Access

- **Sidebar (ingress):** click Voicebox in the sidebar. This route goes through
  Home Assistant's own authentication.
- **Direct:** `http://<your-ha-ip>:17493`

Ingress is enabled because Voicebox has **no authentication of its own**, and
ingress puts HA's login in front of it.

### The upstream UI is patched, because unpatched it cannot work here

The frontend in the published image is built for a **desktop app**, and it shows
in two ways. Both are fixed at build time by `patch-frontend.py`.

1. **Every API call went to a hardcoded `http://127.0.0.1:17493`.** In a desktop
   app that is correct — server and browser are the same machine. Served over a
   network it is not: `127.0.0.1` resolves to *the machine running the browser*,
   so the UI tries to talk to a Voicebox on your laptop. This broke the direct
   port too, not just ingress, for every browser except one running on the Home
   Assistant host itself.

2. **`index.html` referenced its assets with absolute paths** (`/assets/...`).
   Under ingress the app is served from a sub-path, so those requests hit Home
   Assistant's own root and 404 — producing a blank panel.

The patch makes the asset paths relative and replaces the hardcoded origin with
a base URL derived from `location.pathname` at load time. Under ingress that
yields the ingress prefix; anywhere else it yields the empty string, meaning
same-origin. One patch, both routes fixed.

It verifies itself during the build. If a future upstream image changes shape so
that the patch no longer applies, **the build fails** rather than quietly
shipping a blank page.

## Security

**The Voicebox API has no authentication at all.** That includes:

- the REST API
- the web UI itself
- `POST /shutdown`, which stops the service — no credentials required

Confirmed on a live instance: every route answers without a token.

Anything that can reach port 17493 can use your voice profiles, generate speech,
and read anything Voicebox has generated.

Recommendations:

- Prefer the ingress route, which is behind HA login.
- Do **not** port-forward 17493 to the internet. Not behind a "temporary" rule
  either.
- If you must reach it remotely, use a VPN or put an authenticating reverse
  proxy in front of it.
- Voice cloning means voice profiles are biometric-adjacent personal data. Treat
  the add-on's data directory accordingly, and remember it is included in Home
  Assistant backups.

## Data and backups

| Path | Contents | In backups |
|---|---|---|
| `/data/app-data` | Profiles, database, generated audio | yes |
| `/data/cache/huggingface` | Downloaded ML models, several GB | **no** |

The model cache is excluded from backups deliberately. It is a cache — it
re-downloads on demand, and including it would bloat every backup by several GB
for no benefit.

Backups are **cold**: the add-on is stopped for the duration. Given the memory
footprint this is the safer choice.

Voicebox natively writes profiles to `/app/data`, which lives inside the image
and would be destroyed on every update. The add-on redirects that onto the
persistent volume with a symlink on first start, migrating anything already
there. **Your profiles survive add-on updates.**

## Using it from Home Assistant

Voicebox is not a Wyoming service, so it will **not** appear as a TTS engine in
an Assist pipeline. It is a REST service — the published image has no `/mcp`
endpoint, see below — so drive it with `rest_command`.

A minimal example:

```yaml
rest_command:
  voicebox_say:
    url: "http://<your-ha-ip>:17493/generate"
    method: POST
    content_type: "application/json"
    payload: >-
      {"profile_id": "{{ profile_id }}", "text": {{ message | to_json }}}
```

Note `to_json` on the text — it handles quotes, backslashes, newlines and
Unicode correctly. String-concatenating user text into JSON will break the
moment someone uses an apostrophe.

Useful endpoints (no `/api` prefix — this comes from the project's own
`openapi.json`, the prose docs are inconsistent):

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/profiles` | List voice profiles |
| POST | `/generate` | Generate speech |
| GET | `/audio/{id}` | Fetch generated audio |
| POST | `/transcribe` | Speech to text |
| GET | `/models/status` | What is loaded |
| POST | `/models/unload` | **Free RAM** without stopping the add-on |
| POST | `/models/download` | Fetch a model |
| GET | `/history` | Past generations |
| POST | `/shutdown` | **Stops the service — unauthenticated** |

`/models/unload` is worth knowing about on a tight machine — it releases model
memory while leaving the service up.

### `/mcp` is NOT available in the current published image

Upstream documents an MCP endpoint at `/mcp` over Streamable HTTP. **The image
this add-on builds on does not implement it.** Verified against the running
container's own `/openapi.json`: 50+ routes are present and `/mcp` is not among
them. `GET /mcp` returns 404.

The published `:latest` tag was built 2026-02-03 and has not moved since, so MCP
appears to postdate it. Until upstream publishes a newer image, **use the REST
API** — `/generate` plus `/profiles` covers everything needed to speak from Home
Assistant.

This will start working on its own when upstream refreshes the image; nothing in
this add-on needs to change.

## Troubleshooting

### "REFUSING TO START — not enough memory available"

Working as designed. The add-on log shows exactly how much was available, how
much was needed, and how much swap was free. Options, best first:

1. Run Voicebox on a machine with more RAM and point HA at it over REST.
2. Stop other add-ons to free memory, then retry.
3. Lower `min_free_ram_mb` if you have measured your actual need.
4. Set `allow_low_ram_start: true` if you accept the risks above.

### The add-on starts, then dies without an error

Almost certainly the OOM killer. Add-on logs often show nothing because the
process is killed with `SIGKILL` and gets no chance to log. Check:

```sh
dmesg | grep -i "killed process"
```

If Voicebox is named there, it did not fit. Note that something *else* being
named there is also possible, and is the more dangerous outcome.

### First start takes forever

Expected. It is downloading several GB of models. Watch the log. Subsequent
starts are much faster because the cache is on the persistent volume.

Note that the **build** is also slow, and is CPU-heavy. On a machine that is
already busy — an OptiPlex also running Frigate, for instance — schedule the
build for a quiet period. The failure mode of building during peak load is not a
failed build, it is your NVR dropping frames while it runs.

### Sidebar panel is blank, or the UI loads but nothing works

This was the unpatched upstream behaviour — see **Access** above. It is fixed at
build time, so if you are seeing it now, the most likely cause is that the patch
did not run: check the add-on build log for `patch-frontend.py` and for the line
confirming how many files it rewrote.

A quick way to tell the two failure modes apart in the browser console:

- **Blank panel, 404s for `/assets/...`** — the asset paths are still absolute.
- **Panel renders but every action fails, with connection errors to
  `127.0.0.1:17493`** — the hardcoded origin is still in the bundle.

Rebuilding the add-on re-applies the patch.

### The UI loads but the History view is blank or errors

Fixed in 0.6.1. Upstream's `GET /history` declares `limit: int = 50` with no
validation, then builds a `HistoryQuery` inside the handler body, where the
model caps `limit` at 100. A pydantic error raised there is no longer
convertible to a 422, so it surfaces as an unhandled exception:

```
GET /history?limit=100  -> 200
GET /history?limit=101  -> 500 Internal Server Error
```

The shipped frontend hardcodes `limit=1000`, so this fired on every History
load. Because it is thrown during render rather than caught, it can blank the
whole page — which looks like ingress being broken when it is not.

The build now widens the cap to 1000 and clamps the value in the handler, so no
request can reach the raising path.

### Voice generation fails with `No module named 'qwen_tts'`

That is the published upstream image, which ships **without a TTS engine** —
downloading a voice model appears to work and then errors. The in-app provider
installer cannot repair it either: it fetches from `downloads.voicebox.sh`,
which returns 404, and `/providers/start` rejects provider types that
`/providers` itself lists as installed.

This add-on installs the engine during the build, so a freshly built add-on
should not show this. If it does, the build did not complete — check the log for
the `import qwen_tts` smoke test, which runs at build time precisely so this
fails loudly at build rather than quietly at first use.

### Out of disk space

The image is ~11 GB on disk and models add several more. Check with
`df -h`, and remember the model cache lives under the add-on's data volume.

## Support

This add-on is a thin wrapper around the upstream project. Issues with voice
generation, model quality, cloning or the UI belong upstream:

- Upstream docs: https://docs.voicebox.sh
- Upstream image: `ghcr.io/jamiepine/voicebox`

Issues with the RAM preflight, the add-on configuration, ingress, persistence or
packaging belong in this repository.
