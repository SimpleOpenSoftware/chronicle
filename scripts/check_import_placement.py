#!/usr/bin/env python3
"""Fail on imports nested inside functions/classes that carry no explanation.

AGENTS.md: "ALL imports must be at the top of the file after the docstring. Use
lazy imports sparingly and only when absolutely necessary for circular import
issues."

Sparingly does not mean never, so this check does not ban a nested import
outright — it bans an *unexplained* one. Write a comment on the import line, or
on the line(s) directly above it, saying why it has to live there:

    def build_router():
        # Imported here to break the circular import with plugins.router.
        from backend.plugins.router import PluginRouter

Module-level imports are never flagged, including `if TYPE_CHECKING:` blocks and
`try/except ImportError` fallbacks. Nor is a nested import guarded by
`try/except ImportError`, since that structure already says "optional
dependency" without a comment:

    try:
        from opentelemetry import trace
    except ImportError:
        return

Tests may import within a test or fixture to keep individual test runs isolated
from unrelated dependencies. Test modules and conftest.py are exempt.

Usage: check_import_placement.py [FILE ...]   (no args = every tracked .py file)
"""

from __future__ import annotations

import argparse
import ast
import io
import subprocess
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# A bare "# lazy import" explains nothing. Require a real sentence.
MIN_REASON_WORDS = 4

# Comment markers that are tooling directives, not explanations.
DIRECTIVE_PREFIXES = ("noqa", "type:", "pylint:", "ruff:", "mypy:", "fmt:", "isort:")


class Violation:
    __slots__ = ("path", "lineno", "statement")

    def __init__(self, path: Path, lineno: int, statement: str) -> None:
        self.path = path
        self.lineno = lineno
        self.statement = statement

    def __str__(self) -> str:
        return (
            f"{self.path}:{self.lineno}: unexplained nested import — {self.statement}"
        )


def _collect_comments(source: str) -> dict[int, str]:
    """Map line number -> comment text (without the leading '#')."""
    comments: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                comments[tok.start[0]] = tok.string.lstrip("#").strip()
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return comments


def _is_reason(comment: str) -> bool:
    lowered = comment.lower()
    if any(lowered.startswith(prefix) for prefix in DIRECTIVE_PREFIXES):
        return False
    return len(comment.split()) >= MIN_REASON_WORDS


def _has_explanation(
    node: ast.stmt,
    comments: dict[int, str],
    import_lines: set[int],
    blank_lines: set[int],
) -> bool:
    # Trailing comment on the import line, or on any line of a parenthesized
    # multi-line import.
    last_line = node.end_lineno or node.lineno
    for line in range(node.lineno, last_line + 1):
        if line in comments and _is_reason(comments[line]):
            return True

    # Comment block above the import. Sibling imports are stepped over — and so
    # are the blank lines isort puts between import groups — so one comment can
    # explain a whole block of them, the way people actually write it.
    line = node.lineno - 1
    while line in comments or line in import_lines or line in blank_lines:
        if line in comments and _is_reason(comments[line]):
            return True
        line -= 1
    return False


IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError"}


def _catches_import_error(node: ast.Try) -> bool:
    for handler in node.handlers:
        exc = handler.type
        candidates = exc.elts if isinstance(exc, ast.Tuple) else [exc]
        for candidate in candidates:
            if isinstance(candidate, ast.Name) and candidate.id in IMPORT_ERRORS:
                return True
    return False


class _Visitor(ast.NodeVisitor):
    """Walk the tree, tracking whether we are inside a function or class body."""

    def __init__(self) -> None:
        self.depth = 0
        self.guarded = 0
        self.nested_imports: list[ast.stmt] = []

    def _enter_scope(self, node: ast.AST) -> None:
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    visit_FunctionDef = _enter_scope
    visit_AsyncFunctionDef = _enter_scope
    visit_ClassDef = _enter_scope

    def visit_Try(self, node: ast.Try) -> None:
        guard = _catches_import_error(node)
        self.guarded += guard
        for child in node.body:
            self.visit(child)
        self.guarded -= guard
        for child in [*node.handlers, *node.orelse, *node.finalbody]:
            self.visit(child)

    def _record(self, node: ast.stmt) -> None:
        if self.depth and not self.guarded:
            self.nested_imports.append(node)

    visit_Import = _record
    visit_ImportFrom = _record


def _describe(node: ast.stmt) -> str:
    if isinstance(node, ast.ImportFrom):
        names = ", ".join(alias.name for alias in node.names)
        return f"from {'.' * node.level}{node.module or ''} import {names}"
    if isinstance(node, ast.Import):
        return "import " + ", ".join(alias.name for alias in node.names)
    return ast.dump(node)


def check_file(path: Path) -> list[Violation]:
    if (
        "tests" in path.parts
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or path.name == "conftest.py"
    ):
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # Not this hook's job to report syntax errors; black/ruff will.
        return []

    visitor = _Visitor()
    visitor.visit(tree)
    if not visitor.nested_imports:
        return []

    comments = _collect_comments(source)
    import_lines = {
        line
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    blank_lines = {
        number for number, text in enumerate(source.splitlines(), 1) if not text.strip()
    }
    try:
        rel = path.resolve().relative_to(REPO_ROOT)
    except ValueError:  # outside the repo (ad-hoc invocation)
        rel = path
    return [
        Violation(rel, node.lineno, _describe(node))
        for node in visitor.nested_imports
        if not _has_explanation(node, comments, import_lines, blank_lines)
    ]


def _tracked_python_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO_ROOT / name for name in out.split("\0") if name]


def _scan(paths: list[Path]) -> list[Violation]:
    violations: list[Violation] = []
    for path in paths:
        if path.suffix == ".py" and path.is_file():
            violations.extend(check_file(path))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)

    violations = _scan(args.files or _tracked_python_files())

    if violations:
        print("Imports must be at the top of the file (AGENTS.md → Code Style).")
        print(
            "If an import genuinely has to be nested (circular import, optional or "
            "heavy dependency), say why in a comment on or directly above it.\n"
        )
        for violation in violations:
            print(f"  {violation}")
        print(f"\n{len(violations)} unexplained nested import(s).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
