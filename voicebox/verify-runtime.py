#!/usr/bin/env python3
"""Prove the deep-link middleware is really registered, by importing the app.

Both build-time patchers assert their own output through the AST. That catches
a patch that was written wrong, but not one that was written right and never
ran - a decorator inside a branch that was not taken, an exception swallowed
during import, a module shadowed by another on sys.path. The distinction
matters here because this project has shipped two patches that were present
and inert and reported success both times.

So this imports the patched backend and inspects the live Starlette app:

  app.user_middleware -> [Middleware(BaseHTTPMiddleware, dispatch=<fn>), ...]

If the decorator ran, the dispatch function is our function. If it did not,
nothing here can hide that. Run as a build step so the image fails instead of
the add-on.
"""
import os
import shutil
import sys
import tempfile

# The app ROOT, not the backend directory. backend/main.py uses relative
# imports, so it has to be imported as `backend.main` with the root on
# sys.path - importing it as a top-level `main` raises "attempted relative
# import with no known parent package". An explicit path lets the test suite
# run this against a fixture, so the check is exercised rather than trusted.
ROOT = sys.argv[1] if len(sys.argv) > 1 else "/app"
NAME = "_vb_spa_deep_links"
CACHE = "_vb_no_stale_frontend"


def fail(msg: str) -> "typing.NoReturn":  # noqa: F821
    print("RUNTIME CHECK FAILED", file=sys.stderr)
    print(msg, file=sys.stderr)
    raise SystemExit(1)


sys.path.insert(0, ROOT)

# Importing the backend creates a ./data directory as a side effect, so do it
# somewhere disposable rather than littering the image layer.
_scratch = tempfile.mkdtemp(prefix="vb-verify-")
os.chdir(_scratch)
try:
    from backend import main
except Exception as exc:  # noqa: BLE001 - any import failure is fatal here
    fail(
        f"the patched backend does not import: {type(exc).__name__}: {exc}\n"
        "The add-on would die on start with exactly this error."
    )

app = getattr(main, "app", None)
if app is None:
    fail("main.py defines no `app`, so nothing was served at all")

registered = []
for mw in getattr(app, "user_middleware", []):
    dispatch = getattr(mw, "kwargs", {}).get("dispatch")
    name = getattr(dispatch, "__name__", None)
    if name:
        registered.append(name)

if NAME not in registered:
    fail(
        f"{NAME} is NOT registered on the app.\n"
        f"  middleware actually registered: {registered or '(none)'}\n"
        "The source may contain the patch, but the decorator never ran, so\n"
        "reloading /models, /voices, /audio, /server or /stories would still\n"
        "lose the app. This is the 'present but inert' failure again."
    )

if CACHE not in registered:
    fail(
        f"{CACHE} is NOT registered on the app.\n"
        f"  middleware actually registered: {registered or '(none)'}\n"
        "index.html would be served with no Cache-Control, so a browser could\n"
        "keep reusing a previous build's bundle under the same URL and render\n"
        "the old frontend - or nothing at all."
    )

# Registration order decides nesting. Starlette INSERTS each new middleware at
# position 0, and index 0 is the outermost - verified by experiment, because
# getting this backwards is easy and silent:
#
#   register first, then second -> ['second', 'first'], and 'first' runs inner
#
# The cache middleware has to be outside the deep-link one, or the index.html
# that a reload returns bypasses it and goes out with no Cache-Control after
# all - which is the exact bug this is here to prevent.
if registered.index(CACHE) >= registered.index(NAME):
    fail(
        f"{CACHE} is at index {registered.index(CACHE)} and {NAME} at "
        f"{registered.index(NAME)}.\n"
        f"  order (index 0 is outermost): {registered}\n"
        f"{CACHE} must wrap {NAME}, otherwise a deep-link reload returns\n"
        "index.html without a Cache-Control header and the stale-bundle bug\n"
        "comes straight back on exactly the routes it broke before."
    )

routes = getattr(main, "_VB_SPA_ROUTES", None)
if not routes:
    fail(
        f"{NAME} is registered but _VB_SPA_ROUTES is {routes!r}, so it can "
        "never match a reload"
    )

shutil.rmtree(_scratch, ignore_errors=True)
print(f"runtime check: {NAME} + {CACHE} registered, covering {sorted(routes)}")
