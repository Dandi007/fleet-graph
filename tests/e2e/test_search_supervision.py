"""搜索监督规则单元测试；合成索引仅用于故障反例，不是 E2E 证据。"""

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "search_supervision", Path(__file__).parent / "services/work_folder_search.py"
)
search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(search)


class SearchIndexHealthTests(unittest.TestCase):
    def setUp(self):
        runtime = Path(__file__).resolve().parents[2] / ".runtime/e2e"
        runtime.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=runtime)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "work-folder"
        self.storage = Path(self.temp.name) / "search"
        self.root.mkdir()
        (self.storage / "lancedb").mkdir(parents=True)
        self.file = self.root / "INDEX.md"
        self.file.write_text("# Work Folder INDEX\n\n监督测试\n")
        source_id = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()
        digest = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.record = {
            "relative_path": "INDEX.md",
            "sha256": digest,
            "source_root": str(self.root.resolve()),
            "source_id": source_id,
        }
        self.manifest = {
            "sources": [{"root": str(self.root.resolve()), "source_id": source_id}],
            "files": {"INDEX.md": {"sha256": digest, "chunks": 1}},
            "chunk_count": 1,
        }
        self.save()

    def save(self):
        (self.storage / "index-manifest.json").write_text(json.dumps(self.manifest))
        (self.storage / "lancedb/chunks.jsonl").write_text(json.dumps(self.record) + "\n")

    def test_current_index_is_valid(self):
        self.assertEqual(search.validate_index(self.root, self.storage)["indexed_files"], 1)

    def test_deleted_chunks_are_not_an_empty_healthy_search(self):
        (self.storage / "lancedb/chunks.jsonl").unlink()
        with self.assertRaises(OSError):
            search.validate_index(self.root, self.storage)

    def test_old_index_after_edit_is_rejected(self):
        self.file.write_text("# 当前正文已经改变\n")
        with self.assertRaisesRegex(RuntimeError, "落后"):
            search.validate_index(self.root, self.storage)

    def test_index_missing_new_document_is_rejected(self):
        (self.root / "new.md").write_text("# 尚未索引\n")
        with self.assertRaisesRegex(RuntimeError, "集合"):
            search.validate_index(self.root, self.storage)

    def test_chunk_from_other_source_is_rejected(self):
        self.record["source_root"] = "/different-source"
        self.save()
        with self.assertRaisesRegex(RuntimeError, "来源"):
            search.validate_index(self.root, self.storage)

    def test_partial_refresh_is_rejected(self):
        (self.storage / "lancedb/chunks.jsonl").write_text('{"partial":')
        with self.assertRaises(ValueError):
            search.validate_index(self.root, self.storage)


if __name__ == "__main__":
    unittest.main()
