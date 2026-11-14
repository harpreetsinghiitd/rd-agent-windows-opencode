import os
from typing import Literal

from rdagent.app.data_science.conf import DS_RD_SETTING
from rdagent.components.coder.CoSTEER.config import CoSTEERSettings
from rdagent.utils.env import (
    CondaConf,
    DockerEnv,
    DSDockerConf,
    Env,
    LocalConf,
    LocalEnv,
    MLEBDockerConf,
    MLECondaConf,
)


class DSCoderCoSTEERSettings(CoSTEERSettings):
    """Data Science CoSTEER settings"""

    class Config:
        env_prefix = "DS_Coder_CoSTEER_"

    max_seconds_multiplier: int = 4
    env_type: str = "docker"
    # TODO: extract a function for env and conf.
    extra_evaluator: list[str] = []
    """Extra evaluators to use"""

    extra_eval: list[str] = []
    """
    Extra evaluators

    The evaluator follows the following assumptions:
    - It runs after previous evaluator (So the running results are already there)

    It is not a complete feature due to it is only implemented in DS Pipeline & Coder.

    TODO: The complete version should be implemented in the CoSTEERSettings.
    """


def get_ds_env(
    conf_type: Literal["kaggle", "mlebench"] = "kaggle",
    extra_volumes: dict = {},
    running_timeout_period: int | None = DS_RD_SETTING.debug_timeout,
    enable_cache: bool | None = None,
    env_type: str | None = None,
) -> Env:
    """
    Retrieve the appropriate environment configuration based on the env_type setting.

    Returns:
        Env: An instance of the environment configured either as DockerEnv or LocalEnv.

    Raises:
        ValueError: If the env_type is not recognized.
    """
    conf = DSCoderCoSTEERSettings()
    if conf_type not in ("kaggle", "mlebench"):
        raise ValueError(f"Unknown conf_type: {conf_type}")
    selected_env_type = env_type or conf.env_type

    if selected_env_type == "docker":
        env_conf = DSDockerConf() if conf_type == "kaggle" else MLEBDockerConf()
        env = DockerEnv(conf=env_conf)
    elif selected_env_type == "conda":
        env = LocalEnv(
            conf=(
                CondaConf(conda_env_name=conf_type) if conf_type == "kaggle" else MLECondaConf(conda_env_name=conf_type)
            ),
        )
    elif selected_env_type == "local":
        env = LocalEnv(conf=LocalConf(default_entry="python main.py"))
    else:
        raise ValueError(f"Unknown env type: {selected_env_type}")
    env.conf.extra_volumes = extra_volumes.copy()
    env.conf.running_timeout_period = running_timeout_period
    if enable_cache is not None:
        env.conf.enable_cache = enable_cache
    env.prepare()
    return env


def get_clear_ws_cmd(stage: Literal["before_training", "before_inference"] = "before_training") -> str:
    """
    Clean the files in workspace to a specific stage
    """
    assert stage in ["before_training", "before_inference"], f"Unknown stage: {stage}"
    targets = ["submission.csv", "scores.csv", "trace.log"]
    if DS_RD_SETTING.enable_model_dump and stage == "before_training":
        targets.append("models")
    if os.name == "nt":
        # Execute a Python cleanup snippet: `rm` is not part of stock Windows.
        # No single quotes: Docker envs wrap the entry in `sh -c '...'`.
        return (
            'python -c "import sys, shutil; from pathlib import Path; '
            "[shutil.rmtree(p) if Path(p).is_dir() else Path(p).unlink(missing_ok=True) "
            'for p in sys.argv[1:]]" ' + " ".join(targets)
        )
    return "rm " + ("-r " if "models" in targets else "") + " ".join(targets)
