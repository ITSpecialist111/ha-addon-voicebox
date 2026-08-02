# Voicebox — Home Assistant add-on wrapper
#
# Wraps the upstream image rather than rebuilding from source. Upstream is
# Debian trixie + Python 3.12 with all the ML dependencies already installed;
# rebuilding that would take ~20 minutes and produce the same thing.
#
# NOTE ON THE UPSTREAM TAG
# The published image disagrees with its own Dockerfile. Verified against the
# GHCR registry API on 2026-08-01:
#
#   published :latest / :dev   EXPOSE 8000,  uvicorn --port 8000,  runs as root
#   upstream Dockerfile        EXPOSE 17493, uvicorn --port 17493, non-root
#
# :latest and :dev are the SAME digest, built 2026-02-03, and unchanged since.
# run.sh and config.yaml therefore target port 8000. If upstream republishes an
# image matching its Dockerfile, update the port in both places together.
#
# That staleness is why the two build steps below exist at all: the published
# image ships a TTS engine it cannot import and a web UI that only works if the
# browser is on the same machine as the server. Both are repaired here, at
# build time, so a failure is a failed build rather than a broken add-on.
ARG VOICEBOX_TAG=latest
FROM ghcr.io/jamiepine/voicebox:${VOICEBOX_TAG}

# Supervisor 2026.04.0 removed the implicit BUILD_FROM, so the FROM above is
# explicit by necessity as well as by preference.

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=0.3.0

# tini gives correct signal forwarding and zombie reaping, so uvicorn actually
# receives SIGTERM when Home Assistant stops the add-on.
#
# bash is almost certainly already present (the base is the Debian-derived
# python:3.12 image), but run.sh uses [[ ]] and here-docs, so install it
# explicitly rather than depend on that. If it is already there this is a no-op.
#
# curl is not required at runtime — there is no HEALTHCHECK here, because health
# is expressed through config.yaml and the watchdog is deliberately disabled.
# It is included solely so the container is debuggable from `docker exec`.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends bash tini curl sox; \
    rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Missing TTS engine
# ---------------------------------------------------------------------------
# The published image cannot generate speech. Downloading any TTS model fails:
#
#   Marked qwen-tts-0.6B as error: No module named 'qwen_tts'
#   Error: qwen_tts package not found.
#   Install with: pip install git+https://github.com/QwenLM/Qwen3-TTS.git
#
# The in-app provider installer cannot fix it either — it fetches from
# https://downloads.voicebox.sh/providers/v1.0.0/... which now returns 404 —
# and there is no newer image to move to. So we install it here.
#
# Two deliberate departures from the command the app suggests:
#
#   --no-deps on qwen-tts, because its metadata pins transformers==4.57.3 and
#   accelerate==1.12.0. Letting those pins apply would re-resolve the ML stack
#   this image is built around and could break Whisper transcription, which
#   currently works. It also pulls gradio, an entire web framework needed only
#   by the project's standalone demo, which would fight the app's own
#   FastAPI/pydantic versions.
#
#   --no-deps then makes its genuine runtime imports our problem, so they are
#   listed explicitly below. The build-time import test is what tells us which
#   ones are genuine: `sox` and `torchaudio` are imported at module scope by
#   qwen_tts.core.tokenizer_25hz.vq.speech_vq, so they are required; gradio is
#   not reached on the import path at all, confirming it is demo-only.
#
#   The image ships transformers 5.0.0 while qwen-tts asks for 4.57.3. We keep
#   5.0.0: it is what the app's own Whisper support is running against, and
#   downgrading it to satisfy a pin would trade a working feature for an
#   unproven one. The import test below is what establishes whether qwen_tts
#   tolerates 5.0.0 — if it does not, this build fails rather than shipping.
#
#   -c constraints, generated from the environment as it already exists, for
#   everything else. pip is then free to ADD packages but cannot upgrade or
#   downgrade anything already installed. If a genuine conflict exists, the
#   build fails here with pip's resolver output instead of producing an image
#   whose torch no longer matches its torchaudio.
RUN set -eux; \
    echo "--- versions already in the upstream image ---"; \
    for pkg in torch torchaudio transformers accelerate numpy soundfile librosa onnxruntime einops; do \
        ver="$(python3 -c "import importlib.metadata as m; print(m.version('$pkg'))" 2>/dev/null || echo "MISSING")"; \
        printf '    %-14s %s\n' "$pkg" "$ver"; \
    done; \
    python3 -m pip freeze 2>/dev/null | grep -E '^[A-Za-z0-9._-]+==' > /tmp/vb-constraints.txt; \
    echo "--- pinning $(wc -l < /tmp/vb-constraints.txt) existing packages as constraints ---"; \
    python3 -m pip install --no-cache-dir --no-deps "qwen-tts==0.1.1"; \
    python3 -m pip install --no-cache-dir -c /tmp/vb-constraints.txt \
        einops soundfile librosa onnxruntime sox torchaudio; \
    rm -f /tmp/vb-constraints.txt

# Prove both halves still work before shipping the image.
#
# The first catches a qwen-tts dependency we declined to install. The second is
# the important one: it proves the FastAPI app itself still imports after the
# installs above, so a dependency conflict shows up as a failed build rather
# than an add-on that starts and 500s. Run from /tmp so the app's relative
# data/ path does not litter the image.
RUN set -eux; \
    python3 -c "import qwen_tts; print('qwen_tts imports OK')"; \
    cd /tmp && python3 -c "import sys; sys.path.insert(0, '/app'); import backend.main; print('backend.main imports OK')"; \
    rm -rf /tmp/data

# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------
# The shipped frontend is a desktop-app build: it calls a hardcoded
# http://127.0.0.1:17493 and references its assets with absolute paths. Served
# from a server the browser is not sitting on, every API call goes to the
# browser's own machine, and under ingress the assets 404 against the Home
# Assistant origin. Neither the direct port nor ingress produces a working UI.
#
# patch-frontend.py rewrites both to be relative to wherever the page is
# actually served from. It verifies its own work and exits non-zero if the
# upstream bundle no longer matches, so this fails the build rather than
# silently shipping a blank page.
COPY patch-frontend.py /usr/local/bin/patch-frontend.py
RUN python3 /usr/local/bin/patch-frontend.py /app

COPY run.sh /run.sh
RUN chmod +x /run.sh && sed -i 's/\r$//' /run.sh

# Sanity-check the entrypoint at build time rather than discovering a typo
# when a user tries to start the add-on.
RUN bash -n /run.sh

LABEL \
    io.hass.name="Voicebox" \
    io.hass.description="Local AI voice studio — text-to-speech, voice cloning and transcription. Runs entirely on your own hardware." \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/run.sh"]
