from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SOURCE_ROOT = Path("src/sec_xbrl_warehouse")
OUTPUT = Path("FUNCTION_STUBS.md")


@dataclass(frozen=True, slots=True)
class Symbol:
    path: Path
    line: int
    qualified_name: str
    signature: str
    summary: str
    status: str


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    rendered = ast.unparse(node.args)
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix} {node.name}({rendered}){returns}"


def _summary(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    doc = ast.get_docstring(node, clean=True) or "No docstring; inspect the surrounding interface."
    first_paragraph = doc.split("\n\n", 1)[0]
    return " ".join(line.strip() for line in first_paragraph.splitlines()).replace("`", "\\`")


def _has_direct_not_implemented(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for child in node.body:
        if (
            isinstance(child, ast.Raise)
            and isinstance(child.exc, ast.Call)
            and isinstance(child.exc.func, ast.Name)
            and child.exc.func.id == "NotImplementedError"
        ):
            return True
    return False


def _is_protocol_class(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == "Protocol")
        or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
        for base in node.bases
    )


def _iter_symbols(path: Path) -> tuple[list[Symbol], list[Symbol]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    implementation: list[Symbol] = []
    protocols: list[Symbol] = []

    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segment = ast.get_source_segment(source, top) or ""
            if "IMPLEMENT_ME" in segment or _has_direct_not_implemented(top):
                implementation.append(
                    Symbol(
                        path=path,
                        line=top.lineno,
                        qualified_name=top.name,
                        signature=_signature(top),
                        summary=_summary(top),
                        status=(
                            "Required implementation"
                            if _has_direct_not_implemented(top)
                            else "Implemented baseline; hardening required"
                        ),
                    )
                )
        elif isinstance(top, ast.ClassDef):
            protocol = _is_protocol_class(top)
            for child in top.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                segment = ast.get_source_segment(source, child) or ""
                symbol = Symbol(
                    path=path,
                    line=child.lineno,
                    qualified_name=f"{top.name}.{child.name}",
                    signature=_signature(child),
                    summary=_summary(child),
                    status=(
                        "Interface contract"
                        if protocol
                        else (
                            "Required implementation"
                            if _has_direct_not_implemented(child)
                            else "Implemented baseline; hardening required"
                        )
                    ),
                )
                if protocol:
                    protocols.append(symbol)
                elif "IMPLEMENT_ME" in segment or _has_direct_not_implemented(child):
                    implementation.append(symbol)
    return implementation, protocols


def _render_group(title: str, symbols: list[Symbol]) -> list[str]:
    lines = [f"## {title}", ""]
    current_path: Path | None = None
    for symbol in sorted(symbols, key=lambda item: (str(item.path), item.line)):
        if symbol.path != current_path:
            current_path = symbol.path
            lines.extend([f"### `{symbol.path.as_posix()}`", ""])
        lines.extend(
            [
                f"- **`{symbol.qualified_name}`** — {symbol.status} (line {symbol.line})",
                f"  - Signature: `{symbol.signature.replace('`', '\\`')}`",
                f"  - Contract: {symbol.summary}",
            ]
        )
    if not symbols:
        lines.append("No symbols found.")
    lines.append("")
    return lines


def main() -> None:
    implementation: list[Symbol] = []
    protocols: list[Symbol] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        found, interfaces = _iter_symbols(path)
        implementation.extend(found)
        protocols.extend(interfaces)

    content = [
        "# Implementation and Interface Index",
        "",
        "This generated index lists intentionally marked implementation work and the current adapter protocols. `HANDOFF.md` is authoritative for the production acceptance boundary.",
        "",
        f"- Implementation/hardening symbols: **{len(implementation)}**",
        f"- Protocol/interface methods: **{len(protocols)}**",
        "- Regenerate with: `python scripts/generate_stub_index.py`",
        "",
        *_render_group("Implementation and hardening points", implementation),
        *_render_group("Port and protocol contracts", protocols),
    ]
    OUTPUT.write_text("\n".join(content).rstrip() + "\n", encoding="utf-8")
    print(
        f"Wrote {OUTPUT} with {len(implementation)} implementation points and {len(protocols)} interfaces"
    )


if __name__ == "__main__":
    main()
