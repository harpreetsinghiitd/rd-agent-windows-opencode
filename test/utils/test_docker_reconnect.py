"""Tests for the Docker Desktop reconnect helper.

`get_docker_client` exists because Docker Desktop on Windows stops itself
(auto-pause, disk pressure, WSL hiccups) and every long RD-Agent loop then dies
with "Docker is not reachable" even though a retry seconds later succeeds.
"""

import os
import unittest
from unittest import mock

import rdagent.utils.env as env_module
from rdagent.utils.env import normalize_volumes


class DockerAutostartTests(unittest.TestCase):
    def test_autostart_defaults_on_for_windows_only(self):
        with mock.patch.object(os, "name", "nt"), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RDAGENT_DOCKER_AUTOSTART", None)
            self.assertTrue(env_module._docker_autostart_enabled())
        with mock.patch.object(os, "name", "posix"):
            self.assertFalse(env_module._docker_autostart_enabled())

    def test_autostart_can_be_disabled(self):
        for value in ("0", "false", "no", "off", "FALSE"):
            with (
                mock.patch.object(os, "name", "nt"),
                mock.patch.dict(os.environ, {"RDAGENT_DOCKER_AUTOSTART": value}),
            ):
                self.assertFalse(env_module._docker_autostart_enabled(), value)


class GetDockerClientTests(unittest.TestCase):
    def test_returns_client_when_engine_is_up(self):
        sentinel = object()
        fake_docker = mock.Mock()
        fake_docker.from_env.return_value = sentinel
        with mock.patch.object(env_module, "docker", fake_docker):
            self.assertIs(env_module.get_docker_client(wait_seconds=0), sentinel)

    def test_retries_then_raises_actionable_error(self):
        fake_docker = mock.Mock()
        fake_docker.from_env.side_effect = RuntimeError("npipe not found")
        with (
            mock.patch.object(env_module, "docker", fake_docker),
            mock.patch.object(env_module, "_docker_autostart_enabled", return_value=False),
            mock.patch.object(env_module.time, "sleep"),
            self.assertRaises(RuntimeError) as ctx,
        ):
            env_module.get_docker_client(wait_seconds=0)
        self.assertIn("Docker is not reachable", str(ctx.exception))
        self.assertGreaterEqual(fake_docker.from_env.call_count, 1)

    def test_missing_sdk_message_is_specific(self):
        with (
            mock.patch.object(env_module, "docker", None),
            self.assertRaises(RuntimeError) as ctx,
        ):
            env_module.get_docker_client(wait_seconds=0)
        self.assertIn("Docker SDK is missing", str(ctx.exception))

    def test_starts_desktop_once_before_giving_up(self):
        fake_docker = mock.Mock()
        fake_docker.from_env.side_effect = RuntimeError("down")
        with (
            mock.patch.object(env_module, "docker", fake_docker),
            mock.patch.object(env_module, "_docker_autostart_enabled", return_value=True),
            mock.patch.object(env_module, "_start_docker_desktop") as starter,
            mock.patch.object(env_module.time, "sleep"),
            self.assertRaises(RuntimeError),
        ):
            env_module.get_docker_client(wait_seconds=0)
        self.assertEqual(starter.call_count, 1)


if __name__ == "__main__":
    unittest.main()


class TestNormalizeVolumesHost(unittest.TestCase):
    def test_relative_host_path_becomes_absolute(self):
        out = normalize_volumes({'rel/dir': {'bind': '/kaggle/x', 'mode': 'ro'}}, '/workspace')
        (key,) = out.keys()
        self.assertTrue(os.path.isabs(key))
        self.assertEqual(out[key]['bind'], '/kaggle/x')
