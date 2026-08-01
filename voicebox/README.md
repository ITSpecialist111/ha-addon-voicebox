# Home Assistant add-on: Voicebox

Local AI voice studio — text-to-speech, voice cloning, transcription and an MCP
server, running entirely on your own hardware.

![Supports amd64][amd64-shield] ![Supports aarch64][aarch64-shield]

## ⚠️ Needs roughly 8 GB of free RAM

This is a large add-on. Before installing, check **Settings → System → Hardware**
and compare your *available* memory against 8 GB — on top of everything Home
Assistant already runs.

Home Assistant add-ons cannot be given a memory limit, and the Supervisor marks
them as preferred targets for the OOM killer. So an add-on that does not fit
does not simply fail — it can take other add-ons down with it.

This add-on therefore **refuses to start** rather than risk that. If it says no,
believe it. Full explanation and worked numbers in [DOCS.md](DOCS.md).

## Install

Add this repository to Home Assistant, then install **Voicebox** from the store:

```
https://github.com/ITSpecialist111/ha-addon-voicebox
```

## What you get

- Web UI behind Home Assistant authentication via ingress
- REST API on port 17493
- MCP endpoint at `/mcp` for AI agents
- Profiles and generated audio persisted across updates
- Model cache excluded from backups

See [DOCS.md](DOCS.md) for configuration, security notes, endpoints and
troubleshooting.

[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
