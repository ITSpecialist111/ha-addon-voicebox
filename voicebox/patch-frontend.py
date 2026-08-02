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

# The desktop default, shown in the UI's server settings field.
RE_DEFAULT_ORIGIN = re.compile(r'"http://127\.0\.0\.1:17493"')

SKIP_DIRS = {"node_modules", ".git", "site-packages", "__pycache__", "dist-info"}


class PatchError(RuntimeError):
    pass


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

    for js in sorted(assets.glob("*.js")):
        original = js.read_text(encoding="utf-8")
        text, n = RE_GET_BASE_URL.subn(GET_BASE_URL_PATCHED, original)
        text, n_origin = RE_DEFAULT_ORIGIN.subn('""', text)

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
        if RE_GET_BASE_URL.search(js.read_text(encoding="utf-8")):
            raise PatchError(f"{js.name} still contains an unpatched getBaseUrl()")


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
