#!/usr/bin/env python3
"""
Enforce a model-size policy on the Voicebox backend, at image build time.

WHY THIS EXISTS
---------------
Voicebox ships two TTS models. On this hardware only one of them fits:

    0.6B    peak 4220 MB    loaded in 26 s      works
    1.7B    peak ~8130 MB   OOM-killed twice    does not fit

Upstream defaults to 1.7B in four separate places, so the *default* action of
both the web UI and the REST API is the one that cannot work here. Home
Assistant's Supervisor sets oom_score_adj=200 on add-on containers, which
biases the kernel to kill add-ons first - so an oversized Voicebox does not
merely kill itself, it puts Frigate (the NVR) at risk.

WHAT IT CHANGES
---------------
Every TTS load in this image funnels through
PyTorchTTSBackend.load_model_async, so one guard there covers /generate,
/models/load and /models/download alike. The rest are defaults, changed so the
common path never has to hit the guard:

    backends/pytorch_backend.py  inject the policy helpers
    backends/pytorch_backend.py  __init__ default          1.7B -> policy
    backends/pytorch_backend.py  load_model_async          + guard
    models.py                    GenerationRequest default 1.7B -> 0.6B
    main.py                      /generate fallback        1.7B -> 0.6B
    main.py                      /models/load query default 1.7B -> 0.6B

main.py:1463 - the explicit load_model("1.7B") in the download flow - is left
alone deliberately. The guard turns it into a clean HTTP error, which is the
correct answer to "download and load a model that does not fit".

The policy is read from VOICEBOX_ALLOWED_MODEL_SIZES at runtime, which run.sh
sets from the add-on's allow_large_model option. Default is 0.6B only.

WHY IT FAILS THE BUILD RATHER THAN WARNING
------------------------------------------
A previous patch in this repository targeted files that do not exist in this
image. It matched nothing, exited non-zero, and the failure was swallowed
because its RUN was chained with ";". The image shipped unpatched and was
OOM-killed in production.

So this script:

  * pins the SHA-256 of all three files as they exist in the pinned image, and
    refuses to touch anything if a hash has moved;
  * locates its targets through the AST, not by string search, so the two
    identically-named load_model_async methods cannot be confused;
  * asserts every edit matches exactly once;
  * compiles every result before writing anything;
  * writes a receipt, and has a separate --verify mode that re-checks the
    result behaviourally.

It is invoked from the Dockerfile in exec form, so no shell can mask its exit
status.
"""

import argparse
import ast
import hashlib
import json
import os
import sys

RECEIPT = "/app/.voicebox-model-policy.json"

# Upstream defect, unrelated to memory but repaired here because this is the
# one place that already owns models.py and main.py.
#
#   models.py  HistoryQuery.limit is Field(ge=1, le=100)
#   main.py    list_history declares `limit: int = 50` with NO validation, then
#              constructs HistoryQuery(limit=limit) INSIDE the handler body
#
# FastAPI only converts a ValidationError into a 422 while parsing the request.
# Raised inside the handler it is just an unhandled exception, so /history with
# any limit above 100 returns HTTP 500. The shipped frontend hardcodes
# limit=1000, so the History view 500s on every load and takes the UI down with
# it - which looks exactly like "ingress is broken". Verified live on the target
# host: limit=100 -> 200, limit=101 -> 500.
#
# Fixed on both sides: widen the cap to what the frontend actually asks for, and
# clamp in the handler so no input can reach the 500 path at all.
HISTORY_LIMIT_MAX = 1000


# The guard on PyTorchTTSBackend.load_model_async covers every load that the
# BUNDLED backend performs. It does not cover the external-provider subsystem,
# which is a genuine second route to 1.7B:
#
#   main.py:1591            POST /providers/start   (unauthenticated)
#   providers/__init__.py   starts an installed provider EXECUTABLE
#                           and swaps the active provider for LocalProvider
#   providers/local.py:28   LocalProvider._current_model_size = "1.7B"
#   providers/local.py:118  its load_model_async only RECORDS the size
#   providers/local.py:46   generate() posts that size to the external process
#
# So a started provider would run 1.7B in a SEPARATE process - outside the
# guard, and outside any memory accounting this add-on can do. Guarding
# LocalProvider is not sufficient either, because the external server has its
# own default and its own loader.
#
# The provider download currently 404s, but a 404 is not a safety boundary: it
# could be restored upstream at any time, and a binary may already be present.
#
# So under the restrictive policy the two WRITE endpoints are refused outright.
# The read endpoints, /providers/stop and DELETE are left alone - they cannot
# start anything.
MAIN_POLICY_BLOCK = """
# --- injected by the Voicebox add-on: external provider policy ---
import os as _vb_os

_VB_PROVIDER_REFUSAL = (
    "Voicebox add-on: external TTS providers are disabled on this host. A "
    "provider runs as a separate process that this add-on cannot bound, and "
    "the bundled provider client defaults to the 1.7B model, which needs "
    "about 8.1 GB of RAM and has been OOM-killed on this machine. Home "
    "Assistant biases the kernel to kill add-ons first, so starting one also "
    "risks other add-ons such as Frigate. Set allow_large_model: true in the "
    "add-on configuration if this host really can spare the memory."
)


def _vb_external_providers_allowed():
    raw = _vb_os.environ.get("VOICEBOX_ALLOWED_MODEL_SIZES", "0.6B")
    return "1.7B" in [p.strip() for p in raw.split(",") if p.strip()]


# --- end injected block ---
"""

PROVIDER_GUARD = [
    "    if not _vb_external_providers_allowed():",
    "        raise HTTPException(status_code=403, detail=_VB_PROVIDER_REFUSAL)",
]

# SHA-256 of each file as shipped in the image this add-on is built against
# (ghcr.io/jamiepine/voicebox@sha256:b7e39a79...9532, amd64). Verified by
# extracting the 72 KB layer that carries app/backend and hashing it directly.
EXPECTED_BEFORE = {
    "models.py":
        "bc34c4959a86f9b7d51e7199e0e823ec8f9fccbfa9cc9d047fed3dedbac2b38f",
    "main.py":
        "367d73e2bbd238b0b1e1c0b6cdbb79da58524e1eeadf1a2638e0abfaa354620e",
    "backends/pytorch_backend.py":
        "849d57bcb01cca6afa04ebed1eb220a3f18385cd4710e36e2d4784d2b593f9d4",
}

POLICY_BLOCK = '''
# --- Home Assistant add-on: model size policy (injected at build time) -------
# The 1.7B model needs ~8.1 GB on this host and is OOM-killed; the 0.6B model
# peaks at ~4.2 GB and works. Every TTS load funnels through
# PyTorchTTSBackend.load_model_async, so the guard there is the whole control.
import os as _vb_os

_VB_FALLBACK_ALLOWED = "0.6B"


def _vb_allowed_model_sizes():
    """Model sizes this host is permitted to load, most-preferred first."""
    raw = _vb_os.environ.get("VOICEBOX_ALLOWED_MODEL_SIZES") or _VB_FALLBACK_ALLOWED
    sizes = [s.strip() for s in raw.split(",") if s.strip()]
    return sizes or [_VB_FALLBACK_ALLOWED]


def _vb_default_model_size():
    return _vb_allowed_model_sizes()[0]


def _vb_enforce_model_size(model_size):
    allowed = _vb_allowed_model_sizes()
    if model_size in allowed:
        return model_size
    raise ValueError(
        "Voicebox add-on: the %s model is not permitted on this host "
        "(allowed: %s). The 1.7B model needs about 8.1 GB of RAM and has been "
        "OOM-killed on this machine. Home Assistant biases the kernel to kill "
        "add-ons first, so loading it also risks other add-ons such as "
        "Frigate. Pass model_size=0.6B, or set allow_large_model: true in the "
        "add-on configuration if this host really can spare the memory."
        % (model_size, ", ".join(allowed))
    )
# --- end Home Assistant add-on policy ---------------------------------------
'''


class PolicyError(RuntimeError):
    """Anything that must stop the build."""


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read(root, rel):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        raise PolicyError(
            "%s does not exist. The image layout has changed; re-verify the "
            "patch targets before shipping." % path
        )
    # newline="" on both read and write: without it, Python translates on
    # Windows and the patched file comes out CRLF while the container produces
    # LF. That makes local runs non-reproducible against production bytes.
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def replace_line(src, lineno, expect, new, what):
    """Replace one 1-based line, asserting its current content."""
    lines = src.split("\n")
    if lineno < 1 or lineno > len(lines):
        raise PolicyError("%s: line %d out of range" % (what, lineno))
    actual = lines[lineno - 1]
    if actual != expect:
        raise PolicyError(
            "%s: line %d is not what was expected.\n  expected: %r\n  actual:   %r"
            % (what, lineno, expect, actual)
        )
    lines[lineno - 1] = new
    return "\n".join(lines)


def insert_after(src, lineno, new_lines, what):
    lines = src.split("\n")
    if lineno < 1 or lineno > len(lines):
        raise PolicyError("%s: line %d out of range" % (what, lineno))
    return "\n".join(lines[:lineno] + new_lines + lines[lineno:])


def extract_block(src, start_marker, end_marker, what):
    """Pull an injected block back out of a patched file, verbatim.

    Behavioural checks must run the code that will actually ship. Running this
    script's own constant instead proves only that the constant is correct.
    """
    i = src.find(start_marker)
    if i < 0:
        raise PolicyError("%s: injected block start marker is missing" % what)
    if src.find(start_marker, i + 1) >= 0:
        raise PolicyError("%s: injected block start marker appears twice" % what)
    j = src.find(end_marker, i)
    if j < 0:
        raise PolicyError("%s: injected block end marker is missing" % what)
    return src[i:j]


def is_docstring(node):
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def stmt_index(fn, pred):
    """Index of the first top-level statement in fn whose subtree matches pred.

    Used to assert ORDER. Checking only that a call exists somewhere in a
    function is not enough: a guard placed after the load it is meant to
    prevent would satisfy an existence check and prevent nothing.
    """
    for i, st in enumerate(fn.body):
        for node in ast.walk(st):
            if pred(node):
                return i
    return None


def calls_named(name):
    return lambda n: isinstance(n, ast.Call) and getattr(n.func, "id", None) == name


def attr_named(name):
    return lambda n: isinstance(n, ast.Attribute) and n.attr == name


def find_class(tree, name, what):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise PolicyError("%s: class %s not found" % (what, name))


def find_method(cls, name, what):
    found = [
        n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    ]
    if len(found) != 1:
        raise PolicyError(
            "%s: expected exactly one %s.%s, found %d"
            % (what, cls.name, name, len(found))
        )
    return found[0]


def patch_pytorch_backend(src):
    """Inject the policy helpers, default to it, and guard the loader."""
    tree = ast.parse(src)
    cls = find_class(tree, "PyTorchTTSBackend", "pytorch_backend.py")

    # --- the guard -----------------------------------------------------
    # There are two load_model_async methods in this file: the TTS one here
    # and a Whisper one further down. Resolving through the class is what
    # keeps them apart; a string search would not.
    loader = find_method(cls, "load_model_async", "pytorch_backend.py")
    body = [n for n in loader.body if not isinstance(n, ast.Expr)]
    if not body or not isinstance(body[0], ast.If):
        raise PolicyError(
            "pytorch_backend.py: load_model_async no longer begins with the "
            "'if model_size is None' block the guard is anchored to."
        )
    none_check = body[0]
    if ast.unparse(none_check.test) != "model_size is None":
        raise PolicyError(
            "pytorch_backend.py: load_model_async first test is %r, expected "
            "'model_size is None'." % ast.unparse(none_check.test)
        )

    # --- the constructor default ---------------------------------------
    init = find_method(cls, "__init__", "pytorch_backend.py")
    if not init.args.defaults or not isinstance(init.args.defaults[-1], ast.Constant):
        raise PolicyError("pytorch_backend.py: __init__ has no literal default")
    if init.args.defaults[-1].value != "1.7B":
        raise PolicyError(
            "pytorch_backend.py: __init__ default is %r, expected '1.7B'"
            % (init.args.defaults[-1].value,)
        )

    # Apply bottom-up so earlier line numbers stay valid.
    src = insert_after(
        src, none_check.end_lineno,
        ["", "        _vb_enforce_model_size(model_size)"],
        "pytorch_backend.py guard",
    )
    src = replace_line(
        src, init.lineno + 2,
        "        self.model_size = model_size",
        "        self.model_size = (\n"
        "            _vb_default_model_size() if model_size is None else model_size\n"
        "        )",
        "pytorch_backend.py __init__ body",
    )
    src = replace_line(
        src, init.lineno,
        '    def __init__(self, model_size: str = "1.7B"):',
        "    def __init__(self, model_size: str = None):",
        "pytorch_backend.py __init__ signature",
    )

    # --- the helpers ---------------------------------------------------
    last_import = max(
        n.end_lineno for n in tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom))
    )
    src = insert_after(
        src, last_import, POLICY_BLOCK.split("\n"), "pytorch_backend.py policy block"
    )
    return src


def patch_models(src):
    tree = ast.parse(src)
    cls = find_class(tree, "GenerationRequest", "models.py")
    target = None
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "model_size":
            target = node
    if target is None:
        raise PolicyError("models.py: GenerationRequest.model_size not found")
    old = '    model_size: Optional[str] = Field(default="1.7B", pattern="^(1\\\\.7B|0\\\\.6B)$")'
    new = '    model_size: Optional[str] = Field(default="0.6B", pattern="^(1\\\\.7B|0\\\\.6B)$")'
    src = replace_line(src, target.lineno, old, new, "models.py model_size default")

    # --- upstream /history 500 (see HISTORY_LIMIT_MAX) ---
    hq = find_class(tree, "HistoryQuery", "models.py")
    lims = [
        n for n in hq.body
        if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == "limit"
    ]
    if len(lims) != 1:
        raise PolicyError(
            "models.py: expected exactly one HistoryQuery.limit, found %d" % len(lims)
        )
    src = replace_line(
        src, lims[0].lineno,
        "    limit: int = Field(default=50, ge=1, le=100)",
        "    limit: int = Field(default=50, ge=1, le=%d)" % HISTORY_LIMIT_MAX,
        "models.py HistoryQuery.limit cap",
    )
    return src


def patch_main(src):
    tree = ast.parse(src)

    def sole(name):
        fns = [
            n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ]
        if len(fns) != 1:
            raise PolicyError(
                "main.py: expected exactly one %s, found %d" % (name, len(fns))
            )
        return fns[0]

    route = sole("load_model")          # /models/load query-parameter default
    gen = sole("generate_speech")       # /generate fallback
    hist = sole("list_history")         # /history 500
    start_p = sole("start_provider")    # /providers/start
    down_p = sole("download_provider_endpoint")  # /providers/download

    fallbacks = [
        n for n in ast.walk(gen)
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and getattr(n.targets[0], "id", None) == "model_size"
        and isinstance(n.value, ast.BoolOp)
    ]
    if len(fallbacks) != 1:
        raise PolicyError(
            "main.py: expected exactly one 'model_size = ... or ...' in "
            "generate_speech, found %d" % len(fallbacks)
        )

    calls = [
        n for n in ast.walk(hist)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "HistoryQuery"
    ]
    if len(calls) != 1:
        raise PolicyError(
            "main.py: expected exactly one models.HistoryQuery(...) in "
            "list_history, found %d" % len(calls)
        )

    def sole_kw(call, arg):
        kws = [k for k in call.keywords if k.arg == arg]
        if len(kws) != 1:
            raise PolicyError(
                "main.py: expected exactly one %s= keyword in the HistoryQuery "
                "call, found %d" % (arg, len(kws))
            )
        return kws[0]

    kw_limit = sole_kw(calls[0], "limit")
    kw_offset = sole_kw(calls[0], "offset")

    # --- line replacements first: these do not shift any line numbers -----
    src = replace_line(
        src, fallbacks[0].lineno,
        '        model_size = data.model_size or "1.7B"',
        '        model_size = data.model_size or "0.6B"',
        "main.py /generate fallback",
    )
    src = replace_line(
        src, route.lineno,
        'async def load_model(model_size: str = "1.7B"):',
        'async def load_model(model_size: str = "0.6B"):',
        "main.py /models/load default",
    )
    # Both HistoryQuery kwargs are clamped. offset was missed on the first
    # pass: offset=-1 hits ge=0 and produces exactly the same 500 as limit did.
    src = replace_line(
        src, kw_limit.value.lineno,
        "        limit=limit,",
        "        limit=max(1, min(limit, %d))," % HISTORY_LIMIT_MAX,
        "main.py /history limit clamp",
    )
    src = replace_line(
        src, kw_offset.value.lineno,
        "        offset=offset,",
        "        offset=max(0, offset),",
        "main.py /history offset clamp",
    )

    # --- insertions last, BOTTOM-UP, so earlier line numbers stay valid ---
    body_start = lambda fn: (
        fn.body[0].end_lineno
        if isinstance(fn.body[0], ast.Expr)
        and isinstance(fn.body[0].value, ast.Constant)
        and isinstance(fn.body[0].value.value, str)
        else fn.body[0].lineno - 1
    )

    guards = sorted(
        [(body_start(down_p), "download_provider_endpoint"),
         (body_start(start_p), "start_provider")],
        reverse=True,
    )
    for lineno, what in guards:
        src = insert_after(src, lineno, PROVIDER_GUARD, "main.py %s guard" % what)

    # The helper block goes above everything, so it is inserted last.
    imports = [
        n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
    if not imports:
        raise PolicyError("main.py: no top-level imports found")
    src = insert_after(
        src, max(n.end_lineno for n in imports),
        MAIN_POLICY_BLOCK.split("\n"),
        "main.py policy block",
    )
    return src


PATCHERS = {
    "backends/pytorch_backend.py": patch_pytorch_backend,
    "models.py": patch_models,
    "main.py": patch_main,
}


def apply(root):
    if os.path.isfile(RECEIPT):
        # Do not take the receipt's word for it. A stale or planted receipt
        # would otherwise turn --apply into a silent no-op that reports
        # success - which is exactly how the previous attempt failed.
        print("receipt already present; verifying rather than trusting it")
        return verify(root)
        print("model policy already applied (%s exists); nothing to do" % RECEIPT)
        return 0

    # 1. read and verify every original before touching anything
    originals = {}
    for rel, want in EXPECTED_BEFORE.items():
        src = read(root, rel)
        got = sha256(src)
        if got != want:
            raise PolicyError(
                "%s has changed upstream.\n"
                "  expected sha256 %s\n"
                "  actual   sha256 %s\n"
                "The model-size patch was written against the previous "
                "contents and MUST be re-verified against the new ones before "
                "this add-on can be built. Do not simply update the hash: the "
                "1.7B model OOM-kills this host, and a patch that silently "
                "stops applying is how that happened before." % (rel, want, got)
            )
        originals[rel] = src

    # 2. compute every change in memory
    patched = {}
    for rel, src in originals.items():
        patched[rel] = PATCHERS[rel](src)

    # 3. compile every result before a single byte is written
    for rel, src in patched.items():
        try:
            compile(src, rel, "exec")
        except SyntaxError as exc:
            raise PolicyError("%s: patched source does not compile: %s" % (rel, exc))

    # 4. only now write
    for rel, src in patched.items():
        with open(os.path.join(root, rel), "w", encoding="utf-8", newline="") as fh:
            fh.write(src)
        print("patched %s" % rel)

    receipt = {
        "before": EXPECTED_BEFORE,
        "after": {rel: sha256(src) for rel, src in patched.items()},
    }
    with open(RECEIPT, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=2, sort_keys=True)
    print("model policy applied; receipt at %s" % RECEIPT)
    return 0


def verify(root):
    if not os.path.isfile(RECEIPT):
        raise PolicyError(
            "%s is missing - the model-size policy was never applied. Refusing "
            "to ship an image whose default model OOM-kills the host." % RECEIPT
        )
    with open(RECEIPT, "r", encoding="utf-8") as fh:
        receipt = json.load(fh)

    for rel, want in receipt["after"].items():
        got = sha256(read(root, rel))
        if got != want:
            raise PolicyError(
                "%s does not match the receipt (%s != %s)" % (rel, got, want)
            )

    checks = []

    # --- behavioural: the policy helpers actually refuse 1.7B ----------
    # Exec the block AS IT SITS IN THE PATCHED FILE, not this script's own
    # constant. Exec'ing the constant would only prove the patcher's source is
    # correct - it would pass even if the deployed file had been neutered.
    ns = {}
    exec(compile(extract_block(
        read(root, "backends/pytorch_backend.py"),
        "# --- Home Assistant add-on: model size policy",
        "# --- end Home Assistant add-on policy",
        "backends/pytorch_backend.py"), "policy", "exec"), ns)
    saved = os.environ.pop("VOICEBOX_ALLOWED_MODEL_SIZES", None)
    try:
        assert ns["_vb_default_model_size"]() == "0.6B", "default is not 0.6B"
        assert ns["_vb_enforce_model_size"]("0.6B") == "0.6B", "0.6B was refused"
        try:
            ns["_vb_enforce_model_size"]("1.7B")
        except ValueError:
            pass
        else:
            raise AssertionError("1.7B was permitted by default")
        checks.append("policy refuses 1.7B and permits 0.6B by default")

        os.environ["VOICEBOX_ALLOWED_MODEL_SIZES"] = "0.6B,1.7B"
        assert ns["_vb_enforce_model_size"]("1.7B") == "1.7B", "opt-in did not work"
        assert ns["_vb_default_model_size"]() == "0.6B", "opt-in changed the default"
        checks.append("allow_large_model opt-in permits 1.7B but keeps 0.6B default")
    finally:
        os.environ.pop("VOICEBOX_ALLOWED_MODEL_SIZES", None)
        if saved is not None:
            os.environ["VOICEBOX_ALLOWED_MODEL_SIZES"] = saved

    # --- behavioural: GenerationRequest really defaults to 0.6B --------
    # models.py imports only pydantic/typing/datetime, so it loads standalone.
    ns = {}
    exec(compile(read(root, "models.py"), "models.py", "exec"), ns)
    req = ns["GenerationRequest"](profile_id="p", text="t")
    if req.model_size != "0.6B":
        raise PolicyError(
            "GenerationRequest defaults to %r, expected '0.6B'" % (req.model_size,)
        )
    checks.append("GenerationRequest.model_size defaults to 0.6B")

    # --- structural: the guard is present, in the right method ---------
    tree = ast.parse(read(root, "backends/pytorch_backend.py"))
    cls = find_class(tree, "PyTorchTTSBackend", "pytorch_backend.py")
    loader = find_method(cls, "load_model_async", "pytorch_backend.py")
    g = stmt_index(loader, calls_named("_vb_enforce_model_size"))
    if g is None:
        raise PolicyError(
            "PyTorchTTSBackend.load_model_async does not call "
            "_vb_enforce_model_size - the guard is not in the load path."
        )
    # Existence is not enough. A guard that runs AFTER the load it is meant to
    # prevent would satisfy an existence check and prevent nothing, so assert
    # it precedes both the load and the unload.
    for attr, why in (
        ("_load_model_sync", "the model would already have been loaded"),
        ("unload_model", "a refused request would still have unloaded the "
                         "working model"),
    ):
        i = stmt_index(loader, attr_named(attr))
        if i is not None and g >= i:
            raise PolicyError(
                "PyTorchTTSBackend.load_model_async calls _vb_enforce_model_size "
                "at statement %d but %s at statement %d - %s."
                % (g, attr, i, why)
            )
    checks.append(
        "load_model_async guard precedes both _load_model_sync and unload_model")

    init = find_method(cls, "__init__", "pytorch_backend.py")
    if any(
        isinstance(d, ast.Constant) and d.value == "1.7B" for d in init.args.defaults
    ):
        raise PolicyError("PyTorchTTSBackend.__init__ still defaults to 1.7B")
    checks.append("PyTorchTTSBackend.__init__ no longer defaults to 1.7B")

    # --- structural: no 1.7B defaults left in the routes ---------------
    tree = ast.parse(read(root, "main.py"))
    for name in ("load_model", "generate_speech"):
        fns = [
            n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ]
        if len(fns) != 1:
            raise PolicyError("main.py: %s is no longer unique" % name)
        for node in ast.walk(fns[0]):
            if isinstance(node, ast.Constant) and node.value == "1.7B":
                raise PolicyError(
                    "main.py: %s still contains a literal '1.7B' at line %d"
                    % (name, node.lineno)
                )
    checks.append("main.py /generate and /models/load contain no 1.7B literal")

    # --- /history no longer 500s above the old cap ---------------------
    hq = ns["HistoryQuery"]
    if hq(limit=HISTORY_LIMIT_MAX).limit != HISTORY_LIMIT_MAX:
        raise PolicyError("HistoryQuery rejected limit=%d" % HISTORY_LIMIT_MAX)
    checks.append("HistoryQuery accepts limit=%d (frontend asks for 1000)" % HISTORY_LIMIT_MAX)

    hists = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "list_history"
    ]
    if len(hists) != 1:
        raise PolicyError("main.py: list_history is no longer unique")
    hq = [
        n for n in ast.walk(hists[0])
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "HistoryQuery"
    ]
    if len(hq) != 1:
        raise PolicyError("main.py: HistoryQuery call in list_history is no longer unique")
    # Assert the EXACT clamp expression. "some min() appears somewhere" would
    # be satisfied by an unrelated min() and prove nothing.
    want = {
        "limit": "max(1, min(limit, %d))" % HISTORY_LIMIT_MAX,
        "offset": "max(0, offset)",
    }
    for arg, expected in want.items():
        kws = [k for k in hq[0].keywords if k.arg == arg]
        if len(kws) != 1:
            raise PolicyError("main.py: HistoryQuery %s= is not unique" % arg)
        got = ast.unparse(kws[0].value)
        if got != expected:
            raise PolicyError(
                "main.py: HistoryQuery %s= is %r, expected %r - an out-of-range "
                "value would still reach the 500 path." % (arg, got, expected)
            )
    checks.append("main.py /history clamps both limit and offset exactly")

    # --- external providers are refused before they can start -------------
    for fname, route in (
        ("start_provider", "/providers/start"),
        ("download_provider_endpoint", "/providers/download"),
    ):
        fns = [
            n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fname
        ]
        if len(fns) != 1:
            raise PolicyError("main.py: %s is no longer unique" % fname)
        i = stmt_index(fns[0], calls_named("_vb_external_providers_allowed"))
        if i is None:
            raise PolicyError(
                "main.py: %s has no provider policy check. An external provider "
                "runs 1.7B in a separate process, outside the backend guard."
                % route
            )
        first = 1 if is_docstring(fns[0].body[0]) else 0
        if i != first:
            raise PolicyError(
                "main.py: the %s policy check is at statement %d, expected %d "
                "- work would happen before it." % (route, i, first)
            )
    checks.append("/providers/start and /providers/download refuse before doing work")

    # behavioural: the provider helper honours the same env var
    ns2 = {}
    exec(compile(extract_block(
        read(root, "main.py"),
        "# --- injected by the Voicebox add-on: external provider policy ---",
        "# --- end injected block ---",
        "main.py"), "mainpolicy", "exec"), ns2)
    saved = os.environ.pop("VOICEBOX_ALLOWED_MODEL_SIZES", None)
    try:
        if ns2["_vb_external_providers_allowed"]():
            raise PolicyError("external providers are permitted by default")
        os.environ["VOICEBOX_ALLOWED_MODEL_SIZES"] = "0.6B,1.7B"
        if not ns2["_vb_external_providers_allowed"]():
            raise PolicyError("allow_large_model did not re-enable providers")
    finally:
        os.environ.pop("VOICEBOX_ALLOWED_MODEL_SIZES", None)
        if saved is not None:
            os.environ["VOICEBOX_ALLOWED_MODEL_SIZES"] = saved
    checks.append("external providers are refused by default, allowed only on opt-in")

    for line in checks:
        print("  OK  %s" % line)
    print("model policy verified")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--root", default="/app/backend")
    ap.add_argument("--receipt", default=None)
    args = ap.parse_args()

    global RECEIPT
    if args.receipt:
        RECEIPT = args.receipt

    if args.apply == args.verify:
        ap.error("give exactly one of --apply or --verify")

    try:
        return apply(args.root) if args.apply else verify(args.root)
    except (PolicyError, AssertionError) as exc:
        sys.stderr.write("\nMODEL POLICY FAILED\n%s\n\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())