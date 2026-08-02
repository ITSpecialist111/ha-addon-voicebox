#!/usr/bin/env python3
"""
Tests for enforce-model-policy.py.

The point of this suite is NOT to show the patch works on a good day - apply
and verify passing is the easy half. It is to show the patch FAILS LOUDLY when
its targets move, because the previous attempt in this repository failed
silently and shipped an image that OOM-killed the host.

Every "moved target" scenario below must produce a non-zero exit.

Run against the real backend source extracted from the pinned image:

    python test-enforce-model-policy.py ../.imgsrc/app/backend
"""

import ast
import io
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "enforce-model-policy.py")

PASS = 0
FAIL = 0


def check(ok, what, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ok   %s" % what)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (what, detail))


# Exact text of the injected fragments, used by the mutation tests.
GUARD_CALL = "        _vb_enforce_model_size(model_size)\n"
LOAD_CALL = "        await asyncio.to_thread(self._load_model_sync, model_size)\n"
UNLOAD_CALL = "            self.unload_model()\n"
PROVIDER_GUARD_TXT = (
    "    if not _vb_external_providers_allowed():\n"
    "        raise HTTPException(status_code=403, detail=_VB_PROVIDER_REFUSAL)\n"
)


def run(root, mode, receipt):
    return subprocess.run(
        [sys.executable, SCRIPT, mode, "--root", root, "--receipt", receipt],
        capture_output=True, text=True,
    )


class Sandbox:
    """A throwaway copy of the real backend tree."""

    def __init__(self, source):
        self.source = source

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="vbpolicy-")
        self.root = os.path.join(self.dir, "backend")
        shutil.copytree(self.source, self.root)
        self.receipt = os.path.join(self.dir, "receipt.json")
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, rel):
        return os.path.join(self.root, rel)

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as fh:
            return fh.read()

    def write(self, rel, text):
        with open(self.path(rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def sub(self, rel, old, new, expected=1, count=-1):
        """Replace text, asserting how many occurrences exist first.

        count=-1 replaces all; count=1 replaces only the first, which matters
        for anchors that appear in both the TTS and the Whisper backend.
        """
        src = self.read(rel)
        assert src.count(old) == expected, (
            "fixture setup: %r appears %d times in %s, expected %d"
            % (old, src.count(old), rel, expected)
        )
        self.write(rel, src.replace(old, new, count))

    def reseal(self, rel):
        """Refresh one 'after' hash in the receipt.

        A mutation test that only tripped the hash gate would tell us nothing
        about the semantic assertions, because the hash gate catches every
        edit indiscriminately. Resealing forces the assertion to do the work.
        """
        with io.open(self.receipt, encoding="utf-8") as fh:
            r = json.load(fh)
        with io.open(self.path(rel), "rb") as fh:
            r["after"][rel] = hashlib.sha256(fh.read()).hexdigest()
        with io.open(self.receipt, "w", encoding="utf-8") as fh:
            json.dump(r, fh, indent=2)

    def apply(self):
        return run(self.root, "--apply", self.receipt)

    def verify(self):
        return run(self.root, "--verify", self.receipt)


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "..", ".imgsrc", "app", "backend"
    )
    source = os.path.abspath(source)
    if not os.path.isdir(source):
        print("cannot find backend source at %s" % source)
        print("extract it with the command in the repository README, or pass a path")
        return 2

    print("backend source: %s" % source)

    # ---------------------------------------------------------------
    print("\n1. the happy path")
    with Sandbox(source) as s:
        a = s.apply()
        check(a.returncode == 0, "apply succeeds on pristine source", a.stderr)
        v = s.verify()
        check(v.returncode == 0, "verify succeeds after apply", v.stderr)
        check("GenerationRequest.model_size defaults to 0.6B" in v.stdout,
              "verify reports the GenerationRequest default")
        check("load_model_async guard precedes" in v.stdout,
              "verify reports that the loader guard is correctly ORDERED")

    # ---------------------------------------------------------------
    print("\n2. the patched result is sane Python")
    with Sandbox(source) as s:
        s.apply()
        for rel in ("models.py", "main.py", "backends/pytorch_backend.py"):
            r = subprocess.run(
                [sys.executable, "-c",
                 "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())",
                 s.path(rel)],
                capture_output=True, text=True)
            check(r.returncode == 0, "%s parses after patching" % rel, r.stderr)

    # ---------------------------------------------------------------
    print("\n3. only the TTS loader is guarded, not Whisper")
    with Sandbox(source) as s:
        s.apply()
        probe = (
            "import ast,sys\n"
            "t=ast.parse(open(sys.argv[1],encoding='utf-8').read())\n"
            "out=[]\n"
            "for c in t.body:\n"
            "    if isinstance(c,ast.ClassDef):\n"
            "        for m in c.body:\n"
            "            if isinstance(m,(ast.FunctionDef,ast.AsyncFunctionDef)) and m.name=='load_model_async':\n"
            "                g=any(isinstance(n,ast.Call) and getattr(n.func,'id',None)=='_vb_enforce_model_size' for n in ast.walk(m))\n"
            "                out.append('%s=%s'%(c.name,g))\n"
            "print(','.join(out))\n"
        )
        r = subprocess.run([sys.executable, "-c", probe,
                            s.path("backends/pytorch_backend.py")],
                           capture_output=True, text=True)
        check("PyTorchTTSBackend=True" in r.stdout, "TTS loader is guarded", r.stdout)
        check("PyTorchSTTBackend=False" in r.stdout,
              "Whisper loader is NOT guarded (its sizes are base/small/...)", r.stdout)

    # ---------------------------------------------------------------
    print("\n4. the guard runs before the load AND before the unload")
    # This used to use src.index("_vb_enforce_model_size(model_size)"), which
    # matched the function DEFINITION inside the injected block near the top of
    # the file - always before everything, so the test passed no matter where
    # the guard call actually sat. It proved nothing. Located via AST now.
    with Sandbox(source) as s:
        s.apply()
        tree = ast.parse(s.read("backends/pytorch_backend.py"))
        cls = [n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "PyTorchTTSBackend"]
        check(len(cls) == 1, "PyTorchTTSBackend is unique")
        fn = [n for n in cls[0].body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "load_model_async"]
        check(len(fn) == 1, "load_model_async is unique within PyTorchTTSBackend")

        def idx(pred):
            for i, st in enumerate(fn[0].body):
                if any(pred(n) for n in ast.walk(st)):
                    return i
            return None

        g = idx(lambda n: isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "_vb_enforce_model_size")
        ld = idx(lambda n: isinstance(n, ast.Attribute) and n.attr == "_load_model_sync")
        ul = idx(lambda n: isinstance(n, ast.Attribute) and n.attr == "unload_model")
        check(g is not None, "the guard CALL (not its definition) is in the loader body")
        check(ld is not None and g < ld,
              "guard precedes _load_model_sync, so a refusal allocates nothing")
        check(ul is not None and g < ul,
              "guard precedes unload_model, so a refusal cannot drop a working model")

    # ---------------------------------------------------------------
    print("\n4b. MUTATION: a verifier that cannot fail is not a verifier")
    # Each mutation refreshes the receipt hash, so the hash gate cannot be what
    # fails - these test the SEMANTIC assertions.
    mutations = (
        ("guard moved after _load_model_sync", "backends/pytorch_backend.py",
         lambda t: t.replace(GUARD_CALL, "").replace(LOAD_CALL, LOAD_CALL + GUARD_CALL, 1)),
        ("guard moved after unload_model", "backends/pytorch_backend.py",
         lambda t: t.replace(GUARD_CALL, "").replace(UNLOAD_CALL, UNLOAD_CALL + GUARD_CALL)),
        ("one provider guard deleted", "main.py",
         lambda t: t.replace(PROVIDER_GUARD_TXT, "", 1)),
        ("offset clamp reverted", "main.py",
         lambda t: t.replace("offset=max(0, offset),", "offset=offset,")),
        ("limit clamp weakened", "main.py",
         lambda t: t.replace("limit=max(1, min(limit, 1000)),", "limit=min(limit, 99999),")),
        ("policy helper neutered", "backends/pytorch_backend.py",
         lambda t: t.replace("    if model_size in allowed:\n        return model_size\n",
                             "    return model_size\n")),
    )
    for label, rel, mutate in mutations:
        with Sandbox(source) as s:
            s.apply()
            before = s.read(rel)
            after = mutate(before)
            check(after != before, "mutation '%s' actually changed the file" % label)
            s.write(rel, after)
            s.reseal(rel)
            v = s.verify()
            check(v.returncode != 0,
                  "verify REJECTS: %s" % label,
                  (v.stdout + v.stderr).strip()[-160:])

    # ---------------------------------------------------------------
    print("\n5. MOVED TARGET: upstream changes a file")
    for rel, old, new in (
        ("models.py", 'default="1.7B"', 'default="1.5B"'),
        ("main.py", 'async def load_model(model_size: str = "1.7B"):',
                    'async def load_model(model_size: str = "2.0B"):'),
        ("backends/pytorch_backend.py",
         'def __init__(self, model_size: str = "1.7B"):',
         'def __init__(self, model_size: str = "9.9B"):'),
    ):
        with Sandbox(source) as s:
            s.sub(rel, old, new)
            a = s.apply()
            check(a.returncode != 0, "apply refuses when %s changed" % rel)
            check("has changed upstream" in a.stderr,
                  "  ...and says so plainly", a.stderr[:200])
            check(not os.path.exists(s.receipt),
                  "  ...and writes no receipt")

    # ---------------------------------------------------------------
    print("\n6. MOVED TARGET: a file disappears")
    with Sandbox(source) as s:
        os.remove(s.path("models.py"))
        a = s.apply()
        check(a.returncode != 0, "apply refuses when models.py is missing")
        check("does not exist" in a.stderr, "  ...and says which file", a.stderr[:200])

    # ---------------------------------------------------------------
    print("\n7. NOTHING IS WRITTEN when any single file fails")
    # main.py is patched second; break it and confirm models.py is untouched.
    with Sandbox(source) as s:
        before = s.read("models.py")
        s.sub("main.py", 'model_size = data.model_size or "1.7B"',
                         'model_size = data.model_size')
        a = s.apply()
        check(a.returncode != 0, "apply fails when main.py's fallback is gone")
        check(s.read("models.py") == before,
              "models.py was NOT modified - all-or-nothing holds")

    # ---------------------------------------------------------------
    print("\n8. verify is not fooled")
    with Sandbox(source) as s:
        v = s.verify()
        check(v.returncode != 0, "verify fails with no receipt")
        check("never applied" in v.stderr, "  ...and explains why", v.stderr[:200])

    with Sandbox(source) as s:
        s.apply()
        s.sub("backends/pytorch_backend.py",
              "        _vb_enforce_model_size(model_size)\n", "")
        v = s.verify()
        check(v.returncode != 0, "verify fails when the guard is stripped out")

    with Sandbox(source) as s:
        s.apply()
        s.sub("models.py", 'default="0.6B"', 'default="1.7B"')
        v = s.verify()
        check(v.returncode != 0, "verify fails when a default is put back to 1.7B")

    # ---------------------------------------------------------------
    print("\n9. applying twice is a no-op, not a corruption")
    with Sandbox(source) as s:
        s.apply()
        first = s.read("backends/pytorch_backend.py")
        a = s.apply()
        check(a.returncode == 0, "second apply succeeds")
        check("verifying rather than trusting it" in a.stdout, "  ...and says it did nothing")
        check(s.read("backends/pytorch_backend.py") == first,
              "  ...and changed nothing")

    # ---------------------------------------------------------------
    print("\n10. the policy itself behaves")
    with Sandbox(source) as s:
        s.apply()
        probe = (
            "import sys,importlib.util\n"
            "spec=importlib.util.spec_from_file_location('m',sys.argv[1])\n"
            "m=importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "r=m.GenerationRequest(profile_id='p',text='t')\n"
            "print('default',r.model_size)\n"
            "try:\n"
            "    m.GenerationRequest(profile_id='p',text='t',model_size='3.0B')\n"
            "    print('pattern NOT enforced')\n"
            "except Exception:\n"
            "    print('pattern enforced')\n"
        )
        r = subprocess.run([sys.executable, "-c", probe, s.path("models.py")],
                           capture_output=True, text=True)
        check("default 0.6B" in r.stdout, "GenerationRequest defaults to 0.6B", r.stdout + r.stderr)
        check("pattern enforced" in r.stdout,
              "the original size pattern still rejects nonsense", r.stdout)

    # ---------------------------------------------------------------
    # The hash gate fires first, so the AST assertions above are unreachable
    # in the tests so far. They are the second line of defence: they catch a
    # maintainer who bumps the expected hash without re-checking the
    # structure - the lazy response to scenario 5, and the one that would put
    # the OOM back. Simulate exactly that.
    print("\n11. hash bumped WITHOUT re-checking: the AST must still refuse")

    def script_with_refreshed_hashes(sandbox, tmpdir):
        """A copy of the patcher whose EXPECTED_BEFORE matches the sandbox."""
        import hashlib
        src = io.open(SCRIPT, encoding="utf-8").read()
        for rel in ("models.py", "main.py", "backends/pytorch_backend.py"):
            digest = hashlib.sha256(
                sandbox.read(rel).encode("utf-8")).hexdigest()
            marker = '"%s":\n        "' % rel
            i = src.index(marker) + len(marker)
            j = src.index('"', i)
            src = src[:i] + digest + src[j:]
        alt = os.path.join(tmpdir, "patcher.py")
        io.open(alt, "w", encoding="utf-8").write(src)
        return alt

    import io

    for rel, old_text, new_text, why, n_expected, n_count in (
        # This anchor exists in BOTH the TTS and the Whisper loader, which is
        # precisely why the patcher resolves it through the class rather than
        # by searching. Rewrite only the TTS one.
        ("backends/pytorch_backend.py",
         "        if model_size is None:\n            model_size = self.model_size",
         "        model_size = model_size or self.model_size",
         "the anchor the guard is inserted after was rewritten",
         2, 1),
        ("main.py",
         '        model_size = data.model_size or "1.7B"',
         '        model_size = data.model_size',
         "the /generate fallback was removed",
         1, -1),
        ("main.py",
         "        limit=limit,",
         "        limit=int(limit),",
         "the /history limit keyword was rewritten",
         1, -1),
    ):
        with Sandbox(source) as s:
            s.sub(rel, old_text, new_text, expected=n_expected, count=n_count)
            alt = script_with_refreshed_hashes(s, s.dir)
            a = subprocess.run(
                [sys.executable, alt, "--apply", "--root", s.root,
                 "--receipt", s.receipt],
                capture_output=True, text=True)
            check(a.returncode != 0,
                  "refuses %s even with hashes refreshed" % why, a.stdout)
            check("MODEL POLICY FAILED" in a.stderr,
                  "  ...loudly", a.stderr[:160])
            check(not os.path.exists(s.receipt), "  ...and writes no receipt")

    # ---------------------------------------------------------------
    # Upstream returns HTTP 500 for /history?limit=101 and above, because
    # list_history builds HistoryQuery INSIDE the handler where a pydantic
    # ValidationError is no longer convertible to a 422. The frontend asks for
    # limit=1000 on every History view, so the page 500s every time.
    print("\n12. the /history 500 is repaired, and the fix is real")

    accepts_1000 = (
        "import io,sys\n"
        "ns={}\n"
        "exec(compile(io.open(sys.argv[1],encoding='utf-8').read(),'m','exec'),ns)\n"
        "try:\n"
        "    ns['HistoryQuery'](limit=1000); print('ACCEPTED')\n"
        "except Exception: print('REJECTED')\n"
    )

    # First prove the bug is real in the pristine source, so that the
    # post-patch assertion below is meaningful rather than vacuous.
    with Sandbox(source) as s:
        r = subprocess.run([sys.executable, "-c", accepts_1000, s.path("models.py")],
                           capture_output=True, text=True)
        check("REJECTED" in r.stdout,
              "unpatched source really does reject limit=1000 (this IS the 500)",
              r.stdout)

    with Sandbox(source) as s:
        s.apply()
        r = subprocess.run([sys.executable, "-c", accepts_1000, s.path("models.py")],
                           capture_output=True, text=True)
        check("ACCEPTED" in r.stdout,
              "patched source accepts the limit=1000 the frontend sends", r.stdout)

        # The clamp is the belt to the cap's braces: nothing a caller can send
        # should be able to reach the raising path at all.
        sweep = (
            "import io,sys\n"
            "ns={}\n"
            "exec(compile(io.open(sys.argv[1],encoding='utf-8').read(),'m','exec'),ns)\n"
            "HQ=ns['HistoryQuery']\n"
            "bad=[]\n"
            "for v in (-99999,-5,0,1,50,100,101,1000,1001,99999):\n"
            "    try: HQ(limit=max(1,min(v,1000)))\n"
            "    except Exception: bad.append(v)\n"
            "print('BAD=%r'%(bad,))\n"
        )
        r = subprocess.run([sys.executable, "-c", sweep, s.path("models.py")],
                           capture_output=True, text=True)
        check("BAD=[]" in r.stdout,
              "no clamped input can raise, so no input can 500", r.stdout)

        src = s.read("main.py")
        check("limit=max(1, min(limit, 1000))" in src,
              "list_history clamps before building the query")
        v = s.verify()
        check(v.returncode == 0, "verify passes with the history fix applied", v.stderr)
        check("HistoryQuery accepts limit=1000" in v.stdout,
              "verify reports the history cap")
        check("clamps both limit and offset" in v.stdout,
              "verify reports both clamps")

    # ---------------------------------------------------------------
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())