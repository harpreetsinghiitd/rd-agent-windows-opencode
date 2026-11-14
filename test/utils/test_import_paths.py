import importlib.util
from pathlib import Path

import pytest

_module_path = Path(__file__).with_name("test_import.py")
_spec = importlib.util.spec_from_file_location("rdagent_test_import", _module_path)
assert _spec is not None and _spec.loader is not None
_test_import = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_test_import)
module_name_from_file = _test_import.module_name_from_file
should_skip_module = _test_import.should_skip_module


def test_module_name_from_file_is_platform_independent(tmp_path: Path) -> None:
    package_root = tmp_path / "rdagent"
    module_file = package_root / "scenarios" / "data_science" / "loop.py"

    assert module_name_from_file(module_file, package_root) == "rdagent.scenarios.data_science.loop"


def test_module_name_from_file_handles_package_initializer(tmp_path: Path) -> None:
    package_root = tmp_path / "rdagent"
    initializer = package_root / "components" / "__init__.py"

    assert module_name_from_file(initializer, package_root) == "rdagent.components.__init__"


@pytest.mark.parametrize(
    "relative",
    [
        "app/cli.py",
        "app/CI/run.py",
        "app/utils/ape.py",
        "log/ui/utils.py",
        "log/ui/app.py",
        "scenarios/data_science/example/task.py",
        "scenarios/foo/meta_tpl/task.py",
        "scenarios/rl/autorl_bench/workspace/task.py",
        "scenarios/foo/template/task.py",
        "scenarios/foo/tpl/task.py",
        "components/model_coder/task.py",
        "app/llm_st/task.py",
        "components/finetune/extract_parameters.py",
    ],
)
def test_import_filter_excludes_side_effectful_paths(relative: str) -> None:
    assert should_skip_module(Path(relative))


def test_import_filter_keeps_regular_modules() -> None:
    assert not should_skip_module(Path("scenarios/data_science/loop.py"))
    assert not should_skip_module(Path("components/coder/task.py"))
