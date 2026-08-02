#!/usr/bin/env python3
"""Tests for patch-backend.py.

The fixtures are copied from upstream Voicebox's own backend source so that
they double as a canary: if upstream restructures these calls, the fixtures
stop resembling reality and that is worth noticing.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCHER = HERE / "voicebox" / "patch-backend.py"

PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}" + (f"\n         {detail}" if detail else ""))


# Verbatim shape of backend/backends/qwen_custom_voice_backend.py
CUSTOM_VOICE = '''\
import torch


class QwenCustomVoiceBackend:
    def _load_model_sync(self, model_size: str) -> None:
        from qwen_tts import Qwen3TTSModel

        model_path = self._get_model_path(model_size)

        if self.device == "cpu":
            self.model = Qwen3TTSModel.from_pretrained(
                model_path,
                torch_dtype=torch.float32,
                low_cpu_mem_usage=False,
            )
        else:
            self.model = Qwen3TTSModel.from_pretrained(
                model_path,
                device_map=self.device,
                torch_dtype=torch.bfloat16,
            )
'''

# Verbatim shape of backend/backends/qwen_llm_backend.py
LLM = '''\
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


class QwenLLMBackend:
    def _load(self, model_size, repo):
        self.tokenizer = AutoTokenizer.from_pretrained(repo)
        dtype = torch.float16 if self.device in ("cuda", "mps") else torch.float32
        self.model = AutoModelForCausalLM.from_pretrained(
            repo,
            dtype=dtype,
        )
        self.model.to(self.device)
'''

TRAILING_COMMENT = '''\
class B:
    def load(self):
        self.model = SomeModel.from_pretrained(
            path,  # the local snapshot directory
        )
'''

NO_ARGS = '''\
class B:
    def load(self):
        self.model = SomeModel.from_pretrained()
'''

NOTHING_TO_DO = '''\
class B:
    def load(self):
        self.tok = AutoTokenizer.from_pretrained(repo)
'''


def run(dirpath: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PATCHER), "--dir", str(dirpath), *extra],
        capture_output=True, text=True,
    )


def sandbox(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp(prefix="vbpatch-"))
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")
    return d


def kwarg_of(src: str, owner: str, name: str):
    """Value of keyword `name` on the from_pretrained call for `owner`."""
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "from_pretrained"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == owner):
            for kw in node.keywords:
                if kw.arg == name:
                    return kw.value
    return None


def count_calls(src: str, owner: str) -> int:
    return sum(
        1 for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "from_pretrained"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == owner
    )


print("patch-backend.py")
print()

# --- the real custom-voice backend -----------------------------------------
print("qwen_custom_voice_backend.py shape")
d = sandbox({"qwen_custom_voice_backend.py": CUSTOM_VOICE})
r = run(d)
out = (d / "qwen_custom_voice_backend.py").read_text(encoding="utf-8")
check("exits 0", r.returncode == 0, r.stderr)
check("result still parses", (lambda: [ast.parse(out), True][1])())
check("low_cpu_mem_usage flipped to True",
      "low_cpu_mem_usage=True" in out and "low_cpu_mem_usage=False" not in out)
check("float32 left alone on the CPU path",
      "torch_dtype=torch.float32" in out,
      "dtype must not change: Skylake has no bf16 hardware")
check("bfloat16 GPU path untouched", "torch_dtype=torch.bfloat16" in out)
check("device_map call did not gain the kwarg",
      out.count("low_cpu_mem_usage") == 1,
      f"found {out.count('low_cpu_mem_usage')} occurrences")
check("both from_pretrained calls survive", count_calls(out, "Qwen3TTSModel") == 2)

r2 = run(d)
out2 = (d / "qwen_custom_voice_backend.py").read_text(encoding="utf-8")
check("idempotent: second run changes nothing", out2 == out)
check("idempotent: reports 0 changed", "0 changed" in r2.stdout, r2.stdout)
shutil.rmtree(d)

# --- the real LLM backend ---------------------------------------------------
print()
print("qwen_llm_backend.py shape")
d = sandbox({"qwen_llm_backend.py": LLM})
r = run(d)
out = (d / "qwen_llm_backend.py").read_text(encoding="utf-8")
check("exits 0", r.returncode == 0, r.stderr)
check("result still parses", (lambda: [ast.parse(out), True][1])())
v = kwarg_of(out, "AutoModelForCausalLM", "low_cpu_mem_usage")
check("model load gained low_cpu_mem_usage=True",
      isinstance(v, ast.Constant) and v.value is True)
check("tokenizer load untouched",
      kwarg_of(out, "AutoTokenizer", "low_cpu_mem_usage") is None,
      "a tokenizer has no use for this and should not be edited")
check("dtype argument preserved",
      kwarg_of(out, "AutoModelForCausalLM", "dtype") is not None)
check("exactly one insertion", out.count("low_cpu_mem_usage") == 1)
shutil.rmtree(d)

# --- awkward source shapes --------------------------------------------------
print()
print("awkward call shapes")
d = sandbox({"comment_backend.py": TRAILING_COMMENT})
r = run(d)
out = (d / "comment_backend.py").read_text(encoding="utf-8")
check("trailing comment: exits 0", r.returncode == 0, r.stderr + r.stdout)
check("trailing comment: still parses",
      (lambda: [ast.parse(out), True][1])(), out)
check("trailing comment: comment preserved", "# the local snapshot" in out)
check("trailing comment: kwarg added", "low_cpu_mem_usage=True" in out)
shutil.rmtree(d)

d = sandbox({"noargs_backend.py": NO_ARGS})
r = run(d)
out = (d / "noargs_backend.py").read_text(encoding="utf-8")
check("no-arg call: exits 0", r.returncode == 0, r.stderr + r.stdout)
check("no-arg call: still parses", (lambda: [ast.parse(out), True][1])(), out)
check("no-arg call: kwarg added", "low_cpu_mem_usage=True" in out)
shutil.rmtree(d)

# --- fail-loud behaviour ----------------------------------------------------
print()
print("fails loudly rather than silently doing nothing")
d = sandbox({"tok_backend.py": NOTHING_TO_DO})
r = run(d)
check("no model loads at all -> non-zero exit", r.returncode != 0)
check("no model loads at all -> says why",
      "no CPU-path model loads" in (r.stdout + r.stderr), r.stdout + r.stderr)
shutil.rmtree(d)

d = Path(tempfile.mkdtemp(prefix="vbpatch-"))
r = run(d)
check("no *_backend.py -> non-zero exit", r.returncode != 0)
check("no *_backend.py -> says why", "no *_backend.py" in (r.stdout + r.stderr))
shutil.rmtree(d)

missing = Path(tempfile.gettempdir()) / "vbpatch-definitely-not-here"
r = run(missing)
check("missing directory -> non-zero exit", r.returncode != 0)
check("missing directory -> says why",
      "does not exist" in (r.stdout + r.stderr))

# --- --check mode -----------------------------------------------------------
print()
print("--check mode")
d = sandbox({"qwen_custom_voice_backend.py": CUSTOM_VOICE})
r = run(d, "--check")
after = (d / "qwen_custom_voice_backend.py").read_text(encoding="utf-8")
check("--check exits 0", r.returncode == 0, r.stderr)
check("--check does not write", after == CUSTOM_VOICE)
check("--check says 'would change'", "would change" in r.stdout, r.stdout)

r = run(d)
r = run(d, "--check")
check("--check after patching reports 0", "0 would change" in r.stdout, r.stdout)
shutil.rmtree(d)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)