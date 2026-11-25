"""Routing regressions for consecutive Zotero API profiles. All credentials are fixtures."""

import contextlib
import io
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import toml
from utils.config import Config

TEMPLATES = Path(__file__).resolve().parents[1] / "config"


def make_config(profile=None, service="openai", engine="pdf2zh_next"):
    request = {"engine": engine, "service": service, "noDual": True}
    if profile is not None:
        request["llm_api"] = profile
    return Config(request)


class ApiProfileTests(unittest.TestCase):
    def test_consecutive_tasks_route_the_sdk_to_the_selected_profile(self):
        import server

        try:
            import httpx
            from pdf2zh_next.config.main import ConfigManager
            from pdf2zh_next.translator.translator_impl.openai import OpenAITranslator
        except ImportError:
            self.skipTest("The translation runtime is installed separately")

        with TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            base = root / "config.toml"
            shutil.copyfile(TEMPLATES / "config.toml.example", base)
            original = base.read_bytes()
            defaults = root / "defaults.toml"
            defaults.write_text(
                '[openai_detail]\nopenai_base_url = "https://stale.invalid/v1"\n'
                'openai_send_reasoning_effort = true\nopenai_reasoning_effort = "low"\n',
                encoding="utf-8",
            )
            manager = ConfigManager()
            source = root / "sample.pdf"
            source.touch()
            calls, paths = [], []

            def respond(request):
                self.assertIn(
                    request.url.host,
                    {"first.invalid", "next.invalid", "api.openai.com"},
                )
                calls.append(
                    (str(request.url), request.headers, json.loads(request.content))
                )
                return httpx.Response(
                    200,
                    json={
                        "id": "test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "fixture",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                )

            def run_engine(command, environment, *_):
                path = Path(command[command.index("--config-file") + 1])
                paths.append(path)
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(sys, "argv", command),
                    patch.object(manager, "_default_config_file_path", defaults),
                    patch.object(manager, "_update_version_default_config"),
                    patch.object(
                        httpx.HTTPTransport,
                        "handle_request",
                        autospec=True,
                        side_effect=lambda _, req: transport.handle_request(req),
                    ),
                    patch(
                        "socket.socket.connect",
                        side_effect=AssertionError("Network disabled"),
                    ),
                ):
                    translator = OpenAITranslator(
                        manager.initialize_cli_config().to_settings_model(), Mock()
                    )
                    try:
                        self.assertEqual(
                            translator.do_llm_translate("local route test"), "ok"
                        )
                    finally:
                        translator.client.close()
                (root / "sample.no_watermark.zh-CN.mono.pdf").touch()

            transport = httpx.MockTransport(respond)
            a = {
                "service": "openai",
                "apiUrl": "https://first.invalid/v1",
                "apiKey": "fixture-A",
                "model": "same",
                "extraData": {
                    "openai_send_temperature": "true",
                    "openai_temperature": "0",
                    "openai_api_key": "duplicate-field",
                    "openai_base_url": "https://duplicate.invalid",
                },
            }
            b = {**a, "apiKey": "fixture-B", "extraData": {}}
            c = {
                **b,
                "apiUrl": "https://next.invalid/custom",
                "model": "another",
                "extraData": {
                    "openai_send_reasoning_effort": "true",
                    "openai_reasoning_effort": "high",
                },
            }
            inherited = {
                "PDF2ZH_OPENAI_API_KEY": "fixture-stale",
                "PDF2ZH_OPENAI_BASE_URL": "https://stale.invalid/v1",
                "PDF2ZH_OPENAI_MODEL": "stale-model",
                "PDF2ZH_OPENAI_SEND_TEMPRATURE": "false",
                "PDF2ZH_OPENAI_SEND_REASONING_EFFORT": "false",
                "OPENAI_BASE_URL": "https://stale.invalid/v1",
                "OPENAI_PROJECT_ID": "stale-project",
                "OPENAI_ORG_ID": "stale-org",
            }
            executor = (
                "_execute_with_inherit" if os.name == "nt" else "_execute_with_pty"
            )
            options = SimpleNamespace(enable_venv=False, enable_winexe=False)
            with (
                patch.dict(os.environ, inherited),
                patch.object(server, "args", options, create=True),
                patch.object(server, "output_folder", str(root)),
                patch.dict(server.config_path, pdf2zh_next=str(base)),
                patch("utils.execute." + executor, side_effect=run_engine),
            ):
                processor = server.PDFTranslator(options)
                try:
                    for profile in (a, b, c, {**b, "apiUrl": ""}):
                        processor._run_pdf2zh_next(
                            str(source), make_config(profile), None
                        )
                finally:
                    processor.jobs.shutdown()

            self.assertEqual(
                [u for u, _, _ in calls],
                [
                    "https://first.invalid/v1/chat/completions",
                    "https://first.invalid/v1/chat/completions",
                    "https://next.invalid/custom/chat/completions",
                    "https://api.openai.com/v1/chat/completions",
                ],
            )
            self.assertEqual(
                [h["authorization"] for _, h, _ in calls],
                [
                    "Bearer fixture-A",
                    "Bearer fixture-B",
                    "Bearer fixture-B",
                    "Bearer fixture-B",
                ],
            )
            self.assertTrue(
                all(
                    "openai-organization" not in h and "openai-project" not in h
                    for _, h, _ in calls
                )
            )
            self.assertEqual(
                [body["model"] for _, _, body in calls],
                ["same", "same", "another", "same"],
            )
            self.assertEqual(calls[0][2]["temperature"], 0.0)
            self.assertTrue(all("temperature" not in body for _, _, body in calls[1:]))
            self.assertEqual(calls[2][2]["reasoning_effort"], "high")
            self.assertTrue(
                all("reasoning_effort" not in calls[i][2] for i in (0, 1, 3))
            )
            self.assertEqual(base.read_bytes(), original)
            self.assertTrue(
                all(p.parent.parent == root and not p.parent.exists() for p in paths)
            )

    def test_missing_profile_and_incomplete_profile_are_distinct(self):
        with TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            base = Path(folder, "config.toml")
            base.write_text(
                '[openai_detail]\nopenai_api_key="fixture-old"\n'
                'openai_base_url="https://old-gateway.invalid"\nopenai_model="configured"\n',
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, ZOTERO_PDF2ZH_DESKTOP="1"),
                patch(
                    "utils.config.desktop_openai_key", return_value="fixture-backup"
                ) as backup,
            ):
                empty = make_config(
                    {
                        "service": "openai",
                        "apiKey": "",
                        "apiUrl": "https://custom.invalid",
                        "model": "m",
                    }
                )
                with (
                    self.assertRaisesRegex(ValueError, "API Key"),
                    empty.task_config(base),
                ):
                    self.fail("Incomplete profile reached the engine")
                backup.assert_not_called()
                wrong = make_config(
                    {
                        "service": "openailiked",
                        "apiKey": "fixture-B",
                        "apiUrl": "https://other.invalid",
                    }
                )
                with (
                    self.assertRaisesRegex(ValueError, "不一致"),
                    wrong.task_config(base),
                ):
                    self.fail("Mismatched service reached the engine")
                with make_config().task_config(base) as private:
                    fields = toml.load(private)["openai_detail"]
                    self.assertEqual(fields["openai_api_key"], "fixture-backup")
                    self.assertIn(
                        fields.get("openai_base_url"),
                        (None, "null", "https://api.openai.com/v1"),
                    )
                    self.assertEqual(fields["openai_model"], "configured")
                backup.assert_called_once()
            standalone = make_config()
            with (
                patch.dict(os.environ, ZOTERO_PDF2ZH_DESKTOP=""),
                standalone.task_config(base) as private,
            ):
                self.assertEqual(
                    toml.load(private)["openai_detail"]["openai_api_key"], "fixture-old"
                )

    def test_environment_is_filtered_after_venv_merge_and_for_both_winexe_modes(self):
        from utils.execute import execute_with_progress

        import server

        cfg = make_config(
            {
                "service": "openailiked",
                "apiKey": "fixture-G",
                "apiUrl": "https://gateway.invalid/v1",
                "model": "custom",
                "extraData": {
                    "openai_compatible_temperature": "0.75",
                    "openai_compatible_send_temperature": "true",
                },
            },
            service="openailiked",
        )
        cfg.pool_size = 3
        stale = {
            "PDF2ZH_OPENAI_COMPATIBLE_API_KEY": "fixture-stale",
            "PDF2ZH_OPENAI_COMPATIBLE_MODEL": "stale",
            "OPENAI_BASE_URL": "https://stale.invalid",
            "HTTP_PROXY": "http://proxy.invalid",
        }
        options = SimpleNamespace(enable_venv=True, enable_winexe=False)
        manager = Mock()
        manager.get_command_and_env.return_value = (["engine", "run"], stale)
        executor = "_execute_with_inherit" if os.name == "nt" else "_execute_with_pty"
        with (
            patch("utils.execute." + executor) as launch,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            execute_with_progress(["engine", "run"], None, options, manager, config=cfg)
        child = launch.call_args.args[1]
        self.assertNotIn("PDF2ZH_OPENAI_COMPATIBLE_API_KEY", child)
        self.assertNotIn("PDF2ZH_OPENAI_COMPATIBLE_MODEL", child)
        self.assertNotIn("OPENAI_BASE_URL", child)
        self.assertEqual(child["HTTP_PROXY"], stale["HTTP_PROXY"])

        with TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            base = root / "config.toml"
            shutil.copyfile(TEMPLATES / "config.toml.example", base)
            executable = root / "engine.exe"
            executable.touch()
            opts = SimpleNamespace(
                enable_venv=False,
                enable_winexe=True,
                winexe_path=str(executable),
                winexe_attach_console=False,
            )
            (root / "test.no_watermark.zh-CN.mono.pdf").touch()
            processor = server.PDFTranslator(opts)
            try:
                for visible in (False, True):
                    opts.winexe_attach_console = visible

                    def simulate(command, **kwargs):
                        env = kwargs["env"]
                        self.assertNotIn("PDF2ZH_OPENAI_COMPATIBLE_API_KEY", env)
                        self.assertEqual(env["HTTP_PROXY"], stale["HTTP_PROXY"])
                        data = toml.load(command[command.index("--config-file") + 1])
                        detail = data["openaicompatible_detail"]
                        self.assertEqual(
                            detail["openai_compatible_api_key"], "fixture-G"
                        )
                        self.assertIs(
                            detail["openai_compatible_send_temperature"], True
                        )
                        self.assertEqual(data["translation"]["pool_max_workers"], 3)
                        return SimpleNamespace(
                            stderr=None, returncode=0, wait=lambda: 0
                        )

                    method = "Popen" if visible else "run"
                    with (
                        patch.dict(os.environ, stale),
                        patch.object(server, "args", opts, create=True),
                        patch.object(server, "output_folder", str(root)),
                        patch.dict(server.config_path, pdf2zh_next=str(base)),
                        patch.object(
                            server.subprocess, method, side_effect=simulate
                        ) as launch,
                    ):
                        processor._run_pdf2zh_next(str(root / "test.pdf"), cfg)
                        launch.assert_called_once()
            finally:
                processor.jobs.shutdown()

    def test_legacy_profile_is_private_even_if_a_task_fails(self):
        with TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            base = Path(folder, "config.json")
            shutil.copyfile(TEMPLATES / "config.json.example", base)
            previous = base.read_bytes()
            cfg = make_config(
                {
                    "service": "openai",
                    "apiKey": "fixture-B",
                    "apiUrl": "https://other.invalid/v1",
                    "model": "legacy",
                },
                engine="pdf2zh",
            )
            with (
                self.assertRaisesRegex(RuntimeError, "simulated failure"),
                cfg.task_config(base) as private,
            ):
                details = next(
                    x["envs"]
                    for x in json.loads(Path(private).read_text("utf8"))["translators"]
                    if x["name"] == "openai"
                )
                self.assertEqual(
                    (
                        details["OPENAI_API_KEY"],
                        details["OPENAI_BASE_URL"],
                        details["OPENAI_MODEL"],
                    ),
                    ("fixture-B", "https://other.invalid/v1", "legacy"),
                )
                raise RuntimeError("simulated failure")
            self.assertFalse(Path(private).exists())
            self.assertEqual(base.read_bytes(), previous)
