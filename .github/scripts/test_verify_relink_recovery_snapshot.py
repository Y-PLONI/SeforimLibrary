import base64
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("verify_relink_recovery_snapshot.py")


def make_snapshot(path: Path, content: str, context: str = "ספר א") -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE lines_snapshot (
            source_name TEXT NOT NULL,
            canonical_he_title TEXT NOT NULL,
            line_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            context_ref TEXT NOT NULL
        );
        CREATE TABLE lines_snapshot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE INDEX idx_ls_book ON lines_snapshot(source_name, canonical_he_title, line_index);
        """
    )
    connection.execute(
        "INSERT INTO lines_snapshot VALUES(?,?,?,?,?)",
        ("Sefaria", "ספר", 0, content, context),
    )
    for key, value in (
        ("book_count", "1"),
        ("context_policy", "explicit-relative-v1"),
        ("line_count", "1"),
        ("schema_version", "2"),
        ("source_db", "seforim.db"),
    ):
        connection.execute("INSERT INTO lines_snapshot_meta VALUES(?,?)", (key, value))
    connection.commit()
    connection.close()


class RecoverySnapshotVerifierTest(unittest.TestCase):
    def run_case(self, original_content, rebuilt_content, artifact_record=None, rebuilt_context="ספר א",
                 meta_schema=2, baseline_schema=2):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        original, rebuilt = root / "original.db", root / "rebuilt.db"
        make_snapshot(original, original_content)
        make_snapshot(rebuilt, rebuilt_content, rebuilt_context)
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        payload_meta = root / "meta.json"
        payload_meta.write_text(
            json.dumps({"schema_version": meta_schema, "snapshot": {"sha256": digest}}),
            encoding="utf-8",
        )
        baseline = root / "baseline.json"
        baseline.write_text(
            json.dumps({"schema_version": baseline_schema, "snapshot_sha256": digest}),
            encoding="utf-8",
        )
        artifacts = root / "artifacts" / "Sefaria"
        artifacts.mkdir(parents=True)
        if artifact_record is not None:
            (artifacts / "ספר.jsonl").write_text(
                json.dumps(artifact_record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        return subprocess.run(
            [
                "python3", str(SCRIPT), "--original", str(original),
                "--rebuilt", str(rebuilt), "--artifacts", str(root / "artifacts"),
                "--payload-meta", str(payload_meta),
                "--baseline-manifest", str(baseline),
            ],
            text=True,
            capture_output=True,
        )

    def test_identical_snapshots_pass(self):
        result = self.run_case("טקסט", "טקסט")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unlinked_image_src_differences=0", result.stdout)

    def test_linker_meta_schema_3_passes(self):
        # The Linker writes meta.json schema 3 since a9ae2d4; the snapshot sha
        # field it binds is unchanged, so a recovery must still verify.
        result = self.run_case("טקסט", "טקסט", meta_schema=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RECOVERY_SNAPSHOT_SEMANTIC_OK", result.stdout)

    def test_unknown_linker_meta_schema_fails(self):
        result = self.run_case("טקסט", "טקסט", meta_schema=4)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Linker meta schema", result.stderr)

    def test_unknown_baseline_schema_fails(self):
        result = self.run_case("טקסט", "טקסט", baseline_schema=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("line baseline schema 2", result.stderr)

    def test_unlinked_remote_to_inline_image_passes(self):
        inline = base64.b64encode(b"png").decode()
        result = self.run_case(
            '<img src="https://textimages.sefaria.org/book/image.png">',
            f'<img src="data:image/png;base64,{inline}">',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unlinked_image_src_differences=1", result.stdout)

    def test_unlinked_image_inside_text_passes(self):
        inline = base64.b64encode(b"png").decode()
        result = self.run_case(
            'לפני <img class="diagram" src="https://textimages.sefaria.org/book/image.png"> אחרי',
            f'לפני <img class="diagram" src="data:image/png;base64,{inline}"> אחרי',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unlinked_image_src_differences=1", result.stdout)

    def test_only_changed_remote_sources_may_be_embedded(self):
        inline = base64.b64encode(b"png").decode()
        result = self.run_case(
            '<img src="data:image/png;base64,c2FtZQ=="> '
            '<img src="https://textimages.sefaria.org/book/image.png">',
            '<img src="data:image/png;base64,c2FtZQ=="> '
            f'<img src="data:image/png;base64,{inline}">',
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_other_image_src_change_fails(self):
        result = self.run_case(
            '<img src="https://example.com/old.png">',
            '<img src="https://example.com/new.png">',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("text differs", result.stderr)

    def test_text_around_image_difference_fails(self):
        inline = base64.b64encode(b"png").decode()
        result = self.run_case(
            'טקסט ישן <img src="https://textimages.sefaria.org/book/image.png">',
            f'טקסט חדש <img src="data:image/png;base64,{inline}">',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("text differs", result.stderr)

    def test_linked_image_difference_fails(self):
        inline = base64.b64encode(b"png").decode()
        record = {
            "book_key": {"source_name": "Sefaria", "canonical_he_title": "ספר"},
            "line_index": 0,
        }
        result = self.run_case(
            '<img src="https://textimages.sefaria.org/book/image.png">',
            f'<img src="data:image/png;base64,{inline}">',
            artifact_record=record,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has a Linker record", result.stderr)

    def test_linked_mixed_content_image_difference_fails(self):
        inline = base64.b64encode(b"png").decode()
        record = {
            "book_key": {"source_name": "Sefaria", "canonical_he_title": "ספר"},
            "line_index": 0,
        }
        result = self.run_case(
            'טקסט <img src="https://textimages.sefaria.org/book/image.png">',
            f'טקסט <img src="data:image/png;base64,{inline}">',
            artifact_record=record,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has a Linker record", result.stderr)

    def test_text_difference_fails(self):
        result = self.run_case("טקסט ישן", "טקסט חדש")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("text differs", result.stderr)

    def test_context_difference_fails(self):
        result = self.run_case("טקסט", "טקסט", rebuilt_context="ספר ב")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("context_ref differs", result.stderr)


if __name__ == "__main__":
    unittest.main()
