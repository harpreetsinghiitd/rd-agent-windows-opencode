import os
import tempfile
import unittest
from pathlib import Path

from rdagent.utils.env import (
    LocalConf,
    LocalEnv,
    _cleanup_staged_volumes,
    _prepare_volume_target,
    _reject_host_volume_symlinks,
)


class VolumeTargetSafetyTests(unittest.TestCase):
    def test_removes_only_obsolete_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = root / "mount"
            target.symlink_to(source, target_is_directory=True)

            _prepare_volume_target(target)

            self.assertFalse(target.is_symlink())
            self.assertTrue(source.is_dir())

    def test_refuses_existing_directory_without_deleting_data(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "workspace"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "Refusing to replace an existing volume target"):
                _prepare_volume_target(target)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_refuses_existing_file_without_deleting_data(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "workspace-file"
            target.write_text("preserve", encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "Refusing to replace an existing volume target"):
                _prepare_volume_target(target)

            self.assertEqual(target.read_text(encoding="utf-8"), "preserve")

    def test_rejects_host_file_symlink_including_dangling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "host-link"
            try:
                target.symlink_to(root / "missing")
            except OSError as exc:
                self.skipTest(f"Symlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "host volume containing symlinks"):
                _reject_host_volume_symlinks(target)

            self.assertTrue(target.is_symlink())

    def test_rejects_descendant_symlink_without_following_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "host-volume"
            target.mkdir()
            outside = root / "outside"
            outside.write_text("preserve", encoding="utf-8")
            (target / "link").symlink_to(outside)

            with self.assertRaisesRegex(RuntimeError, "host volume containing symlinks"):
                _reject_host_volume_symlinks(target)

            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")
            self.assertTrue((target / "link").is_symlink())

    def test_cleans_all_staged_mounts_after_one_copyback_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host_good = root / "host-good"
            host_bad = root / "host-bad"
            host_good.mkdir()
            host_bad.mkdir()
            target_good = root / "target-good"
            target_bad = root / "target-bad"
            target_good.mkdir()
            target_bad.mkdir()
            (target_good / "keep.txt").write_text("preserve", encoding="utf-8")
            outside = root / "outside"
            outside.write_text("preserve", encoding="utf-8")
            try:
                (target_bad / "link").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"Symlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "Refusing to copy back symlinks"):
                _cleanup_staged_volumes(
                    [
                        (target_good, host_good, True, True, True),
                        (target_bad, host_bad, True, True, True),
                    ]
                )

            self.assertFalse(target_good.exists())
            self.assertFalse(target_bad.exists())
            self.assertEqual((host_good / "keep.txt").read_text(encoding="utf-8"), "preserve")
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    @unittest.skipUnless(os.name == "posix", "POSIX LocalEnv volume behavior")
    def test_existing_workspace_target_is_preserved_by_local_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = root / "workspace"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")

            env = LocalEnv(LocalConf(default_entry="", enable_cache=False))
            env.conf.extra_volumes = {str(source): str(target)}

            with self.assertRaisesRegex(FileExistsError, "Refusing to replace an existing volume target"):
                env._run(local_path=directory, entry="true")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            self.assertFalse(target.is_symlink())

    @unittest.skipUnless(os.name == "posix", "POSIX LocalEnv volume behavior")
    def test_existing_file_target_is_preserved_by_local_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = root / "workspace-file"
            target.write_text("preserve", encoding="utf-8")

            env = LocalEnv(LocalConf(default_entry="", enable_cache=False))
            env.conf.extra_volumes = {str(source): str(target)}

            with self.assertRaisesRegex(FileExistsError, "Refusing to replace an existing volume target"):
                env._run(local_path=directory, entry="true")

            self.assertEqual(target.read_text(encoding="utf-8"), "preserve")
            self.assertFalse(target.is_symlink())


if __name__ == "__main__":
    unittest.main()
