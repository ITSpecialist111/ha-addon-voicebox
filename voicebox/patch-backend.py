#!/usr/bin/env python3
"""Stop the CPU model load from needing 8 GB of RAM.

The upstream image loads Qwen TTS on CPU like this:

    Qwen3TTSModel.from_pretrained(
        model_path,
        torch_dtype=torch.float32,
        low_cpu_mem_usage=False,
    )

`low_cpu_mem_usage=False` is the expensive part. With it disabled,
transformers builds a complete randomly-initialised model in RAM, then loads
the checkpoint as a second complete copy, then copies tensor by tensor into
the first. Peak is roughly 2-3x the model size on top of the model itself.

Measured on the target machine, loading the 0.6B model (1.74 GB of bf16
weights on disk) with the stock code reached 8127 MB resident and was killed
by the kernel OOM killer:

    Out of memory: Killed process 2148335 (uvicorn)
      total-vm:16218332kB, anon-rss:8322080kB

That is where Voicebox's "8 GB minimum" figure actually comes from. It is not
the model's requirement, it is this loading strategy's requirement.

`low_cpu_mem_usage=True` loads weights straight into a meta-device skeleton,
one tensor at a time, so peak is approximately the model size rather than a
multiple of it. It needs `accelerate`, which this image has.

What this deliberately does NOT change is the dtype. float32 is correct here:
the target CPU is Skylake, which has no AVX512-BF16, so bfloat16 arithmetic
would be emulated and slower. Halving memory by switching dtype is available
as a further step if float32 still does not fit, but it trades speed and
possibly output for it, so it is not done blindly.

Only CPU-path model loads are touched. A call carrying `device_map=` is the
accelerator path and is left exactly as upstream wrote it, and tokenizer
loads are left alone.

Fails loudly rather than silently doing nothing: if the expected calls cannot
be found, the upstream image has changed shape and a human needs to look.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

KWARG = "low_cpu_mem_usage"
DEFAULT_DIR = Path("/app/backend/backends")


def line_offsets(src: str) -> list[int]:
    """Absolute offset of the start of each line, 1-indexed by line number."""
    offsets = [0, 0]
    for line in src.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def abs_offset(offsets: list[int], lineno: int, col: int) -> int:
    return offsets[lineno] + col


def is_model_load(node: ast.AST) -> bool:
    """A from_pretrained call on something that looks like a model class."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "from_pretrained":
        return False
    owner = func.value
    name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
    return "Model" in name


def owner_name(node: ast.Call) -> str:
    owner = node.func.value
    return owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "?")


def find_targets(src: str) -> tuple[list[ast.Call], list[ast.Call]]:
    """Return (cpu_path_calls, accelerator_calls) for model loads."""
    tree = ast.parse(src)
    cpu, accel = [], []
    for node in ast.walk(tree):
        if not is_model_load(node):
            continue
        kwnames = {kw.arg for kw in node.keywords}
        (accel if "device_map" in kwnames else cpu).append(node)
    return cpu, accel


def patch_source(src: str, path: Path, verbose: bool) -> tuple[str, int]:
    """Return (new_src, edits). Edits are applied back-to-front."""
    cpu, accel = find_targets(src)

    if verbose:
        for node in accel:
            print(f"    {path.name}:{node.lineno} {owner_name(node)}"
                  f".from_pretrained(device_map=...) - accelerator path, left alone")

    edits: list[tuple[int, int, str]] = []
    for node in cpu:
        existing = next((kw for kw in node.keywords if kw.arg == KWARG), None)
        offsets = line_offsets(src)

        if existing is not None:
            value = existing.value
            already = isinstance(value, ast.Constant) and value.value is True
            if already:
                if verbose:
                    print(f"    {path.name}:{node.lineno} {owner_name(node)}"
                          f" already has {KWARG}=True")
                continue
            start = abs_offset(offsets, value.lineno, value.col_offset)
            end = abs_offset(offsets, value.end_lineno, value.end_col_offset)
            edits.append((start, end, "True"))
            if verbose:
                print(f"    {path.name}:{node.lineno} {owner_name(node)}"
                      f" {KWARG}={src[start:end]} -> True")
            continue

        # No such keyword: add one. Anchor on the end of the last existing
        # argument rather than on the closing paren, so a trailing comment or
        # a trailing comma cannot land the insertion in the wrong place.
        close = abs_offset(offsets, node.end_lineno, node.end_col_offset) - 1
        if src[close] != ")":
            raise SystemExit(
                f"FATAL: {path}:{node.lineno}: expected ')' at end of call, "
                f"found {src[close]!r}. Refusing to edit blind."
            )
        tail = [*node.args, *(kw.value for kw in node.keywords)]
        if tail:
            at = max(abs_offset(offsets, n.end_lineno, n.end_col_offset)
                     for n in tail)
            insertion = f", {KWARG}=True"
        else:
            at = close
            insertion = f"{KWARG}=True"
        edits.append((at, at, insertion))
        if verbose:
            print(f"    {path.name}:{node.lineno} {owner_name(node)}"
                  f" adding {KWARG}=True")

    for start, end, text in sorted(edits, key=lambda e: e[0], reverse=True):
        src = src[:start] + text + src[end:]
    return src, len(edits)


def verify(src: str, path: Path) -> None:
    """Every CPU-path model load must end up with low_cpu_mem_usage=True."""
    try:
        cpu, _ = find_targets(src)
    except SyntaxError as exc:
        raise SystemExit(f"FATAL: {path} no longer parses after patching: {exc}")

    for node in cpu:
        kw = next((k for k in node.keywords if k.arg == KWARG), None)
        if kw is None:
            raise SystemExit(
                f"FATAL: {path}:{node.lineno}: {KWARG} still missing after patch."
            )
        if not (isinstance(kw.value, ast.Constant) and kw.value.value is True):
            raise SystemExit(
                f"FATAL: {path}:{node.lineno}: {KWARG} is not True after patch."
            )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--check", action="store_true",
                    help="report what would change; do not write")
    args = ap.parse_args()

    if not args.dir.is_dir():
        print(f"FATAL: {args.dir} does not exist. The upstream image has been "
              f"restructured; the memory patch cannot be applied.", file=sys.stderr)
        return 1

    files = sorted(args.dir.glob("*_backend.py"))
    if not files:
        print(f"FATAL: no *_backend.py under {args.dir}.", file=sys.stderr)
        return 1

    total_edits = 0
    total_cpu_loads = 0
    for path in files:
        src = path.read_text(encoding="utf-8")
        try:
            cpu, _ = find_targets(src)
        except SyntaxError as exc:
            print(f"FATAL: {path} does not parse: {exc}", file=sys.stderr)
            return 1
        if not cpu:
            continue
        total_cpu_loads += len(cpu)
        print(f"  {path}")
        new_src, edits = patch_source(src, path, verbose=True)
        verify(new_src, path)
        total_edits += edits
        if edits and not args.check:
            path.write_text(new_src, encoding="utf-8")

    if total_cpu_loads == 0:
        print("FATAL: found no CPU-path model loads to patch. The upstream "
              "image has changed; re-check patch-backend.py against it.",
              file=sys.stderr)
        return 1

    verb = "would change" if args.check else "changed"
    print(f"patch-backend: {total_cpu_loads} CPU model load(s) found, "
          f"{total_edits} {verb}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())