"""Offline inventory/privacy regressions; no real Hermes files or HTTP calls."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location(
    "telemetry_logger", Path(__file__).resolve().parents[1] / "scripts/aikub_telemetry_logger.py"
)
logger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(logger)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / "config.yaml"
        self.config.write_text(
            "model:\n  provider: openai-codex\n  default: gpt-test\n  context_length: 12345\n",
            encoding="utf-8",
        )
        self.skill = self.home / "skills/tools/demo/SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("---\nname: demo\ndescription: Demo skill\n---\n", encoding="utf-8")
        self.crons = self.home / "cron/jobs.json"
        self.crons.parent.mkdir()
        self.crons.write_text(json.dumps({"jobs": [{
            "id": "job-1", "name": "daily", "enabled": True,
            "schedule": {"kind": "cron", "expr": "0 9 * * *"},
            "provider": "openai-codex", "model": "gpt-test",
            "prompt": "private prompt", "delivery_error": "private error",
        }]}), encoding="utf-8")
        for filename in ("auth.json", "credentials.json", ".codex/auth.json"):
            auth = self.home / filename
            auth.parent.mkdir(parents=True, exist_ok=True)
            auth.write_text(json.dumps({"active_provider": "openai-codex", "credential_pool": {
                "openai-codex": [{"email": "private@example.test", "access_token": "private-token",
                                  "plan": "private-plan", "quota": 42}]
            }}), encoding="utf-8")
        self.env = patch.dict(os.environ, {
            "HOME": str(self.home), "HERMES_HOME": str(self.home),
            "AIKUB_TELEMETRY_BASE_URL": "https://telemetry.example.test",
            "AIKUB_TELEMETRY_BOTOPS_TOKEN": "test-write-token",
            "AIKUB_TELEMETRY_BOT_ID": "test-bot",
            "AIKUB_CODEX_ACCOUNT_EMAIL": "override@example.test",
            "OPENAI_CODEX_EMAIL": "other@example.test",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.plugin = {"name": "demo-plugin", "label": "Demo", "version": "1",
                       "source": "local", "description": "Demo plugin",
                       "tab": {"path": "/demo"}, "has_api": True}
        self.requests = []

        def respond(request, **kwargs):
            self.requests.append(request)
            response = MagicMock()
            response.status = 200
            response.read.return_value = json.dumps(
                [self.plugin] if request.get_method() == "GET" else {"accepted": True}
            ).encode()
            response.__enter__.return_value = response
            return response

        self.http = patch.object(logger.urllib.request, "urlopen", side_effect=respond)
        self.http.start()
        self.addCleanup(self.http.stop)

    def assert_no_accounts(self, value):
        forbidden = {"accounts", "codex", "oauth", "email", "fingerprint", "quota", "plan",
                     "credential", "access_token", "refresh_token", "id_token", "authType"}
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertFalse(any(word.lower() in key.lower() for word in forbidden), key)
                self.assert_no_accounts(item)
        elif isinstance(value, list):
            for item in value:
                self.assert_no_accounts(item)
        serialized = json.dumps(value)
        for private in ("private@example.test", "private-token", "private-plan",
                        "override@example.test", "other@example.test"):
            self.assertNotIn(private, serialized)

    def test_only_inventory_files_are_opened_and_no_account_env_is_read(self):
        opened = []
        original_open = Path.open
        original_getenv = os.getenv
        requested_env = []

        def record_open(path, *args, **kwargs):
            opened.append(path)
            return original_open(path, *args, **kwargs)

        def record_getenv(name, *args):
            requested_env.append(name)
            return original_getenv(name, *args)

        with patch.object(Path, "open", record_open), patch.object(os, "getenv", record_getenv):
            logger.build_payload("test-bot", self.home)
        self.assertEqual(set(opened), {self.config, self.skill, self.crons})
        self.assertNotIn("AIKUB_CODEX_ACCOUNT_EMAIL", requested_env)
        self.assertNotIn("OPENAI_CODEX_EMAIL", requested_env)

    def test_payload_preserves_non_account_inventory(self):
        event = logger.build_payload("test-bot", self.home)
        self.assert_no_accounts(event)
        self.assertEqual(set(event), {"botId", "eventType", "severity", "source", "traceId",
                                      "sessionId", "occurredAt", "payload"})
        inventory = event["payload"]
        self.assertEqual(set(inventory), {"identity", "model", "skillCount", "skills", "crons", "plugins"})
        self.assertEqual(inventory["model"], {"provider": "openai-codex", "name": "gpt-test", "context": 12345})
        self.assertEqual(inventory["identity"]["kind"], "Hermes Agent")
        self.assertEqual(inventory["skillCount"], 1)
        self.assertEqual(inventory["skills"], [{"name": "demo", "description": "Demo skill",
                                               "path": str(self.skill), "category": "tools"}])
        self.assertEqual(inventory["crons"]["cronNames"], ["daily"])
        job = inventory["crons"]["items"][0]
        self.assertEqual(job["provider"], "openai-codex")
        self.assertEqual(job["model"], "gpt-test")
        self.assertEqual(job["schedule"], {"kind": "cron", "display": "0 9 * * *"})
        self.assertNotIn("prompt", job)
        self.assertNotIn("delivery_error", job)
        self.assertEqual(inventory["plugins"]["enabledCount"], 1)
        self.assertEqual(inventory["plugins"]["items"][0]["dashboardTab"], "/demo")

    def test_dry_run_contains_no_accounts_and_keeps_token_redacted(self):
        output = io.StringIO()
        with patch.object(logger.sys, "argv", ["logger", "--dry-run"]), contextlib.redirect_stdout(output):
            self.assertEqual(logger.main(), 0)
        document = json.loads(output.getvalue())
        self.assert_no_accounts(document)
        self.assertNotIn("test-write-token", output.getvalue())
        self.assertEqual(document["config"]["headers"]["x-aikub-botops-token"], "[REDACTED]")
        self.assertEqual([req.get_method() for req in self.requests], ["GET"])

    def test_missing_model_values_are_null_per_inventory_contract(self):
        for config in (None, "", "model:\n  provider: ''\n  default: ''\n",
                       "model:\n  context_length: 12345\n"):
            with self.subTest(config=config):
                if config is None:
                    self.config.unlink()
                else:
                    self.config.write_text(config, encoding="utf-8")
                model = logger.build_payload("test-bot", self.home)["payload"]["model"]
                self.assertIsNone(model["provider"])
                self.assertIsNone(model["name"])
                self.assertEqual(model["context"], 12345 if config and "context_length" in config else None)

    def test_posted_body_contains_no_accounts(self):
        with patch.object(logger.sys, "argv", ["logger"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(logger.main(), 0)
        posts = [req for req in self.requests if req.get_method() == "POST"]
        self.assertEqual(len(posts), 1)
        self.assert_no_accounts(json.loads(posts[0].data))
        self.assertEqual(posts[0].get_header("X-aikub-botops-token"), "test-write-token")


if __name__ == "__main__":
    unittest.main()
