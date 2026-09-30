import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def test_backend_domain_modules_do_not_import_the_api_layer():
    violations = []

    for source_path in BACKEND.rglob("*.py"):
        relative_path = source_path.relative_to(BACKEND)
        if relative_path.parts[0] == "api" or relative_path.name == "web_api.py":
            continue

        tree = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported_modules = [node.module or ""]
            else:
                continue

            if any(module == "api" or module.startswith("api.") for module in imported_modules):
                violations.append(f"{relative_path}:{node.lineno}")

    assert violations == []
