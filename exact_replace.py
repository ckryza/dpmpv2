#!/usr/bin/env python3
import sys
from pathlib import Path

def die(msg: str, code: int = 2):
    print(msg, file=sys.stderr)
    sys.exit(code)

def main():
    if len(sys.argv) != 4:
        die("Usage: exact_replace.py <file> <FIND> <REPLACE>\n"
            "Example: ./exact_replace.py dpmp/dpmpv2.py 'old' 'new'")

    file_path = Path(sys.argv[1])
    find = sys.argv[2]
    repl = sys.argv[3]

    if not file_path.exists():
        die(f"ERROR: file not found: {file_path}")

    s = file_path.read_text(encoding="utf-8", errors="strict")
    count = s.count(find)

    if count != 1:
        die(f"ERROR: Expected exactly 1 match, found {count}.\n"
            f"File: {file_path}\n"
            f"FIND (repr): {find!r}")

    out = s.replace(find, repl)
    file_path.write_text(out, encoding="utf-8", errors="strict")
    print("OK: Replaced exactly 1 match.")

if __name__ == "__main__":
    main()
