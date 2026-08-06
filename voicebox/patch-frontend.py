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

import ast
import hashlib
import json
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

# The live microphone waveform shown behind the "Start Recording" button. It
# calls getUserMedia with no guard at all, inside a useEffect:
#
#   let m=null;return navigator.mediaDevices.getUserMedia({audio:!0,video:!1})
#     .then(...).catch(v=>{console.warn("Could not access microphone ...",v)})
#
# The .catch() only handles a REJECTED promise. navigator.mediaDevices is
# [SecureContext] in the spec, so over plain http:// it is not merely falsy - the
# property does not exist - and the call throws a synchronous TypeError before
# any promise is created. Nothing catches it, it escapes the effect, and React
# unmounts the tree: the whole panel becomes "Something went wrong! Cannot read
# properties of undefined (reading 'getUserMedia')" the instant the dialog opens,
# before the user has clicked anything.
#
# Home Assistant is commonly reached over http:// on a LAN address, and a private
# IP is NOT a secure context (that proposal was never shipped), so this is the
# normal case here rather than an edge case.
#
# The record button itself already handles this properly - it checks
# mediaDevices, waits, re-checks, and shows "Microphone access is not available.
# ... ensure you are using a secure context (HTTPS or localhost)". So guarding
# the waveform is enough to turn a dead panel into a working one that explains
# itself when you press record.
RE_MIC_PREVIEW = re.compile(
    r"let ([A-Za-z_$][A-Za-z0-9_$]*)=null;return navigator\.mediaDevices"
    r"\.getUserMedia\(\{audio:!0,video:!1\}\)"
)
MIC_PREVIEW_PATCHED = (
    r'let \g<1>=null;if(!navigator.mediaDevices||!navigator.mediaDevices'
    r'.getUserMedia){console.warn("Voicebox: no microphone API - this page is '
    r'not a secure context (needs https:// or localhost). Waveform preview '
    r'disabled.");return}return navigator.mediaDevices'
    r".getUserMedia({audio:!0,video:!1})"
)

# The POSITIVE half of the check. RE_MIC_PREVIEW going quiet only proves that
# SOMETHING was inserted before the call - not that what was inserted can ever
# fire. A review caught exactly that: mutating the emitted guard to
# `if(false&&...)` left the marker in place, the count correct, verify() passing
# and the crash fully reintroduced. That is the third time this project has been
# one assertion away from shipping a patch that reports success while doing
# nothing, so the guard is now asserted by shape, not by presence.
#
# Deliberately spelled with regex escaping (\(, \., \|) so that it shares no
# literal text with MIC_PREVIEW_PATCHED above. A mutation to one therefore
# cannot silently mutate the other - which is the entire point of a cross-check.
RE_MIC_GUARDED = re.compile(
    r"let ([A-Za-z_$][A-Za-z0-9_$]*)=null;"
    r"if\(!navigator\.mediaDevices\|\|!navigator\.mediaDevices\.getUserMedia\)\{"
    # ...and it must BAIL OUT UNCONDITIONALLY.
    #
    # This part has been wrong twice, and both versions accepted a guard that
    # still crashed:
    #
    #   [^{}]*\}         accepted a body that only warns and falls straight
    #                    through to the very call it was meant to prevent.
    #   [^{}]*return\}   accepted `...;if(0)return}` - a return that never runs -
    #                    and, because it only looked for the WORD, also accepted
    #                    the word "return" sitting inside the warning string.
    #
    # Both were confirmed by execution to reintroduce the exact reported error,
    # so the body is now matched by shape: a single console.warn call, then a
    # bare return, and nothing else. Anything more elaborate is not something
    # this patcher emits, and refusing it costs only a deliberate edit here.
    r"console\.warn\((?:[^()]|\([^()]*\))*\);return\}"
    r"return navigator\.mediaDevices\.getUserMedia"
)
MIC_PREVIEW_MARKER = "Waveform preview disabled."

# Client-side routes. Navigating to one never touches the server - TanStack
# pushes history - but RELOADING one does, and then the app disappears: /models
# has no API route and 404s, while /stories has one and answers raw JSON.
#
# StaticFiles(html=True) cannot fix this. Given a directory it answers 307 to the
# path plus "/", and Starlette rebuilds that Location from the proxied request,
# WITHOUT the /api/hassio_ingress/<token> prefix - so the browser would be sent
# out of the add-on entirely. Measured against Starlette, not assumed.
#
# Read from the route table rather than the navigation sidebar: the sidebar is a
# presentation list and could omit a route that still exists.
RE_SPA_ROUTE = re.compile(
    r'getParentRoute:\(\)=>[A-Za-z_$][A-Za-z0-9_$]*,\s*path:"(/[^"]*)"'
)

# The single statement that serves the built SPA. Anchored on the whole call so
# that an upstream change to how it is mounted fails loudly rather than leaving
# the fallback silently unattached.
RE_SPA_MOUNT = re.compile(
    r'^([ \t]*)app\.mount\(\s*"/",\s*StaticFiles\(\s*directory=str\('
    r'_web_dist_path\s*\),\s*html=True\s*\),\s*name="web"\s*\)[ \t]*$',
    re.MULTILINE,
)
SPA_FALLBACK_MARKER = "_vb_spa_deep_links"
CACHE_MARKER = "_vb_no_stale_frontend"

# enforce-model-policy.py records a hash of every file it patched, and run.sh
# re-checks those hashes on EVERY start - that is what makes a tampered image
# fail loudly instead of quietly running without the 1.7B guard. Editing main.py
# afterwards therefore has to re-seal the hash, or the add-on would report a
# policy failure at every boot.
POLICY_RECEIPT = ".voicebox-model-policy.json"

# Inserted directly after the mount, inside the same `if` block, so it is added
# only when there is a built frontend to serve.
SPA_FALLBACK = '''
{i}# --- Home Assistant ingress deep links (added by patch-frontend.py) ---
{i}# Reloading a client-side route must return the app, not a 404 and not raw
{i}# API JSON. Middleware runs BEFORE routing, so this also covers routes that
{i}# collide with a real endpoint - a catch-all route could never reach those,
{i}# because the endpoint matches first.
{i}#
{i}# The route list is extracted from the built bundle at image-build time, so a
{i}# route added upstream is picked up instead of quietly missing its fallback.
{i}# The Accept test leaves API clients untouched: the web UI sets no Accept
{i}# header at all, so only a real browser navigation can match here.
{i}_VB_SPA_ROUTES = frozenset({routes!r})
{i}_VB_SPA_INDEX = _web_dist_path / "index.html"

{i}@app.middleware("http")
{i}async def _vb_spa_deep_links(request, call_next):
{i}    if (
{i}        request.method in ("GET", "HEAD")
{i}        and request.url.path.rstrip("/") in _VB_SPA_ROUTES
{i}        and "text/html" in request.headers.get("accept", "")
{i}        and _VB_SPA_INDEX.is_file()
{i}    ):
{i}        return FileResponse(_VB_SPA_INDEX, media_type="text/html")
{i}    return await call_next(request)

{i}# --- Home Assistant ingress cache control (added by patch-frontend.py) ---
{i}# The static file server sends no Cache-Control at all - only ETag and
{i}# Last-Modified - so a browser applies HEURISTIC freshness and may reuse a
{i}# previously stored bundle WITHOUT revalidating it. The bundle filename comes
{i}# from the upstream build and does not change when this add-on is updated, so
{i}# the stale copy is served under the very same URL as the new one: an updated
{i}# add-on then renders the PREVIOUS frontend, and if that copy predates the
{i}# ingress patches it renders nothing at all.
{i}#
{i}# HTTP caches are keyed by origin, so this appears as "works on the LAN
{i}# address but blank through the external hostname" - the two origins simply
{i}# hold different cached copies. No amount of server-side testing can see it,
{i}# because the browser never sends the request.
{i}#
{i}# no-cache does NOT mean do-not-store: the file is still cached, the browser
{i}# just has to revalidate it, and the ETag above answers that with a 304 and no
{i}# body. The cost is one conditional request per asset per load.
{i}_VB_NO_CACHE_PATHS = frozenset(("/", "/index.html"))

{i}@app.middleware("http")
{i}async def _vb_no_stale_frontend(request, call_next):
{i}    response = await call_next(request)
{i}    _vb_path = request.url.path
{i}    if (
{i}        _vb_path in _VB_NO_CACHE_PATHS
{i}        or _vb_path.startswith("/assets/")
{i}        or _vb_path.rstrip("/") in _VB_SPA_ROUTES
{i}    ):
{i}        response.headers["Cache-Control"] = "no-cache"
{i}    return response
'''

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


def spa_routes(assets: pathlib.Path) -> tuple[str, ...]:
    """Every client-side route that a browser could be reloaded on."""
    found: set[str] = set()
    for js in sorted(assets.glob("*.js")):
        text = js.read_text(encoding="utf-8").replace("\r\n", "\n")
        for raw in RE_SPA_ROUTE.findall(text):
            route = "/" + "/".join(seg for seg in raw.split("/") if seg)
            if route == "/":
                continue  # the index route is already served by the mount itself
            if "$" in route or "*" in route:
                # A parameterised route cannot be listed ahead of time, and
                # skipping it silently would ship a reload that still 404s.
                # The upstream image is pinned by digest, so this can only
                # appear when we deliberately bump it - exactly when a loud
                # failure is cheap and a silent gap is not.
                raise PatchError(
                    f"the bundle declares a parameterised route ({route!r}) "
                    "which the deep-link fallback cannot enumerate. Reloading "
                    "it would still lose the app. Teach spa_routes() to emit a "
                    "prefix match before shipping this image."
                )
            found.add(route)
    return tuple(sorted(found))


def assert_live_middleware(source: str, routes: tuple[str, ...]) -> None:
    """Assert the fallback is an ACTIVE middleware, not merely present.

    This project has shipped two patches that were present and inert: the
    0.4.0 backend patch that targeted files which did not exist, and the 0.7.0
    model guard that accepted `if False: _vb_enforce_model_size(...)`. Both
    reported success. A substring test for the marker repeats that mistake -
    it passes on a comment, on a removed decorator, and on `and False`.

    So the emitted construct is asserted through the AST instead: a real async
    function, really decorated with app.middleware("http"), whose guard is not
    a constant and which really returns a FileResponse.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise PatchError(f"main.py does not parse: {exc}") from exc

    fn = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == SPA_FALLBACK_MARKER):
            fn = node
            break
    if fn is None:
        raise PatchError(
            "main.py has no deep-link fallback: no async "
            f"{SPA_FALLBACK_MARKER} function is defined, so reloading any "
            "route would lose the app. A marker in a comment or a string does "
            "not count - this is checked through the AST precisely because a "
            "substring test passes on a patch that never took."
        )

    decorated = any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and d.func.attr == "middleware"
        and isinstance(d.func.value, ast.Name)
        and d.func.value.id == "app"
        and [a for a in d.args
             if isinstance(a, ast.Constant) and a.value == "http"]
        for d in fn.decorator_list
    )
    if not decorated:
        raise PatchError(
            f"{SPA_FALLBACK_MARKER} is defined but not registered with "
            '@app.middleware("http"), so it never runs and every reload still '
            "returns a 404"
        )

    guard = next((n for n in fn.body if isinstance(n, ast.If)), None)
    if guard is None:
        raise PatchError(f"{SPA_FALLBACK_MARKER} has no guard - it would "
                         "intercept every request, not just SPA reloads")
    if isinstance(guard.test, ast.Constant):
        raise PatchError(
            f"{SPA_FALLBACK_MARKER}'s guard is the constant "
            f"{guard.test.value!r}, so it is inert (or intercepts everything)"
        )

    # `False and <the real test>` is a BoolOp, not a Constant, so the check
    # above sails past it - and that is precisely the shape that got the 0.7.0
    # model guard shipped inert. Any constant operand of an and/or inside the
    # guard is either dead weight or a short circuit; neither belongs here.
    for node in ast.walk(guard.test):
        if isinstance(node, ast.BoolOp):
            for operand in node.values:
                if isinstance(operand, ast.Constant):
                    raise PatchError(
                        f"{SPA_FALLBACK_MARKER}'s guard short-circuits on the "
                        f"constant {operand.value!r}, so it never matches a "
                        "real reload"
                    )

    # And the guard must actually be about SPA routes. A guard that is neither
    # constant nor short-circuited can still have been replaced by something
    # unrelated that happens to parse.
    if not any(
        isinstance(node, ast.Name) and node.id == "_VB_SPA_ROUTES"
        for node in ast.walk(guard.test)
    ):
        raise PatchError(
            f"{SPA_FALLBACK_MARKER}'s guard does not consult _VB_SPA_ROUTES, "
            "so it is not deciding what it is supposed to decide"
        )

    served = any(
        isinstance(n, ast.Return)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "FileResponse"
        for n in ast.walk(guard)
    )
    if not served:
        raise PatchError(
            f"{SPA_FALLBACK_MARKER} matches a reload but never returns a "
            "FileResponse, so the app is still not served"
        )

    listed = {
        elt.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_VB_SPA_ROUTES"
                for t in node.targets)
        for call in ([node.value] if isinstance(node.value, ast.Call) else [])
        for arg in call.args
        for elt in getattr(arg, "elts", [])
        if isinstance(elt, ast.Constant)
    }
    missing = sorted(set(routes) - listed)
    if missing:
        raise PatchError(
            "the deep-link fallback's route set does not cover "
            + ", ".join(missing)
            + " - reloading those would still lose the app"
        )

    assert_live_cache_middleware(tree)


def assert_live_cache_middleware(tree: "ast.Module") -> None:
    """Assert the no-stale-frontend middleware is registered and does its job.

    Held to the same standard as the deep-link fallback, and for the same
    reason: a patch that is present but inert reports success and ships a bug.
    Here the bug is invisible from the server side - the browser simply never
    asks - so nothing downstream would catch it.
    """
    fn = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == CACHE_MARKER):
            fn = node
            break
    if fn is None:
        raise PatchError(
            f"main.py has no async {CACHE_MARKER}, so index.html would be "
            "served with no Cache-Control and a browser could keep reusing a "
            "previous build's bundle"
        )

    if not any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and d.func.attr == "middleware"
        and isinstance(d.func.value, ast.Name)
        and d.func.value.id == "app"
        and [a for a in d.args
             if isinstance(a, ast.Constant) and a.value == "http"]
        for d in fn.decorator_list
    ):
        raise PatchError(
            f"{CACHE_MARKER} is defined but not registered with "
            '@app.middleware("http"), so no response ever gets a Cache-Control '
            "header"
        )

    guard = next((n for n in fn.body if isinstance(n, ast.If)), None)
    if guard is None:
        raise PatchError(
            f"{CACHE_MARKER} has no guard, so it would mark every response "
            "no-cache, including the generated audio it is not about"
        )
    if isinstance(guard.test, ast.Constant):
        raise PatchError(
            f"{CACHE_MARKER}'s guard is the constant {guard.test.value!r}, "
            "so it is inert (or applies to everything)"
        )
    for node in ast.walk(guard.test):
        if isinstance(node, ast.BoolOp):
            for operand in node.values:
                if isinstance(operand, ast.Constant):
                    raise PatchError(
                        f"{CACHE_MARKER}'s guard short-circuits on the "
                        f"constant {operand.value!r}, so the header is never "
                        "set where it matters"
                    )

    # Present, decorated and guarded is still not the same as effective: the
    # body has to actually write the header. Assert the assignment itself.
    set_header = [
        n for n in ast.walk(guard)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Subscript)
            and isinstance(t.slice, ast.Constant)
            and isinstance(t.slice.value, str)
            and t.slice.value.lower() == "cache-control"
            for t in n.targets
        )
    ]
    if not set_header:
        raise PatchError(
            f"{CACHE_MARKER} matches, but never assigns a Cache-Control "
            "header, so a stale bundle can still be reused without "
            "revalidating"
        )
    if not any(
        isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
        and "no-cache" in n.value.value
        for n in set_header
    ):
        raise PatchError(
            f"{CACHE_MARKER} sets Cache-Control to something that does not "
            "include no-cache, so the browser is still free to reuse a stored "
            "bundle without revalidating it"
        )


def read_py(path: pathlib.Path) -> str:
    """Read a Python source file WITHOUT translating line endings.

    enforce-model-policy.py hashes main.py through an identical newline=""
    read. If this module read through Python's default translation, then on a
    CRLF checkout the two would hash different strings and the re-sealed
    receipt would be wrong - which run.sh reports as a model-policy failure on
    every single start. Same reason, same handling, deliberately.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_py(path: pathlib.Path, text: str) -> None:
    """Write Python source verbatim - see read_py."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def reseal_policy_receipt(root: pathlib.Path, main_py: pathlib.Path,
                          was: str | None = None) -> str | None:
    """Update the recorded hash of main.py after the fallback was inserted.

    Only the hash moves. Everything that gives the receipt its value is left
    alone and still runs at boot: the policy block is exec'd out of
    pytorch_backend.py and main.py's AST is re-asserted, so a guard that had
    been removed or neutered would still be caught.

    Hashed the same way enforce-model-policy.py hashes it - decoded text read
    with newline="", not raw bytes and not translated - so the two agree on a
    checkout with CRLF line endings.
    """
    receipt = root / POLICY_RECEIPT
    if not receipt.is_file():
        return None  # policy not applied yet; the Dockerfile orders it first

    data = json.loads(receipt.read_text(encoding="utf-8"))
    after = data.get("after")
    if not isinstance(after, dict) or "main.py" not in after:
        raise PatchError(
            f"{receipt} records no hash for main.py, so it cannot be re-sealed "
            "- the model policy would fail to verify on every start"
        )

    # Re-seal ONLY over an edit this script made. If main.py did not match the
    # receipt before the fallback went in, something else changed it between
    # the policy step and here - and re-sealing would silently bless that too,
    # turning a tripwire into a rubber stamp.
    if was is not None and after["main.py"] not in (was, None):
        raise PatchError(
            "main.py did not match the model-policy receipt BEFORE the "
            "deep-link fallback was added, so something else modified it "
            f"(receipt {after['main.py'][:12]}..., actual {was[:12]}...). "
            "Refusing to re-seal: that would hide the change instead of "
            "reporting it."
        )

    digest = hashlib.sha256(read_py(main_py).encode("utf-8")).hexdigest()
    if after["main.py"] == digest:
        return None
    after["main.py"] = digest
    receipt.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return "model-policy receipt re-sealed over the deep-link fallback"


def patch_backend(root: pathlib.Path, routes: tuple[str, ...]) -> list[str]:
    main_py = root / "backend" / "main.py"
    if not main_py.is_file():
        raise PatchError(
            f"{main_py} does not exist, so reloading a route cannot be made to "
            "return the app"
        )

    # Work in LF and restore whatever the file actually used. The container's
    # copy is LF, but a CRLF checkout would otherwise defeat RE_SPA_MOUNT - its
    # [ \t]*$ cannot match a trailing \r - and the fallback would refuse to
    # attach for a reason that has nothing to do with the upstream code.
    raw = read_py(main_py)
    was = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    newline = "\r\n" if "\r\n" in raw else "\n"
    original = raw.replace("\r\n", "\n")
    if SPA_FALLBACK_MARKER in original:
        notes = ["main.py: deep-link fallback already present"]
        # The receipt may have been written by a later step on a previous run.
        resealed = reseal_policy_receipt(root, main_py)
        if resealed:
            notes.append(resealed)
        return notes

    if not routes:
        raise PatchError(
            "found no client-side routes in the bundle, so the deep-link "
            "fallback would be empty and every reload would still fail"
        )

    match = RE_SPA_MOUNT.search(original)
    if not match:
        raise PatchError(
            "main.py: could not find the StaticFiles mount that serves the web "
            "UI - the deep-link fallback has nothing to attach to"
        )

    block = SPA_FALLBACK.format(i=match.group(1), routes=routes)
    text = original[: match.end()] + "\n" + block + original[match.end():]

    try:
        compile(text, str(main_py), "exec")
    except SyntaxError as exc:
        raise PatchError(f"main.py: patch produced invalid Python: {exc}") from exc
    assert_live_middleware(text, routes)

    write_py(main_py, text.replace("\n", newline)
             if newline != "\n" else text)

    notes = ["main.py: reloading " + ", ".join(routes) + " now returns the app"]
    resealed = reseal_policy_receipt(root, main_py, was)
    if resealed:
        notes.append(resealed)
    return notes


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


# --- cache-busting fingerprints ---------------------------------------------
#
# Upstream's Vite build names the entry bundle assets/index-<hash>.js, where
# <hash> is derived from UPSTREAM's sources. This add-on rewrites the bytes of
# that file on every single build, but the name never changes. A URL whose
# content changes while its name does not is the classic cache-poisoning shape,
# and it has produced a blank ingress panel twice now.
#
# It CANNOT be fixed with response headers, because we do not control every hop.
# Measured through a Cloudflare tunnel, the browser is told
#
#     cache-control: max-age=14400          (4 hours)
#
# for assets/*.js even though _vb_no_stale_frontend below sets no-cache on
# exactly that path: the CDN caches static extensions and applies its own
# browser-cache TTL on top. index.html is not cached (no extension, DYNAMIC),
# so the page itself is always fresh - and a fresh index.html that points at an
# unchanged asset URL still loads the STALE bundle out of the browser's cache.
# That asymmetry is the whole bug.
#
# Caches are also keyed by origin, which is why this shows up as "fine on the
# LAN address, blank through the external hostname": the two origins simply
# hold different copies. No server-side test can observe it, because the
# browser never sends the request.
#
# So change the one thing we do control: the URL. A URL that has never been
# requested cannot be in anybody's cache - not the browser's, not a service
# worker's, not the CDN's edge, not a corporate proxy's. Deriving the name from
# a digest of the PATCHED bytes makes every release self-busting, and makes a
# release that changed nothing cost nothing.
RE_INDEX_ASSET = re.compile(r'(?:src|href)="assets/([^"]+)"')
RE_FINGERPRINT = re.compile(r"\.vb[0-9a-f]{8}(?=\.[^.]+\Z)")

# Used ONLY by verify(), and deliberately not RE_INDEX_ASSET.
#
# RE_INDEX_ASSET names the attributes it will look at. fingerprint_assets()
# uses it to decide what to rename, so narrowing that one alternation - src|href
# down to href, say - hides the entry bundle from the rename step AND from the
# check that is supposed to catch the rename step failing. One token, both sides
# blind, exit code 0, and the stale-cache bug straight back in production. Two
# spellings of the digest are not enough if both sides agree on the same wrong
# work list.
#
# So this one keys off the assets/ path itself and ignores the attribute
# entirely. It sees strictly more than the rename step can, which is the
# property that matters: anything the page might load, this will notice.
RE_ANY_ASSET_REF = re.compile(r"assets/([A-Za-z0-9][A-Za-z0-9._-]*\.[A-Za-z0-9]+)")
FINGERPRINT_LEN = 8


def asset_fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:FINGERPRINT_LEN]


def fingerprinted_name(name: str, digest: str) -> str:
    """index-DPaWep75.js -> index-DPaWep75.vb1a2b3c4d.js

    Stripping any fingerprint already present first is what makes this
    idempotent: the digest covers the file's bytes, not its name, so a second
    run over unchanged content reproduces the same name rather than stacking a
    second suffix onto it.
    """
    base = RE_FINGERPRINT.sub("", name)
    stem, dot, suffix = base.rpartition(".")
    if not dot:
        return f"{base}.vb{digest}"
    return f"{stem}.vb{digest}{dot}{suffix}"


def fingerprint_assets(frontend: pathlib.Path) -> list[str]:
    index_path = frontend / "index.html"
    html = index_path.read_text(encoding="utf-8")
    assets = frontend / "assets"

    # dict.fromkeys de-duplicates while keeping order. index.html is entitled to
    # name the same asset twice - a <link rel="modulepreload"> beside the
    # <script> is the usual case - and acting on it twice would send the second
    # pass looking for a file the first pass had already renamed away.
    referenced = list(dict.fromkeys(RE_INDEX_ASSET.findall(html)))
    if not referenced:
        raise PatchError(
            "index.html references no assets/ files, so there is nothing to "
            "fingerprint. Refusing rather than silently skipping the only "
            "defence against a stale cached bundle"
        )

    # PLAN EVERYTHING, THEN COMMIT. Every check that can fail runs, and the new
    # index.html is built in memory, before a single file is renamed. A refusal
    # therefore leaves the tree byte-for-byte as it was found.
    #
    # Renaming as we went would be unrecoverable rather than merely failed: the
    # files would sit under their new names while index.html still named the old
    # ones, and the first thing any later run does is look up the names
    # index.html gives it - so every retry would fail on a tree that no longer
    # matches its own manifest, with the original cause long since fixed.
    notes: list[str] = []
    planned: list[tuple[str, str]] = []
    for name in referenced:
        src = assets / name
        if not src.is_file():
            raise PatchError(
                f"index.html references assets/{name}, which is not on disk"
            )
        new_name = fingerprinted_name(name, asset_fingerprint(src.read_bytes()))
        if new_name == name:
            notes.append(f"assets/{name}: fingerprint already current")
            continue

        # A chunk that other bundles import BY NAME cannot simply be renamed:
        # rewriting those imports would change their bytes, and therefore their
        # own fingerprints, after they had already been computed. Upstream's
        # entry chunk is imported by nobody, so this should never fire - but if
        # a future upstream build changes that, fail loudly instead of shipping
        # a bundle whose dynamic imports 404.
        # The needle is `name`: the name this file carries on disk right now,
        # and therefore the only name anything can be importing it by. It was
        # once a de-fingerprinted variant of that - which is equal to `name` on
        # upstream's own output, and so looked correct and tested correct, but
        # is a string on nobody's disk the moment an asset already carries a
        # fingerprint. It then matched nothing and waved through precisely the
        # rename this guard exists to stop.
        for other in sorted(assets.glob("*.js")):
            if other.name in (name, new_name):
                continue
            if name in other.read_text(encoding="utf-8", errors="ignore"):
                raise PatchError(
                    f"assets/{other.name} refers to {name} by name, so "
                    "renaming it would break a dynamic import"
                )
        planned.append((name, new_name))

    for name, new_name in planned:
        before = html
        html = html.replace(f'assets/{name}"', f'assets/{new_name}"')
        if html == before:
            raise PatchError(
                f"cannot update the reference to assets/{name} in index.html, "
                "so renaming it would leave the page loading nothing"
            )

    # Nothing above this line has touched the filesystem.
    for name, new_name in planned:
        (assets / name).rename(assets / new_name)
        notes.append(f"assets/{name} -> assets/{new_name} (cache-busting)")
    index_path.write_text(html, encoding="utf-8")
    return notes


def patch_bundles(assets: pathlib.Path) -> list[str]:
    notes = []
    total = 0

    routers = 0
    router_sites = 0
    js_assets = 0
    sse = 0
    mic = 0

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

        text, n_mic = RE_MIC_PREVIEW.subn(MIC_PREVIEW_PATCHED, text)
        if n_mic:
            mic += n_mic
            notes.append(
                f"{js.name}: guarded {n_mic} microphone preview call(s) "
                "against a non-secure context"
            )

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

    # Fail closed, like the router above. If upstream reshapes this call the
    # panel goes back to crashing on open over http, and the only symptom is an
    # opaque "Something went wrong!" - so stop the build and re-check by hand
    # rather than ship a UI that dies on the address most people use.
    if mic == 0:
        # Checked by shape, not by marker: the console message survives a
        # mutation that neuters the condition it sits inside.
        already = any(
            RE_MIC_GUARDED.search(js.read_text(encoding="utf-8"))
            for js in assets.glob("*.js")
        )
        if already:
            notes.append("microphone preview already guarded")
        else:
            raise PatchError(
                f"no unguarded microphone preview found in {assets}. Either "
                "upstream fixed it - in which case drop RE_MIC_PREVIEW - or the "
                "bundle changed shape and the guard no longer applies. Left "
                "unpatched, opening the voice-creation dialog over http:// "
                "crashes the whole panel with 'Cannot read properties of "
                "undefined (reading getUserMedia)'."
            )

    return notes


def verify(frontend: pathlib.Path, root: pathlib.Path) -> None:
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
    if not any(
        RE_MIC_GUARDED.search(js.read_text(encoding="utf-8"))
        for js in assets.glob("*.js")
    ):
        raise PatchError(
            "no bundle contains a working microphone guard. Something was "
            "written in front of the getUserMedia call, but it does not have "
            "the shape of a guard that can actually fire - so the voice dialog "
            "would still crash the whole panel over http://"
        )
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
        if RE_MIC_PREVIEW.search(text):
            raise PatchError(
                f"{js.name} still dereferences navigator.mediaDevices without "
                "a guard. Over http:// that property does not exist, so opening "
                "the voice-creation dialog throws a synchronous TypeError that "
                "no .catch() sees and the entire panel dies"
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

    # Every asset the page pulls must carry a fingerprint that actually matches
    # its bytes. Recomputing the digest here, rather than trusting the name, is
    # what makes this a real check rather than a restatement of what the rename
    # step just did: a name left behind by an edit that happened afterwards is
    # caught, and so is a rename step that was disabled entirely.
    referenced = list(dict.fromkeys(RE_ANY_ASSET_REF.findall(index)))
    if not referenced:
        raise PatchError("index.html references no assets/ files")
    for name in referenced:
        asset = assets / name
        if not asset.is_file():
            raise PatchError(
                f"index.html references assets/{name}, which is not on disk - "
                "the page would load nothing at all"
            )
        if not RE_FINGERPRINT.search(name):
            raise PatchError(
                f"assets/{name} carries no cache-busting fingerprint. Its URL "
                "would be identical in every release while its bytes change, "
                "so a browser, a service worker or a CDN is entitled to serve "
                "a stale copy - measured through a CDN the browser is told to "
                "keep it for 4 hours regardless of what this add-on sends"
            )
        # Spelled out longhand ON PURPOSE, rather than calling
        # asset_fingerprint(). If this check reused the very function the
        # rename step used, a mutation to that function - returning a constant,
        # say - would change both sides identically and this would still pass,
        # while every release shipped the same unchanging URL and the bug came
        # straight back. Two independent spellings cannot be defeated by one
        # edit. This repo has shipped two present-but-inert patches already.
        want = hashlib.sha256(asset.read_bytes()).hexdigest()[:8]
        if f".vb{want}" not in name:
            raise PatchError(
                f"assets/{name} carries a fingerprint that does not match its "
                f"bytes (which hash to {want}), so caches would not be busted "
                "by this release"
            )

    routes = spa_routes(assets)
    if not routes:
        raise PatchError("found no client-side routes in the bundle")
    main_py = root / "backend" / "main.py"
    if not main_py.is_file():
        raise PatchError(f"{main_py} does not exist")
    on_disk = read_py(main_py)
    assert_live_middleware(on_disk.replace("\r\n", "\n"), routes)

    receipt = root / POLICY_RECEIPT
    if receipt.is_file():
        recorded = json.loads(receipt.read_text(encoding="utf-8")).get("after", {})
        # Hash what is ON DISK, not a normalised copy: enforce-model-policy.py
        # reads with newline="" too, and comparing a normalised hash against a
        # verbatim one would fail on a CRLF checkout for no real reason.
        digest = hashlib.sha256(on_disk.encode("utf-8")).hexdigest()
        if recorded.get("main.py") not in (None, digest):
            raise PatchError(
                "the model-policy receipt does not match the patched main.py, "
                "so the add-on would report a policy failure on every start"
            )


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
            verify(frontend, root)
            print(f"patch-frontend: {frontend} is patched")
            return 0

        notes = patch_index(frontend / "index.html")
        notes += patch_bundles(frontend / "assets")
        notes += fingerprint_assets(frontend)
        notes += patch_backend(root, spa_routes(frontend / "assets"))
        verify(frontend, root)
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
