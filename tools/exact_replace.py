#!/usr/bin/env python3
"""
Exact-match replacer:
- Replaces ONLY when the "needle" text occurs EXACTLY once in the target file.
- Fails loudly otherwise (0 matches or >1 matches).
Usage:
  python3 tools/exact_replace.py <target_file> <needle_file> <replacement_file>
"""
from __future__ import annotations
import sys
from pathlib import Path

def die(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)

def main() -> None:
    if len(sys.argv) != 4:
        die("Usage: python3 tools/exact_replace.py <target_file> <needle_file> <replacement_file>", 2)

    target_path = Path(sys.argv[1])
    needle_path = Path(sys.argv[2])
    repl_path   = Path(sys.argv[3])

    if not target_path.exists():
        die(f"Target file not found: {target_path}")
    if not needle_path.exists():
        die(f"Needle file not found: {needle_path}")
    if not repl_path.exists():
        die(f"Replacement file not found: {repl_path}")

    target = target_path.read_text(encoding="utf-8")
    needle = needle_path.read_text(encoding="utf-8")
    repl   = repl_path.read_text(encoding="utf-8")

    if needle == "":
        die("Needle is empty (refusing to replace).")

    count = target.count(needle)
    if count != 1:
        die(f"Needle match count is {count}, expected exactly 1. No changes made.")

    new_text = target.replace(needle, repl, 1)

    # Sanity: ensure file actually changed
    if new_text == target:
        die("Replacement produced no change (unexpected).")

    target_path.write_text(new_text, encoding="utf-8")
    print("OK: Replaced exactly 1 match.")

if __name__ == "__main__":
    main()
