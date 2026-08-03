#!/usr/bin/env python3
"""Make the Voicebox web UI usable when it is not being served to localhost.

The published Voicebox frontend is built for a desktop app, where the browser
and the API server are the same machine. Two consequences make the UI unusable
as a Home Assistant add-on. This script fixes both.

1. Every API call goes to a hardcoded absolute origin. From the shipped bundle:

       serverUrl: "http://127.0.0.1:17493"
       getBaseUrl(){return Mn.getState().serverUrl}
       async request(t,n){const r=`${this.getBaseUrl()}${t}`, ... fetch(r, ...)}

   Served from the add-on that resolves to *the machine running the browser*,
   so every request fails for anyone not sitting at the Home Assistant host.
   This breaks the UI over the direct port as well as under ingress.

2. index.html references its assets with absolute paths (/assets/...). Under
   ingress the page is served from /api/hassio_ingress/<token>/, so those
   resolve against the Home Assistant origin rather than the add-on, and the
   page renders blank.

Both are fixed by making the frontend relative to wherever the page actually is:

  * asset references become relative, so they resolve under the ingress prefix
  * getBaseUrl() returns window.__VB_BASE__, computed at load time from
    location.pathname -- the ingress prefix when running under ingress, and the
    empty string otherwise, which yields same-origin paths like "/health" and
    is correct for direct port access.

Deriving the prefix by regex from the whole pathname (rather than assuming the
page sits at the root) keeps this correct if the SPA client-side routes to a
sub-path and the user then reloads.

This runs at BUILD time, so a mismatch against a future upstream bundle fails
the add-on build loudly instead of silently serving a blank page.

Usage:
    patch-frontend.py [ROOT] [--check]

    ROOT      directory to search for the built frontend (default: /app)
    --check   verify an existing patch instead of applying one; exits non-zero
              if ROOT is not patched
"""

from __future__ import annotations

import pathlib
import re
import sys

MARKER = "__VB_BASE__"

# Injected into <head> ahead of the module script, so it runs before the bundle.
BASE_SNIPPET = (
    "<script>window.__VB_BASE__=(function(){"
    "var m=String(location.pathname)"
    ".match(/^(.*?\\/api\\/hassio_ingress\\/[^\\/]+)/);"
    'return m?m[1]:"";})();</script>'
)

# getBaseUrl(){return <minified-store>.getState().serverUrl}
# The store identifier is minified and will change if upstream rebuilds, so
# match its shape rather than its name.
RE_GET_BASE_URL = re.compile(
    r"getBaseUrl\(\)\s*\{\s*return\s+[A-Za-z_$][A-Za-z0-9_$]*"
    r"\.getState\(\)\.serverUrl\s*\}"
)
GET_BASE_URL_PATCHED = 'getBaseUrl(){return window.__VB_BASE__||""}'

# Absolute src=/href= paths, excluding protocol-relative //host ones.
RE_ABS_ASSET = re.compile(r'(\b(?:src|href)=")/(?!/)')

# TanStack Router is constructed with only a routeTree, so its basepath defaults
# to "/". Under ingress the app lives at /api/hassio_ingress/<token>/, nothing
# matches, and every route renders the router's bare <p>Not Found</p> - while
# the surrounding shell still paints, which makes it look half-working.
#
# Matched on the option key rather than the minified factory name, and written
# to tolerate additional options after routeTree, so an upstream build that adds
# one does not silently skip the patch.
RE_ROUTER = re.compile(r"\{routeTree:([A-Za-z_$][A-Za-z0-9_$]*)")
ROUTER_PATCHED = '{routeTree:\\g<1>,basepath:window.__VB_BASE__||"/"'
ROUTER_MARKER = ',basepath:window.__VB_BASE__||"/"'

# Vite inlines imported images as root-absolute URLs. Under ingress "/assets/x"
# resolves against the Home Assistant origin, not the add-on, so the logo 404s.
#
# The lookbehind skips only OUR OWN wrapper, so a second run is a no-op while a
# genuine `prefix+"/assets/x"` elsewhere is still caught. Matching any preceding
# "+" would silently leave such a site unpatched - and verify() uses this same
# expression, so the miss would go unreported.
RE_ABS_ASSET_JS = re.compile(
    r'(?<!\(window\.__VB_BASE__\|\|""\)\+)"/assets/([^"]+)"'
)
ABS_ASSET_JS_PATCHED = '(window.__VB_BASE__||"")+"/assets/\\g<1>"'
ABS_ASSET_JS_MARKER = '(window.__VB_BASE__||"")+"/assets/'

# The model-download progress stream is the one place that does NOT go through
# getBaseUrl() - it builds an EventSource URL straight from the store's
# serverUrl. Left alone that resolves against Home Assistant under ingress and
# 404s, so downloads appear to hang with no progress at all.
RE_SSE_BASE = re.compile(r"\$\{[A-Za-z_$][A-Za-z0-9_$]*\}(/models/progress/)")
SSE_BASE_PATCHED = '${window.__VB_BASE__||""}\\g<1>'
SSE_BASE_MARKER = '${window.__VB_BASE__||""}/models/progress/'

# The desktop default, shown in the UI's server settings field. Both SSE call
# sites bail out early when serverUrl is falsy, so blanking it to "" would keep
# the progress stream switched off even with the URL corrected. location.origin
# is the honest answer for direct access and is truthy, while under ingress the
# base is the ingress prefix.
RE_DEFAULT_ORIGIN = re.compile(r'"http://127\.0\.0\.1:17493"')
DEFAULT_ORIGIN_PATCHED = '(window.__VB_BASE__||location.origin)'

SKIP_DIRS = {"node_modules", ".git", "site-packages", "__pycache__", "dist-info"}


class PatchError(RuntimeError):
    pass


def unpatched_router_sites(text: str) -> int:
    """Count routeTree options that are NOT followed by our basepath.

    The patch inserts after the routeTree identifier, so RE_ROUTER still
    matches afterwards - "the regex no longer matches" would be the wrong
    test here, and would report success on an untouched bundle.
    """
    return sum(
        1
        for m in RE_ROUTER.finditer(text)
        if not text[m.end():].startswith(ROUTER_MARKER)
    )


def add_basepath(m: "re.Match[str]") -> str:
    """Insert the ingress basepath, unless this site already has one.

    Used as a re.sub replacement so each site is judged on its own; a
    file-wide substitution would double the key on a mixed bundle.
    """
    rest = m.string[m.end():]
    if rest.startswith(ROUTER_MARKER):
        return m.group(0)

    # An existing basepath LATER in the same options object would win, because
    # a duplicate key takes its last value - so the patch would appear to apply
    # and verify cleanly while ingress stayed broken. Refuse instead.
    depth, i = 1, 0
    while i < len(rest) and depth:
        if rest[i] == "{":
            depth += 1
        elif rest[i] == "}":
            depth -= 1
        i += 1
    if re.search(r"[,{]\s*basepath\s*:", rest[:i]):
        raise PatchError(
            "the router already sets its own basepath: "
            f"{{routeTree:{m.group(1)}...{rest[:i][:80]}. Adding ours would be "
            "overridden by it, so ingress would stay broken while the build "
            "reported success. Update patch-frontend.py to replace that value."
        )
    return m.group(0) + ROUTER_MARKER


def find_frontend(root: pathlib.Path) -> pathlib.Path:
    """Locate the built SPA: a directory holding index.html beside assets/*.js.

    Located rather than hardcoded, so the script does not silently no-op if
    upstream moves the frontend.
    """
    matches = []
    for index in root.rglob("index.html"):
        if SKIP_DIRS & set(index.parts):
            continue
        assets = index.parent / "assets"
        if assets.is_dir() and any(assets.glob("*.js")):
            matches.append(index.parent)

    if not matches:
        raise PatchError(
            f"no built frontend under {root} "
            "(looked for an index.html next to assets/*.js)"
        )
    if len(matches) > 1:
        # Ambiguity means the assumption is wrong; refuse rather than guess.
        listed = ", ".join(str(m) for m in sorted(matches))
        raise PatchError(f"expected exactly one frontend, found {len(matches)}: {listed}")
    return matches[0]


def patch_index(index: pathlib.Path) -> list[str]:
    original = index.read_text(encoding="utf-8")
    text = original

    text, n_assets = RE_ABS_ASSET.subn(r"\1", text)
    if n_assets == 0 and MARKER not in original:
        raise PatchError(
            f"{index}: found no absolute src=/href= asset paths to make relative"
        )

    notes = [f"{index.name}: made {n_assets} asset reference(s) relative"]

    if MARKER in text:
        notes.append(f"{index.name}: base-path snippet already present")
    elif "<head>" in text:
        text = text.replace("<head>", "<head>\n    " + BASE_SNIPPET, 1)
        notes.append(f"{index.name}: injected base-path snippet")
    else:
        raise PatchError(f"{index}: no <head> to inject the base-path snippet into")

    if text != original:
        index.write_text(text, encoding="utf-8")
    return notes


def patch_bundles(assets: pathlib.Path) -> list[str]:
    notes = []
    total = 0

    routers = 0
    router_sites = 0
    js_assets = 0
    sse = 0

    for js in sorted(assets.glob("*.js")):
        original = js.read_text(encoding="utf-8")
        text, n = RE_GET_BASE_URL.subn(GET_BASE_URL_PATCHED, original)
        text, n_origin = RE_DEFAULT_ORIGIN.subn(DEFAULT_ORIGIN_PATCHED, text)
        text, n_sse = RE_SSE_BASE.subn(SSE_BASE_PATCHED, text)
        if n_sse:
            sse += n_sse
            notes.append(f"{js.name}: pointed {n_sse} progress stream(s) at the ingress base")

        # Decide per site, not per file. A blanket subn() would re-patch an
        # already-patched router in a file that also contains an unpatched
        # one, emitting the basepath key twice.
        site_count = len(RE_ROUTER.findall(text))
        if site_count:
            router_sites += site_count
            text, n_router = RE_ROUTER.subn(add_basepath, text)
            if n_router:
                routers += n_router
                notes.append(f"{js.name}: gave {n_router} router(s) an ingress basepath")

        text, n_asset = RE_ABS_ASSET_JS.subn(ABS_ASSET_JS_PATCHED, text)
        if n_asset:
            js_assets += n_asset
            notes.append(f"{js.name}: rebased {n_asset} absolute /assets/ URL(s)")

        if n:
            total += n
            notes.append(f"{js.name}: rewrote {n} getBaseUrl() definition(s)")
        if n_origin:
            notes.append(f"{js.name}: neutralised {n_origin} hardcoded 127.0.0.1 origin(s)")
        if text != original:
            js.write_text(text, encoding="utf-8")

    if total == 0:
        already = any(
            GET_BASE_URL_PATCHED in js.read_text(encoding="utf-8")
            for js in assets.glob("*.js")
        )
        if already:
            notes.append("getBaseUrl() already patched")
        else:
            raise PatchError(
                f"no getBaseUrl() definition found in {assets}. The upstream bundle "
                "has changed shape; re-check how the frontend builds its API base "
                "URL before shipping, or the UI will silently call the browser's "
                "own localhost."
            )
    # Fail closed. A router left at basepath "/" renders Not Found on every
    # route under ingress, which is exactly the failure this patch exists to
    # prevent - so a bundle whose shape no longer matches must stop the build,
    # not ship a half-working UI.
    if router_sites == 0:
        already = any(
            ROUTER_MARKER in js.read_text(encoding="utf-8")
            for js in assets.glob("*.js")
        )
        if not already:
            raise PatchError(
                f"no router construction found in {assets}. The upstream bundle "
                "has changed shape; re-check how the frontend creates its router "
                "before shipping, or every route will render Not Found when "
                "opened through Home Assistant ingress."
            )
    if routers == 0 and router_sites:
        notes.append("router basepath already patched")
    if js_assets == 0:
        if any(
            ABS_ASSET_JS_MARKER in js.read_text(encoding="utf-8")
            for js in assets.glob("*.js")
        ):
            notes.append("absolute /assets/ URLs already rebased")

    return notes


def verify(frontend: pathlib.Path) -> None:
    index = (frontend / "index.html").read_text(encoding="utf-8")
    if MARKER not in index:
        raise PatchError("index.html is missing the base-path snippet")
    if RE_ABS_ASSET.search(index):
        raise PatchError("index.html still contains absolute asset paths")

    assets = frontend / "assets"
    if not any(
        GET_BASE_URL_PATCHED in js.read_text(encoding="utf-8")
        for js in assets.glob("*.js")
    ):
        raise PatchError("no bundle contains the patched getBaseUrl()")
    for js in assets.glob("*.js"):
        text = js.read_text(encoding="utf-8")
        if RE_GET_BASE_URL.search(text):
            raise PatchError(f"{js.name} still contains an unpatched getBaseUrl()")
        if unpatched_router_sites(text):
            raise PatchError(
                f"{js.name} constructs a router without an ingress basepath - "
                "every route would render Not Found under ingress"
            )
        if RE_ABS_ASSET_JS.search(text):
            raise PatchError(
                f"{js.name} still contains an absolute /assets/ URL, which "
                "resolves against Home Assistant rather than the add-on"
            )
        if RE_SSE_BASE.search(text):
            raise PatchError(
                f"{js.name} builds a /models/progress/ stream URL from the "
                "stored server URL, which 404s under ingress - downloads would "
                "show no progress at all"
            )

    if not any(
        ROUTER_MARKER in js.read_text(encoding="utf-8")
        for js in assets.glob("*.js")
    ):
        raise PatchError("no bundle contains a router with an ingress basepath")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    check_only = "--check" in argv[1:]
    root = pathlib.Path(args[0] if args else "/app")

    if not root.is_dir():
        print(f"patch-frontend: {root} is not a directory", file=sys.stderr)
        return 1

    try:
        frontend = find_frontend(root)
        if check_only:
            verify(frontend)
            print(f"patch-frontend: {frontend} is patched")
            return 0

        notes = patch_index(frontend / "index.html")
        notes += patch_bundles(frontend / "assets")
        verify(frontend)
    except PatchError as exc:
        print(f"patch-frontend: FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"patch-frontend: {frontend}")
    for note in notes:
        print(f"patch-frontend:   {note}")
    print("patch-frontend: verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
