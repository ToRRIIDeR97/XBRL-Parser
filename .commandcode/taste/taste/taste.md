# Taste
- Runs the full validation gate before declaring work complete: `python -m compileall -q ...`, `ruff check .`, `ruff format --check .`, `mypy src`, and `pytest`. Confidence: 0.9
- Verifies each implemented module with its own `pytest` run (unit + integration), going back to fix failures before moving on. Confidence: 0.8
- Uses ruff for both linting and formatting. Auto-fixes safe issues with `ruff check --fix`, and applies targeted `# noqa` comments for intentional suppressions (e.g., `# noqa: ASYNC240` for cheap startup filesystem checks in async code) rather than blanket-ignoring rules. Confidence: 0.8
- Uses mypy (strict) and satisfies it via explicit type annotations, `# type: ignore` for genuinely untyped third-party libraries (arelle, pyarrow, lxml, SQLAlchemy), and casts at boundary modules. Confidence: 0.8
- Configures lint/type tooling centrally in `pyproject.toml` (mypy module overrides with `ignore_missing_imports`; ruff per-file-ignores for test files using the `importorskip` pattern). Confidence: 0.7
- Prefers modern Python typing: `from __future__ import annotations`, built-in generics (`list[...]`, `dict[str, Any]`), and typed return annotations. Confidence: 0.8
- Uses Pydantic v2 idioms (`model_copy`, `model_validate`, `model_dump`) for domain models and immutable config. Confidence: 0.7
- Writes short docstrings for public classes/methods describing invariants, failure behavior, and intent. Confidence: 0.7
