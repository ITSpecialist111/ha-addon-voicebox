#!/usr/bin/env python3
"""Tests for patch-frontend.py.

The fixtures below are not invented: index.html and the getBaseUrl()/request()
snippet are copied verbatim from the running Voicebox container
(ghcr.io/jamiepine/voicebox:latest, built 2026-02-03). If upstream republishes
and these stop matching, that is exactly the signal we want.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "voicebox" / "patch-frontend.py"

PASSED = 0
SKIPPED = 0
FAILED = 0

# Verbatim from the container.
REAL_INDEX = """<!doctype html>
<html lang="en" class="dark">
  <head>
    <meta charset="UTF-8" />
    <link rel="icon" type="image/svg+xml" href="/vite.svg" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>voicebox</title>
    <script type="module" crossorigin src="/assets/index-DPaWep75.js"></script>
    <link rel="stylesheet" crossorigin href="/assets/index-DfXZrYe2.css">
  </head>
  <body>
    <div id="root"></div>
  </body>
</html>
"""

# The route table, verbatim in shape from the bundle. The sidebar list above it
# in the real file is a separate, presentational list; the routes below are what
# the router actually matches on.
REAL_ROUTES_JS = (
    ";const ca=U3({component:MJ}),AJ=oa({getParentRoute:()=>ca,path:\"/\","
    "component:uZ}),IJ=oa({getParentRoute:()=>ca,path:\"/stories\","
    "component:eJ}),LJ=oa({getParentRoute:()=>ca,path:\"/voices\","
    "component:EJ}),OJ=oa({getParentRoute:()=>ca,path:\"/audio\","
    "component:FW}),FJ=oa({getParentRoute:()=>ca,path:\"/models\","
    "component:PZ}),VJ=oa({getParentRoute:()=>ca,path:\"/server\","
    "component:WZ});"
)

# The live-waveform effect, verbatim in shape from the bundle. Note the .catch():
# it looks like this code handles a missing microphone, and that is exactly why
# the bug survived - a rejected promise IS handled, but navigator.mediaDevices
# being ABSENT throws synchronously, before any promise exists, so nothing here
# ever sees it.
REAL_MIC_JS = (
    "p.useEffect(()=>{if(!d)return;let m=null;"
    "return navigator.mediaDevices.getUserMedia({audio:!0,video:!1})"
    ".then(v=>{m=v,h(v)})"
    '.catch(v=>{console.warn("Could not access microphone for visualization:",v)}),'
    "()=>{m&&m.getTracks().forEach(v=>{v.stop()})}},[d]);"
)

# A microphone preview, for fixtures that supply their own JS.
MIC_JS = REAL_MIC_JS


# Extracts the waveform effect from a bundle and runs it, rather than re-typing
# an approximation of it: the point is to exercise the shipped bytes.
HARNESS_JS = r"""
const fs = require("fs"), path = require("path");
const dir = process.argv[2];
function body(src) {
  const i = src.indexOf("if(!d)return;let ");
  if (i < 0) throw new Error("effect not found");
  let depth = 0, j = i;
  for (; j < src.length; j++) {
    const c = src[j];
    if (c === "{") depth++;
    else if (c === "}") { if (depth === 0) break; depth--; }
  }
  return src.slice(i, j);
}
const out = [];
for (const which of ["before", "after"]) {
  const b = body(fs.readFileSync(path.join(dir, which + ".js"), "utf8"));
  for (const [ctx, nav] of [
    ["http", {}],
    ["https", { mediaDevices: { getUserMedia: () =>
        Promise.resolve({ getTracks: () => [] }) } }],
  ]) {
    try {
      const ret = new Function("d", "h", "navigator", "console", b)(
        true, () => {}, nav, { warn() {} });
      out.push(which + "/" + ctx + "=OK");
      out.push(which + "/" + ctx + "/ret=" + typeof ret);
    } catch (e) {
      out.push(which + "/" + ctx + "=THREW " + e.message);
    }
  }
}
console.log(out.join("\n"));
"""

# Verbatim from the container: the whole block that serves the built SPA.
REAL_MAIN_PY = '''from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

app = FastAPI(title="Voicebox API")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/stories")
async def list_stories():
    return [{"id": 1, "title": "a story"}]


# ============================================
# WEB UI STATIC FILES
# ============================================

# Serve web UI at root if dist directory exists
_web_dist_path = Path(__file__).parent.parent / "web" / "dist"
if _web_dist_path.exists():
    app.mount("/", StaticFiles(directory=str(_web_dist_path), html=True), name="web")


# ============================================
# STARTUP & SHUTDOWN
# ============================================
'''


# Verbatim shape from the container bundle.
REAL_JS = (
    'Mn=md()(B$(e=>({serverUrl:"http://127.0.0.1:17493",'
    "setServerUrl:t=>e({serverUrl:t}),isConnected:!1}),"
    '{name:"voicebox-server"}));'
    "class U${getBaseUrl(){return Mn.getState().serverUrl}"
    "async request(t,n){const r=`${this.getBaseUrl()}${t}`,"
    "s=await fetch(r,{...n});return s.json()}"
    'async getHealth(){return this.request("/health")}}'
    # The router is built with only a routeTree, so its basepath defaults to
    # "/" and nothing matches under the ingress prefix. Verbatim from the
    # shipped bundle, including the Vite-inlined absolute logo URL.
    ';$J=ca.addChildren([AJ,IJ,LJ,OJ,FJ,VJ]),zJ=Y3({routeTree:$J}),'
    'Pk=["Warming up tensors..."];'
    'const DR="/assets/voicebox-logo-DQ1k8iIe.png",k0=p.createContext({});'
    # The two model-download progress streams, which build their URL from the
    # store's serverUrl instead of getBaseUrl(). Verbatim shapes from the bundle.
    'const h=`${i}/models/progress/${e}`;'
    'if(!o||!n)return;const u=new EventSource(`${o}/models/progress/${e}`);'
    + REAL_ROUTES_JS
    + REAL_MIC_JS
)

# A router construction, for fixtures that supply their own JS.
ROUTER_JS = ";zJ=Y3({routeTree:$J});"

# The same bundle with no router at all, for the fail-closed case.
JS_NO_ROUTER = REAL_JS.replace("Y3({routeTree:$J})", "Y3(void 0)")


# Used only by the inert-patch mutations, to excise the emitted block wholesale
# or blank its guard without depending on exact whitespace.
RE_FALLBACK_BLOCK = re.compile(
    r"[ \t]*# --- Home Assistant ingress deep links.*?return await call_next\(request\)\n",
    re.DOTALL,
)
RE_GUARD = re.compile(r"^[ \t]*if \($", re.MULTILINE)
RE_CACHE_BLOCK = re.compile(
    r"[ \t]*# --- Home Assistant ingress cache control.*?"
    r'response\.headers\["Cache-Control"\] = "no-cache"\n',
    re.DOTALL,
)
RE_ROUTESET = re.compile(r"^[ \t]*_VB_SPA_ROUTES = frozenset\(.*\)$",
                         re.MULTILINE)


def skip(name: str) -> None:
    """Record a scenario that did not run.

    Counted, printed and returned in the exit status. Two of the scenarios
    here are the only ones that catch a patch which is present but inert, and
    a suite that silently skips them reports success while testing nothing -
    which is the failure this project keeps having.
    """
    global SKIPPED
    SKIPPED += 1
    print(f"  SKIP  {name}")


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))


def make_fixture(tmp: pathlib.Path, index: str = REAL_INDEX, js: str = REAL_JS,
                 assets: bool = True, main_py: str | None = REAL_MAIN_PY,
                 front_dir: str = "frontend/dist") -> pathlib.Path:
    root = tmp / "app"
    front = root.joinpath(*front_dir.split("/"))
    front.mkdir(parents=True)
    (front / "index.html").write_text(index, encoding="utf-8")
    if assets:
        (front / "assets").mkdir()
        (front / "assets" / "index-DPaWep75.js").write_text(js, encoding="utf-8")
        (front / "assets" / "index-DfXZrYe2.css").write_text("body{}", encoding="utf-8")
    if main_py is not None:
        (root / "backend").mkdir(parents=True)
        # newline="" so the fixture is LF on every platform, exactly like the
        # file inside the container. Letting Python translate would make these
        # scenarios test Windows line endings instead of the shipped bytes.
        with open(root / "backend" / "main.py", "w",
                  encoding="utf-8", newline="") as fh:
            fh.write(main_py)
    return root


def backend_of(root: pathlib.Path) -> str:
    return (root / "backend" / "main.py").read_text(encoding="utf-8")


def entry_js(root: pathlib.Path, front_dir: str = "frontend/dist") -> pathlib.Path:
    """The patched entry bundle.

    Its name now carries a content fingerprint, so the tests cannot hard-code
    it. Resolving it by glob also means a scenario fails loudly if the rename
    produced two bundles or none, rather than reading a stale file that the
    patcher had already moved aside.
    """
    assets = root.joinpath(*front_dir.split("/")) / "assets"
    found = sorted(assets.glob("index-*.js"))
    if len(found) != 1:
        raise AssertionError(
            f"expected exactly one entry bundle, found {[p.name for p in found]}"
        )
    return found[0]


def run(root: pathlib.Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root), *extra],
        capture_output=True, text=True,
    )


PROBE = r'''
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / "backend"))
import main
from starlette.testclient import TestClient

HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ANY = "*/*"
# The entry bundle's name carries a content fingerprint, so it cannot be
# hard-coded here either.
_entry = sorted(pathlib.Path(sys.argv[1]).rglob("assets/index-*.js"))[0].name
cases = [
    ("reload_models", "/models", HTML),
    ("reload_stories", "/stories", HTML),
    ("reload_trailing", "/models/", HTML),
    ("api_stories", "/stories", ANY),
    ("api_health", "/health", HTML),
    ("api_models", "/models", ANY),
    ("root", "/", HTML),
    ("asset", f"/assets/{_entry}", ANY),
]
out = {}
with TestClient(main.app) as c:
    for name, path, accept in cases:
        r = c.get(path, headers={"accept": accept}, follow_redirects=False)
        out[name] = {
            "status": r.status_code,
            "type": r.headers.get("content-type", ""),
            # Computed over the WHOLE body: a truncated preview would make an
            # assertion pass or fail for reasons unrelated to what is served.
            "is_app": '<div id="root">' in r.text,
            "body": r.text[:200],
            "location": r.headers.get("location"),
            "cache": r.headers.get("cache-control"),
        }
print(json.dumps(out))
'''


def _has_server_libs() -> bool:
    import importlib.util
    return all(importlib.util.find_spec(m) for m in ("fastapi", "starlette"))


def probe(root: pathlib.Path) -> dict:
    import json
    script = root / "_probe.py"
    script.write_text(PROBE, encoding="utf-8")
    r = subprocess.run([sys.executable, str(script), str(root)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-800:])
    return json.loads(r.stdout)


def _compiles(text: str) -> bool:
    try:
        compile(text, "main.py", "exec")
    except SyntaxError:
        return False
    return True


def scenario(title: str):
    print(f"\n{title}")


def main() -> int:
    print("patch-frontend.py")
    print("=" * 60)

    # ---------------------------------------------------------------
    scenario("1. Happy path against the real shipped files")
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        root = make_fixture(tmp)
        r = run(root)
        check("exits 0", r.returncode == 0, r.stderr)

        index = (root / "frontend/dist/index.html").read_text(encoding="utf-8")
        js = (entry_js(root)).read_text(encoding="utf-8")

        check("asset src made relative", 'src="assets/index-DPaWep75.vb' in index)
        check("stylesheet href made relative", 'href="assets/index-DfXZrYe2.vb' in index)
        check("favicon made relative", 'href="vite.svg"' in index)
        check("no absolute src=/href= left", not re.search(r'(?:src|href)="/(?!/)', index))
        check("base snippet injected", "__VB_BASE__" in index)
        check("snippet precedes the module script",
              index.index("__VB_BASE__") < index.index("index-DPaWep75.vb"))
        check("getBaseUrl rewritten",
              'getBaseUrl(){return window.__VB_BASE__||""}' in js)
        check("original getBaseUrl gone", "Mn.getState().serverUrl" not in js)
        check("hardcoded 127.0.0.1 origin neutralised", "127.0.0.1:17493" not in js)
        check("default origin resolves per-context, and is truthy",
              "(window.__VB_BASE__||location.origin)" in js)
        check("request() left intact", "const r=`${this.getBaseUrl()}${t}`" in js)

    # ---------------------------------------------------------------
    scenario("2. Idempotent - running twice is safe")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        first = run(root)
        after_first = (root / "frontend/dist/index.html").read_text(encoding="utf-8")
        second = run(root)
        after_second = (root / "frontend/dist/index.html").read_text(encoding="utf-8")
        check("second run exits 0", second.returncode == 0, second.stderr)
        check("content unchanged by second run", after_first == after_second)
        check("snippet not injected twice", after_second.count("__VB_BASE__") == 1)
        check("first run reported success", first.returncode == 0)

    # ---------------------------------------------------------------
    scenario("3. --check reflects real state")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        before = run(root, "--check")
        check("unpatched tree fails --check", before.returncode != 0)
        run(root)
        after = run(root, "--check")
        check("patched tree passes --check", after.returncode == 0, after.stderr)

    # ---------------------------------------------------------------
    scenario("4. Fails loudly when upstream changes shape")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td), js="export const x=1;// no getBaseUrl here")
        r = run(root)
        check("exits non-zero", r.returncode != 0)
        check("explains what it looked for", "getBaseUrl" in r.stderr, r.stderr)
        check("warns about the localhost consequence",
              "localhost" in r.stderr.lower(), r.stderr)

    # ---------------------------------------------------------------
    scenario("5. Fails when no frontend is present")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "app"
        (root / "backend").mkdir(parents=True)
        r = run(root)
        check("exits non-zero", r.returncode != 0)
        check("says no frontend found", "no built frontend" in r.stderr, r.stderr)

    # ---------------------------------------------------------------
    scenario("6. Refuses to guess between two frontends")
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        root = make_fixture(tmp)
        other = root / "other" / "dist"
        (other / "assets").mkdir(parents=True)
        (other / "index.html").write_text(REAL_INDEX, encoding="utf-8")
        (other / "assets" / "a.js").write_text(REAL_JS, encoding="utf-8")
        r = run(root)
        check("exits non-zero", r.returncode != 0)
        check("names the ambiguity", "found 2" in r.stderr, r.stderr)

    # ---------------------------------------------------------------
    scenario("7. node_modules is ignored, not mistaken for the app")
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        root = make_fixture(tmp)
        nm = root / "node_modules" / "some-pkg"
        (nm / "assets").mkdir(parents=True)
        (nm / "index.html").write_text(REAL_INDEX, encoding="utf-8")
        (nm / "assets" / "b.js").write_text(REAL_JS, encoding="utf-8")
        r = run(root)
        check("still finds exactly one frontend", r.returncode == 0, r.stderr)
        check("node_modules left untouched",
              "Mn.getState().serverUrl" in (nm / "assets/b.js").read_text(encoding="utf-8"))

    # ---------------------------------------------------------------
    scenario("8. Whitespace variants of getBaseUrl still match")
    with tempfile.TemporaryDirectory() as td:
        spaced = ("class A { getBaseUrl() { return store.getState().serverUrl } }"
                  + ROUTER_JS + REAL_ROUTES_JS + MIC_JS)
        root = make_fixture(pathlib.Path(td), js=spaced)
        r = run(root)
        js = (entry_js(root)).read_text(encoding="utf-8")
        check("exits 0", r.returncode == 0, r.stderr)
        check("rewritten", 'window.__VB_BASE__||""' in js)

    # ---------------------------------------------------------------
    # The symptom this exists to prevent: the shell paints, the nav rail
    # renders and API polling works, yet every route shows a bare "Not Found"
    # because TanStack Router was left at basepath "/".
    scenario("9. The router is given the ingress basepath")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        r = run(root)
        js = (entry_js(root)).read_text(encoding="utf-8")
        check("exits 0", r.returncode == 0, r.stderr)
        check("basepath added to the router",
              'routeTree:$J,basepath:window.__VB_BASE__||"/"' in js)
        check("every routeTree has one",
              js.count("{routeTree:") == js.count('basepath:window.__VB_BASE__||"/"'))
        check("reported in the output", "basepath" in r.stdout, r.stdout)

    # ---------------------------------------------------------------
    # Vite inlines imported images as root-absolute URLs, which under ingress
    # resolve against Home Assistant rather than the add-on - a broken logo.
    scenario("10. Absolute /assets/ URLs inside the bundle are rebased")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        r = run(root)
        js = (entry_js(root)).read_text(encoding="utf-8")
        check("exits 0", r.returncode == 0, r.stderr)
        check("logo URL rebased",
              '(window.__VB_BASE__||"")+"/assets/voicebox-logo-DQ1k8iIe.png"' in js)
        check("no bare absolute /assets/ literal left",
              not re.search(r'(?<!\+)"/assets/', js))

    # ---------------------------------------------------------------
    scenario("11. Router and asset patches are idempotent")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        run(root)
        first = (entry_js(root)).read_text(encoding="utf-8")
        again = run(root)
        second = (entry_js(root)).read_text(encoding="utf-8")
        check("second run exits 0", again.returncode == 0, again.stderr)
        check("bundle unchanged", first == second)
        check("exactly one basepath",
              second.count('basepath:window.__VB_BASE__||"/"') == 1)
        check("logo wrapped exactly once",
              second.count('(window.__VB_BASE__||"")+"/assets/') == 1)

    # ---------------------------------------------------------------
    # A file-wide substitution would re-patch the already-patched router while
    # fixing the other one, emitting basepath twice in the same object.
    scenario("11b. A half-patched bundle gains no duplicate basepath")
    with tempfile.TemporaryDirectory() as td:
        mixed = (REAL_JS
                 + ';q1=Y3({routeTree:aa,basepath:window.__VB_BASE__||"/"});'
                 + ";q2=Y3({routeTree:bb});")
        root = make_fixture(pathlib.Path(td), js=mixed)
        r = run(root)
        js = (entry_js(root)).read_text(encoding="utf-8")
        check("exits 0", r.returncode == 0, r.stderr)
        check("all three routers have a basepath",
              js.count("{routeTree:") == js.count('basepath:window.__VB_BASE__||"/"'))
        check("none has two",
              'basepath:window.__VB_BASE__||"/",basepath:' not in js)
        check("the previously patched one is untouched",
              'routeTree:aa,basepath:window.__VB_BASE__||"/"}' in js)
        check("the unpatched one was fixed",
              'routeTree:bb,basepath:window.__VB_BASE__||"/"}' in js)

    # ---------------------------------------------------------------
    # A duplicate key takes its LAST value, so a basepath already present later
    # in the same options object would silently defeat ours - and verify()
    # would still pass. Refusing is the only safe answer.
    scenario("11c. A router with its own basepath is refused, not shadowed")
    with tempfile.TemporaryDirectory() as td:
        own = REAL_JS.replace("{routeTree:$J}", '{routeTree:$J,basepath:"/"}')
        root = make_fixture(pathlib.Path(td), js=own)
        r = run(root)
        check("exits non-zero", r.returncode != 0)
        check("says the router already sets one",
              "already sets its own basepath" in r.stderr, r.stderr)

    # ---------------------------------------------------------------
    # Download progress is the one stream that bypasses getBaseUrl().
    scenario("11d. Model-download progress streams get the ingress base")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        r = run(root)
        js = (entry_js(root)).read_text(encoding="utf-8")
        check("exits 0", r.returncode == 0, r.stderr)
        check("both streams rebased",
              js.count('${window.__VB_BASE__||""}/models/progress/') == 2)
        check("no stream still built from the stored URL",
              not re.search(r"\$\{[A-Za-z_$][A-Za-z0-9_$]*\}/models/progress/", js))
        check("reported", "progress stream" in r.stdout, r.stdout)

    # ---------------------------------------------------------------
    # Rewriting only the URL is not enough: both call sites bail out early when
    # the stored server URL is falsy, so blanking it would leave SSE off.
    scenario("11e. The early-return guard still sees a truthy server URL")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        run(root)
        js = (entry_js(root)).read_text(encoding="utf-8")
        check("default origin is not blanked", 'serverUrl:""' not in js)
        check("truthy in both contexts",
              'serverUrl:(window.__VB_BASE__||location.origin)' in js)

    # ---------------------------------------------------------------
    # If upstream changes how the router is built, shipping a UI where every
    # route says Not Found is worse than failing the build.
    scenario("12. Router missing: fails closed instead of shipping Not Found")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td), js=JS_NO_ROUTER)
        r = run(root)
        check("exits non-zero", r.returncode != 0)
        check("names what it looked for", "router" in r.stderr, r.stderr)
        check("explains the consequence", "Not Found" in r.stderr, r.stderr)

    # ---------------------------------------------------------------
    # Exactly the state that shipped in 0.7.2: the API was patched, the router
    # was not. --check has to reject it.
    scenario("13. --check rejects an API-only patch")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        setup = run(root)
        # Without this, a setup that failed would leave an UNPATCHED tree,
        # --check would rightly reject it, and the scenario would pass while
        # testing nothing but scenario 3 over again.
        check("setup patched cleanly", setup.returncode == 0, setup.stderr[-200:])
        jsp = entry_js(root)
        before = jsp.read_text(encoding="utf-8")
        after = before.replace(',basepath:window.__VB_BASE__||"/"', "")
        # And without this, a patcher that stopped emitting the basepath would
        # make the mutation a no-op - the scenario would then be checking an
        # intact tree and would fail confusingly rather than clearly.
        check("the basepath removal actually removed something", after != before)
        jsp.write_text(after, encoding="utf-8")
        r = run(root, "--check")
        check("exits non-zero", r.returncode != 0)
        check("says why", "basepath" in r.stderr or "Not Found" in r.stderr, r.stderr)

    # ---------------------------------------------------------------
    scenario("14. The injected base-path expression resolves correctly")
    # The snippet ships a JS regex. Transliterate it and check the cases that
    # matter, so a broken expression is caught here rather than in a browser.
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"\.match\(/\^\((.*?)\)/\)", src)
    check("regex is extractable from the shipped snippet", m is not None)
    if m:
        pattern = re.compile("^(" + m.group(1).replace("\\\\", "\\") + ")")

        def base_for(path: str) -> str:
            hit = pattern.match(path)
            return hit.group(1) if hit else ""

        token = "/api/hassio_ingress/iBuKItTTwtVDEK8zVLV5Q2NaaR9GjEHQhZXcBLrdpeY"
        check("ingress root -> ingress prefix", base_for(token + "/") == token,
              base_for(token + "/"))
        check("ingress sub-route -> same prefix (survives a reload)",
              base_for(token + "/profiles/abc") == token, base_for(token + "/profiles/abc"))
        check("direct port root -> empty (same-origin)", base_for("/") == "")
        check("direct port sub-route -> empty", base_for("/profiles") == "")
        # "" + "/health" == "/health", the correct same-origin URL.
        check("empty base yields a same-origin API path",
              base_for("/") + "/health" == "/health")
        check("ingress base yields a prefixed API path",
              base_for(token + "/") + "/health" == token + "/health")

    # ---------------------------------------------------------------
    # Navigating to /models works - the router pushes history and never asks
    # the server. RELOADING it does ask, and without this the answer is a 404
    # and a blank page: the app vanishes on refresh.
    scenario("15. Reloading a client-side route returns the app")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        r = run(root)
        check("exits 0", r.returncode == 0, r.stderr)
        backend = backend_of(root)
        check("a fallback was added", "_vb_spa_deep_links" in backend)
        for route in ("/stories", "/voices", "/audio", "/models", "/server"):
            check(f"{route} is covered", repr(route) in backend)
        check("the index route is not listed",
              "'/'" not in backend.split("_VB_SPA_ROUTES = ")[1].split(")")[0],
              "the mount already serves / - listing it adds a needless branch")
        check("the mount is still there",
              'app.mount("/", StaticFiles(' in backend)
        check("main.py is still valid Python",
              _compiles(backend), "the patch produced code Python cannot parse")
        check("reported", "reloading" in r.stdout)

    # ---------------------------------------------------------------
    # A catch-all route could never fix /stories: the API endpoint of the same
    # name is registered first and wins. Only middleware runs before routing.
    scenario("16. The fallback runs before routing, so it covers /stories")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        run(root)
        backend = backend_of(root)
        check("uses middleware, not a route",
              "@app.middleware(" in backend and "@app.get(" not in
              backend.split("_VB_SPA_ROUTES")[1])
        check("/stories is covered even though it is a real endpoint",
              "'/stories'" in backend)

    # ---------------------------------------------------------------
    # The whole point is to be inert for anything that is not a browser
    # navigation. The web UI sets no Accept header, so it must never match.
    scenario("17. The fallback only answers browser navigations")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        run(root)
        backend = backend_of(root)
        block = backend.split("async def _vb_spa_deep_links")[1]
        check("requires text/html", '"text/html" in request.headers' in block)
        check("read-only methods only", '("GET", "HEAD")' in block)
        check("falls through otherwise", "return await call_next(request)" in block)
        check("serves index.html as HTML",
              'FileResponse(_VB_SPA_INDEX, media_type="text/html")' in block)

    # ---------------------------------------------------------------
    scenario("18. Running twice does not stack two fallbacks")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        run(root)
        first = backend_of(root)
        r = run(root)
        second = backend_of(root)
        check("second run exits 0", r.returncode == 0, r.stderr)
        check("byte-identical", first == second)
        check("exactly one fallback", second.count("_vb_spa_deep_links") ==
              first.count("_vb_spa_deep_links"))
        check("said so", "already present" in r.stdout)

    # ---------------------------------------------------------------
    # If upstream stops mounting the SPA this way, silently doing nothing would
    # ship an app that dies on every refresh.
    scenario("19. A missing or changed mount fails closed")
    with tempfile.TemporaryDirectory() as td:
        moved = REAL_MAIN_PY.replace(
            'app.mount("/", StaticFiles(directory=str(_web_dist_path), '
            'html=True), name="web")',
            'app.mount("/ui", StaticFiles(directory=str(_web_dist_path)), name="web")',
        )
        root = make_fixture(pathlib.Path(td), main_py=moved)
        r = run(root)
        check("exits non-zero", r.returncode != 0)
        check("names what it looked for", "StaticFiles mount" in r.stderr)
        check("explains the consequence",
              "nothing to attach to" in r.stderr)

    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td), main_py=None)
        r = run(root)
        check("no backend at all also fails", r.returncode != 0)
        check("says which file is missing", "main.py" in r.stderr)

    # ---------------------------------------------------------------
    # The route list must come from the bundle, so a route added upstream is
    # picked up rather than quietly left without a fallback.
    scenario("20. The route list is read from the bundle, not hardcoded")
    with tempfile.TemporaryDirectory() as td:
        extra = REAL_JS.replace(
            'path:"/server",component:WZ});',
            'path:"/server",component:WZ}),QQ=oa({getParentRoute:()=>ca,'
            'path:"/presets",component:QZ});',
        )
        check("the fixture really gained a route", extra != REAL_JS)
        root = make_fixture(pathlib.Path(td), js=extra)
        r = run(root)
        check("exits 0", r.returncode == 0, r.stderr)
        backend = backend_of(root)
        check("the new route is covered", "'/presets'" in backend)
        check("the old ones still are", "'/models'" in backend)

    with tempfile.TemporaryDirectory() as td:
        # A parameterised route cannot be enumerated ahead of time - listing it
        # literally would match the text "$voiceId" and nothing else. Skipping
        # it silently used to be the behaviour, and it shipped a reload that
        # still 404s. The image is pinned by digest, so this can only appear
        # when we deliberately bump it; failing there is cheap, a silent gap
        # is not.
        param = REAL_JS.replace('path:"/server"', 'path:"/voices/$voiceId"')
        check("the fixture really gained a parameterised route",
              param != REAL_JS)
        root = make_fixture(pathlib.Path(td), js=param)
        r = run(root)
        check("a parameterised route fails the build, not shipped half-covered",
              r.returncode != 0, r.stdout + r.stderr)
        check("...and names the route it could not enumerate",
              "voiceId" in r.stderr, r.stderr[-300:])
        check("...and says what would break",
              "lose the app" in r.stderr, r.stderr[-300:])

    # ---------------------------------------------------------------
    scenario("21. --check rejects a tree whose backend was never patched")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        run(root)
        # Undo only the backend half, leaving a fully patched frontend.
        main_py = root / "backend" / "main.py"
        main_py.write_text(REAL_MAIN_PY, encoding="utf-8")
        r = run(root, "--check")
        check("exits non-zero", r.returncode != 0)
        check("says the fallback function is gone", "deep-link fallback" in r.stderr)
        check("explains the consequence", "lose the app" in r.stderr)

    # ---------------------------------------------------------------
    # Everything above asserts on the TEXT of the patch. This one runs it: the
    # patched main.py is imported and driven with a real ASGI client, so a patch
    # that is present but inert cannot pass.
    scenario("22. End to end: the patched server really answers a reload")
    if not _has_server_libs():
        print("  SKIP  fastapi/starlette not installed")
    else:
        with tempfile.TemporaryDirectory() as td:
            root = make_fixture(pathlib.Path(td), front_dir="web/dist")
            r = run(root)
            check("patcher exits 0", r.returncode == 0, r.stderr)
            try:
                got = probe(root)
            except RuntimeError as exc:
                check("the patched app imports and serves", False, str(exc))
                got = {}

            if got:
                m = got["reload_models"]
                check("reloading /models returns 200",
                      m["status"] == 200, repr(m))
                check("...and it is the app, not JSON",
                      m["is_app"], repr(m["body"][:120]))
                check("...served as HTML", "text/html" in m["type"], m["type"])
                check("...not a redirect that would leave the add-on",
                      m["location"] is None, repr(m["location"]))

                st = got["reload_stories"]
                check("reloading /stories returns the app, beating the API route",
                      st["status"] == 200 and st["is_app"], repr(st["body"][:120]))

                tr = got["reload_trailing"]
                check("a trailing slash works too",
                      tr["status"] == 200 and tr["is_app"], repr(tr))

                api = got["api_stories"]
                check("GET /stories without Accept still returns the API",
                      "a story" in api["body"], repr(api["body"][:120]))
                check("...as JSON", "json" in api["type"], api["type"])

                h = got["api_health"]
                check("/health is not hijacked even from a browser",
                      "ok" in h["body"] and not h["is_app"], repr(h))

                am = got["api_models"]
                check("GET /models without Accept is left alone",
                      not am["is_app"], repr(am)[:160])

                rt = got["root"]
                check("/ still serves the app", rt["is_app"], repr(rt)[:160])

                a = got["asset"]
                check("assets are untouched",
                      a["status"] == 200 and not a["is_app"], repr(a)[:160])

                # The stale-bundle defect. Upstream serves the frontend with an
                # ETag but NO Cache-Control, so a browser is free to apply
                # heuristic freshness and reuse a stored copy without asking.
                # The bundle filename never changes between add-on versions, so
                # the stale copy sits at the very same URL as the new one and an
                # updated add-on renders the PREVIOUS frontend - or, if that one
                # predates the ingress patches, nothing at all. Caches are keyed
                # by ORIGIN, which is why it showed up as "fine on the LAN
                # address, blank through the external hostname".
                check("/ is served no-cache so a new build is picked up",
                      rt["cache"] == "no-cache", repr(rt["cache"]))
                check("...and so are the hashed assets",
                      a["cache"] == "no-cache", repr(a["cache"]))
                check("...and the deep-link reloads that return index.html",
                      m["cache"] == "no-cache", repr(m["cache"]))
                # Scope matters: generated audio is large and immutable, and
                # marking it no-cache would re-download it on every replay.
                check("but the API is left alone",
                      h["cache"] != "no-cache", repr(h["cache"]))

    # ------------------------------------------------------------------
    # 23. the two main.py patchers, in Dockerfile order, on the real source
    #
    # This is the scenario that caught a BUILD-BREAKING conflict. Both
    # enforce-model-policy.py and patch-frontend.py now edit backend/main.py.
    # The policy pins a SHA-256 of pristine upstream main.py and refuses to
    # apply once it has moved, so with patch-frontend running first the image
    # would not build at all. And because run.sh re-runs the policy verifier at
    # EVERY BOOT, merely reversing the order without re-sealing the recorded
    # hash would move the failure from build time to every single start.
    #
    # The order asserted here is the order the Dockerfile uses, run against the
    # actual shipped backend rather than a fixture.
    # ------------------------------------------------------------------
    scenario("23. model policy then frontend patch, on the real backend")
    imgsrc = HERE / ".imgsrc" / "app" / "backend"
    policy = HERE / "voicebox" / "enforce-model-policy.py"
    if not imgsrc.is_dir() or not policy.is_file():
        skip("no .imgsrc backend to test the real build order against")
    else:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "app"
            shutil.copytree(imgsrc, root / "backend")
            dist = root / "web" / "dist"
            (dist / "assets").mkdir(parents=True)
            (dist / "index.html").write_text(REAL_INDEX, encoding="utf-8")
            (dist / "assets" / "index-DPaWep75.js").write_text(REAL_JS, encoding="utf-8")
            (dist / "assets" / "index-DfXZrYe2.css").write_text("body{}", encoding="utf-8")
            receipt = root / ".voicebox-model-policy.json"

            def run_policy(*flags):
                return subprocess.run(
                    [sys.executable, str(policy), *flags,
                     "--root", str(root / "backend"),
                     "--receipt", str(receipt)],
                    capture_output=True, text=True)

            def sealed_hash() -> str:
                return json.loads(
                    receipt.read_text(encoding="utf-8"))["after"]["main.py"]

            r = run_policy("--apply")
            check("the model policy applies to pristine main.py",
                  r.returncode == 0, r.stderr[-400:])
            check("it wrote a receipt", receipt.is_file())
            before = sealed_hash() if receipt.is_file() else ""

            r = run(root)
            check("patch-frontend then succeeds on the same tree",
                  r.returncode == 0, r.stderr[-400:])
            check("it says it re-sealed the receipt",
                  "re-sealed" in r.stdout, r.stdout[-300:])
            check("the recorded hash actually moved", sealed_hash() != before)

            # The point of the whole exercise: run.sh does this at every boot.
            r = run_policy("--verify")
            check("the model policy STILL verifies - this is the boot path",
                  r.returncode == 0, r.stderr[-500:])

            final = backend_of(root)
            check("the deep-link fallback survived",
                  "_vb_spa_deep_links" in final)
            check("the provider guard survived",
                  "_vb_external_providers_allowed" in final)
            check("the policy's injected block was not corrupted",
                  "# --- end injected block ---" in final)
            check("...and the fallback was not inserted inside it",
                  final.index("_vb_spa_deep_links")
                  > final.rindex("# --- end injected block ---"))
            check("the doubly-patched main.py is valid Python", _compiles(final))

            # A cached rebuild layer replays these steps; nothing may drift.
            snap_main = final
            snap_receipt = receipt.read_text(encoding="utf-8")
            r = run(root)
            check("a second patch-frontend run exits 0", r.returncode == 0,
                  r.stderr[-300:])
            check("main.py is byte-identical", backend_of(root) == snap_main)
            check("the receipt is byte-identical",
                  receipt.read_text(encoding="utf-8") == snap_receipt)

            # And the negative: a receipt that no longer matches must be caught
            # at BUILD time, because at run time it aborts every start.
            data = json.loads(receipt.read_text(encoding="utf-8"))
            data["after"]["main.py"] = "0" * 64
            receipt.write_text(json.dumps(data), encoding="utf-8")
            r = run(root, "--check")
            check("--check refuses a receipt that no longer matches",
                  r.returncode != 0, (r.stdout + r.stderr)[-300:])
            check("...and says what it would cost",
                  "every start" in r.stderr, r.stderr[-300:])

    # ------------------------------------------------------------------
    # 24. every way of making the middleware INERT must be caught
    #
    # This project has twice shipped a patch that was present and reported
    # success while doing nothing: the 0.4.0 backend patch that targeted files
    # which did not exist, and the 0.7.0 model guard that accepted
    # `if False: _vb_enforce_model_size(...)`. A substring test for the marker
    # would pass every mutation below. The AST assertion is what stops them,
    # and this scenario is what stops the AST assertion from being weakened.
    # ------------------------------------------------------------------
    scenario("24. a fallback that is present but inert is rejected")
    MUTATIONS = [
        ("the marker only appears in a comment",
         lambda t: RE_FALLBACK_BLOCK.sub(
             "        # _vb_spa_deep_links: removed\n"
             "        # '/models' '/stories' '/voices' '/audio' '/server'\n", t)),
        ("the decorator was removed",
         lambda t: re.sub(r'[ \t]*@app\.middleware\("http"\)\n', "", t)),
        ("the guard was ANDed with False",
         lambda t: t.replace('request.method in ("GET", "HEAD")',
                             'False and request.method in ("GET", "HEAD")')),
        ("the guard is a constant",
         lambda t: RE_GUARD.sub("        if False:", t)),
        ("the guard was replaced by something unrelated",
         lambda t: t.replace(
             "request.url.path.rstrip(\"/\") in _VB_SPA_ROUTES",
             "request.url.path.startswith(\"/zzz\")")),
        ("it never returns the app",
         lambda t: t.replace(
             '            return FileResponse(_VB_SPA_INDEX, media_type="text/html")',
             "            pass")),
        ("a route was dropped from the set",
         lambda t: t.replace("'/models', ", "")),
        # The cache middleware gets the same treatment. Every one of these
        # leaves the marker in the file and the add-on working, and every one
        # brings the stale-bundle bug back.
        ("the cache middleware is only a comment",
         lambda t: RE_CACHE_BLOCK.sub(
             "        # _vb_no_stale_frontend: removed\n", t)),
        ("the cache guard was ORed with a constant",
         lambda t: t.replace('_vb_path in _VB_NO_CACHE_PATHS',
                             'False or _vb_path in _VB_NO_CACHE_PATHS')),
        ("the cache guard is a constant",
         lambda t: t.replace(
             "        _vb_path = request.url.path\n        if (",
             "        _vb_path = request.url.path\n        if False and (")),
        ("it matches, but never sets the header",
         lambda t: t.replace(
             '            response.headers["Cache-Control"] = "no-cache"',
             "            pass")),
        ("the header is set to something that still allows reuse",
         lambda t: t.replace('= "no-cache"', '= "public, max-age=31536000"')),
        ("the cache middleware is defined but never registered",
         lambda t: t.replace(
             '    @app.middleware("http")\n'
             "    async def _vb_no_stale_frontend",
             "    async def _vb_no_stale_frontend")),
    ]
    for title, mutate in MUTATIONS:
        with tempfile.TemporaryDirectory() as td:
            root = make_fixture(pathlib.Path(td))
            setup = run(root)
            check(f"[{title}] setup patched cleanly",
                  setup.returncode == 0, setup.stderr[-200:])
            before = backend_of(root)
            after = mutate(before)
            check(f"[{title}] the mutation changed something", after != before)
            with open(root / "backend" / "main.py", "w",
                      encoding="utf-8", newline="") as fh:
                fh.write(after)
            r = run(root, "--check")
            check(f"[{title}] --check rejects it", r.returncode != 0,
                  (r.stdout + r.stderr)[-250:])

    # ------------------------------------------------------------------
    # 25. the build's runtime check
    #
    # An AST assertion proves the patch is WRITTEN correctly. It cannot prove
    # the decorator RAN - a branch not taken, an import that failed quietly, a
    # shadowed module. verify-runtime.py imports the patched backend and looks
    # at app.user_middleware, which is the only place that distinction shows.
    # It runs as a Docker build step, so it has to be exercised here.
    # ------------------------------------------------------------------
    scenario("25. the runtime check catches what the AST cannot")
    runtime = HERE / "voicebox" / "verify-runtime.py"
    if not runtime.is_file():
        skip("verify-runtime.py is missing")
    elif not _has_server_libs():
        skip("fastapi/starlette not installed - cannot import the patched app")
    else:
        def live_fixture(td: str, main_py: str = REAL_MAIN_PY) -> pathlib.Path:
            """A fixture whose mount branch is actually taken.

            main.py mounts Path(__file__).parent.parent / "web" / "dist", so
            without that directory the `if` is False, the decorator never runs
            and NOTHING is registered - which is precisely what this check
            exists to detect. Create it so the good case is genuinely good.
            """
            root = make_fixture(pathlib.Path(td), main_py=main_py)
            dist = root / "web" / "dist"
            dist.mkdir(parents=True)
            (dist / "index.html").write_text("<div id=root></div>",
                                             encoding="utf-8")
            return root

        def run_runtime(root: pathlib.Path) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(runtime), str(root)],
                capture_output=True, text=True)

        def rewrite(root: pathlib.Path, text: str) -> None:
            with open(root / "backend" / "main.py", "w",
                      encoding="utf-8", newline="") as fh:
                fh.write(text)

        with tempfile.TemporaryDirectory() as td:
            root = live_fixture(td)
            setup = run(root)
            check("setup patched cleanly", setup.returncode == 0,
                  setup.stderr[-200:])
            r = run_runtime(root)
            check("a correctly patched backend passes", r.returncode == 0,
                  (r.stdout + r.stderr)[-400:])
            check("...and reports the routes it covers",
                  "/models" in r.stdout, r.stdout[-200:])

        with tempfile.TemporaryDirectory() as td:
            # The shipped backend/main.py uses RELATIVE imports, so it can only
            # be imported as `backend.main` with the app root on sys.path.
            # Importing it as a top-level `main` raises "attempted relative
            # import with no known parent package".
            #
            # This case exists because the fixture above has no relative import,
            # so it loads happily under either style - and a build was therefore
            # published with the wrong one. The fixture was more forgiving than
            # the image, which is the only kind of test failure that matters.
            rel = REAL_MAIN_PY.replace(
                "from pathlib import Path",
                "from pathlib import Path\nfrom .settings import MARKER", 1)
            check("the relative-import fixture differs", rel != REAL_MAIN_PY)
            root = live_fixture(td, main_py=rel)
            (root / "backend" / "settings.py").write_text(
                "MARKER = 1\n", encoding="utf-8")
            setup = run(root)
            check("a backend with relative imports patches cleanly",
                  setup.returncode == 0, setup.stderr[-200:])
            r = run_runtime(root)
            check("...and is imported as a package, not a loose module",
                  r.returncode == 0, (r.stdout + r.stderr)[-400:])
            check("...so no 'no known parent package' error",
                  "no known parent package" not in (r.stdout + r.stderr),
                  r.stderr[-300:])

        with tempfile.TemporaryDirectory() as td:
            # A decorator that does nothing. The function is still defined with
            # the right name, the guard is intact, the route set is intact -
            # every AST assertion in patch-frontend.py still passes. Only
            # importing the module shows it was never registered. This is the
            # exact shape of the two inert patches this project has shipped.
            root = live_fixture(td)
            run(root)
            text = backend_of(root)
            inert = text.replace('@app.middleware("http")',
                                 "@(lambda _f: _f)", 1)
            check("the inert fixture differs from the good one", inert != text)
            rewrite(root, inert)
            r = run_runtime(root)
            check("a decorator that never registers is caught",
                  r.returncode != 0, (r.stdout + r.stderr)[-400:])
            check("...and says it is the inert failure again",
                  "inert" in r.stderr or "NOT registered" in r.stderr,
                  r.stderr[-300:])

        with tempfile.TemporaryDirectory() as td:
            # Registered, runs, matches nothing.
            root = live_fixture(td)
            run(root)
            text = backend_of(root)
            emptied = RE_ROUTESET.sub("    _VB_SPA_ROUTES = frozenset()", text)
            check("the empty-route fixture differs", emptied != text)
            rewrite(root, emptied)
            r = run_runtime(root)
            check("an empty route set is caught", r.returncode != 0,
                  (r.stdout + r.stderr)[-300:])

        with tempfile.TemporaryDirectory() as td:
            # The frontend is gone at runtime, so the mount branch is skipped
            # and the middleware is never reached. An image built this way
            # would serve no UI at all; better to fail the build.
            root = live_fixture(td)
            run(root)
            for f in sorted((root / "web" / "dist").iterdir()):
                f.unlink()
            (root / "web" / "dist").rmdir()
            r = run_runtime(root)
            check("a missing web/dist is caught, not silently unmounted",
                  r.returncode != 0, (r.stdout + r.stderr)[-300:])

    # ------------------------------------------------------------------
    # 26. the microphone guard
    #
    # Reported symptom: clicking "Create Voice" replaced the whole panel with
    # "Something went wrong! Cannot read properties of undefined (reading
    # 'getUserMedia')". Not the record button - the live waveform behind it,
    # which runs on mount.
    #
    # navigator.mediaDevices is [SecureContext]. Over http:// the property does
    # not exist, so the call throws a synchronous TypeError. The upstream
    # .catch() only sees REJECTED PROMISES, so the throw escapes the effect and
    # React unmounts the tree. Home Assistant is normally reached over http on a
    # LAN address, and a private IP is not a secure context, so this is the
    # common case rather than an edge one.
    # ------------------------------------------------------------------
    scenario("26. the microphone preview cannot crash the panel")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        r = run(root)
        js = (entry_js(root)).read_text(encoding="utf-8")
        check("exits 0", r.returncode == 0, r.stderr)
        check("says what it guarded",
              "microphone preview" in r.stdout, r.stdout[-300:])
        check("the bare dereference is gone",
              "let m=null;return navigator.mediaDevices.getUserMedia" not in js)
        check("a guard now precedes the call",
              "if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia)" in js)
        check("the guard returns before the call",
              js.index("if(!navigator.mediaDevices")
              < js.index("return navigator.mediaDevices.getUserMedia"))
        check("it explains itself in the console",
              "not a secure context" in js)
        # The happy path must be untouched: on https the stream is still
        # requested, still stored, and the tracks are still stopped on unmount.
        check("the real call survives",
              "navigator.mediaDevices.getUserMedia({audio:!0,video:!1})" in js)
        check("the stream is still stored", ".then(v=>{m=v,h(v)})" in js)
        check("the original catch survives",
              "Could not access microphone for visualization:" in js)
        check("the cleanup still stops tracks",
              "m&&m.getTracks().forEach(v=>{v.stop()})" in js)

        # Braces must balance across the inserted block, or the bundle is not
        # parseable JavaScript and NOTHING renders - a far worse failure than
        # the one being fixed.
        seg_start = js.index("if(!navigator.mediaDevices")
        seg = js[seg_start:js.index("return navigator.mediaDevices.getUserMedia")]
        check("the inserted block balances its braces",
              seg.count("{") == seg.count("}"), seg)
        check("the inserted block balances its parens",
              seg.count("(") == seg.count(")"), seg)

    with tempfile.TemporaryDirectory() as td:
        # Idempotency: the patcher runs again on every build, and a second guard
        # wrapped around the first would be harmless but is a sign the regex no
        # longer describes what is in the file.
        root = make_fixture(pathlib.Path(td))
        run(root)
        first = (entry_js(root)).read_text(encoding="utf-8")
        r = run(root)
        again = (entry_js(root)).read_text(encoding="utf-8")
        check("a second run exits 0", r.returncode == 0, r.stderr)
        check("says it was already guarded",
              "already guarded" in r.stdout, r.stdout[-300:])
        check("the bundle is byte-identical", first == again)
        check("there is exactly one guard",
              again.count("if(!navigator.mediaDevices||") == 1)

    with tempfile.TemporaryDirectory() as td:
        # Fail closed. If upstream restructures this effect the regex stops
        # matching, and a silent no-op ships a UI that dies on open. The only
        # symptom is an opaque "Something went wrong!", so the build must stop.
        root = make_fixture(
            pathlib.Path(td),
            js=REAL_JS.replace(REAL_MIC_JS, "p.useEffect(()=>{if(!d)return},[d]);"),
        )
        r = run(root)
        check("a bundle with no recognisable preview fails the build",
              r.returncode != 0, (r.stdout + r.stderr)[-300:])
        check("...and names the symptom it would have shipped",
              "getUserMedia" in r.stderr, r.stderr[-400:])
        check("...and says what to do about it",
              "RE_MIC_PREVIEW" in r.stderr, r.stderr[-400:])

    # A patcher that "succeeds" while leaving the crash in place must be caught
    # by the build, not by a human noticing a dead panel weeks later. Each
    # mutation below leaves RE_MIC_PREVIEW unable to match - so the NEGATIVE
    # check alone reports success - while reintroducing the bug completely.
    #
    # The first of these was written as `returncode != 0 or "if(false&&" in js`
    # and a review pointed out that the right-hand side is true by construction:
    # the assertion could not fail, and it was quietly documenting that the
    # mutant SURVIVED. It really did survive, all the way through verify(). That
    # is what motivated the positive RE_MIC_GUARDED assertion; these now assert
    # a non-zero exit and nothing else.
    # Targets must be CONTIGUOUS in the source. MIC_PREVIEW_PATCHED is built by
    # joining several r'...' literals, so a string that reads as one piece in
    # the emitted JavaScript can be split across a line break in the file - two
    # of these were originally written that way and matched nothing at all,
    # which the "appears exactly once" check below is here to catch.
    MIC_MUTATIONS = [
        ("the guard is ANDed with a constant",
         "if(!navigator.mediaDevices||", "if(false&&"),
        ("the guard tests something unrelated",
         "!navigator.mediaDevices||!navigator.mediaDevices",
         "window.__vb_never&&window.__vb_never"),
        ("the guard warns but never bails out",
         'disabled.");return}', 'disabled.");}'),
        # Found by attacking the positive regex rather than by writing a test
        # for behaviour already believed correct. Both of these were ACCEPTED by
        # earlier versions of RE_MIC_GUARDED, and both were then confirmed by
        # execution to reproduce the exact reported crash. A regex that merely
        # looks for the WORD "return" somewhere in the body is not enough.
        ("the bail-out is inside a branch that never runs",
         'disabled.");return}', 'disabled.");if(0)return}'),
        ("the word return only appears inside the message",
         'disabled.");return}', 'disabled. no return}'),
    ]
    src = SCRIPT.read_text(encoding="utf-8")
    for label, frm, to in MIC_MUTATIONS:
        # The mutation must be unambiguous, or it might be editing the comment
        # or the positive regex rather than the emitted template.
        check(f"[{label}] the target appears exactly once",
              src.count(frm) == 1, f"count={src.count(frm)}")
        with tempfile.TemporaryDirectory() as td:
            root = make_fixture(pathlib.Path(td))
            broken = src.replace(frm, to, 1)
            check(f"[{label}] the mutated patcher really differs", broken != src)
            mutant = pathlib.Path(td) / "mutant.py"
            mutant.write_text(broken, encoding="utf-8")
            r = subprocess.run([sys.executable, str(mutant), str(root)],
                               capture_output=True, text=True)
            check(f"[{label}] the build fails", r.returncode != 0,
                  (r.stdout + r.stderr)[-300:])
            check(f"[{label}] ...and says the guard cannot fire",
                  "microphone guard" in r.stderr or "still dereferences" in r.stderr,
                  r.stderr[-300:])

    # ------------------------------------------------------------------
    # 27. the guarded effect is EXECUTED, not just pattern-matched
    #
    # Every assertion above is a string search over text the patcher itself
    # emitted. None of them proves the emitted JavaScript actually behaves. The
    # bug being fixed was precisely a semantic one - code that LOOKS like it
    # handles a missing microphone (it has a .catch()) but does not, because the
    # throw is synchronous - so pattern-matching is the wrong instrument.
    #
    # This runs the real effect body out of the fixture bundle under two
    # navigators: one with no mediaDevices (plain http) and one with it (https).
    # ------------------------------------------------------------------
    scenario("27. the guarded effect really survives a missing microphone")
    node = shutil.which("node")
    if not node:
        skip("node is not installed, so the emitted JavaScript is never run")
    else:
        with tempfile.TemporaryDirectory() as td:
            root = make_fixture(pathlib.Path(td))
            before = (entry_js(root)).read_text(
                encoding="utf-8")
            r = run(root)
            check("exits 0", r.returncode == 0, r.stderr)
            after = (entry_js(root)).read_text(
                encoding="utf-8")

            harness = pathlib.Path(td) / "harness.js"
            harness.write_text(HARNESS_JS, encoding="utf-8")
            (pathlib.Path(td) / "before.js").write_text(before, encoding="utf-8")
            (pathlib.Path(td) / "after.js").write_text(after, encoding="utf-8")
            p = subprocess.run([node, str(harness), td],
                               capture_output=True, text=True)
            out = p.stdout
            check("the harness ran", p.returncode == 0 or out, p.stderr[-400:])
            # The bug must be REPRODUCED first, or the fix proves nothing.
            check("unpatched really does throw over http",
                  "before/http=THREW" in out, out)
            check("...with the exact reported message",
                  "reading 'getUserMedia'" in out, out)
            check("patched does NOT throw over http",
                  "after/http=OK" in out, out)
            check("unpatched works over https", "before/https=OK" in out, out)
            check("patched still works over https", "after/https=OK" in out, out)
            # The cleanup contract: React needs a function or undefined, never
            # a promise, or it will try to call it on unmount.
            check("the guard path returns undefined, not a promise",
                  "after/http/ret=undefined" in out, out)
            check("the https path still returns the cleanup function",
                  "after/https/ret=function" in out, out)

    # ------------------------------------------------------------------
    # 28. cache-busting: the asset URL changes when its bytes change
    #
    # Measured through Graham's Cloudflare tunnel, the browser is handed
    # Cache-Control: max-age=14400 for the entry bundle - a four-hour browser
    # cache - no matter that the add-on itself sends no-cache. index.html is
    # NOT cached (cf-cache-status: DYNAMIC), so a genuinely fresh page keeps
    # pointing at a bundle the browser already holds and will not re-request.
    # Headers cannot fix that; only the URL can. Upstream's Vite build names
    # the entry chunk from upstream's sources, so it is byte-identical across
    # every add-on release while we rewrite its contents on every build.
    #
    # These assertions are about the PROPERTY that makes it work - the name
    # tracks the content - not about any particular hash.
    # ------------------------------------------------------------------
    scenario("28. assets are fingerprinted so a stale cache cannot win")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        r = run(root)
        check("exits 0", r.returncode == 0, r.stderr)

        assets = root / "frontend" / "dist" / "assets"
        index = (root / "frontend/dist/index.html").read_text(encoding="utf-8")

        js = entry_js(root)
        check("the entry bundle was renamed",
              re.fullmatch(r"index-DPaWep75\.vb[0-9a-f]{8}\.js", js.name) is not None,
              js.name)
        # The digest is recomputed here from the file's bytes, in the test, with
        # no reference to the patcher's own helper. If the two disagree the name
        # is decorative and the cache would never be busted.
        want = hashlib.sha256(js.read_bytes()).hexdigest()[:8]
        check("the fingerprint really is a digest of the shipped bytes",
              f".vb{want}." in js.name, f"{js.name} vs {want}")

        check("index.html points at the fingerprinted name",
              f'src="assets/{js.name}"' in index, index[:400])
        css = sorted(assets.glob("index-*.css"))
        check("the stylesheet is fingerprinted too",
              len(css) == 1 and ".vb" in css[0].name, [p.name for p in css])
        check("index.html points at the fingerprinted stylesheet",
              f'href="assets/{css[0].name}"' in index, index[:400])

        # The old URL must be GONE, not merely superseded. A file left behind at
        # the old name would keep serving the stale bundle to every browser that
        # still has the old index.html - which is the exact failure being fixed.
        check("the un-fingerprinted name no longer exists on disk",
              not (assets / "index-DPaWep75.js").exists())
        check("nothing still references the un-fingerprinted name",
              'assets/index-DPaWep75.js"' not in index)

        # Idempotency. The build runs the patcher over a tree it may have
        # patched before; a name that grew a second suffix each time would
        # break the moment anything re-ran.
        r2 = run(root)
        check("a second run exits 0", r2.returncode == 0, r2.stderr)
        js2 = entry_js(root)
        check("the name is stable across runs", js2.name == js.name,
              f"{js.name} -> {js2.name}")
        check("suffixes do not stack", js2.name.count(".vb") == 1, js2.name)
        check("index.html is unchanged by the second run",
              (root / "frontend/dist/index.html").read_text(encoding="utf-8") == index)

    # The whole point: different bytes MUST produce a different URL. A constant
    # or name-derived fingerprint would satisfy every assertion above and still
    # ship the same URL forever.
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        check("baseline exits 0", run(root).returncode == 0)
        first = entry_js(root).name

    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td), js=REAL_JS + "\n// one more byte\n")
        check("modified build exits 0", run(root).returncode == 0)
        second = entry_js(root).name

    check("changing the bundle changes its URL", first != second,
          f"{first} == {second}")

    # verify() must reject a tree whose names have drifted from their contents,
    # because that is what a disabled rename step, or an edit made after the
    # rename, actually looks like on disk.
    scenario("28b. verify() rejects a fingerprint that no longer matches")
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        check("patch exits 0", run(root).returncode == 0)
        js = entry_js(root)
        js.write_text(js.read_text(encoding="utf-8") + "\n// edited after\n",
                      encoding="utf-8")
        r = run(root, "--check")
        check("--check fails on a stale fingerprint", r.returncode != 0,
              (r.stdout + r.stderr)[-300:])
        check("...and says the fingerprint does not match its bytes",
              "does not match its" in (r.stdout + r.stderr),
              (r.stdout + r.stderr)[-300:])

    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        check("patch exits 0", run(root).returncode == 0)
        idx = root / "frontend/dist/index.html"
        idx.write_text(
            idx.read_text(encoding="utf-8").replace(
                entry_js(root).name, "index-DPaWep75.vbdeadbeef.js"),
            encoding="utf-8")
        r = run(root, "--check")
        check("--check fails when the referenced asset is missing",
              r.returncode != 0, (r.stdout + r.stderr)[-300:])
        check("...and says the page would load nothing",
              "not on disk" in (r.stdout + r.stderr), (r.stdout + r.stderr)[-300:])

    # Fail-closed: a chunk that imports the entry by name would 404 after a
    # rename, so the patcher must refuse rather than ship a broken bundle.
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td))
        (root / "frontend/dist/assets/lazy.js").write_text(
            'import("./index-DPaWep75.js")', encoding="utf-8")
        r = run(root)
        check("refuses to rename a chunk another bundle imports by name",
              r.returncode != 0, (r.stdout + r.stderr)[-300:])
        check("...and names the dynamic import as the reason",
              "dynamic import" in (r.stdout + r.stderr), (r.stdout + r.stderr)[-300:])

    # ------------------------------------------------------------------
    # 28c. the fingerprinting is LOAD-BEARING
    #
    # Everything above passes just as happily against a patcher that renames
    # nothing, if the assertions are read but the code is not run. Each mutation
    # disables one part of the mechanism and the build must go red. Two of these
    # are caught only because verify() recomputes the digest with a different
    # spelling than the rename step used.
    # ------------------------------------------------------------------
    scenario("28c. a build that stops busting caches cannot pass")
    FP_MUTATIONS = [
        ("the rename never happens",
         "        (assets / name).rename(assets / new_name)\n",
         "        pass\n"),
        ("the fingerprint is a constant, so every release shares a URL",
         '    return hashlib.sha256(data).hexdigest()[:FINGERPRINT_LEN]',
         '    return "deadbeef"'),
        ("fingerprinting is never called",
         "        notes += fingerprint_assets(frontend)\n",
         ""),
        ("index.html is left pointing at the old name",
         """        html = html.replace(f'assets/{name}"', f'assets/{new_name}"')""",
         """        html = html.replace(f'assets/{name}"', f'assets/{name}"')"""),
        # The one edit that used to defeat BOTH sides at once. fingerprint_assets()
        # and verify() once shared this regex, so narrowing the alternation hid the
        # entry bundle from the rename step and from the check meant to catch the
        # rename step failing - and the build reported success with the stale-cache
        # bug fully intact. verify() now derives its own work list from the
        # assets/ path rather than the attribute name, so it still sees what the
        # rename step was made blind to.
        ("only href is recognised, so the entry bundle is never renamed",
         'RE_INDEX_ASSET = re.compile(r\'(?:src|href)="assets/([^"]+)"\')',
         'RE_INDEX_ASSET = re.compile(r\'(?:href)="assets/([^"]+)"\')'),
        ("only src is recognised, so the stylesheet is never renamed",
         'RE_INDEX_ASSET = re.compile(r\'(?:src|href)="assets/([^"]+)"\')',
         'RE_INDEX_ASSET = re.compile(r\'(?:src)="assets/([^"]+)"\')'),
    ]
    src = SCRIPT.read_text(encoding="utf-8")
    for label, frm, to in FP_MUTATIONS:
        # A target that matches nothing would make the mutation a no-op and the
        # "build fails" assertion meaningless. Two of the mic mutations were
        # silently doing exactly that until this precondition was added.
        check(f"[{label}] the target appears exactly once",
              src.count(frm) == 1, f"count={src.count(frm)}")
        with tempfile.TemporaryDirectory() as td:
            root = make_fixture(pathlib.Path(td))
            broken = src.replace(frm, to, 1)
            check(f"[{label}] the mutated patcher really differs", broken != src)
            mutant = pathlib.Path(td) / "mutant.py"
            mutant.write_text(broken, encoding="utf-8")
            r = subprocess.run([sys.executable, str(mutant), str(root)],
                               capture_output=True, text=True)
            check(f"[{label}] the build fails", r.returncode != 0,
                  (r.stdout + r.stderr)[-300:])
            check(f"[{label}] ...and says why the cache would go stale",
                  any(w in (r.stdout + r.stderr) for w in
                      ("fingerprint", "not on disk", "update the reference")),
                  (r.stdout + r.stderr)[-300:])

    # ------------------------------------------------------------------
    # 28d. an asset named twice is renamed once, not renamed then lost
    #
    # A <link rel="modulepreload"> beside the <script> is ordinary Vite output.
    # Acting on the reference list without de-duplicating it renamed the file on
    # the first pass and then went looking for it again on the second, which
    # failed AFTER the rename had already happened - leaving files under new
    # names and index.html still naming the old ones.
    # ------------------------------------------------------------------
    scenario("28d. an asset referenced twice does not wedge the tree")
    DUP_INDEX = REAL_INDEX.replace(
        '    <script type="module" crossorigin src="/assets/index-DPaWep75.js"></script>\n',
        '    <link rel="modulepreload" crossorigin href="/assets/index-DPaWep75.js">\n'
        '    <script type="module" crossorigin src="/assets/index-DPaWep75.js"></script>\n')
    check("the fixture really does name the asset twice",
          DUP_INDEX.count("assets/index-DPaWep75.js") == 2)
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td), index=DUP_INDEX)
        r = run(root)
        check("exits 0", r.returncode == 0, (r.stdout + r.stderr)[-400:])
        idx = (root / "frontend/dist/index.html").read_text(encoding="utf-8")
        js = entry_js(root)
        check("both references were updated",
              idx.count(f"assets/{js.name}") == 2, idx[:500])
        check("no reference to the old name survives",
              'assets/index-DPaWep75.js"' not in idx, idx[:500])
        check("--check passes on the result", run(root, "--check").returncode == 0)

    # ------------------------------------------------------------------
    # 28e. a refusal leaves the tree exactly as it was found
    #
    # The dynamic-import guard fires part-way through the asset list. If renames
    # were applied as we went, the assets ahead of the failure would already be
    # renamed on disk while index.html still named them the old way - and every
    # later run would then fail looking up names that no longer exist, with the
    # original cause long since removed. A failed build must be retryable.
    # ------------------------------------------------------------------
    scenario("28e. a refusal is recoverable, not a wedged tree")
    CSS_FIRST = REAL_INDEX.replace(
        '    <script type="module" crossorigin src="/assets/index-DPaWep75.js"></script>\n'
        '    <link rel="stylesheet" crossorigin href="/assets/index-DfXZrYe2.css">\n',
        '    <link rel="stylesheet" crossorigin href="/assets/index-DfXZrYe2.css">\n'
        '    <script type="module" crossorigin src="/assets/index-DPaWep75.js"></script>\n')
    check("the fixture really does list the stylesheet first",
          CSS_FIRST.index("index-DfXZrYe2.css") < CSS_FIRST.index("index-DPaWep75.js"))
    with tempfile.TemporaryDirectory() as td:
        root = make_fixture(pathlib.Path(td), index=CSS_FIRST)
        assets = root / "frontend" / "dist" / "assets"
        (assets / "lazy.js").write_text('import("./index-DPaWep75.js")',
                                        encoding="utf-8")
        before_names = sorted(p.name for p in assets.iterdir())
        before_index = (root / "frontend/dist/index.html").read_text(encoding="utf-8")

        r = run(root)
        check("the build fails", r.returncode != 0, (r.stdout + r.stderr)[-300:])
        check("...naming the dynamic import as the reason",
              "dynamic import" in (r.stdout + r.stderr), (r.stdout + r.stderr)[-300:])
        check("NOTHING on disk was renamed",
              sorted(p.name for p in assets.iterdir()) == before_names,
              sorted(p.name for p in assets.iterdir()))
        # patch_index() runs first and legitimately rewrites index.html (absolute
        # paths made relative, base snippet injected) before fingerprinting is
        # even reached, so the file is not expected to be byte-identical. What
        # must not have happened is a fingerprinted name being committed while
        # the matching rename was refused - that is the inconsistency that made
        # the tree unrecoverable.
        after_index = (root / "frontend/dist/index.html").read_text(encoding="utf-8")
        check("no fingerprinted name was written to index.html",
              ".vb" not in after_index, after_index[:500])
        check("index.html still names the assets that are still on disk",
              'assets/index-DPaWep75.js"' in after_index
              and 'assets/index-DfXZrYe2.css"' in after_index, after_index[:500])

        # Remove the cause and the very same tree must now build.
        (assets / "lazy.js").unlink()
        r2 = run(root)
        check("the retry succeeds once the cause is removed",
              r2.returncode == 0, (r2.stdout + r2.stderr)[-400:])
        check("--check passes on the retried result",
              run(root, "--check").returncode == 0)

    print("\n" + "=" * 60)
    tail = f", {SKIPPED} SKIPPED" if SKIPPED else ""
    print(f"RESULT: {PASSED} passed, {FAILED} failed{tail}")
    if SKIPPED and not os.environ.get("VOICEBOX_TEST_ALLOW_SKIPS"):
        print(
            "\nA scenario did not run. The runtime probe and the real-backend\n"
            "build-order scenario are the only ones that catch a patch which\n"
            "is present but inert, so a skip is treated as a failure. Install\n"
            "fastapi + starlette and extract .imgsrc, or set\n"
            "VOICEBOX_TEST_ALLOW_SKIPS=1 to accept reduced coverage."
        )
        return 1
    return 1 if FAILED else 0


if __name__ == "__main__":
    if not SCRIPT.is_file():
        print(f"cannot find {SCRIPT}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
