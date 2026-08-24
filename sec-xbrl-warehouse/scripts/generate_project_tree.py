from __future__ import annotations

from pathlib import Path

IGNORED = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".coverage",
}


def render(directory: Path, prefix: str = "") -> list[str]:
    entries = sorted(
        (item for item in directory.iterdir() if item.name not in IGNORED),
        key=lambda item: (item.is_file(), item.name.lower()),
    )
    lines: list[str] = []
    for index, entry in enumerate(entries):
        last = index == len(entries) - 1
        connector = "└── " if last else "├── "
        lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
        if entry.is_dir():
            extension = "    " if last else "│   "
            lines.extend(render(entry, prefix + extension))
    return lines


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    lines = [f"{root.name}/", *render(root)]
    (root / "PROJECT_TREE.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {root / 'PROJECT_TREE.txt'}")


if __name__ == "__main__":
    main()
