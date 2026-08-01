# Voicebox — Home Assistant add-on repository

A Home Assistant add-on that packages [Voicebox](https://docs.voicebox.sh), a
local AI voice studio: text-to-speech, voice cloning, transcription, and an MCP
server for AI agents. Everything runs on your own hardware.

## Add this repository

Settings → Add-ons → Add-on Store → ⋮ → Repositories, then add:

```
https://github.com/ITSpecialist111/ha-addon-voicebox
```

## Add-ons in this repository

| Add-on | Description |
|---|---|
| [Voicebox](voicebox/) | Local AI voice studio — TTS, voice cloning, transcription, MCP server |

## Before you install: the RAM question

**Voicebox needs around 8 GB of RAM free, on top of everything Home Assistant is
already running.** For many Home Assistant installations that is more than is
available, and it is worth checking before you spend time on the install.

Two things make this more serious than a typical add-on being a bit heavy:

1. **Home Assistant add-ons cannot be given a memory limit.** There is no
   `mem_limit` in the add-on schema, and the Supervisor passes no memory
   argument when creating the container. The cgroup ceiling you would use in a
   plain Docker deployment cannot be expressed here.

2. **Add-ons are created with `oom_score_adj=200`** — a positive bias, meaning
   the kernel picks add-ons *first* when it needs to free memory. An add-on that
   does not fit can therefore cause a different add-on to be killed.

So this add-on performs a memory preflight and **refuses to start** if there is
not enough available, explaining exactly what it found. That refusal is a
feature. There is an override, documented and deliberately unattractive.

Check your own figure with `free -m` and look at the **available** column, not
`free`.

### It genuinely does not fit everywhere

Measured on a real 16 GB machine running 19 add-ons including Frigate:

```
Voicebox minimum          8192 MB
Actually available        6482 MB
Short by                  1710 MB
Free swap                  817 MB    <- smaller than the shortfall
```

That allocation cannot be satisfied by any means, and the add-on correctly
refuses. 16 GB of installed RAM is not the same as 8 GB of available RAM.

If Voicebox does not fit alongside Home Assistant, the better pattern is to run
it on a separate machine and point Home Assistant at it over REST or MCP. The
service is designed to be used over the network.

## What the add-on adds over the upstream container

- **RAM preflight** — refuses to start rather than triggering the OOM killer,
  with a clear explanation and an opt-in override
- **Persistence** — Voicebox writes profiles to `/app/data` inside the image,
  which would be lost on every update. The add-on redirects that onto the
  persistent volume and migrates existing data once
- **Model cache on the data volume** — the published image runs as root, so the
  cache would otherwise land somewhere ephemeral and re-download several GB on
  every recreate
- **Ingress** — puts the UI behind Home Assistant authentication, which matters
  because Voicebox has none of its own
- **Backups** — profiles included, multi-GB model cache excluded
- **No watchdog, manual boot** — an OOM-killed 8 GB process that restarts in a
  loop is worse than one that stays down

## Testing

The RAM preflight is the safety-critical part, so it is tested:

```sh
./test-ram-preflight.sh
```

38 assertions across 11 scenarios, run against the real `run.sh` via path seams
rather than a modified copy: normal start, genuine refusal, override behaviour,
boundary conditions, malformed and missing configuration, and data migration.

## Notes on the upstream image

The published image differs from the upstream Dockerfile in ways that matter:

| | Published `:latest` | Upstream Dockerfile |
|---|---|---|
| Port | 8000 | 17493 |
| User | root | non-root `voicebox` |

The add-on targets the **published image**, so the container listens on 8000 and
is mapped to host port 17493 to match upstream's documented port. If you build
from source instead, that mapping needs revisiting.

The upstream docs also state prebuilt images are "coming soon" — they exist.

## Support

Voice generation, model quality, cloning and the UI are upstream concerns:
https://docs.voicebox.sh

The preflight, packaging, ingress, persistence and configuration are handled
here.

## Licence

The add-on packaging in this repository is provided as-is. Voicebox itself is
licensed by its own authors — see the upstream project.
