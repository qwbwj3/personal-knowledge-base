"""Model the observed host mkdir defect without disabling any host protection.

The shim double raises PermissionError for existing nonrecursive directories,
as WorkBuddy's broker does. It delegates ordinary operations to pathlib; no
broker/native escape route is used by the implementation under test.
"""
import contextlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_multiformat
import search_index
import install_lifecycle
import ocr_runtime

ORIGINAL_MKDIR = Path.mkdir


def host_mkdir(path, mode=0o777, parents=False, exist_ok=False):
    if path.exists() and not parents:
        raise PermissionError('EEXIST: observed host broker test double')
    return ORIGINAL_MKDIR(path, mode=mode, parents=parents, exist_ok=exist_ok)


class ReachedNextStage(Exception):
    pass


class HostMkdirTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='pkb-host-mkdir-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_search_reuses_existing_directory_and_preserves_lock(self):
        directory = self.root / 'derived-search'
        directory.mkdir()
        with patch.object(Path, 'mkdir', host_mkdir):
            with search_index.locked(self.root) as database:
                self.assertEqual(database, directory / 'index.sqlite3')
                self.assertTrue((directory / 'index.lock').is_file())

    def test_search_missing_directory_still_uses_intercepted_creation(self):
        calls = []
        def traced(path, **kwargs):
            calls.append(path)
            return host_mkdir(path, **kwargs)
        with patch.object(Path, 'mkdir', traced):
            with search_index.locked(self.root):
                pass
        self.assertEqual(calls, [self.root / 'derived-search'])

    def test_search_actual_creation_denial_is_not_swallowed(self):
        denied = PermissionError('actual host policy denial')
        with patch.object(Path, 'mkdir', side_effect=denied):
            with self.assertRaises(PermissionError) as caught:
                with search_index.locked(self.root):
                    pass
        self.assertIs(caught.exception, denied)

    def test_search_file_and_symlink_are_not_treated_as_existing_directory(self):
        directory = self.root / 'derived-search'
        directory.write_text('not a directory')
        with self.assertRaises(OSError):
            with search_index.locked(self.root):
                pass
        directory.unlink()
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            directory.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if sys.platform == 'win32':
                self.skipTest('Windows user cannot create synthetic symlinks: ' + str(exc))
            raise
        with self.assertRaises(OSError):
            with search_index.locked(self.root):
                pass
        self.assertFalse((outside / 'index.lock').exists())

    def lifecycle(self):
        lifecycle = install_lifecycle.Lifecycle.__new__(install_lifecycle.Lifecycle)
        lifecycle.target = self.root / 'installed'
        lifecycle.manager = self.root / '.installed.install-manager'
        return lifecycle

    def test_installer_existing_manager_reaches_recovery_under_host_shim(self):
        lifecycle = self.lifecycle()
        lifecycle.manager.mkdir()
        with patch.object(Path, 'mkdir', host_mkdir), patch.object(
                lifecycle, 'recover', side_effect=ReachedNextStage):
            with self.assertRaises(ReachedNextStage):
                lifecycle.run('update')

    def test_installer_missing_manager_denial_is_not_swallowed(self):
        lifecycle = self.lifecycle()
        denied = PermissionError('actual host policy denial')
        def deny_manager(path, **kwargs):
            if path == lifecycle.manager:
                raise denied
            return host_mkdir(path, **kwargs)
        with patch.object(Path, 'mkdir', deny_manager):
            with self.assertRaises(PermissionError) as caught:
                lifecycle.run('update')
        self.assertIs(caught.exception, denied)

    def test_ocr_existing_models_reused_without_network_or_native_bypass(self):
        # Stop just before model download. Subprocess/version doubles isolate
        # directory setup; this does not claim an OCR environment was prepared.
        def existing_models(home):
            ORIGINAL_MKDIR(home / 'models')
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(ocr_runtime.sys, 'version_info', (3, 13, 0)))
            stack.enter_context(patch.object(ocr_runtime, '_compatible_ready', return_value=False))
            stack.enter_context(patch.object(ocr_runtime, '_write_marker'))
            stack.enter_context(patch.object(ocr_runtime, '_python', return_value=Path(sys.executable)))
            stack.enter_context(patch.object(ocr_runtime.subprocess, 'run'))
            stack.enter_context(patch.object(ocr_runtime, '_check_versions', side_effect=existing_models))
            stack.enter_context(patch.object(ocr_runtime, '_spec', side_effect=ReachedNextStage))
            stack.enter_context(patch.object(Path, 'mkdir', host_mkdir))
            with self.assertRaises(ReachedNextStage):
                ocr_runtime.prepare(self.root)


if __name__ == '__main__':
    unittest.main()
