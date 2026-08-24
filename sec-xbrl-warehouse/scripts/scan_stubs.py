from __future__ import annotations

import re
from pathlib import Path

ROOTS = (Path("src"), Path("scripts"), Path("tests"), Path("docs"), Path("HANDOFF.md"))
PATTERN = re.compile(r"IMPLEMENT_ME")


def iter_files() -> list[Path]:
    files: list[Path] = []
    for root in ROOTS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return sorted(files)


def main() -> int:
    count = 0
    for path in iter_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, start=1):
            if PATTERN.search(line):
                count += 1
                print(f"{path}:{number}: {line.strip()}")
    print(f"\nRemaining IMPLEMENT_ME markers: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
