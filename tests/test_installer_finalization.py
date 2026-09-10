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

    def test_error_journal_crosses_backend_transition_and_finishes_only_grub(self):
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
