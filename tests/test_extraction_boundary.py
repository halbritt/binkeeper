from __future__ import annotations

import ast
from pathlib import Path


def test_standalone_tests_do_not_import_engram() -> None:
    violations: list[str] = []
    for path in sorted(Path("tests").rglob("test_*.py")):
        if path.name == "test_engram_compatibility_contract.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = [node.module]
            else:
                continue
            if any(module == "engram" or module.startswith("engram.") for module in modules):
                violations.append(f"{path}:{node.lineno}")

    assert violations == []
