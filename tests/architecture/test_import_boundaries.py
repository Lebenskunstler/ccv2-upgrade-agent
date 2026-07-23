import ast
import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _collect_imports(file_path: pathlib.Path) -> set[str]:
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    imports: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)

    return imports


class ArchitectureImportBoundaryTests(unittest.TestCase):
    def test_workflows_do_not_import_adapters(self):
        workflows_dir = REPO_ROOT / "upgrade_agent" / "workflows"
        for file_path in workflows_dir.glob("*.py"):
            imports = _collect_imports(file_path)
            forbidden = [imp for imp in imports if imp.startswith("upgrade_agent.adapters")]
            self.assertFalse(
                forbidden,
                msg=f"{file_path} imports adapters directly: {forbidden}",
            )

    def test_domain_depends_only_on_domain_or_stdlib(self):
        domain_dir = REPO_ROOT / "upgrade_agent" / "domain"
        for file_path in domain_dir.rglob("*.py"):
            imports = _collect_imports(file_path)
            forbidden = [
                imp for imp in imports
                if imp.startswith("upgrade_agent.adapters")
                or imp.startswith("upgrade_agent.application")
                or imp.startswith("upgrade_agent.interfaces")
                or imp.startswith("upgrade_agent.workflows")
            ]
            self.assertFalse(
                forbidden,
                msg=f"{file_path} violates domain boundary: {forbidden}",
            )


if __name__ == "__main__":
    unittest.main()
