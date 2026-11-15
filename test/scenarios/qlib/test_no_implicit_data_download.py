from pathlib import Path
from unittest.mock import patch

import pytest
from rdagent.scenarios.qlib.experiment import utils as qlib_experiment_utils
from rdagent.utils.env import DockerEnv, QlibDockerConf, QTDockerEnv


@pytest.mark.offline
def test_qlib_docker_prepare_does_not_download_without_opt_in() -> None:
    env = QTDockerEnv(QlibDockerConf(auto_download_cn_data=False))
    with (
        patch.object(DockerEnv, "prepare") as prepare_image,
        patch.object(env, "check_output") as run_command,
    ):
        env.prepare()

    prepare_image.assert_called_once_with()
    run_command.assert_not_called()


@pytest.mark.offline
def test_qlib_docker_prepare_downloads_only_with_explicit_opt_in(tmp_path: Path) -> None:
    qlib_home = tmp_path / "qlib"
    conf = QlibDockerConf(
        auto_download_cn_data=True,
        extra_volumes={str(qlib_home): {"bind": "/root/.qlib/", "mode": "rw"}},
    )
    env = QTDockerEnv(conf)
    with (
        patch.object(DockerEnv, "prepare"),
        patch.object(env, "check_output") as run_command,
    ):
        env.prepare()

    run_command.assert_called_once()
    assert "--region cn" in run_command.call_args.kwargs["entry"]


@pytest.mark.offline
def test_qlib_docker_prepare_opt_in_requires_host_mount() -> None:
    env = QTDockerEnv(QlibDockerConf(auto_download_cn_data=True, extra_volumes={}))
    with (
        patch.object(DockerEnv, "prepare"),
        pytest.raises(ValueError, match="has no host mount"),
    ):
        env.prepare()


@pytest.mark.offline
def test_factor_data_intro_requires_user_data_without_generating(tmp_path: Path) -> None:
    missing_all = tmp_path / "all"
    missing_debug = tmp_path / "debug"
    with (
        patch.object(qlib_experiment_utils.FACTOR_COSTEER_SETTINGS, "data_folder", str(missing_all)),
        patch.object(qlib_experiment_utils.FACTOR_COSTEER_SETTINGS, "data_folder_debug", str(missing_debug)),
        patch.object(qlib_experiment_utils, "generate_data_folder_from_qlib") as generate,
        pytest.raises(FileNotFoundError, match="No data is downloaded or generated automatically"),
    ):
        qlib_experiment_utils.get_data_folder_intro()

    generate.assert_not_called()


@pytest.mark.offline
def test_factor_data_intro_rejects_folders_without_hdf5(tmp_path: Path) -> None:
    source_dir = tmp_path / "all"
    debug_dir = tmp_path / "debug"
    source_dir.mkdir()
    debug_dir.mkdir()
    (source_dir / "notes.txt").write_text("not factor data", encoding="utf-8")
    (debug_dir / "README.md").write_text("not factor data", encoding="utf-8")

    with (
        patch.object(qlib_experiment_utils.FACTOR_COSTEER_SETTINGS, "data_folder", str(source_dir)),
        patch.object(qlib_experiment_utils.FACTOR_COSTEER_SETTINGS, "data_folder_debug", str(debug_dir)),
        pytest.raises(FileNotFoundError, match="prebuilt .h5 files"),
    ):
        qlib_experiment_utils.get_data_folder_intro()


@pytest.mark.offline
def test_factor_data_intro_describes_prebuilt_debug_files(tmp_path: Path) -> None:
    source_dir = tmp_path / "all"
    debug_dir = tmp_path / "debug"
    source_dir.mkdir()
    debug_dir.mkdir()
    (source_dir / "source.h5").write_bytes(b"not-a-real-hdf5-file")
    (debug_dir / "daily_pv.h5").write_bytes(b"not-a-real-hdf5-file")
    (debug_dir / "README.md").write_text("Prebuilt test fixture", encoding="utf-8")

    with (
        patch.object(qlib_experiment_utils.FACTOR_COSTEER_SETTINGS, "data_folder", str(source_dir)),
        patch.object(qlib_experiment_utils.FACTOR_COSTEER_SETTINGS, "data_folder_debug", str(debug_dir)),
    ):
        description = qlib_experiment_utils.get_data_folder_intro(fname_reg="README")

    assert "Prebuilt test fixture" in description
