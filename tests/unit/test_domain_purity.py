"""M1-2 — Domain purity test.

Parse every .py file in borgesica/domain/ and assert zero imports of external
provider/adapter SDKs.
"""
import ast
from pathlib import Path

FORBIDDEN_IMPORTS = frozenset(
    ["anthropic", "openai", "ollama", "litellm", "instructor", "srt", "ebooklib", "pdfplumber"]
)

DOMAIN_DIR = Path(__file__).parent.parent.parent / "borgesica" / "domain"


def _collect_imports(tree: ast.Module) -> set[str]:
    """Return top-level package names for all import statements in the AST."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def test_domain_has_no_forbidden_imports() -> None:
    violations: list[str] = []

    for py_file in sorted(DOMAIN_DIR.glob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        found = _collect_imports(tree) & FORBIDDEN_IMPORTS
        if found:
            violations.append(f"{py_file.name}: {', '.join(sorted(found))}")

    assert not violations, (
        "Domain files contain forbidden external SDK imports:\n" + "\n".join(violations)
    )
