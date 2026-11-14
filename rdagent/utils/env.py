"""
The motivation of the utils is for environment management

Tries to create uniform environment for the agent to run;
- All the code and data is expected included in one folder
"""

from __future__ import annotations

# TODO: move the scenario specific docker env into other folders.
import contextlib
import json
import os
import posixpath
import queue
import re
import select
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from abc import abstractmethod
from collections import deque
from collections.abc import Callable, Generator, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Generic,
    TypeVar,
    cast,
)

try:
    import docker  # type: ignore[import-untyped]
    import docker.models  # type: ignore[import-untyped]
    import docker.models.containers  # type: ignore[import-untyped]
    import docker.types  # type: ignore[import-untyped]
except ImportError:  # Docker is optional for the native LocalEnv backend.
    docker = None  # type: ignore[assignment]
from pydantic import model_validator
from pydantic_settings import SettingsConfigDict
from rdagent.core.conf import ExtendedBaseSettings
from rdagent.core.experiment import RD_AGENT_SETTINGS
from rdagent.core.serialization import UntrustedArtifactError
from rdagent.core.serialization import dump as secure_pickle_dump
from rdagent.core.serialization import load as secure_pickle_load
from rdagent.core.utils import cache_with_pickle
from rdagent.log import rdagent_logger as logger
from rdagent.oai.llm_utils import md5_hash
from rdagent.utils import filter_redundant_text
from rdagent.utils.agent.tpl import T
from rdagent.utils.fmt import shrink_text
from rdagent.utils.workflow import wait_retry
from rich import print
from rich.console import Console
from rich.live import Live
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from tqdm import tqdm

CacheKeyFunc = Callable[[str | Path], list[list[str]]]


def extract_dir_name_from_path_config(path_str: str) -> str:
    """
    Extract the first directory component from a relative path string.

    This is used to get the basename from path configurations like "./workspace_input/"
    to use in chmod exclusion patterns.

    Args:
        path_str: A path string, typically from T() template configuration

    Returns:
        The first directory component, or empty string if not a relative path

    Examples:
        "./workspace_input/" -> "workspace_input"
        "./assets/" -> "assets"
        "/absolute/path" -> ""
    """
    p = Path(path_str)
    if not p.is_absolute() and p.parts:
        return p.parts[0]
    return ""


def cleanup_container(container: Any | None, context: str = "") -> None:
    """
    Shared helper function to clean up a Docker container.
    Always stops the container before removing it.

    Parameters
    ----------
    container : docker container object or None
        The container to clean up, or None if no container to clean up
    context : str
        Additional context for logging (e.g., "health check", "GPU test")
    """
    if container is not None:
        try:
            # Always stop first - stop() doesn't raise error if already stopped
            container.stop()
            container.remove()
        except Exception as cleanup_error:
            # Log cleanup error but don't mask the original exception
            context_str = f" {context}" if context else ""
            logger.warning(f"Failed to cleanup{context_str} container {container.id}: {cleanup_error}")


# Normalize all bind paths in volumes to absolute paths using the workspace (working_dir).
def normalize_volumes(vols: dict[str, str | dict[str, str]], working_dir: str) -> dict:
    abs_vols: dict[str, str | dict[str, str]] = {}

    def to_abs(path: str) -> str:
        # Converts a relative path to an absolute path using the workspace (working_dir).
        # A POSIX-style absolute path is a *container* path (e.g. /kaggle/workspace/...);
        # on Windows os.path.abspath would rewrite it to C:\kaggle\..., which Docker rejects.
        if path.startswith("/"):
            return path
        if working_dir.startswith("/"):
            # The workspace lives inside the container, so join with POSIX semantics.
            return posixpath.normpath(posixpath.join(working_dir, path))
        return os.path.abspath(os.path.join(working_dir, path)) if not os.path.isabs(path) else path

    for lp, vinfo in list(vols.items()):
        # A relative host path would be treated by Docker as a named volume.
        if not os.path.isabs(lp) and not lp.startswith("/"):
            lp = os.path.abspath(lp)
        # Support both:
        # 1. {'host_path': {'bind': 'container_path', ...}}
        # 2. {'host_path': 'container_path'}
        if isinstance(vinfo, dict):
            # abs_vols = cast(dict[str, dict[str, str]], abs_vols)
            vinfo = vinfo.copy()
            vinfo["bind"] = to_abs(vinfo["bind"])
            abs_vols[lp] = vinfo
        else:
            # abs_vols = cast(dict[str, str], abs_vols)
            abs_vols[lp] = to_abs(vinfo)
    return abs_vols


def _remove_path(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def _prepare_volume_target(link_path: Path) -> None:
    """Remove an obsolete symlink but never replace an existing host path."""
    if link_path.is_symlink():
        link_path.unlink()
    elif link_path.exists():
        raise FileExistsError(f"Refusing to replace an existing volume target: {link_path}")


def _reject_host_volume_symlinks(host_path: Path) -> None:
    """Reject links before Windows staging, which otherwise dereferences them."""
    if host_path.is_symlink() or (host_path.is_dir() and any(path.is_symlink() for path in host_path.rglob("*"))):
        msg = f"Refusing to stage host volume containing symlinks: {host_path}"
        raise RuntimeError(msg)


def _mirror_staged_directory(staged_path: Path, host_path: Path) -> None:
    """Apply staged directory changes, including deletions, to the host mount."""
    if staged_path.is_symlink():
        msg = f"Refusing to copy back a replaced staged volume root: {staged_path}"
        raise RuntimeError(msg)
    if not host_path.exists():
        host_path.mkdir(parents=True, exist_ok=True)
    if not staged_path.exists():
        for child in host_path.iterdir():
            _remove_path(child)
        return
    if not staged_path.is_dir():
        msg = f"Refusing to copy back a replaced staged volume root: {staged_path}"
        raise RuntimeError(msg)
    if any(child.is_symlink() for child in staged_path.rglob("*")):
        msg = f"Refusing to copy back symlinks from staged volume: {staged_path}"
        raise RuntimeError(msg)

    def remove_missing_or_changed(source: Path, staged: Path) -> None:
        for source_child in source.iterdir():
            staged_child = staged / source_child.name
            if not staged_child.exists() and not staged_child.is_symlink():
                _remove_path(source_child)
                continue
            if source_child.is_symlink() or staged_child.is_symlink():
                _remove_path(source_child)
                continue
            source_is_dir = source_child.is_dir()
            staged_is_dir = staged_child.is_dir()
            if source_is_dir != staged_is_dir:
                _remove_path(source_child)
            elif source_is_dir:
                remove_missing_or_changed(source_child, staged_child)

    remove_missing_or_changed(host_path, staged_path)
    shutil.copytree(staged_path, host_path, dirs_exist_ok=True, symlinks=True)


def _cleanup_staged_volume(
    link_path: Path, real_path: Path, writable: bool, is_directory: bool, staged_ready: bool
) -> None:
    try:
        if staged_ready and writable and real_path.exists():
            if is_directory:
                _mirror_staged_directory(link_path, real_path)
            elif link_path.exists() or link_path.is_symlink():
                if link_path.is_symlink():
                    msg = f"Refusing to copy back a staged file symlink: {link_path}"
                    raise RuntimeError(msg)
                real_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(link_path, real_path)
            else:
                real_path.unlink(missing_ok=True)
    finally:
        if link_path.is_dir() and not link_path.is_symlink():
            shutil.rmtree(link_path)
        else:
            link_path.unlink(missing_ok=True)


def _cleanup_staged_volumes(created_links: list[tuple[Path, Path, bool, bool, bool]]) -> None:
    cleanup_errors: list[Exception] = []
    for link_path, real_path, writable, is_directory, staged_ready in reversed(created_links):
        try:
            _cleanup_staged_volume(link_path, real_path, writable, is_directory, staged_ready)
        except FileNotFoundError:
            pass
        except Exception as exc:
            cleanup_errors.append(exc)

    if len(cleanup_errors) == 1:
        raise cleanup_errors[0]
    if cleanup_errors:
        raise RuntimeError(f"Failed to clean up {len(cleanup_errors)} staged volume(s)") from cleanup_errors[0]


def _docker_autostart_enabled() -> bool:
    """Windows-only: should we try to (re)start Docker Desktop on connection failure?

    Docker Desktop on Windows silently stops itself (auto-pause, disk pressure,
    WSL hiccups). A multi-hour RD-Agent loop then dies with "Docker is not
    reachable" even though a retry a few seconds later would work. Set
    ``RDAGENT_DOCKER_AUTOSTART=0`` to opt out.
    """
    if os.name != "nt":
        return False
    return os.environ.get("RDAGENT_DOCKER_AUTOSTART", "1").strip().lower() not in {"0", "false", "no", "off"}


def _start_docker_desktop() -> None:
    """Best-effort `docker desktop start`, falling back to launching the GUI."""
    candidates = [
        [shutil.which("docker") or "docker", "desktop", "start"],
    ]
    gui = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe"
    for cmd in candidates:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return
        logger.warning(f"[docker] `{' '.join(cmd)}` exited {proc.returncode}: {(proc.stdout or proc.stderr).strip()[:200]}")
        break
    if gui.exists():
        logger.info(f"[docker] launching {gui}")
        with contextlib.suppress(OSError):
            subprocess.Popen([str(gui)], close_fds=True)  # noqa: S603


def get_docker_client(wait_seconds: int | None = None):  # type: ignore[no-untyped-def]
    """`docker.from_env()` that survives Docker Desktop stopping itself on Windows.

    Retries in a loop and, when enabled, asks Docker Desktop to start again.
    Raises the original connection error with actionable advice if all fails.
    """
    if docker is None:
        raise RuntimeError("Docker SDK is missing. Install the Docker backend dependencies to use DockerEnv.")
    total_wait = wait_seconds if wait_seconds is not None else int(os.environ.get("RDAGENT_DOCKER_WAIT", "180"))
    autostart = _docker_autostart_enabled()
    deadline = time.time() + total_wait
    started = False
    last_exc: Exception | None = None
    while True:
        try:
            return docker.from_env()
        except Exception as exc:  # docker.errors.DockerException subclasses vary by version
            last_exc = exc
        if autostart and not started:
            logger.warning("[docker] engine unreachable, attempting to start Docker Desktop")
            _start_docker_desktop()
            started = True
        if time.time() >= deadline:
            msg = (
                "Docker is not reachable. Start Docker Desktop with its Linux engine enabled, then retry. "
                f"(waited {total_wait}s)"
            )
            raise RuntimeError(msg) from last_exc
        time.sleep(3)


def pull_image_with_progress(image: str) -> None:
    if docker is None:
        raise RuntimeError("Docker SDK is missing. Install the Docker backend dependencies to use this scenario.")
    client = get_docker_client().api
    pull_logs = client.pull(image, stream=True, decode=True)
    progress_bars = {}

    for log in pull_logs:
        if "id" in log and log.get("progressDetail"):
            layer_id = log["id"]
            progress_detail = log["progressDetail"]
            current = progress_detail.get("current", 0)
            total = progress_detail.get("total", 0)

            if total:
                if layer_id not in progress_bars:
                    progress_bars[layer_id] = tqdm(total=total, desc=f"Layer {layer_id}", unit="B", unit_scale=True)
                progress_bars[layer_id].n = current
                progress_bars[layer_id].refresh()

        elif "status" in log:
            print(log["status"])

    for pb in progress_bars.values():
        pb.close()


class EnvConf(ExtendedBaseSettings):
    default_entry: str
    env_dict: dict = {}
    extra_volumes: dict = {}
    running_timeout_period: int | None = 3600  # 10 minutes

    """it is a function to calculating hash keys"""

    def get_workspace_content_for_hash(self, local_path: str | Path) -> list[list[str]]:
        """Get content of key files in workspace for cache hash calculation.

        Scans .py, .csv, and .yaml files.
        """
        # we must add the information of data (beyond code) into the key.
        # Otherwise, all commands operating on data will become invalid (e.g. rm -r submission.csv)
        # So we recursively walk in the folder and add the sorted relative filename list as part of the key.
        # data_key = []
        # for path in Path(local_path).rglob("*"):
        #     p = str(path.relative_to(Path(local_path)))
        #     if p.startswith("__pycache__"):
        #         continue
        #     data_key.append(p)
        # data_key = sorted(data_key)
        local_path = Path(local_path)
        return [
            [str(path.relative_to(local_path)), path.read_text()]
            for path in sorted(
                list(local_path.rglob("*.py")) + list(local_path.rglob("*.csv")) + list(local_path.rglob("*.yaml")),
            )
        ]

    redirect_stdout_to_file: bool = False
    # helper settings to support transparent;
    enable_cache: bool = True
    retry_count: int = 5  # retry count for the docker run
    retry_wait_seconds: int = 10  # retry wait seconds for the docker run
    exclude_chmod_paths: list[str] = []  # List of directory names to exclude from chmod operation

    model_config = SettingsConfigDict(
        # TODO: add prefix ....
        env_parse_none_str="None",  # Nthis is the key to accept `RUNNING_TIMEOUT_PERIOD=None`
    )


ASpecificEnvConf = TypeVar("ASpecificEnvConf", bound=EnvConf)


@dataclass
class EnvResult:
    """
    The result of running the environment.
    It contains the stdout, the exit code, and the running time in seconds.
    """

    full_stdout: str
    exit_code: int
    running_time: float
    stored_full_stdout_to_truncated_stdout: dict[str, str]

    def __init__(self, stdout: str, exit_code: int, running_time: float):
        self.full_stdout = stdout
        self.exit_code = exit_code
        self.running_time = running_time
        self.stored_full_stdout_to_truncated_stdout = {}

    def update_stdout(self, stdout: str) -> None:
        self.full_stdout = stdout

    @property
    def stdout(self) -> str:
        if self.full_stdout not in self.stored_full_stdout_to_truncated_stdout:
            truncated: str = self._get_truncated_stdout(self.full_stdout)
            self.stored_full_stdout_to_truncated_stdout[self.full_stdout] = truncated
        return self.stored_full_stdout_to_truncated_stdout[self.full_stdout]

    def hash_full_stdout(self, full_stdout: str) -> str:
        return md5_hash(full_stdout)

    @cache_with_pickle(hash_full_stdout)
    def _get_truncated_stdout(self, full_stdout: str) -> str:
        return shrink_text(
            filter_redundant_text(full_stdout),
            context_lines=RD_AGENT_SETTINGS.stdout_context_len,
            line_len=RD_AGENT_SETTINGS.stdout_line_len,
        )


class Env(Generic[ASpecificEnvConf]):
    """
    We use BaseModel as the setting due to the features it provides
    - It provides base typing and checking features.
    - loading and dumping the information will be easier: for example, we can use package like `pydantic-yaml`
    """

    conf: ASpecificEnvConf  # different env have different conf.

    def __init__(self, conf: ASpecificEnvConf):
        self.conf = conf

    def zip_a_folder_into_a_file(self, folder_path: str, zip_file_path: str) -> None:
        """
        Zip a folder into a file, use zipfile instead of subprocess
        """
        with zipfile.ZipFile(zip_file_path, "w") as z:
            for root, _, files in os.walk(folder_path):
                for file in files:
                    z.write(
                        os.path.join(root, file),
                        os.path.relpath(os.path.join(root, file), folder_path),
                    )

    def unzip_a_file_into_a_folder(
        self, zip_file_path: str, folder_path: str, files_to_extract: list[str] | None = None,
    ) -> None:
        """
        Unzip a file into a folder, use zipfile instead of subprocess
        """
        if files_to_extract is None:
            # Clear folder_path before extracting
            if os.path.exists(folder_path):
                shutil.rmtree(folder_path)
            os.makedirs(folder_path)

        with zipfile.ZipFile(zip_file_path, "r") as z:
            if files_to_extract is not None:
                for file_name in files_to_extract:
                    try:
                        z.extract(file_name, folder_path)
                    except KeyError:
                        logger.warning(f"File {file_name} not found in cache zip.")
            else:
                z.extractall(folder_path)

    @abstractmethod
    def prepare(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        """
        Prepare for the environment based on it's configure
        """

    def check_output(
        self,
        entry: str | None = None,
        local_path: str = ".",
        env: dict | None = None,
        running_extra_volume: Mapping = MappingProxyType({}),
        cache_key_extra_func: CacheKeyFunc | None = None,
        cache_files_to_extract: list[str] | None = None,
    ) -> str:
        result = self.run(
            entry=entry,
            local_path=local_path,
            env=env,
            running_extra_volume=running_extra_volume,
            cache_key_extra_func=cache_key_extra_func,
            cache_files_to_extract=cache_files_to_extract,
        )
        return result.stdout

    def __run_with_retry(
        self,
        entry: str | None = None,
        local_path: str = ".",
        env: dict | None = None,
        running_extra_volume: Mapping = MappingProxyType({}),
    ) -> EnvResult:
        for retry_index in range(self.conf.retry_count + 1):
            try:
                start = time.time()
                log_output, return_code = self._run(
                    entry,
                    local_path,
                    env,
                    running_extra_volume=running_extra_volume,
                )
                end = time.time()
                logger.info(f"Running time: {end - start} seconds")
                if self.conf.running_timeout_period is not None and end - start + 1 >= self.conf.running_timeout_period:
                    logger.warning(
                        f"The running time exceeds {self.conf.running_timeout_period} seconds, so the process is killed.",
                    )
                    log_output += f"\n\nThe running time exceeds {self.conf.running_timeout_period} seconds, so the process is killed."
                return EnvResult(log_output, return_code, end - start)
            except Exception as e:
                if retry_index == self.conf.retry_count:
                    raise
                logger.warning(
                    f"Error while running the container: {e}, current try index: {retry_index + 1}, {self.conf.retry_count - retry_index - 1} retries left.",
                )
                time.sleep(self.conf.retry_wait_seconds)
        raise RuntimeError  # for passing CI

    def run(
        self,
        entry: str | None = None,
        local_path: str = ".",
        env: dict | None = None,
        running_extra_volume: Mapping = MappingProxyType({}),
        cache_key_extra_func: CacheKeyFunc | None = None,
        cache_files_to_extract: list[str] | None = None,
    ) -> EnvResult:
        """
        Run the folder under the environment and return the stdout, exit code, and running time.

        Parameters
        ----------
        entry : str | None
            We may we the entry point when we run it.
            For example, we may have different entries when we run and summarize the project.
        local_path : str | None
            the local path (to project, mainly for code) will be mounted into the docker
            Here are some examples for a None local path
            - for example, run docker for updating the data in the extra_volumes.
            - simply run the image. The results are produced by output or network
        env : dict | None
            Run the code with your specific environment.
        running_extra_volume : Mapping
            Extra volumes to mount during execution.
        cache_key_extra_func : CacheKeyFunc | None
            Optional function to calculate extra information for cache key calculation
        cache_files_to_extract : list[str] | None
            Optional list of files to extract from cache zip. If None, extract all.

        Returns
        -------
            EnvResult: An object containing the stdout, the exit code, and the running time in seconds.
        """
        _env = self.conf.env_dict.copy()
        if env:
            _env.update(env)
        env = _env

        if entry is None:
            entry = self.conf.default_entry

        if "|" in entry:
            logger.warning(
                "You are using a command with a shell pipeline (i.e., '|'). "
                "The exit code ($exit_code) will reflect the result of "
                "the last command in the pipeline.",
            )

        # Exclude configured directories from chmod operation to prevent modifying
        # read-only or specially configured directories that may produce warnings.
        def _get_chmod_cmd(workspace_path: str) -> str:
            find_cmd = f"find {workspace_path} -mindepth 1 -maxdepth 1"

            # Use configurable exclude paths from DockerConf
            for name in self.conf.exclude_chmod_paths:
                if name:  # Skip empty names
                    find_cmd += f" ! -name {name}"

            chmod_cmd = f"{find_cmd} -exec chmod -R 777 {{}} +"
            return chmod_cmd

        if self.conf.redirect_stdout_to_file:
            log_file_name = md5_hash(entry)[:8] + ".log"
            log_file = Path(local_path) / f"{log_file_name}"
            log_file_relative_path = log_file.relative_to(Path(local_path))
            entry = f"{entry} > {log_file_relative_path} 2>&1"

        # LocalEnv on Windows runs through cmd.exe, not the POSIX shell used by Docker
        # and Unix LocalEnv. Apply the timeout in LocalEnv itself so commands remain
        # native and no WSL/Git Bash installation is required for this backend.
        if os.name == "nt" and isinstance(self.conf, LocalConf):
            entry_add_timeout = entry
        else:
            if self.conf.running_timeout_period is None:
                timeout_cmd = entry
            else:
                timeout_cmd = f"timeout --kill-after=10 {self.conf.running_timeout_period} {entry}"
            entry_add_timeout = (
                "/bin/sh -c '"  # start of the sh command
                 f"{timeout_cmd}; entry_exit_code=$?; "
                + (
                    f"{_get_chmod_cmd(self.conf.mount_path)}; "
                    # We don't have to change the permission of the cache and input folder to remove it
                    # + f"if [ -d {self.conf.mount_path}/cache ]; then chmod 777 {self.conf.mount_path}/cache; fi; " +
                    #     f"if [ -d {self.conf.mount_path}/input ]; then chmod 777 {self.conf.mount_path}/input; fi; "
                    if isinstance(self.conf, DockerConf)
                    else ""
                )
                + "exit $entry_exit_code"
                + "'"  # end of the sh command
            )

        if self.conf.enable_cache:
            result = self.cached_run(
                entry_add_timeout,
                local_path,
                env,
                running_extra_volume,
                cache_key_extra_func,
                cache_files_to_extract,
            )
        else:
            result = self.__run_with_retry(
                entry_add_timeout,
                local_path,
                env,
                running_extra_volume,
            )
        if self.conf.redirect_stdout_to_file:
            stdout = log_file.read_text(errors="replace")
            log_file.unlink(missing_ok=True)
            result.update_stdout(stdout)
        if str(Path(local_path).resolve()) in result.stdout:
            result.update_stdout(result.stdout.replace(str(Path(local_path).resolve()), "<WORKSPACE_PATH>"))

        return result

    def cached_run(
        self,
        entry: str | None = None,
        local_path: str = ".",
        env: dict | None = None,
        running_extra_volume: Mapping = MappingProxyType({}),
        cache_key_extra_func: CacheKeyFunc | None = None,
        cache_files_to_extract: list[str] | None = None,
    ) -> EnvResult:
        """
        Run the folder under the environment.
        Will cache the output and the folder diff for next round of running.
        Use the python codes and the parameters(entry, running_extra_volume) as key to hash the input.
        """
        target_folder = Path(RD_AGENT_SETTINGS.pickle_cache_folder_path_str) / "utils.env.run"
        target_folder.mkdir(parents=True, exist_ok=True)

        if cache_key_extra_func is not None:
            cache_key_extra = cache_key_extra_func(local_path)
        else:
            cache_key_extra = self.conf.get_workspace_content_for_hash(local_path)

        key = md5_hash(
            json.dumps(cache_key_extra)
            + json.dumps({"entry": entry, "running_extra_volume": dict(running_extra_volume)})
            + json.dumps({"extra_volumes": self.conf.extra_volumes}),
            # + json.dumps(data_key)
        )
        if Path(target_folder / f"{key}.pkl").exists() and Path(target_folder / f"{key}.zip").exists():
            try:
                with open(target_folder / f"{key}.pkl", "rb") as f:
                    ret = secure_pickle_load(f)
            except UntrustedArtifactError:
                Path(target_folder / f"{key}.pkl").unlink(missing_ok=True)
                Path(target_folder / f"{key}.zip").unlink(missing_ok=True)
                ret = self.__run_with_retry(entry, local_path, env, running_extra_volume)
                with open(target_folder / f"{key}.pkl", "wb") as f:
                    secure_pickle_dump(ret, f)
                self.zip_a_folder_into_a_file(local_path, str(target_folder / f"{key}.zip"))
            else:
                self.unzip_a_file_into_a_folder(str(target_folder / f"{key}.zip"), local_path, cache_files_to_extract)
        else:
            ret = self.__run_with_retry(entry, local_path, env, running_extra_volume)
            with open(target_folder / f"{key}.pkl", "wb") as f:
                secure_pickle_dump(ret, f)
            self.zip_a_folder_into_a_file(local_path, str(target_folder / f"{key}.zip"))
        return cast("EnvResult", ret)

    @abstractmethod
    def _run(
        self,
        entry: str | None,
        local_path: str = ".",
        env: dict | None = None,
        running_extra_volume: Mapping = MappingProxyType({}),
        **kwargs: Any,
    ) -> tuple[str, int]:
        """
        Execute the specified entry point within the given environment and local path.

        Parameters
        ----------
        entry : str | None
            The entry point to execute. If None, defaults to the configured entry.
        local_path : str
            The local directory path where the execution should occur.
        env : dict | None
            Environment variables to set during execution.
        kwargs : dict
            Additional keyword arguments for execution customization.

        Returns
        -------
        tuple[str, int]
            A tuple containing the standard output and the exit code.
        """

    def dump_python_code_run_and_get_results(
        self,
        code: str,
        dump_file_names: list[str],
        local_path: str,
        env: dict | None = None,
        running_extra_volume: Mapping = MappingProxyType({}),
        code_dump_file_py_name: str | None = None,
    ) -> tuple[str, list]:
        """
        Dump the code into the local path and run the code.
        """
        from rdagent.utils.artifact_transport import load_result_artifact

        random_file_name = f"{uuid.uuid4()}.py" if code_dump_file_py_name is None else f"{code_dump_file_py_name}.py"
        with open(os.path.join(local_path, random_file_name), "w") as f:
            f.write(code)
        entry = f"python {random_file_name}"
        log_output = self.check_output(entry, local_path, env, running_extra_volume=dict(running_extra_volume))
        results = []
        os.remove(os.path.join(local_path, random_file_name))
        for name in dump_file_names:
            result_path = Path(local_path) / name
            if not result_path.exists():
                return log_output, []
            results.extend(load_result_artifact(result_path))
            if result_path.name == "manifest.json" and result_path.parent.name == "rdagent_artifacts":
                shutil.rmtree(result_path.parent)
                continue
            result_path.unlink()
        return log_output, results

    def refresh_env(self) -> None:
        """Refresh the environment, e.g., pull the latest docker image. rebuild the conda env."""


# class EnvWithCache
#

## Local Environment -----


class LocalConf(EnvConf):
    bin_path: str = ""
    """path like <path1>:<path2>:<path3>, which will be prepend to bin path."""

    retry_count: int = 0  # retry count for; run `retry_count + 1` times
    live_output: bool = True


ASpecificLocalConf = TypeVar("ASpecificLocalConf", bound=LocalConf)


class LocalEnv(Env[ASpecificLocalConf]):
    """
    Sometimes local environment may be more convenient for testing
    """

    def prepare(self) -> None: ...

    def _run(
        self,
        entry: str | None = None,
        local_path: str | None = None,
        env: dict | None = None,
        running_extra_volume: Mapping = MappingProxyType({}),
        **kwargs: dict,
    ) -> tuple[str, int]:

        # Handle volume links
        volumes = {}
        if self.conf.extra_volumes is not None:
            volumes.update(self.conf.extra_volumes)
            is_sample = "/sample/" in "".join(self.conf.extra_volumes.keys())
            cache_path = (
                Path(tempfile.gettempdir()) / "rdagent" / ("sample" if is_sample else "full")
                if os.name == "nt"
                else Path("/tmp/sample" if is_sample else "/tmp/full")
            )
            cache_path.mkdir(parents=True, exist_ok=True)
            volumes[str(cache_path)] = T("scenarios.data_science.share:scen.cache_path").r()
        for lp, rp in running_extra_volume.items():
            volumes[lp] = rp

        assert local_path is not None, "local_path should not be None"
        volumes = normalize_volumes(volumes, local_path)

        @contextlib.contextmanager
        def _symlink_ctx(vol_map: Mapping[str, str | dict[str, str]]) -> Generator[None, None, None]:
            created_links: list[tuple[Path, Path, bool, bool, bool]] = []
            try:
                for real, volume_info in vol_map.items():
                    if isinstance(volume_info, dict):
                        link = volume_info["bind"]
                        writable = volume_info.get("mode", "rw") != "ro"
                    else:
                        link = volume_info
                        writable = True
                    link_path = Path(link)
                    real_path = Path(real)
                    link_path.parent.mkdir(parents=True, exist_ok=True)
                    _prepare_volume_target(link_path)
                    if os.name == "nt":
                        # Symlink creation commonly requires Developer Mode or admin rights.
                        # Stage a private copy; only writable volumes are copied back.
                        _reject_host_volume_symlinks(real_path)
                        if not real_path.exists():
                            if real_path.is_dir() or not real_path.suffix:
                                real_path.mkdir(parents=True, exist_ok=True)
                            else:
                                real_path.parent.mkdir(parents=True, exist_ok=True)
                                real_path.touch()
                        is_directory = real_path.is_dir()
                        staged_ready = False
                        try:
                            with tempfile.TemporaryDirectory(prefix="rdagent-volume-") as temp_dir:
                                staged_path = Path(temp_dir) / "mount"
                                if is_directory:
                                    shutil.copytree(real_path, staged_path)
                                else:
                                    shutil.copy2(real_path, staged_path)
                                shutil.move(str(staged_path), str(link_path))
                            staged_ready = True
                        finally:
                            created_links.append((link_path, real_path, writable, is_directory, staged_ready))
                    else:
                        is_directory = real_path.is_dir()
                        link_path.symlink_to(real_path, target_is_directory=is_directory)
                        created_links.append((link_path, real_path, writable, is_directory, True))
                yield
            finally:
                if os.name == "nt":
                    _cleanup_staged_volumes(created_links)
                else:
                    for link_path, _, _, _, _ in reversed(created_links):
                        try:
                            if link_path.is_symlink():
                                link_path.unlink()
                        except FileNotFoundError:
                            pass

        with _symlink_ctx(volumes):
            # Setup environment
            if env is None:
                env = {}

            # Auto-propagate CUDA_VISIBLE_DEVICES for proper GPU isolation
            if "CUDA_VISIBLE_DEVICES" in os.environ and "CUDA_VISIBLE_DEVICES" not in env:
                env["CUDA_VISIBLE_DEVICES"] = os.environ["CUDA_VISIBLE_DEVICES"]

            inherited_path = env.get("PATH", os.environ.get("PATH", ""))
            path = [part for part in self.conf.bin_path.split(os.pathsep) if part]
            if os.name != "nt":
                path.extend(["/bin/", "/usr/bin/"])
            path.extend(part for part in inherited_path.split(os.pathsep) if part)
            env["PATH"] = os.pathsep.join(path)

            if entry is None:
                entry = self.conf.default_entry

            print(Rule("[bold green]LocalEnv Logs Begin[/bold green]", style="dark_orange"))
            table = Table(title="Run Info", show_header=False)
            table.add_column("Key", style="bold cyan")
            table.add_column("Value", style="bold magenta")
            table.add_row("Entry", entry)
            table.add_row("Local Path", local_path or "")
            table.add_row("Env", "\n".join(f"{k}:{v}" for k, v in env.items()))
            table.add_row("Volumes", "\n".join(f"{k}:\n  {v}" for k, v in volumes.items()))
            print(table)

            cwd = Path(local_path).resolve() if local_path else None
            env = {k: str(v) if isinstance(v, int) else v for k, v in env.items()}

            timed_out = False
            if os.name == "nt":
                command = entry
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env={**os.environ, **env},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=True,
                    bufsize=1,
                    universal_newlines=True,
                )
                if process.stdout is None or process.stderr is None:
                    raise RuntimeError("The subprocess did not correctly create stdout/stderr pipes")

                output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

                def _read_stream(stream, label: str) -> None:
                    try:
                        for line in iter(stream.readline, ""):
                            output_queue.put((label, line))
                    finally:
                        output_queue.put((label, None))

                readers = [
                    threading.Thread(target=_read_stream, args=(process.stdout, "stdout"), daemon=True),
                    threading.Thread(target=_read_stream, args=(process.stderr, "stderr"), daemon=True),
                ]
                for reader in readers:
                    reader.start()

                output_parts: list[str] = []
                closed_streams = 0
                deadline = (
                    time.monotonic() + self.conf.running_timeout_period
                    if self.conf.running_timeout_period is not None
                    else None
                )
                while closed_streams < len(readers):
                    if deadline is not None and time.monotonic() >= deadline and process.poll() is None:
                        timed_out = True
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                        if process.poll() is None:
                            process.kill()
                        output_parts.append(
                            f"\nProcess exceeded timeout of {self.conf.running_timeout_period} seconds and was terminated.\n",
                        )
                        deadline = None
                    try:
                        label, chunk = output_queue.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if chunk is None:
                        closed_streams += 1
                        continue
                    output_parts.append(chunk)
                    if self.conf.live_output:
                        Console().print(chunk.rstrip("\r\n"), markup=False)

                process.wait()
                process.stdout.close()
                process.stderr.close()
                return_code = 124 if timed_out else process.returncode
                combined_output = "".join(output_parts)
                if not self.conf.live_output:
                    Console().print(combined_output, end="", markup=False)
            else:
                process = subprocess.Popen(
                    entry,
                    cwd=cwd,
                    env={**os.environ, **env},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=True,
                    bufsize=1,
                    universal_newlines=True,
                )

                # Setup polling
                if process.stdout is None or process.stderr is None:
                    raise RuntimeError("The subprocess did not correctly create stdout/stderr pipes")

                if self.conf.live_output:
                    stdout_fd = process.stdout.fileno()
                    stderr_fd = process.stderr.fileno()

                    poller = select.poll()
                    poller.register(stdout_fd, select.POLLIN)
                    poller.register(stderr_fd, select.POLLIN)

                    combined_output = ""
                    while True:
                        if process.poll() is not None:
                            break
                        events = poller.poll(100)
                        for fd, event in events:
                            if event & select.POLLIN:
                                if fd == stdout_fd:
                                    while True:
                                        output = process.stdout.readline()
                                        if output == "":
                                            break
                                        Console().print(output.strip(), markup=False)
                                        combined_output += output
                                elif fd == stderr_fd:
                                    while True:
                                        error = process.stderr.readline()
                                        if error == "":
                                            break
                                        Console().print(error.strip(), markup=False)
                                        combined_output += error

                    # Capture any final output
                    remaining_output, remaining_error = process.communicate()
                    if remaining_output:
                        Console().print(remaining_output.strip(), markup=False)
                        combined_output += remaining_output
                    if remaining_error:
                        Console().print(remaining_error.strip(), markup=False)
                        combined_output += remaining_error
                else:
                    # Sacrifice real-time output to avoid possible standard I/O hangs
                    out, err = process.communicate()
                    Console().print(out, end="", markup=False)
                    Console().print(err, end="", markup=False)
                    combined_output = out + err

                return_code = process.returncode
            print(Rule("[bold green]LocalEnv Logs End[/bold green]", style="dark_orange"))

            return combined_output, return_code


class CondaConf(LocalConf):
    conda_env_name: str
    default_entry: str = "python main.py"

    @model_validator(mode="after")
    def change_bin_path(self, **data: Any) -> CondaConf:
        self._update_bin_path()
        return self

    def _update_bin_path(self) -> None:
        """Update bin_path by querying the conda environment's PATH.

        This is called during initialization and can be called again after prepare()
        to ensure bin_path is set correctly even if the conda env was just created.
        """
        if os.name == "nt":
            command = [
                "conda",
                "run",
                "-n",
                self.conda_env_name,
                "--no-capture-output",
                "python",
                "-c",
                "import os; print(os.environ['PATH'])",
            ]
            conda_path_result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.bin_path = conda_path_result.stdout.strip() if conda_path_result.returncode == 0 else self.bin_path
        else:
            conda_path_result = subprocess.run(
                f"conda run -n {self.conda_env_name} --no-capture-output env | grep '^PATH='",
                capture_output=True,
                text=True,
                shell=True,
            )
            self.bin_path = conda_path_result.stdout.strip().split("=")[1] if conda_path_result.returncode == 0 else ""


class MLECondaConf(CondaConf):
    enable_cache: bool = False  # aligning with the docker settings.


## Docker Environment -----
class DockerConf(EnvConf):
    build_from_dockerfile: bool = False
    dockerfile_folder_path: Path | None = (
        None  # the path to the dockerfile optional path provided when build_from_dockerfile is False
    )
    image: str  # the image you want to build
    mount_path: str  # the path in the docker image to mount the folder
    default_entry: str  # the entry point of the image

    extra_volumes: dict = {}
    """It accept a dict of volumes, which can be either
    {<host_path>: <container_path>} or
    {<host_path>: {"bind": <container_path>, "mode": <mode, ro/rw/default is extra_volume_mode>}}
    """
    extra_volume_mode: str = "ro"  # by default. only the mount_path should be writable, others are changed to read-only

    exclude_chmod_paths: list[str] = []
    """List of directory names to exclude from chmod -R 777 operation.
    This prevents modifying permissions of read-only or specially configured directories."""

    # Declarative configuration for auto-populating exclude_chmod_paths from share.yaml
    # Subclasses can override these to specify which config keys to read
    _scenario_name: str | None = None  # e.g., "data_science", "finetune"
    _exclude_path_keys: list[str] = []  # e.g., ["input_path", "cache_path"]

    # Sometime, we need maintain some extra data for the workspace.
    # And the extra data may be shared and the downloading can be time consuming.
    # So we just want to download it once.
    network: str | None = "bridge"  # the network mode for the docker
    shm_size: str | None = None
    enable_gpu: bool = True  # because we will automatically disable GPU if not available. So we enable it by default.
    mem_limit: str | None = "48g"  # Add memory limit attribute
    cpu_count: int | None = None  # Add CPU limit attribute

    running_timeout_period: int | None = 3600  # 1 hour

    enable_cache: bool = True  # enable the cache mechanism

    retry_count: int = 5  # retry count for the docker run
    retry_wait_seconds: int = 10  # retry wait seconds for the docker run
    save_logs_to_file: bool = True
    terminal_tail_lines: int = 20

    @model_validator(mode="after")
    def populate_exclude_chmod_paths(self) -> DockerConf:
        """
        Automatically populate exclude_chmod_paths from share.yaml configuration.

        This method reads path configurations from scenarios/<scenario_name>/share.yaml
        based on _scenario_name and _exclude_path_keys class attributes.
        """
        if not self.exclude_chmod_paths and self._scenario_name and self._exclude_path_keys:
            # Extract directory names from scenario configuration
            self.exclude_chmod_paths = [
                name
                for key in self._exclude_path_keys
                if (
                    name := extract_dir_name_from_path_config(
                        T(f"scenarios.{self._scenario_name}.share:scen.{key}").r(),
                    )
                )
            ]
        return self


class QlibCondaConf(CondaConf):
    conda_env_name: str = "rdagent4qlib"
    enable_cache: bool = False
    default_entry: str = "qrun conf.yaml"
    # extra_volumes: dict = {str(Path("~/.qlib/").expanduser().resolve().absolute()): "/root/.qlib/"}


class QlibCondaEnv(LocalEnv[QlibCondaConf]):
    def prepare(self) -> None:
        """Prepare the conda environment if not already created."""
        try:
            envs = subprocess.run("conda env list", capture_output=True, text=True, shell=True)
            if self.conf.conda_env_name not in envs.stdout:
                print(f"[yellow]Conda env '{self.conf.conda_env_name}' not found, creating...[/yellow]")
                subprocess.check_call(
                    f"conda create -y -n {self.conf.conda_env_name} python=3.10",
                    shell=True,
                )
                subprocess.check_call(
                    f"conda run -n {self.conf.conda_env_name} pip install --upgrade pip cython",
                    shell=True,
                )
                subprocess.check_call(
                    f"conda run -n {self.conf.conda_env_name} pip install git+https://github.com/microsoft/qlib.git@2fb9380b342556ddb50a4b24e4fe8655d548b2b8",
                    shell=True,
                )
                subprocess.check_call(
                    f"conda run -n {self.conf.conda_env_name} pip install catboost xgboost tables torch",
                    shell=True,
                )

        except Exception as e:
            print(f"[red]Failed to prepare conda env: {e}[/red]")


# ========== Conda Environment Configuration Loader ==========
# Config files location: rdagent/scenarios/finetune/env/conda/

FT_CONDA_CONFIG_DIR = Path(__file__).parent.parent / "scenarios" / "finetune" / "env" / "conda"

# Track which conda environments have been prepared in this process
# This avoids redundant pip install checks that produce verbose output
_CONDA_ENV_PREPARED: set[str] = set()
_CONDA_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PYTHON_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


def _sync_conda_cache_with_real_envs() -> None:
    """Ensure the prepared cache includes environments that already exist on disk."""
    try:
        result = subprocess.run(
            ["conda", "env", "list"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - best-effort helper
        logger.warning(f"Failed to inspect conda env list: {exc}")
        return

    env_names: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Lines look like: "base                  *  /opt/conda"
        first_column = line.split()[0]
        name = first_column.replace("*", "").strip()
        if name:
            env_names.add(name)

    _CONDA_ENV_PREPARED.update(env_names)


def _prepare_conda_env(env_name: str, requirements_file: Path, python_version: str = "3.10") -> None:
    """Prepare conda environment with dependencies from requirements.txt.

    Creates the env if it doesn't exist, then installs dependencies.
    Uses a process-level cache to avoid redundant preparation in the same run.

    Args:
        env_name: Conda environment name
        requirements_file: Path to requirements.txt file
        python_version: Python version for the environment
    """
    if not _CONDA_ENV_NAME_RE.fullmatch(env_name):
        message = f"Invalid conda environment name: {env_name}"
        raise ValueError(message)
    if not _PYTHON_VERSION_RE.fullmatch(python_version):
        message = f"Invalid Python version: {python_version}"
        raise ValueError(message)

    requirements_file = requirements_file.resolve()
    _sync_conda_cache_with_real_envs()
    if env_name not in _CONDA_ENV_PREPARED:
        print(f"[yellow]Creating conda env '{env_name}' (Python {python_version})...[/yellow]")
        subprocess.check_call(["conda", "create", "-y", "-n", env_name, f"python={python_version}"])
        subprocess.check_call(["conda", "run", "-n", env_name, "pip", "install", "--upgrade", "pip"])

    print(f"[yellow]Installing dependencies from {requirements_file.name}...[/yellow]")
    subprocess.check_call(["conda", "run", "-n", env_name, "pip", "install", "-r", str(requirements_file)])
    print(f"[green]Conda env '{env_name}' ready[/green]")

    _CONDA_ENV_PREPARED.add(env_name)


# ========== FT (LLaMA Factory) Conda Environment ==========
class FTCondaConf(CondaConf):
    """Conda configuration for LLM fine-tuning environment."""

    model_config = SettingsConfigDict(env_prefix="FT_CONDA_")

    conda_env_name: str = "llm_finetune"
    default_entry: str = "llamafactory-cli version"
    enable_cache: bool = False


class FTCondaEnv(LocalEnv[FTCondaConf]):
    """LLaMA Factory Conda Environment with auto-dependency installation.

    Requirements: rdagent/scenarios/finetune/conda/llm_finetune_requirements.txt
    Docker equivalent: rdagent/scenarios/finetune/docker/llm_finetune_docker/Dockerfile
    """

    def prepare(self) -> None:
        try:
            # Skip if already prepared
            _sync_conda_cache_with_real_envs()
            if self.conf.conda_env_name in _CONDA_ENV_PREPARED:
                return

            # Step 1: Install base dependencies (torch, llamafactory, etc.)
            req_file = FT_CONDA_CONFIG_DIR / "llm_finetune_requirements.txt"
            _prepare_conda_env(self.conf.conda_env_name, req_file)

            # Step 2: Install flash-attn (requires torch first, uses --no-build-isolation)
            # --no-cache-dir: avoid cross-filesystem hardlink error when /tmp and ~/.cache/pip are on different mounts
            # Note: flash-attn>=2.8 is required for B200 (sm_100) support
            print("[yellow]Installing flash-attn (compiling, may take a few minutes)...[/yellow]")
            subprocess.check_call(
                [
                    "conda",
                    "run",
                    "-n",
                    self.conf.conda_env_name,
                    "pip",
                    "install",
                    "flash-attn>=2.8",
                    "--no-build-isolation",
                    "--no-cache-dir",
                ],
            )

            # Re-update bin_path after prepare() in case the conda env was just created
            if not self.conf.bin_path:
                self.conf._update_bin_path()
        except Exception as e:
            print(f"[red]Failed to prepare LLaMA Factory conda env: {e}[/red]")


# ========== Benchmark (OpenCompass) Conda Environment ==========
class BenchmarkCondaConf(CondaConf):
    """Conda configuration for OpenCompass benchmark evaluation."""

    model_config = SettingsConfigDict(env_prefix="BENCHMARK_CONDA_")

    conda_env_name: str = "opencompass"
    default_entry: str = "opencompass --help"
    enable_cache: bool = False
    env_dict: dict = {"COMPASS_DATA_CACHE": "/benchmarks/opencompass_data"}


class BenchmarkCondaEnv(LocalEnv[BenchmarkCondaConf]):
    """OpenCompass Conda Environment with auto-dependency installation.

    Requirements: rdagent/scenarios/finetune/conda/opencompass_requirements.txt
    Docker equivalent: rdagent/scenarios/finetune/docker/opencompass/Dockerfile
    """

    def prepare(self) -> None:
        try:
            # Skip if already prepared
            _sync_conda_cache_with_real_envs()
            if self.conf.conda_env_name in _CONDA_ENV_PREPARED:
                return
            req_file = FT_CONDA_CONFIG_DIR / "opencompass_requirements.txt"
            _prepare_conda_env(self.conf.conda_env_name, req_file)
            # Re-update bin_path after prepare() in case the conda env was just created
            if not self.conf.bin_path:
                self.conf._update_bin_path()
        except Exception as e:
            print(f"[red]Failed to prepare OpenCompass conda env: {e}[/red]")


class QlibDockerConf(DockerConf):
    model_config = SettingsConfigDict(
        env_prefix="QLIB_DOCKER_",
        env_parse_none_str="None",  # Nthis is the key to accept `RUNNING_TIMEOUT_PERIOD=None`
    )

    build_from_dockerfile: bool = True
    dockerfile_folder_path: Path = Path(__file__).parent.parent / "scenarios" / "qlib" / "docker"
    image: str = "local_qlib:latest"
    mount_path: str = "/workspace/qlib_workspace/"
    default_entry: str = "qrun conf.yaml"
    extra_volumes: dict = {
        str(Path("~/.qlib/").expanduser().resolve().absolute()): {
            "bind": "/root/.qlib/",
            "mode": "rw",
        },
    }
    shm_size: str | None = "16g"
    enable_gpu: bool = True
    enable_cache: bool = False
    save_logs_to_file: bool = True  # Explicitly inherit from DockerConf for compatibility


class KGDockerConf(DockerConf):
    model_config = SettingsConfigDict(env_prefix="KG_DOCKER_")

    build_from_dockerfile: bool = True
    dockerfile_folder_path: Path = Path(__file__).parent.parent / "scenarios" / "kaggle" / "docker" / "kaggle_docker"
    image: str = "local_kg:latest"
    # image: str = "gcr.io/kaggle-gpu-images/python:latest"
    mount_path: str = "/workspace/kg_workspace/"
    default_entry: str = "python train.py"
    # extra_volumes: dict = {
    #     # TODO connect to the place where the data is stored
    #     Path("git_ignore_folder/data").resolve(): "/root/.data/"
    # }

    running_timeout_period: int | None = 600
    mem_limit: str | None = (
        "48g"  # Add memory limit attribute # new-york-city-taxi-fare-prediction may need more memory
    )


class DSDockerConf(DockerConf):
    model_config = SettingsConfigDict(env_prefix="DS_DOCKER_")

    build_from_dockerfile: bool = True
    dockerfile_folder_path: Path = Path(__file__).parent.parent / "scenarios" / "kaggle" / "docker" / "DS_docker"
    image: str = "local_ds:latest"
    mount_path: str = "/kaggle/workspace"
    default_entry: str = "python main.py"

    running_timeout_period: int | None = 600
    mem_limit: str | None = (
        "48g"  # Add memory limit attribute # new-york-city-taxi-fare-prediction may need more memory
    )

    # Declarative configuration: automatically loads from scenarios/data_science/share.yaml
    _scenario_name: str = "data_science"
    _exclude_path_keys: list[str] = ["input_path", "cache_path"]


class MLEBDockerConf(DockerConf):
    model_config = SettingsConfigDict(env_prefix="MLEB_DOCKER_")

    build_from_dockerfile: bool = True
    dockerfile_folder_path: Path = Path(__file__).parent.parent / "scenarios" / "kaggle" / "docker" / "mle_bench_docker"
    image: str = "local_mle:latest"
    # image: str = "gcr.io/kaggle-gpu-images/python:latest"
    mount_path: str = "/workspace/data_folder/"
    default_entry: str = "mlebench prepare --all"
    # extra_volumes: dict = {
    #     # TODO connect to the place where the data is stored
    #     Path("git_ignore_folder/data").resolve(): "/root/.data/"
    # }
    mem_limit: str | None = (
        "48g"  # Add memory limit attribute # new-york-city-taxi-fare-prediction may need more memory
    )
    enable_cache: bool = False


class FTDockerConf(DockerConf):
    model_config = SettingsConfigDict(env_prefix="FT_DOCKER_")

    build_from_dockerfile: bool = True
    dockerfile_folder_path: Path = (
        Path(__file__).parent.parent / "scenarios" / "finetune" / "env" / "docker" / "llm_finetune"
    )
    image: str = "local_llm_finetune:latest"
    mount_path: str = "/workspace/"
    default_entry: str = "llamafactory-cli version"

    running_timeout_period: int | None = 36000  # 10 hours for training
    mem_limit: str | None = "48g"  # Large memory for LLM training
    shm_size: str | None = "16g"  # Shared memory for multi-GPU training
    enable_gpu: bool = True  # Enable GPU for LLM training
    enable_cache: bool = False  # Disable cache to avoid conflicts during training, True for debug

    # Override log output control for FT training
    save_logs_to_file: bool = True
    terminal_tail_lines: int = 20

    # Declarative configuration: automatically loads from scenarios/finetune/share.yaml
    _scenario_name: str = "finetune"
    _exclude_path_keys: list[str] = ["assets_path"]

    network: str | None = "host"  # Use host network for finetune access to litellm proxy

    def get_workspace_content_for_hash(self, local_path: str | Path) -> list[list[str]]:
        """Include dataset_info.json in cache key calculation."""
        content = super().get_workspace_content_for_hash(local_path)
        local_path = Path(local_path)
        # Add dataset_info.json if it exists
        # NOTE: data.json is excluded because it is a generated file
        for path in local_path.rglob("dataset_info.json"):
            content.append([str(path.relative_to(local_path)), path.read_text()])

        # Sort again to ensure deterministic order (though super is sorted, appended one might not be)
        content.sort(key=lambda x: x[0])
        return content


class BenchmarkDockerConf(DockerConf):
    """Docker configuration for OpenCompass benchmark evaluation."""

    model_config = SettingsConfigDict(env_prefix="BENCHMARK_DOCKER_")

    build_from_dockerfile: bool = True
    dockerfile_folder_path: Path = (
        Path(__file__).parent.parent / "scenarios" / "finetune" / "env" / "docker" / "opencompass"
    )
    image: str = "rdagent-opencompass:latest"
    mount_path: str = "/workspace/"
    default_entry: str = "opencompass --help"

    running_timeout_period: int | None = 3600  # 1 hour default for benchmarks
    mem_limit: str | None = "32g"  # Moderate memory for inference
    shm_size: str | None = "8g"  # Shared memory for model loading
    enable_gpu: bool = True  # Enable GPU for fast inference
    enable_cache: bool = False  # Disable cache for reproducibility

    # Benchmark-specific log settings
    save_logs_to_file: bool = True
    terminal_tail_lines: int = 50  # Show more lines for benchmark progress

    network: str | None = "host"  # Use host network for benchmark access to litellm proxy
    env_dict: dict = {"COMPASS_DATA_CACHE": "/benchmarks/opencompass_data"}


# physionet.org/files/mimic-eicu-fiddle-feature/1.0.0/FIDDLE_mimic3
class DockerEnv(Env[DockerConf]):
    # TODO: Save the output into a specific file

    def prepare(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        """
        Download image if it doesn't exist
        """
        if docker is None:
            raise RuntimeError("Docker SDK is missing. Install the Docker backend dependencies to use DockerEnv.")
        client = get_docker_client()
        if (
            self.conf.build_from_dockerfile
            and self.conf.dockerfile_folder_path is not None
            and self.conf.dockerfile_folder_path.exists()
        ):
            logger.info(f"Building the image from dockerfile: {self.conf.dockerfile_folder_path}")
            resp_stream = client.api.build(
                path=str(self.conf.dockerfile_folder_path),
                tag=self.conf.image,
                network_mode=self.conf.network,
            )
            if isinstance(resp_stream, str):
                logger.info(resp_stream)
            # NOTE: deliberately no rich Progress bar here. On legacy Windows consoles
            # (cp1252) rich's spinner characters raise
            # `UnicodeEncodeError: 'charmap' codec can't encode character '\u2839'`
            # while unwinding, which masks the real build error. Plain logging is portable.
            last_reported = ""
            for part in resp_stream:
                lines = part.decode("utf-8", errors="replace").split("\r\n")
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        status_dict = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug(f"[docker build] {line}")
                        continue
                    if "error" in status_dict:
                        raise RuntimeError(f"Docker build failed: {status_dict['error']}")
                    if "stream" in status_dict:
                        text = str(status_dict["stream"]).strip()
                        if text and text != last_reported:
                            last_reported = text
                            logger.info(f"[docker build] {text}")
            logger.info(f"Finished building the image from dockerfile: {self.conf.dockerfile_folder_path}")
        try:
            client.images.get(self.conf.image)
        except docker.errors.ImageNotFound:
            image_pull = client.api.pull(self.conf.image, stream=True, decode=True)
            layer_set = set()
            completed_layers = 0
            # Plain logging instead of a rich Progress bar: see the note above about
            # cp1252 consoles raising UnicodeEncodeError on rich spinner glyphs.
            for line in image_pull:
                if "error" in line:
                    raise RuntimeError(f"Error while pulling image: {line['error']}")

                layer_id = line.get("id", "")
                status = line.get("status", "")

                if layer_id not in layer_set:
                    layer_set.add(layer_id)
                    logger.info(f"[docker pull] layer {layer_id} {status}")
                elif status in ("Pull complete", "Already exists"):
                    completed_layers += 1
                    logger.info(f"[docker pull] {completed_layers}/{len(layer_set)} layers completed")
        except docker.errors.APIError as e:
            raise RuntimeError(f"Error while pulling the image: {e}")

    def _gpu_kwargs(self, client: Any) -> dict:
        """get gpu kwargs based on its availability.

        Supports GPU selection via CUDA_VISIBLE_DEVICES environment variable.
        If set, only the specified GPUs will be available in the container.
        Example: CUDA_VISIBLE_DEVICES=0,1 will only expose GPU 0 and 1.
        """
        if not self.conf.enable_gpu:
            return {}

        # Check if specific GPUs are requested via CUDA_VISIBLE_DEVICES
        cuda_visible = os.environ.get("CUDA_VISIBLE_DEVICES")
        if cuda_visible:
            # Use device_ids to specify exact GPUs (cannot use count with device_ids)
            device_ids = [gpu.strip() for gpu in cuda_visible.split(",") if gpu.strip()]
            gpu_kwargs = {
                "device_requests": [docker.types.DeviceRequest(device_ids=device_ids, capabilities=[["gpu"]])],
            }
            logger.info(f"GPU selection: using specific GPUs {device_ids}")
        else:
            # Default: use all available GPUs
            gpu_kwargs = {
                "device_requests": [docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])],
            }

        def get_image(image_name: str) -> None:
            try:
                client.images.get(image_name)
            except docker.errors.ImageNotFound:
                pull_image_with_progress(image_name)

        @wait_retry(5, 10)
        def _f() -> dict:
            container = None
            try:
                get_image(self.conf.image)
                container = client.containers.run(self.conf.image, "nvidia-smi", detach=True, **gpu_kwargs)
                # Wait for container to complete
                container.wait()
                logger.info("GPU Devices are available.")
            except docker.errors.APIError:
                return {}
            finally:
                cleanup_container(container, context="GPU test")
            return gpu_kwargs

        return _f()

    def _generate_log_header(self, entry: str | None = None) -> str:
        """
        Generate a header for log files with execution info.

        Args:
            entry: Command entry that was executed

        Returns:
            Formatted header string
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = "=" * 80 + "\n"
        header += "Docker Execution Log\n"
        header += f"Timestamp: {timestamp}\n"
        header += f"Image: {self.conf.image}\n"
        if entry:
            header += f"Command: {entry}\n"
        header += "=" * 80 + "\n\n"
        return header

    def _process_container_logs(self, logs: Iterable[bytes], local_path: str = ".", entry: str | None = None) -> str:
        """
        Process Docker container logs with optional tail mode.

        This method can be controlled via configuration:
        - save_logs_to_file: Save full logs to timestamped files in logs/ subdirectory
        - terminal_tail_lines: Show only last N lines in terminal (0 = show all)

        Args:
            logs: Docker container log stream
            local_path: Path to workspace for saving log files
            entry: Command entry that was executed (for logging header)

        Returns:
            Complete log output as string
        """
        log_output = ""

        # Determine if we should use tail mode
        use_tail_mode = self.conf.terminal_tail_lines > 0
        save_to_file = self.conf.save_logs_to_file

        # Set up log file with timestamp if needed
        log_file_path = None
        if save_to_file and local_path:
            workspace = Path(local_path)

            # Create logs subdirectory
            logs_dir = workspace / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file_path = logs_dir / f"docker_execution_{timestamp}.log"

            # Write header with execution info
            header = self._generate_log_header(entry)
            with open(log_file_path, "w", encoding="utf-8") as f:
                f.write(header)

            # Also create/update a symlink to the latest log for convenience
            latest_link = logs_dir / "docker_execution_latest.log"

            print(f"[cyan]Full logs will be saved to: {log_file_path.absolute()}[/cyan]")

        # Process logs with tail mode
        if use_tail_mode:

            log_buffer: deque[str] = deque(maxlen=self.conf.terminal_tail_lines)

            def format_tail_display() -> Text:
                text = Text()
                text.append(
                    f"[Showing last {len(log_buffer)}/{self.conf.terminal_tail_lines} lines",
                    style="dim",
                )
                if log_file_path:
                    text.append(f" | Full log: {log_file_path.name}]\n", style="dim cyan")
                else:
                    text.append("]\n", style="dim")
                text.append("-" * 80 + "\n", style="dim")
                for line in log_buffer:
                    text.append(line + "\n")
                return text

            with Live(format_tail_display(), refresh_per_second=2, console=Console()) as live:
                for log in logs:
                    decoded_log = log.strip().decode()
                    log_output += decoded_log + "\n"
                    log_buffer.append(decoded_log)

                    if log_file_path:
                        with open(log_file_path, "a", encoding="utf-8") as f:
                            f.write(decoded_log + "\n")

                    live.update(format_tail_display())
        else:
            # Default behavior: show all logs
            for log in logs:
                decoded_log = log.strip().decode()
                Console().print(decoded_log, markup=False)
                log_output += decoded_log + "\n"

                if log_file_path:
                    with open(log_file_path, "a", encoding="utf-8") as f:
                        f.write(decoded_log + "\n")

        # Show log file location and create latest symlink
        if log_file_path and log_file_path.exists():
            print(f"[green]Full execution log saved to: {log_file_path.absolute()}[/green]")

            # Create or update symlink to latest log
            latest_link = log_file_path.parent / "docker_execution_latest.log"
            if latest_link.exists() or latest_link.is_symlink():
                latest_link.unlink()
            try:
                latest_link.symlink_to(log_file_path.name)
                print(f"[dim]Latest log symlink: logs/{latest_link.name} -> {log_file_path.name}[/dim]")
            except Exception:
                # Symlinks might not work on all systems (e.g., Windows without admin)
                pass

        return log_output

    def _run(
        self,
        entry: str | None = None,
        local_path: str = ".",
        env: dict | None = None,
        running_extra_volume: Mapping = MappingProxyType({}),
        **kwargs: Any,
    ) -> tuple[str, int]:
        if env is None:
            env = {}
        env["PYTHONWARNINGS"] = "ignore"
        env["TF_CPP_MIN_LOG_LEVEL"] = "2"
        env["PYTHONUNBUFFERED"] = "1"
        env["TOKENIZERS_PARALLELISM"] = "false"  # Avoid tokenizer fork warning in multi-process training
        client = get_docker_client()

        volumes = {}
        if local_path is not None:
            local_path = os.path.abspath(local_path)
            volumes[local_path] = {"bind": self.conf.mount_path, "mode": "rw"}

        if self.conf.extra_volumes is not None:
            for lp, rp in self.conf.extra_volumes.items():
                volumes[lp] = rp if isinstance(rp, dict) else {"bind": rp, "mode": self.conf.extra_volume_mode}
            cache_path = "/tmp/sample" if "/sample/" in "".join(self.conf.extra_volumes.keys()) else "/tmp/full"
            if os.name == "nt":
                # Windows has no /tmp; a POSIX-looking host path would be passed straight
                # through to the daemon and rejected ("too many colons").
                cache_path = os.path.join(tempfile.gettempdir(), "rdagent", Path(cache_path).name)
            Path(cache_path).mkdir(parents=True, exist_ok=True)
            volumes[cache_path] = {
                "bind": T("scenarios.data_science.share:scen.cache_path").r(),
                "mode": "rw",
            }
        for lp, rp in running_extra_volume.items():
            volumes[lp] = rp if isinstance(rp, dict) else {"bind": rp, "mode": self.conf.extra_volume_mode}

        volumes = normalize_volumes(cast("dict[str, str | dict[str, str]]", volumes), self.conf.mount_path)

        log_output = ""
        container: Any | None = None

        try:
            container = client.containers.run(
                image=self.conf.image,
                command=entry,
                volumes=volumes,
                environment=env,
                detach=True,
                working_dir=self.conf.mount_path,
                # auto_remove=True, # remove too fast might cause the logs not to be get
                network=self.conf.network,
                shm_size=self.conf.shm_size,
                mem_limit=self.conf.mem_limit,  # Set memory limit
                cpu_count=self.conf.cpu_count,  # Set CPU limit
                **self._gpu_kwargs(client),
            )
            assert container is not None  # Ensure container was created successfully
            logs = container.logs(stream=True)
            print(Rule("[bold green]Docker Logs Begin[/bold green]", style="dark_orange"))
            table = Table(title="Run Info", show_header=False)
            table.add_column("Key", style="bold cyan")
            table.add_column("Value", style="bold magenta")
            table.add_row("Image", self.conf.image)
            table.add_row("Container ID", container.id)
            table.add_row("Container Name", container.name)
            table.add_row("Entry", entry)
            table.add_row("Env", "\n".join(f"{k}:{v}" for k, v in env.items()))
            table.add_row("Volumes", "\n".join(f"{k}:\n  {v}" for k, v in volumes.items()))
            print(table)

            # Process logs (supports tail mode if configured)
            log_output = self._process_container_logs(logs, local_path, entry=entry)

            exit_status = container.wait()["StatusCode"]
            print(Rule("[bold green]Docker Logs End[/bold green]", style="dark_orange"))
            return log_output, exit_status
        except docker.errors.ContainerError as e:
            raise RuntimeError(f"Error while running the container: {e}")
        except docker.errors.ImageNotFound:
            raise RuntimeError("Docker image not found.")
        except docker.errors.APIError as e:
            raise RuntimeError(f"Error while running the container: {e}")
        finally:
            cleanup_container(container)

    def refresh_env(self) -> None:
        """Remove the Docker image associated with this environment."""
        client = get_docker_client()
        try:
            # Remove the specific image
            client.images.remove(image=self.conf.image, force=True)
            logger.info(f"Removed Docker image: {self.conf.image}")

            client.images.prune()
            client.api.prune_builds()
            logger.info(f"Successfully removed Docker image: {self.conf.image}")
        except docker.errors.ImageNotFound:
            logger.warning(f"Docker image not found, cannot remove: {self.conf.image}")
        except docker.errors.APIError as e:
            logger.error(f"Error while removing Docker image: {e}")
        self.prepare()


class QTDockerEnv(DockerEnv):
    """Qlib Torch Docker"""

    def __init__(self, conf: DockerConf = QlibDockerConf()):
        super().__init__(conf)

    def prepare(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        """
        Download image & data if it doesn't exist
        """
        super().prepare()
        qlib_data_path = next(iter(self.conf.extra_volumes.keys()))
        if not (Path(qlib_data_path) / "qlib_data" / "cn_data").exists():
            logger.info("We are downloading!")
            cmd = "python -m qlib.run.get_data qlib_data --target_dir ~/.qlib/qlib_data/cn_data --region cn --interval 1d --delete_old False"
            self.check_output(entry=cmd)
        else:
            logger.info("Data already exists. Download skipped.")


class KGDockerEnv(DockerEnv):
    """Kaggle Competition Docker"""

    def __init__(self, competition: str | None = None, conf: DockerConf = KGDockerConf()):
        super().__init__(conf)


class MLEBDockerEnv(DockerEnv):
    """MLEBench Docker"""

    def __init__(self, conf: DockerConf = MLEBDockerConf()):
        super().__init__(conf)


class FTDockerEnv(DockerEnv):
    """
    LLM Fine-tuning Docker Environment with improved log output control.

    FTDockerConf enables:
    - save_logs_to_file: True (saves full logs to workspace/docker_execution.log)
    - terminal_tail_lines: 20 (only shows last 20 lines in terminal)

    To customize, set environment variables:
        export FT_DOCKER_terminal_tail_lines=50  # show last 50 lines
        export FT_DOCKER_save_logs_to_file=false # disable log file
    """

    def __init__(self, conf: DockerConf = FTDockerConf()):
        super().__init__(conf)


class BenchmarkDockerEnv(DockerEnv):
    """
    OpenCompass Benchmark Docker Environment.

    Uses BenchmarkDockerConf for evaluation-specific settings:
    - Moderate memory/GPU allocation for inference
    - Longer terminal output (50 lines) to track benchmark progress
    - Automatic Dockerfile building from scenarios/finetune/docker/opencompass

    To customize, set environment variables:
        export BENCHMARK_DOCKER_running_timeout_period=7200  # 2 hours
        export BENCHMARK_DOCKER_terminal_tail_lines=100  # show last 100 lines
    """

    def __init__(self, conf: DockerConf = BenchmarkDockerConf()):
        super().__init__(conf)
