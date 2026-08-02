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
        check("load_model_async is guarded" in v.stdout,
              "verify reports the loader guard")

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
    print("\n4. the guard runs before anything is unloaded")
    with Sandbox(source) as s:
        s.apply()
        src = s.read("backends/pytorch_backend.py")
        guard = src.index("_vb_enforce_model_size(model_size)")
        unload = src.index("self.unload_model()")
        check(guard < unload,
              "guard precedes unload_model, so a refusal cannot drop a working model")

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
        check("already applied" in a.stdout, "  ...and says it did nothing")
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
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())