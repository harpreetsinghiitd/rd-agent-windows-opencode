import importlib
import unittest
from pathlib import Path

import pytest

_EXCLUDED_ENTRYPOINTS = {
    ("app", "cli.py"),
    ("app", "CI", "run.py"),
    ("app", "utils", "ape.py"),
    ("log", "ui", "utils.py"),
}


def module_name_from_file(file: Path, package_root: Path) -> str:
    """Convert a Python file path under the package root to a dotted import path."""
    relative = file.relative_to(package_root).with_suffix("")
    return ".".join((package_root.name, *relative.parts))


def should_skip_module(relative: Path) -> bool:
    """Return whether a package-relative module path is unsafe for import-all tests."""
    parts = relative.parts
    return (
        any("example" in part for part in parts)
        or any("meta_tpl" in part for part in parts)
        or ("autorl_bench" in parts and "workspace" in parts)
        or any("template" in part or "tpl" in part for part in parts)
        or any("model_coder" in part for part in parts)
        or any("llm_st" in part for part in parts)
        or Path("log/ui") in relative.parents
        or parts in _EXCLUDED_ENTRYPOINTS
        or any("extract_parameters" in part for part in parts)
    )


@pytest.mark.offline
class TestRDAgentImports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rdagent_directory = Path(__file__).resolve().parent.parent.parent
        cls.modules = list(cls.import_all_modules_from_directory(cls.rdagent_directory))

    @staticmethod
    def import_all_modules_from_directory(directory):
        package_root = directory / "rdagent"
        for file in package_root.rglob("*.py"):
            relative = file.relative_to(package_root)
            if should_skip_module(relative):
                continue
            yield module_name_from_file(file, package_root)

    def test_import_modules(self):
        print(self.modules)
        for module_name in self.modules:
            with self.subTest(module=module_name):
                try:
                    print(module_name)
                    importlib.import_module(module_name)
                except Exception as e:
                    self.fail(f"Failed to import {module_name}: {e}")


if __name__ == "__main__":
    unittest.main()
