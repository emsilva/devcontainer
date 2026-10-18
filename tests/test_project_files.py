"""Repository replication is deliberately retired from the runtime package."""
from pathlib import Path
import unittest
from devcontainer_setup.project_sync import replication, rpc, workspace

class RetiredRepositoryReplicationTests(unittest.TestCase):
    def test_source_snapshot_and_restore_module_removed(self):
        self.assertFalse((Path(replication.__file__).parent / 'project_files.py').exists())
        self.assertNotIn('restore', rpc.OPERATIONS)
        self.assertFalse(hasattr(workspace, '_recover'))
        self.assertFalse(hasattr(workspace, 'local'))
        self.assertFalse(hasattr(workspace, 'local_project'))
