"""只读验收权限准备的回归测试。"""

import stat
import tempfile
import unittest
from pathlib import Path

from permissions import prepare_readable


class PermissionTests(unittest.TestCase):
    def test_preserves_content_executable_bit_and_external_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            objects = repo / ".git/objects"
            objects.mkdir(parents=True)
            obj = objects / "object"
            obj.write_bytes(b"immutable git object")
            obj.chmod(0o600)
            objects.chmod(0o700)
            executable = repo / "cli"
            executable.write_text("cli")
            executable.chmod(0o700)
            outside = root / "outside"
            outside.write_text("outside")
            outside.chmod(0o600)
            (repo / "link").symlink_to(outside)
            result = prepare_readable(repo)
            self.assertFalse(result["content_modified"])
            self.assertEqual(obj.read_bytes(), b"immutable git object")
            self.assertEqual(stat.S_IMODE(obj.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(objects.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o744)
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o600)
            self.assertEqual(prepare_readable(repo)["changes"], [])
