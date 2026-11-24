import json
import os
import runpy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


class DesktopDataDirectoryTest(unittest.TestCase):
    def test_checkout_serves_existing_files_and_migrates_from_its_own_templates(self):
        entry = Path(__file__).resolve().parents[1] / "server.py"
        with (
            TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                ZOTERO_PDF2ZH_DATA_DIR=tmp,
                ZOTERO_PDF2ZH_SOURCE_REVISION="test-checkout",
            ),
        ):
            data = Path(tmp)
            config = data / "config"
            config.mkdir()
            (config / "config.json").write_text(
                '{"PDF2ZH_LANG_FROM":"custom"}', encoding="utf-8"
            )
            (config / "config.json.example").write_text(
                '{"stale":true}', encoding="utf-8"
            )
            translated = data / "translated"
            translated.mkdir()
            (translated / "existing.pdf").write_bytes(b"%PDF-1.7\n%%EOF")

            service = runpy.run_path(str(entry))
            service["prepare_path"]()
            values = json.loads((config / "config.json").read_bytes())
            defaults = json.loads(
                (entry.parent / "config/config.json.example").read_bytes()
            )
            self.assertEqual(values, {**defaults, "PDF2ZH_LANG_FROM": "custom"})
            self.assertTrue((config / "config.toml").is_file())
            app = service["PDFTranslator"](SimpleNamespace(enable_venv=False)).app
            with app.test_client() as client:
                health = client.get("/health").json
                self.assertEqual(Path(health["serverFile"]), entry)
                self.assertEqual(health["sourceRevision"], "test-checkout")
                self.assertEqual(Path(health["outputDir"]), translated)
                with client.get("/translatedFile/existing.pdf", buffered=True) as reply:
                    self.assertEqual(reply.status_code, 200)
                    self.assertEqual(reply.data, b"%PDF-1.7\n%%EOF")
