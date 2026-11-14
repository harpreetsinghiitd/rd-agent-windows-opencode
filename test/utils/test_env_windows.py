import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rdagent.utils.env import LocalConf, LocalEnv, _mirror_staged_directory


@unittest.skipUnless(os.name == "nt", "Windows LocalEnv behavior")
class WindowsLocalEnvTests(unittest.TestCase):
    def test_inherits_process_path_when_env_omits_path(self):
        with tempfile.TemporaryDirectory() as directory:
            env = LocalEnv(LocalConf(default_entry="python --version", enable_cache=False))
            result = env.run(local_path=directory, entry="python --version")
        self.assertEqual(result.exit_code, 0, result.full_stdout)
        self.assertIn("Python ", result.full_stdout)

    def test_volume_copyback_without_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "before.txt").write_text("before", encoding="utf-8")
            target = root / "mounted"
            env = LocalEnv(LocalConf(default_entry="", enable_cache=False))
            env.conf.extra_volumes = {str(source): str(target)}
            # Run in the same process to check the volume context lifecycle directly.
            env._run(local_path=directory, entry='python -c "from pathlib import Path; Path(\'mounted/after.txt\').write_text(\'after\')"')
            self.assertEqual((source / "before.txt").read_text(encoding="utf-8"), "before")
            self.assertEqual((source / "after.txt").read_text(encoding="utf-8"), "after")
            self.assertFalse(target.exists())

    def test_volume_copyback_mirrors_file_and_directory_deletions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            nested = source / "nested"
            nested.mkdir(parents=True)
            (source / "remove.txt").write_text("remove", encoding="utf-8")
            (nested / "remove.txt").write_text("remove", encoding="utf-8")
            (nested / "keep.txt").write_text("keep", encoding="utf-8")
            target = root / "mounted"
            env = LocalEnv(LocalConf(default_entry="", enable_cache=False))
            env.conf.extra_volumes = {str(source): str(target)}
            command = (
                'python -c "from pathlib import Path; '
                'Path(\'mounted/remove.txt\').unlink(); '
                'Path(\'mounted/nested/remove.txt\').unlink(); '
                'Path(\'mounted/nested/keep.txt\').write_text(\'updated\')"'
            )
            env._run(local_path=directory, entry=command)
            self.assertFalse((source / "remove.txt").exists())
            self.assertFalse((nested / "remove.txt").exists())
            self.assertEqual((nested / "keep.txt").read_text(encoding="utf-8"), "updated")
            self.assertFalse(target.exists())

    def test_rejects_staged_symlink_and_cleans_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            outside = root / "outside.txt"
            outside.write_text("untouched", encoding="utf-8")
            probe = root / "symlink-probe"
            try:
                os.symlink(str(outside), str(probe))
            except OSError as exc:
                self.skipTest(f"Symlink creation is unavailable: {exc}")
            else:
                probe.unlink()

            target = root / "mounted"
            env = LocalEnv(LocalConf(default_entry="", enable_cache=False))
            env.conf.extra_volumes = {str(source): str(target)}
            command = 'python -c "from pathlib import Path; Path(\'mounted/link\').symlink_to(\'../outside.txt\')"'
            with self.assertRaisesRegex(RuntimeError, "Refusing to copy back symlinks"):
                env._run(local_path=directory, entry=command)

            self.assertFalse(target.exists(), "staged mount must be cleaned after copyback rejection")
            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")
            self.assertEqual(list(source.iterdir()), [])

    def test_staging_failure_does_not_delete_host_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            sentinel = source / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            target = root / "mounted"
            env = LocalEnv(LocalConf(default_entry="", enable_cache=False))
            env.conf.extra_volumes = {str(source): str(target)}

            with (
                mock.patch("rdagent.utils.env.shutil.copytree", side_effect=OSError("staging failed")),
                self.assertRaisesRegex(OSError, "staging failed"),
            ):
                env._run(local_path=directory, entry="python --version")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            self.assertFalse(target.exists())

    def test_cleanup_continues_after_one_volume_copyback_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_ok = root / "source-ok"
            source_bad = root / "source-bad"
            source_ok.mkdir()
            source_bad.mkdir()
            outside = root / "outside.txt"
            outside.write_text("preserve", encoding="utf-8")
            probe = root / "symlink-probe"
            try:
                os.symlink(str(outside), str(probe))
            except OSError as exc:
                self.skipTest(f"Symlink creation is unavailable: {exc}")
            else:
                probe.unlink()

            target_ok = root / "mounted-ok"
            target_bad = root / "mounted-bad"
            env = LocalEnv(LocalConf(default_entry="", enable_cache=False))
            env.conf.extra_volumes = {str(source_ok): str(target_ok), str(source_bad): str(target_bad)}
            command = (
                'python -c "from pathlib import Path; '
                'Path(\'mounted-ok/updated.txt\').write_text(\'updated\'); '
                'Path(\'mounted-bad/link\').symlink_to(\'../outside.txt\')"'
            )

            with self.assertRaisesRegex(RuntimeError, "Refusing to copy back symlinks"):
                env._run(local_path=directory, entry=command)

            self.assertFalse(target_ok.exists())
            self.assertFalse(target_bad.exists())
            self.assertEqual((source_ok / "updated.txt").read_text(encoding="utf-8"), "updated")
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_rejects_host_symlink_volume_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            outside = root / "outside.txt"
            outside.write_text("preserve", encoding="utf-8")
            try:
                (source / "link").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"Symlink creation is unavailable: {exc}")
            target = root / "mounted"
            env = LocalEnv(LocalConf(default_entry="", enable_cache=False))
            env.conf.extra_volumes = {str(source): str(target)}

            with self.assertRaisesRegex(RuntimeError, "host volume containing symlinks"):
                env._run(local_path=directory, entry="python --version")

            self.assertFalse(target.exists())
            self.assertTrue((source / "link").is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_rejects_dangling_staged_root_symlink_without_deleting_host_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "staged"
            host = root / "host"
            host.mkdir()
            sentinel = host / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            try:
                staged.symlink_to(root / "missing-target", target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Symlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "replaced staged volume root"):
                _mirror_staged_directory(staged, host)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            self.assertTrue(staged.is_symlink())

    def test_timeout_returns_124(self):
        with tempfile.TemporaryDirectory() as directory:
            env = LocalEnv(LocalConf(default_entry="", enable_cache=False, running_timeout_period=1))
            result = env.run(local_path=directory, entry='python -c "import time; time.sleep(10)"')
        self.assertEqual(result.exit_code, 124)
        self.assertIn("exceeded timeout", result.full_stdout)


if __name__ == "__main__":
    unittest.main()
