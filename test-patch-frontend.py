#!/usr/bin/env python3
"""Tests for patch-frontend.py.

The fixtures below are not invented: index.html and the getBaseUrl()/request()
snippet are copied verbatim from the running Voicebox container
(ghcr.io/jamiepine/voicebox:latest, built 2026-02-03). If upstream republishes
and these stop matching, that is exactly the signal we want.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "voicebox" / "patch-frontend.py"

PASSED = 0
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
)

# A router construction, for fixtures that supply their own JS.
ROUTER_JS = ";zJ=Y3({routeTree:$J});"

# The same bundle with no router at all, for the fail-closed case.
JS_NO_ROUTER = REAL_JS.replace("Y3({routeTree:$J})", "Y3(void 0)")


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))


def make_fixture(tmp: pathlib.Path, index: str = REAL_INDEX, js: str = REAL_JS,
                 assets: bool = True) -> pathlib.Path:
    root = tmp / "app"
    front = root / "frontend" / "dist"
    front.mkdir(parents=True)
    (front / "index.html").write_text(index, encoding="utf-8")
    if assets:
        (front / "assets").mkdir()
        (front / "assets" / "index-DPaWep75.js").write_text(js, encoding="utf-8")
        (front / "assets" / "index-DfXZrYe2.css").write_text("body{}", encoding="utf-8")
    return root


def run(root: pathlib.Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root), *extra],
        capture_output=True, text=True,
    )


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
        js = (root / "frontend/dist/assets/index-DPaWep75.js").read_text(encoding="utf-8")

        check("asset src made relative", 'src="assets/index-DPaWep75.js"' in index)
        check("stylesheet href made relative", 'href="assets/index-DfXZrYe2.css"' in index)
        check("favicon made relative", 'href="vite.svg"' in index)
        check("no absolute src=/href= left", not re.search(r'(?:src|href)="/(?!/)', index))
        check("base snippet injected", "__VB_BASE__" in index)
        check("snippet precedes the module script",
              index.index("__VB_BASE__") < index.index("index-DPaWep75.js"))
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
                  + ROUTER_JS)
        root = make_fixture(pathlib.Path(td), js=spaced)
        r = run(root)
        js = (root / "frontend/dist/assets/index-DPaWep75.js").read_text(encoding="utf-8")
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
        js = (root / "frontend/dist/assets/index-DPaWep75.js").read_text(encoding="utf-8")
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
        js = (root / "frontend/dist/assets/index-DPaWep75.js").read_text(encoding="utf-8")
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
        first = (root / "frontend/dist/assets/index-DPaWep75.js").read_text(encoding="utf-8")
        again = run(root)
        second = (root / "frontend/dist/assets/index-DPaWep75.js").read_text(encoding="utf-8")
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
        js = (root / "frontend/dist/assets/index-DPaWep75.js").read_text(encoding="utf-8")
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
        js = (root / "frontend/dist/assets/index-DPaWep75.js").read_text(encoding="utf-8")
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
        js = (root / "frontend/dist/assets/index-DPaWep75.js").read_text(encoding="utf-8")
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
        run(root)
        jsp = root / "frontend/dist/assets/index-DPaWep75.js"
        jsp.write_text(
            jsp.read_text(encoding="utf-8").replace(
                ',basepath:window.__VB_BASE__||"/"', ""),
            encoding="utf-8")
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

    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    if not SCRIPT.is_file():
        print(f"cannot find {SCRIPT}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
