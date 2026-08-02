#!/usr/bin/env python3
"""Write an inventory of the image's model-loading code.

Two speculative fixes have now been aimed at code that was assumed to be in
this image and was not. The published tag is opaque, six months old, and does
not match upstream's main branch, so anything derived from GitHub is a guess.

This records what is actually present, at build time, and run.sh prints it at
startup. It changes no behaviour - it only removes the guesswork.
"""
from __future__ import annotations

import ast
import pathlib
import sys


def call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        owner = f.value.id if isinstance(f.value, ast.Name) else (
            f.value.attr if isinstance(f.value, ast.Attribute) else "?")
        return f"{owner}.{f.attr}"
    if isinstance(f, ast.Name):
        return f.id
    return "?"


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/app/backend")
    out = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "/app/.voicebox-inventory.txt")

    lines: list[str] = []
    if not root.is_dir():
        lines.append(f"MISSING {root}")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines))
        return 0

    backends = sorted(p for p in root.rglob("*.py") if "backends" in p.parts)
    lines.append(f"backend modules under {root}: {len(backends)}")
    for p in backends:
        lines.append(f"  {p.relative_to(root)}")

    lines.append("")
    lines.append("from_pretrained call sites:")
    found = 0
    for p in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node)
            if not name.endswith(".from_pretrained"):
                continue
            found += 1
            kw = sorted(k.arg for k in node.keywords if k.arg)
            star = " **kwargs" if any(k.arg is None for k in node.keywords) else ""
            lines.append(f"  {p.relative_to(root)}:{node.lineno} {name}({', '.join(kw)}{star})")
    if not found:
        lines.append("  NONE FOUND")

    text = "\n".join(lines) + "\n"
    out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())