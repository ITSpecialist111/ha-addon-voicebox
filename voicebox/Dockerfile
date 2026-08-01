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
# Both tags are the same 2026-02-03 build. run.sh and config.yaml therefore
# target port 8000. If upstream republishes an image matching its Dockerfile,
# update the port in both places together.
ARG VOICEBOX_TAG=latest
FROM ghcr.io/jamiepine/voicebox:${VOICEBOX_TAG}

# Supervisor 2026.04.0 removed the implicit BUILD_FROM, so the FROM above is
# explicit by necessity as well as by preference.

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=0.1.0

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
    apt-get install -y --no-install-recommends bash tini curl; \
    rm -rf /var/lib/apt/lists/*

COPY run.sh /run.sh
RUN chmod +x /run.sh && sed -i 's/\r$//' /run.sh

# Sanity-check the entrypoint at build time rather than discovering a typo
# when a user tries to start the add-on.
RUN bash -n /run.sh

LABEL \
    io.hass.name="Voicebox" \
    io.hass.description="Local AI voice studio — TTS, voice cloning, transcription, MCP" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/run.sh"]
