"""Tests for the MemPalace shared-brain Hermes plugin.

No network access — exercises config resolution and JSON-RPC envelope handling
against a stub HTTP layer, since a real test would need a live hub.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import __init__ as plugin_module  # noqa: E402

MempalaceSharedBrainProvider = plugin_module.MempalaceSharedBrainProvider


class ConfigResolutionTests(unittest.TestCase):
    def setUp(self):
        self.env_backup = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)

    def test_env_vars_used_when_no_config_file(self):
        os.environ["MEMPALACE_HUB_URL"] = "https://example.ts.net/mcp"
        os.environ["MEMPALACE_AGENT_ID"] = "test-machine-hermes"
        os.environ["MEMPALACE_MCP_HTTP_TOKEN"] = "tok123"
        provider = MempalaceSharedBrainProvider()
        provider._resolve_config("/nonexistent/hermes/home")
        self.assertEqual(provider._hub_url, "https://example.ts.net/mcp")
        self.assertEqual(provider._agent_id, "test-machine-hermes")
        self.assertEqual(provider._token, "tok123")

    def test_config_file_overrides_env(self, ):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "mempalace_sharedbrain.json"
            cfg_path.write_text(json.dumps({"hub_url": "http://mempalace:8765/mcp", "agent_id": "file-agent"}))
            os.environ["MEMPALACE_HUB_URL"] = "https://env-should-lose.example/mcp"
            provider = MempalaceSharedBrainProvider()
            provider._resolve_config(tmp)
            self.assertEqual(provider._hub_url, "http://mempalace:8765/mcp")
            self.assertEqual(provider._agent_id, "file-agent")

    def test_is_available_false_without_token(self):
        os.environ.pop("MEMPALACE_MCP_HTTP_TOKEN", None)
        os.environ["MEMPALACE_HUB_URL"] = "https://example.ts.net/mcp"
        provider = MempalaceSharedBrainProvider()
        self.assertFalse(provider.is_available())


class EnvelopeHandlingTests(unittest.TestCase):
    def test_unwrap_parses_inner_json(self):
        provider = MempalaceSharedBrainProvider()
        result = {"content": [{"type": "text", "text": '{"success": true, "drawer_id": "abc"}'}]}
        self.assertEqual(provider._unwrap(result), {"success": True, "drawer_id": "abc"})

    def test_unwrap_falls_back_to_raw_text_on_bad_json(self):
        provider = MempalaceSharedBrainProvider()
        result = {"content": [{"type": "text", "text": "not json"}]}
        self.assertEqual(provider._unwrap(result), "not json")

    def test_format_search_result_joins_hits(self):
        provider = MempalaceSharedBrainProvider()
        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"results": [{"text": "fact one"}, {"text": "fact two"}]}),
                }
            ]
        }
        formatted = provider._format_search_result(result)
        self.assertIn("fact one", formatted)
        self.assertIn("fact two", formatted)

    def test_format_search_result_empty_when_no_hits(self):
        provider = MempalaceSharedBrainProvider()
        result = {"content": [{"type": "text", "text": json.dumps({"results": []})}]}
        self.assertEqual(provider._format_search_result(result), "")

    def test_call_tool_raises_on_jsonrpc_error(self):
        provider = MempalaceSharedBrainProvider()
        provider._hub_url = "http://hub.example/mcp"
        provider._token = "tok"

        fake_response = mock.MagicMock()
        fake_response.read.return_value = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "error": {"message": "boom"}}
        ).encode("utf-8")
        fake_response.__enter__.return_value = fake_response

        with mock.patch("urllib.request.urlopen", return_value=fake_response):
            with self.assertRaises(RuntimeError):
                provider._call_tool("mempalace_search", {"query": "x"})


class HandleToolCallTests(unittest.TestCase):
    def test_unknown_tool_raises(self):
        provider = MempalaceSharedBrainProvider()
        with self.assertRaises(NotImplementedError):
            provider.handle_tool_call("not_a_real_tool", {})


if __name__ == "__main__":
    unittest.main()
