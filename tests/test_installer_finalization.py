"""Backend and real executor integration at the failed GRUB boundary."""
import copy
import hashlib
import unittest

from tests import test_installer_executor as fixture_module
from zeus_installer import backend


class FinalizationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.ExecutorFixtureTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def _service_at_grub_boundary(self):
        fixture = self.fixture
        record = fixture._prepare_grub_failure_boundary()
        payload = fixture.artifact_path.read_bytes()
        artifact = {**fixture.artifact, "sha256": hashlib.sha256(payload).hexdigest(),
                    "name": fixture.artifact_path.name, "size": len(payload)}
        record["artifact"] = artifact
        record["release"] = {"archive": {"name": artifact["name"], "sha256": artifact["sha256"]}}
        fixture.journal.write(record)
        inventory = fixture._finalization_inventory()
        fixture.executor.inventory_provider = lambda: inventory
        service = backend.InstallerBackend(
            fixture.journal.root, require_root=False,
            inventory_provider=lambda: inventory,
            artifact_verify=lambda path, release: copy.deepcopy(artifact),
            maintenance_executor=fixture.executor,
            runner=fixture.runner.run,
            boot_id=lambda: fixture.boot["id"],
        )
        return service, inventory

    def test_error_journal_crosses_backend_transition_and_finishes_only_grub(self):
        fixture = self.fixture
        service, inventory = self._service_at_grub_boundary()
        self.assertTrue(service.status()["can_resume_finalization"])
        before = len(fixture.runner.calls)
        result = service.install()
        self.assertEqual(result["phase"], "installed")
        self.assertIsNone(result["error"])
        commands = [argv for argv, _kwargs in fixture.runner.calls[before:]]
        writes = [argv for argv in commands if fixture._is_mutating_command(argv)]
        self.assertEqual(writes, [[fixture_module.installer_executor.GRUB2_MKCONFIG,
                                  "--no-grubenv-update", "-o", "/boot/grub2/grub.cfg"]])
        self.assertEqual(service.journal.load()["executor_state"]["phase"], "installed")

    def test_battery_error_is_specific_and_charger_retry_finishes(self):
        service, inventory = self._service_at_grub_boundary()
        fixture = self.fixture
        record = service.journal.load()
        # Reproduce the old build's incorrectly persisted target mismatch.
        record["error"] = "target_mismatch"
        service.journal.write(record)
        inventory["power"]["ac_online"] = False
        before = len(fixture.runner.calls)
        files = copy.deepcopy(fixture.files)
        with self.assertRaises(backend.InstallError) as error:
            service.install()
        self.assertEqual(error.exception.code, "ac_required")
        self.assertIn("AC power", str(error.exception))
        self.assertEqual(fixture.files, files)
        self.assertFalse(any(fixture._is_mutating_command(argv)
                             for argv, _ in fixture.runner.calls[before:]))
        status = service.status()
        self.assertEqual(status["error"], "ac_required")
        self.assertIn("AC power", status["message"])
        self.assertTrue(status["can_resume_finalization"])
        inventory["power"]["ac_online"] = True
        self.assertEqual(service.install()["phase"], "installed")
        writes = [argv for argv, _ in fixture.runner.calls[before:]
                  if fixture._is_mutating_command(argv)]
        self.assertEqual(writes, [[fixture_module.installer_executor.GRUB2_MKCONFIG,
                                  "--no-grubenv-update", "-o", "/boot/grub2/grub.cfg"]])

    def test_retryable_error_still_rejects_changed_disk(self):
        service, inventory = self._service_at_grub_boundary()
        record = service.journal.load()
        record["error"] = "target_mismatch"
        service.journal.write(record)
        inventory["partition_table"]["partitions"][5]["size"] -= 2048
        inventory["sfdisk"] = {"partitiontable": inventory["partition_table"]}
        before = len(self.fixture.runner.calls)
        files = copy.deepcopy(self.fixture.files)
        with self.assertRaises(backend.InstallError) as error:
            service.install()
        self.assertEqual(error.exception.code, "target_mismatch")
        self.assertEqual(self.fixture.files, files)
        self.assertEqual(len(self.fixture.runner.calls), before)
