"""Cross-check the add-on's own files against each other.

A mismatch between config.yaml, translations and run.sh does not fail any
syntax check - it just produces an add-on that shows raw option keys in the UI,
or binds a port nothing is mapped to. So check it explicitly.
"""
import pathlib
import re
import sys

import yaml

cfg = yaml.safe_load(open("voicebox/config.yaml", encoding="utf-8"))
tr = yaml.safe_load(open("voicebox/translations/en.yaml", encoding="utf-8"))
run = open("voicebox/run.sh", encoding="utf-8").read()
dockerfile = open("voicebox/Dockerfile", encoding="utf-8").read()
rc = 0


def report(ok, good, bad):
    global rc
    print(f"  {'OK  ' if ok else 'FAIL'} {good if ok else bad}")
    if not ok:
        rc = 1


opts = set(cfg.get("options", {}))
schema = set(cfg.get("schema", {}))
trans = set(tr.get("configuration", {}))

report(opts == schema, f"options == schema  ({sorted(opts)})",
       f"options vs schema differ: {sorted(opts ^ schema)}")
report(opts == trans, "every option has an English translation",
       f"options vs translations differ: {sorted(opts ^ trans)}")

print()
ports = cfg.get("ports", {})
report(ports == {"8000/tcp": 17493},
       "container 8000 -> host 17493 (matches the published image)",
       f"unexpected ports mapping: {ports}")
report(cfg.get("ingress_port") == 8000,
       "ingress_port matches the container port",
       f"ingress_port {cfg.get('ingress_port')} != 8000")
report(set(cfg.get("ports_description", {})) == set(ports),
       "every published port is described",
       "ports_description does not cover every port")
report(set(tr.get("network", {})) == set(ports),
       "translations describe every port",
       "translations network keys do not match ports")

print()
m = re.search(r"--port\s+(\d+)", run)
report(bool(m) and int(m.group(1)) == 8000,
       "run.sh binds 8000, matching config.yaml",
       f"run.sh binds {m.group(1) if m else 'nothing detectable'}")

d = re.search(r"get_opt min_free_ram_mb (\d+)", run)
report(bool(d) and int(d.group(1)) == cfg["options"]["min_free_ram_mb"],
       f"min_free_ram_mb default agrees ({cfg['options']['min_free_ram_mb']})",
       "min_free_ram_mb default differs between run.sh and config.yaml")

allowed = set(re.search(r"list\((.*?)\)", cfg["schema"]["log_level"]).group(1).split("|"))
uvicorn_ok = {"critical", "error", "warning", "info", "debug", "trace"}
report(allowed <= uvicorn_ok,
       f"all log_level values are valid for uvicorn ({sorted(allowed)})",
       f"uvicorn would reject: {sorted(allowed - uvicorn_ok)}")

print()
required = {"name", "version", "slug", "description", "arch"}
missing = required - set(cfg)
report(not missing, "all required add-on keys present",
       f"missing required keys: {sorted(missing)}")
report("image" not in cfg,
       "no `image:` key, so Supervisor builds locally from the Dockerfile",
       "`image:` is set - Supervisor will pull instead of building, "
       "and no such image is published")

# Supervisor normally injects BUILD_VERSION, but the Dockerfile default is what
# lands in the image label if anything builds it directly. Silent drift here
# produces an image that misreports its own version.
# The upstream image is pinned by digest in TWO places: the Dockerfile ARG
# default and build.yaml, which is what Supervisor actually passes. If they
# drift, a build silently uses a different upstream image from the one the
# patcher's hashes were computed against - and the failure would surface as a
# confusing hash mismatch rather than as the drift it is.
df_digest = re.search(r"ARG VOICEBOX_DIGEST=(sha256:[0-9a-f]{64})", dockerfile)
by_path = pathlib.Path("voicebox/build.yaml")
by_digest = None
if by_path.is_file():
    by = yaml.safe_load(by_path.read_text(encoding="utf-8")) or {}
    by_digest = (by.get("args") or {}).get("VOICEBOX_DIGEST")
report(bool(df_digest), "Dockerfile pins the upstream image by digest",
       "Dockerfile has no ARG VOICEBOX_DIGEST=sha256:... - `latest` is mutable "
       "and this add-on patches the image's backend")
report(bool(by_digest), "build.yaml pins the upstream image by digest",
       "build.yaml has no args.VOICEBOX_DIGEST")
report(bool(df_digest) and by_digest == df_digest.group(1),
       "Dockerfile and build.yaml pin the SAME digest",
       f"build.yaml {by_digest} != Dockerfile "
       f"{df_digest.group(1) if df_digest else 'missing'}")

bv = re.search(r"ARG BUILD_VERSION=([^\s]+)", dockerfile)
report(bool(bv) and bv.group(1) == str(cfg["version"]),
       f"Dockerfile BUILD_VERSION matches config.yaml ({cfg['version']})",
       f"Dockerfile BUILD_VERSION {bv.group(1) if bv else 'missing'} "
       f"!= config.yaml version {cfg['version']}")

# Supervisor can only substitute the LAN address into webui's [HOST], so the
# button it renders is a private-IP, plain-HTTP URL. Through any HTTPS front
# door (Cloudflare Tunnel here) that is unroutable AND mixed content, and the
# add-on looks broken while running fine. Ingress covers both cases.
report("webui" not in cfg,
       "no webui button (it would emit a LAN-only http:// URL)",
       f"config.yaml sets webui: {cfg.get('webui')!r} - that URL cannot work "
       "from a remote HTTPS session; rely on ingress instead")

report(cfg.get("ingress") is True and cfg.get("ingress_port") == 8000,
       "ingress enabled on port 8000 (the only advertised entry point)",
       "ingress must stay enabled - with no webui it is the sole entry point")

print()
# The published image has no /mcp endpoint - verified against the running
# container's own /openapi.json. Claiming otherwise sends users and agents at a
# route that 404s, so keep the claim out of anything user-facing.
mcp_ok = True
# DOCS.md and run.sh are as user-facing as the rest: one is the add-on's
# documentation tab, the other prints advice into the log when a start fails.
for rel in ("voicebox/config.yaml", "voicebox/translations/en.yaml",
            "README.md", "voicebox/README.md",
            "voicebox/DOCS.md", "voicebox/run.sh"):
    path = pathlib.Path(rel)
    if not path.is_file():
        continue
    lines = path.read_text(encoding="utf-8").splitlines()

    # A correct caveat often spans several lines - the mention lands on one and
    # the negation on the next. Judging line by line flagged our own accurate
    # warning, so qualify each mention against its whole paragraph.
    para_of = {}
    start = 0
    for i, line in enumerate(lines + [""]):
        if not line.strip():
            for j in range(start, i):
                para_of[j] = " ".join(lines[start:i])
            start = i + 1

    for lineno, line in enumerate(lines, 1):
        # Strip code formatting so `/mcp` and /mcp read the same.
        low = line.lower().replace("`", "").replace("*", "")
        if "mcp" not in low:
            continue
        # A caveat that says it is absent is exactly what we want to keep.
        context = para_of.get(lineno - 1, line).lower().replace("`", "").replace("*", "")
        if any(k in context for k in ("not available", "no /mcp", "does not", "no mcp",
                                      "absent", "postdate", "once upstream", "404",
                                      "none of", "not yet", "no longer", "there is no",
                                      "not implement", "not among")):
            continue
        report(False, "", f"{rel}:{lineno} still advertises MCP: {line.strip()}")
        mcp_ok = False
if mcp_ok:
    report(True, "no unqualified MCP claims in user-facing files", "")

# The frontend patch is what makes the UI usable at all; a missing file would
# only show up as a failed build.
patcher = pathlib.Path("voicebox/patch-frontend.py")
report(patcher.is_file(), "patch-frontend.py present",
       "patch-frontend.py is missing - the web UI will not work")
report("patch-frontend.py /app" in dockerfile,
       "Dockerfile runs patch-frontend.py",
       "Dockerfile never runs patch-frontend.py, so the UI stays broken")
report("qwen-tts" in dockerfile,
       "Dockerfile installs the missing qwen-tts engine",
       "Dockerfile does not install qwen-tts - TTS model downloads will fail")

# Two build steps now edit backend/main.py, and the order is load-bearing.
# enforce-model-policy.py pins a SHA-256 of pristine upstream main.py and
# refuses to apply once it has moved, so it has to see the file first - with
# patch-frontend.py ahead of it the image does not build at all. Getting this
# wrong once cost a full build cycle to diagnose; it is cheaper to assert it.
_policy_at = dockerfile.find("enforce-model-policy.py")
_frontend_at = dockerfile.find("patch-frontend.py")
report(_policy_at != -1 and _frontend_at != -1 and _policy_at < _frontend_at,
       "enforce-model-policy runs before patch-frontend (both edit main.py)",
       "patch-frontend.py runs before enforce-model-policy.py. The policy "
       "refuses to patch a main.py whose hash has moved, so the build will "
       "fail with 'main.py has changed upstream'.")

# patch-frontend.py re-seals the policy receipt after it edits main.py, and
# run.sh re-runs the policy verifier at EVERY boot. So a re-seal that goes
# wrong builds perfectly and then fails every single start. Requiring a second
# --verify after the frontend patch moves that failure into the build log,
# where it costs one build instead of a support round-trip.
_frontend_run = dockerfile.find("patch-frontend.py /app")
_last_verify = dockerfile.rfind("--verify")
report(_frontend_run != -1 and _last_verify > _frontend_run,
       "model policy is re-verified after the frontend patch",
       "Dockerfile does not re-verify the model policy after patch-frontend.py "
       "re-seals the receipt, so a corrupt re-seal would fail at every boot "
       "instead of failing the build.")

# An AST assertion proves the patch is WRITTEN correctly. It cannot prove the
# decorator RAN. This project has twice shipped a patch that was present and
# inert, and reported success both times; importing the patched app is the
# only step that tells those two apart.
_runtime_at = dockerfile.rfind('verify-runtime.py"]')
if _runtime_at == -1:
    report(False, "",
           "Dockerfile never runs verify-runtime.py, so a deep-link patch "
           "that is present but never registered would ship.")
else:
    report(_runtime_at > _frontend_run,
           "the patched backend is imported and checked at build time",
           "verify-runtime.py runs BEFORE patch-frontend.py, so it imports an "
           "unpatched app and passes without checking anything. Move it after "
           "the frontend patch.")
report(pathlib.Path("voicebox/verify-runtime.py").is_file(),
       "verify-runtime.py present",
       "voicebox/verify-runtime.py is missing - the Dockerfile COPY will fail")

# The two files name the cache middleware independently: patch-frontend.py
# EMITS it, verify-runtime.py LOOKS FOR it. Rename one and the build fails
# loudly, which is survivable - but it fails at image-build time, minutes in,
# with a message about a missing middleware rather than a typo. Catch it here
# in milliseconds instead.
_pf = pathlib.Path("voicebox/patch-frontend.py").read_text(encoding="utf-8")
_vr = pathlib.Path("voicebox/verify-runtime.py").read_text(encoding="utf-8")
_emitted = re.search(r'^CACHE_MARKER = "([^"]+)"', _pf, re.M)
_sought = re.search(r'^CACHE = "([^"]+)"', _vr, re.M)
if not _emitted or not _sought:
    report(False, "",
           "cannot find CACHE_MARKER in patch-frontend.py or CACHE in "
           "verify-runtime.py - the stale-frontend check has lost its name")
else:
    report(_emitted.group(1) == _sought.group(1),
           "the cache middleware has the same name in patcher and verifier",
           f"patch-frontend.py emits {_emitted.group(1)!r} but "
           f"verify-runtime.py looks for {_sought.group(1)!r}, so the build "
           "fails on a name mismatch rather than a real problem")
    report(f"async def {_emitted.group(1)}" in _pf,
           "...and the patcher really emits it as an async function",
           f"patch-frontend.py names {_emitted.group(1)!r} but never emits "
           "`async def` for it, so no Cache-Control header is ever set and a "
           "browser can keep serving a previous build's bundle")

sys.exit(rc)
