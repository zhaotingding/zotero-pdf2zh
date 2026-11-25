import json
import os
import runpy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch


class DesktopDataDirectoryTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows desktop launcher")
    def test_launcher_creates_one_visible_service_console_only_on_cold_start(self):
        import subprocess
        import sys

        entry = Path(__file__).resolve().parents[2] / "start_service.py"
        main = runpy.run_path(str(entry))["main"]
        with (
            patch.dict(main.__globals__, {"Tk": Mock()}),
            patch.dict(os.environ, clear=True, PATH="fixture-path"),
            patch("socket.socket") as probe,
            patch("subprocess.check_output", return_value="test-revision"),
            patch("subprocess.Popen") as launch,
            patch("tkinter.messagebox.showinfo"),
            patch.object(sys, "argv", [str(entry)]),
        ):
            check = probe.return_value.__enter__.return_value.connect_ex
            check.return_value = 10061
            self.assertEqual(main(), 0)
            command = launch.call_args.args[0]
            options = launch.call_args.kwargs
            self.assertEqual(Path(command[0]).name, "python.exe")
            self.assertEqual(Path(command[1]), entry.parent / "server" / "server.py")
            self.assertEqual(options["creationflags"], subprocess.CREATE_NEW_CONSOLE)
            self.assertTrue(
                options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
            )
            self.assertEqual(options["startupinfo"].wShowWindow, 1)
            self.assertEqual(options["env"]["ZOTERO_PDF2ZH_DESKTOP"], "1")
            self.assertFalse(any(key.startswith("PDF2ZH_OPENAI_") for key in options["env"]))
            check.return_value = 0
            main()
            launch.assert_called_once()

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
