# Home Assistant add-on: Voicebox

Local AI voice studio — text-to-speech, voice cloning and transcription, running
entirely on your own hardware.

![Supports amd64][amd64-shield] ![Supports aarch64][aarch64-shield]

## ⚠️ Read the RAM note before installing

Voicebox states a minimum of roughly 8 GB. That is its *peak during inference*,
not what it occupies at rest — measured idle usage is around 600 MB.

Home Assistant add-ons cannot be given a memory limit, and the Supervisor marks
them as preferred targets for the OOM killer. So an add-on that does not fit
does not simply fail — it can take other add-ons down with it.

This add-on therefore runs a memory preflight and **refuses to start** rather
than risk that. If it says no, read what it printed: it tells you exactly how
much was available and how much it wanted. Worked numbers, and why a recently
rebooted machine can give a very different answer, are in [DOCS.md](DOCS.md).

On a busy machine **CPU is usually the tighter constraint than RAM.** The
`cpu_priority` option defaults to `low` so Voicebox yields to things like
Frigate instead of stealing camera-detection time.

## Install

Add this repository to Home Assistant, then install **Voicebox** from the store:

```
https://github.com/ITSpecialist111/ha-addon-voicebox
```

## What you get

- Web UI behind Home Assistant authentication via ingress
- REST API on port 17493
- Profiles and generated audio persisted across updates
- Model cache excluded from backups
- The upstream image's missing TTS engine, installed at build time
- A frontend that works when served over the network rather than only to
  localhost

Note that the published upstream image has **no MCP endpoint** despite the
project documentation; `/mcp` returns 404. Integrate over REST for now. See
[DOCS.md](DOCS.md) for configuration, security notes, endpoints and
troubleshooting.

[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
