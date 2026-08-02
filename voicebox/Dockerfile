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
ARG BUILD_VERSION=0.6.0

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
#   transformers is pinned back to 4.57.3. This is a correction, not a
#   preference: upstream's own backend/requirements.txt declares
#   `transformers>=4.36.0,<=4.57.6`, and the published image ships 5.0.0 —
#   outside the range the application itself says it supports. Left at 5.0.0,
#   the engine loads and then fails on the first model with
#   `'Qwen3TTSTalkerConfig' object has no attribute 'pad_token_id'`, because
#   transformers 5 moved the generation attributes off PretrainedConfig.
#   4.57.3 is what qwen-tts pins and sits inside upstream's cap, so this puts
#   the image back to a combination both halves agree on. accelerate is pinned
#   alongside it for the same reason.
#
#   This happens BEFORE the constraints file is generated, so the constraints
#   record the corrected state rather than freezing the broken one in place.
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
    python3 -m pip install --no-cache-dir "transformers==4.57.3" "accelerate==1.12.0"; \
    python3 -m pip freeze 2>/dev/null | grep -E '^[A-Za-z0-9._-]+==' > /tmp/vb-constraints.txt; \
    echo "--- pinning $(wc -l < /tmp/vb-constraints.txt) existing packages as constraints ---"; \
    python3 -m pip install --no-cache-dir --no-deps "qwen-tts==0.1.1"; \
    python3 -m pip install --no-cache-dir -c /tmp/vb-constraints.txt \
        einops soundfile librosa onnxruntime sox; \
    rm -f /tmp/vb-constraints.txt

# torchaudio has to match torch's BUILD VARIANT, not just its version number.
#
# Installing it from PyPI looks correct and is not: the PyPI wheel is the CUDA
# build, so it imports and then dies on `libcudart.so.13: cannot open shared
# object file`, because this image carries a CPU-only torch with no CUDA
# runtime. The version constraint cannot catch that — 2.11.0 satisfies 2.11.0
# either way. The variant is encoded in torch's local version tag (+cpu, +cu128)
# and only the matching index serves the matching wheel.
#
# So derive it from the installed torch rather than hardcoding, which keeps this
# correct if the upstream image is ever rebuilt against a different torch, and
# would pick up a GPU image automatically. --no-deps because torch is already
# present and pinned; the exact-version retry covers PyTorch dropping the local
# tag for a release.
RUN set -eux; \
    TORCH_VER="$(python3 -c 'import torch; print(torch.__version__)')"; \
    BASE="${TORCH_VER%%+*}"; \
    VARIANT="${TORCH_VER#*+}"; \
    if [ "$VARIANT" = "$TORCH_VER" ]; then VARIANT=""; fi; \
    echo "--- torch ${TORCH_VER} (base ${BASE}, variant ${VARIANT:-none}) ---"; \
    if [ -n "$VARIANT" ]; then \
        IDX="https://download.pytorch.org/whl/${VARIANT}"; \
        python3 -m pip install --no-cache-dir --no-deps --index-url "$IDX" "torchaudio==${TORCH_VER}" \
            || python3 -m pip install --no-cache-dir --no-deps --index-url "$IDX" "torchaudio==${BASE}"; \
    else \
        python3 -m pip install --no-cache-dir --no-deps "torchaudio==${BASE}"; \
    fi; \
    python3 -c "import torchaudio; print('torchaudio', torchaudio.__version__, 'loads against this torch')"

# Prove both halves still work before shipping the image.
#
# The first catches a qwen-tts dependency we declined to install. The second is
# the important one: it proves the FastAPI app itself still imports after the
# installs above, so a dependency conflict shows up as a failed build rather
# than an add-on that starts and 500s. Run from /tmp so the app's relative
# data/ path does not litter the image.
RUN set -eux; \
    python3 -c "import transformers, accelerate; print('transformers', transformers.__version__, '/ accelerate', accelerate.__version__)"; \
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

# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
# This tag is opaque, six months old, and does NOT match upstream's main
# branch. A memory patch was written here against backend files taken from
# GitHub - qwen_custom_voice_backend.py and qwen_llm_backend.py - which do not
# exist in this image at all. It patched nothing, and because that RUN was
# chained with ";" instead of "set -eux" every failure was swallowed and the
# build reported success. Both mistakes are removed; the second cannot recur,
# because every RUN in this file now begins with set -eux.
#
# Rather than guess again, record what is actually here and print it at
# startup. This changes no behaviour.
COPY inventory.py /usr/local/bin/inventory.py
RUN set -eux; \
    python3 /usr/local/bin/inventory.py /app/backend /app/.voicebox-inventory.txt

# ---------------------------------------------------------------------------
# Model size policy
# ---------------------------------------------------------------------------
# Voicebox ships two TTS models. Measured on the target hardware:
#
#     0.6B    peak 4220 MB    loaded in 26 s      works
#     1.7B    peak ~8130 MB   OOM-killed twice    does not fit
#
# Upstream defaults to 1.7B at every entry point - the /generate request model,
# the /generate fallback, the /models/load query parameter, and the backend
# constructor that an unqualified load falls through to. So the DEFAULT action
# of the web UI was the one that cannot work here, and because Supervisor sets
# oom_score_adj=200 on add-ons the kernel kills add-ons first: an oversized
# Voicebox endangers Frigate, the NVR, not just itself.
#
# enforce-model-policy.py rewrites those defaults and guards load_model_async,
# the one function every TTS load funnels through. Refusals become HTTP errors.
#
# Two RUN steps, both in exec form. Exec form means no shell is involved, so
# nothing can mask a non-zero exit - which is exactly how the previous patch in
# this repository failed silently and shipped an image that was OOM-killed in
# production. --apply refuses to touch a file whose SHA-256 has moved, and
# --verify re-checks the result behaviourally afterwards.
COPY enforce-model-policy.py /usr/local/bin/enforce-model-policy.py
RUN ["python3", "/usr/local/bin/enforce-model-policy.py", "--apply"]
RUN ["python3", "/usr/local/bin/enforce-model-policy.py", "--verify"]

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
