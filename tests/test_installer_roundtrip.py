"""Exercise the desktop/facade/helper protocol without privilege or disk writes."""

import contextlib
import copy
import io
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'installer'))
from zeus_installer import InstallerError, backend, gui


def reviewed_plan(fingerprint='target-a'):
    return {'schema_version': 1, 'supported': True, 'blockers': [],
            'fingerprint': fingerprint, 'allocation_gib': 128,
            'target': {'fingerprint': fingerprint}}


class DesktopHelperRoundTripTests(unittest.TestCase):
    def exercise(self, *, changed_target=False, existing=None):
        calls = []
        downloaded = []
        initial = reviewed_plan()
        service = types.SimpleNamespace(
            status=lambda: copy.deepcopy(existing) if existing else {
                'ok': True, 'state': 'idle', 'phase': 'idle'},
            prepare=lambda selected: downloaded.append(copy.deepcopy(selected)) or {
                'ok': True, 'state': 'prepared', 'phase': 'prepared', 'prepared': True},
        )

        def invoke(argv, **kwargs):
            self.assertEqual(argv[0], backend.PKEXEC_COMMAND)
            self.assertFalse(kwargs['shell'])
            helper_index = argv.index(backend.HELPER_COMMAND)
            helper_args = argv[helper_index + 1:]
            calls.append(helper_args[0])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = backend.helper_main(helper_args, backend=service)
            return subprocess.CompletedProcess(argv, code, stdout=output.getvalue(), stderr='')

        plans = [copy.deepcopy(initial), reviewed_plan('target-b' if changed_target else 'target-a')]
        with mock.patch.object(backend.os, 'geteuid', return_value=0), \
                mock.patch.object(backend, '_root_preflight_plan', side_effect=plans) as root_check, \
                mock.patch.object(backend.subprocess, 'run', side_effect=invoke):
            controller = gui.InstallerController(backend_module=backend)
            controller.refresh(128)
            if existing:
                self.assertFalse(controller.can_prepare())
            elif changed_target:
                with self.assertRaisesRegex(InstallerError, 'target layout changed'):
                    controller.prepare()
            else:
                self.assertEqual(controller.plan['fingerprint'], initial['fingerprint'])
                controller.prepare()
                self.assertTrue(controller.prepared)
            count = root_check.call_count
        return calls, downloaded, count

    def test_one_review_and_one_prepare_keep_both_root_target_checks(self):
        calls, downloaded, root_checks = self.exercise()
        self.assertEqual(calls, ['review', 'prepare'])
        self.assertEqual(root_checks, 2)
        self.assertEqual(len(downloaded), 1)
        self.assertEqual(downloaded[0]['fingerprint'], 'target-a')

    def test_target_change_after_review_prevents_download(self):
        calls, downloaded, root_checks = self.exercise(changed_target=True)
        self.assertEqual(calls, ['review', 'prepare'])
        self.assertEqual(root_checks, 2)
        self.assertEqual(downloaded, [])

    def test_existing_operation_is_not_replaced_by_a_fresh_plan(self):
        existing = {'ok': True, 'state': 'downloading', 'phase': 'downloading',
                    'plan': reviewed_plan(), 'progress': {'bytes': 256, 'total': 1024}}
        calls, downloaded, root_checks = self.exercise(existing=existing)
        self.assertEqual(calls, ['review'])
        self.assertEqual(root_checks, 0)
        self.assertEqual(downloaded, [])


if __name__ == '__main__':
    unittest.main()
