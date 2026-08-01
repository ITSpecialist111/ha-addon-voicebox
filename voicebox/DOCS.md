# Voicebox

Local AI voice studio for Home Assistant — text-to-speech, voice cloning,
transcription, and an MCP server, running entirely on your own hardware.

## Read this first: will it fit?

**Voicebox needs about 8 GB of RAM free, on top of everything Home Assistant is
already running.** That is a genuinely large amount for a typical HA box, and
it is the single thing most likely to stop this working for you.

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

A worked example from a real 16 GB machine running 19 add-ons including Frigate:

```
Voicebox minimum          8192 MB
Actually available        6482 MB
Short by                  1710 MB
Free swap                  817 MB
```

The shortfall is larger than the remaining swap, so the allocation cannot be
satisfied at all — not by swapping, not by anything. On that machine the add-on
refuses to start, and it is right to.

**16 GB is not automatically enough.** It depends entirely on what else you are
running. Frigate alone can use 5 GB.

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

### `cors_origins`

Optional comma-separated list of allowed origins, if you want to call the
Voicebox API from a browser page served from somewhere else. Leave empty unless
you need it.

## Access

- **Sidebar (ingress):** click Voicebox in the sidebar. This route goes through
  Home Assistant's own authentication.
- **Direct:** `http://<your-ha-ip>:17493`

Ingress is enabled because Voicebox has **no authentication of its own**, and
ingress puts HA's login in front of it. However, Voicebox serves a single-page
app, and single-page apps sometimes emit absolute asset paths that do not
survive being served under an ingress sub-path.

**If the sidebar panel renders blank or the UI misbehaves, use the direct port
instead.** That is why the port is still published. This particular combination
has not been verified against a running instance.

## Security

**The Voicebox API has no authentication at all.** That includes:

- the REST API
- the `/mcp` endpoint
- the web UI itself

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
an Assist pipeline. It is a REST/MCP service. Use it via `rest_command`, or
point an MCP-capable agent at the `/mcp` endpoint.

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
| — | `/mcp` | MCP over Streamable HTTP |

`/models/unload` is worth knowing about on a tight machine — it releases model
memory while leaving the service up.

## Troubleshooting

### "REFUSING TO START — not enough memory available"

Working as designed. The add-on log shows exactly how much was available, how
much was needed, and how much swap was free. Options, best first:

1. Run Voicebox on a machine with more RAM and point HA at it over REST/MCP.
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

### Sidebar panel is blank

Likely the ingress sub-path issue described under **Access**. Use
`http://<your-ha-ip>:17493` directly.

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
