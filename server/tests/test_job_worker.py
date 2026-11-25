import base64
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf as pdf
from utils.job_worker import run_isolated
from utils.task_manager import TaskManager


def _sample_job(exit_early, *, progress):
    if exit_early:
        os._exit(23)
    progress({"progress": 30})
    return "done"


class WorkerTests(unittest.TestCase):
    def test_layout_review_page_numbers_reach_the_client_and_task_history(self):
        import server

        def execute(_worker, args, _progress):
            path = Path(args[4])
            target = path.with_name("result.mono.pdf")
            target.write_bytes(path.read_bytes())
            return {
                "status": "success",
                "fileList": [target.name],
                "layoutWarnings": {"mono": {2: ["分数线"]}},
                "message": "成功生成 1 个文件；排版待核对的原文页码：2",
            }

        with pdf.open() as source:
            source.new_page()
            source.new_page()
            data = base64.b64encode(source.tobytes()).decode()
        with (
            TemporaryDirectory() as tmp,
            patch.object(server, "output_folder", tmp),
            patch.object(server, "task_manager", TaskManager()),
            patch.object(server, "run_isolated", side_effect=execute),
        ):
            processor = server.PDFTranslator(SimpleNamespace(enable_venv=False))
            with processor.app.test_client() as client:
                try:
                    reply = client.post(
                        "/translate",
                        json={
                            "fileName": "source.pdf",
                            "fileContent": data,
                            "engine": "pdf2zh_next",
                            "service": "openai",
                        },
                    )
                    self.assertEqual(reply.status_code, 200)
                    self.assertIn("2", reply.json["layoutWarnings"]["mono"])
                    job = client.get("/api/history").json["history"][0]
                    self.assertEqual(
                        job["result"]["layoutWarnings"], reply.json["layoutWarnings"]
                    )
                    self.assertIn("页码：2", job["result"]["message"])
                    live = client.get("/api/tasks/" + job["taskId"]).json["task"]
                    self.assertEqual(live["message"], reply.json["message"])
                finally:
                    processor.jobs.shutdown()

    def test_bad_process_does_not_break_next_job(self):
        events = []
        with self.assertRaisesRegex(RuntimeError, "0x00000017"):
            run_isolated(_sample_job, (True,), events.append)
        self.assertEqual(run_isolated(_sample_job, (False,), events.append), "done")
        self.assertEqual(events, [{"progress": 30}])

    def test_queued_same_name_files_remain_separate_and_fetchable(self):
        import server

        started, release = threading.Event(), threading.Event()
        order = []

        def execute(_worker, arguments, _on_progress):
            path = Path(arguments[4])
            payload = path.read_bytes()
            order.append(payload)
            if len(order) == 1:
                started.set()
                if not release.wait(10):
                    raise RuntimeError("gate timed out")
            output = path.with_name("same.cut.pdf")
            output.write_bytes(payload)
            return {"status": "success", "fileList": [output.name]}

        def submit(client, data):
            return client.post(
                "/crop",
                json={
                    "fileName": "same.pdf",
                    "asyncJob": True,
                    "fileContent": base64.b64encode(data).decode(),
                },
                headers={"X-PDF2zh-Task-Files": "1"},
            ).json["taskId"]

        manager = TaskManager()
        with (
            TemporaryDirectory() as tmp,
            patch.object(server, "output_folder", tmp),
            patch.object(server, "task_manager", manager),
            patch.object(server, "run_isolated", side_effect=execute),
        ):
            service = server.PDFTranslator(SimpleNamespace(enable_venv=False))
            with service.app.test_client() as client:
                first = submit(client, b"first")
                try:
                    self.assertTrue(started.wait(4))
                    second = submit(client, b"second")
                    self.assertEqual(
                        client.get("/api/tasks/" + second).json["task"]["status"],
                        "排队中",
                    )
                finally:
                    release.set()
                    service.jobs.shutdown()
                self.assertEqual(order, [b"first", b"second"])
                for tid, content in [(first, b"first"), (second, b"second")]:
                    with client.get(
                        "/translatedFile/same.cut.pdf?taskId=" + tid, buffered=True
                    ) as response:
                        self.assertEqual(response.data, content)
                self.assertEqual(Path(tmp, "same.cut.pdf").read_bytes(), b"second")
                self.assertEqual(client.get("/api/tasks/lost").status_code, 404)
                self.assertEqual(
                    client.get("/translatedFile/same.pdf?taskId=" + first).status_code,
                    404,
                )
            for directory, _ in manager.result_dirs.values():
                directory.cleanup()

    def test_generated_pdf_is_recognized_after_its_filename_changes(self):
        import server

        with pdf.open() as doc:
            doc.new_page()
            doc.set_metadata(
                {
                    "producer": "BabelDOCv0.7.1_123_Translation_generated_by_AI,please_carefully_discern"
                }
            )
            data = base64.b64encode(doc.tobytes()).decode()
        client = server.PDFTranslator(
            SimpleNamespace(enable_venv=False)
        ).app.test_client()
        response = client.post(
            "/translate",
            json={
                "fileName": "author-year.pdf",
                "fileContent": data,
                "engine": "pdf2zh_next",
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["errorType"], "TranslatedInput")
