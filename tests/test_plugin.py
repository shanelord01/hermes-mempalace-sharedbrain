"""Tests for the MemPalace shared-brain Hermes plugin.

No network access - exercises config resolution and JSON-RPC envelope handling
against a stub HTTP layer, since a real test would need a live hub.

Any test that lets the provider resolve its own config must inherit from
IsolatedConfigTestCase. Several provider methods (get_tool_schemas,
get_config_schema, is_available, unavailable_reason) deliberately re-resolve
config from $HERMES_HOME on every call, so a test that does not pin HERMES_HOME
reads whatever the machine running the suite actually has installed. That made
two "off by default" assertions pass on a dev box with no Hermes and fail on the
deployment host, where the live config has the gated flags switched on.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import __init__ as plugin_module  # noqa: E402

MempalaceSharedBrainProvider = plugin_module.MempalaceSharedBrainProvider

_BASE_TOOL_NAMES = {schema["name"] for schema in plugin_module._BASE_TOOL_SCHEMAS}
_KG_WRITE_TOOL_NAMES = plugin_module._KG_WRITE_TOOL_NAMES
_COORDINATION_TOOL_NAMES = plugin_module._COORDINATION_TOOL_NAMES


class IsolatedConfigTestCase(unittest.TestCase):
    """Base case that pins HERMES_HOME to an empty directory.

    Restores the environment afterwards, and points HERMES_HOME at a fresh temp
    dir with no mempalace_sharedbrain.json in it, so "no config file" means
    exactly that no matter what is installed on the machine running the suite.
    Subclasses that want a config file write one into self.hermes_home.
    """

    def setUp(self):
        self.env_backup = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.hermes_home = self._tmp.name
        os.environ["HERMES_HOME"] = self.hermes_home

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        self._tmp.cleanup()


class IsolationContractTests(IsolatedConfigTestCase):
    """The base class is load-bearing, so assert it actually does its job."""

    def test_hermes_home_is_pinned_to_a_directory_with_no_config_file(self):
        self.assertEqual(os.environ["HERMES_HOME"], self.hermes_home)
        self.assertFalse((Path(self.hermes_home) / "mempalace_sharedbrain.json").exists())

    def test_provider_resolves_against_the_pinned_home_not_the_real_one(self):
        """The exact bug this base class exists for: without the pin, a provider
        constructed here reads whatever config the machine running the suite has,
        which is how "off by default" passed on a dev box and failed on the
        deployment host.
        """
        provider = MempalaceSharedBrainProvider()
        self.assertEqual(provider._current_hermes_home(), self.hermes_home)
        provider.get_tool_schemas()
        self.assertFalse(provider._enable_kg_write)
        self.assertFalse(provider._enable_coordination)


class ConfigResolutionTests(IsolatedConfigTestCase):
    def test_env_vars_used_when_no_config_file(self):
        os.environ["MEMPALACE_HUB_URL"] = "https://example.ts.net/mcp"
        os.environ["MEMPALACE_AGENT_ID"] = "test-machine-hermes"
        os.environ["MEMPALACE_MCP_HTTP_TOKEN"] = "tok123"
        provider = MempalaceSharedBrainProvider()
        provider._resolve_config("/nonexistent/hermes/home")
        self.assertEqual(provider._hub_url, "https://example.ts.net/mcp")
        self.assertEqual(provider._agent_id, "test-machine-hermes")
        self.assertEqual(provider._token, "tok123")

    def test_config_file_overrides_env(self):
        cfg_path = Path(self.hermes_home) / "mempalace_sharedbrain.json"
        cfg_path.write_text(json.dumps({"hub_url": "http://mempalace:8765/mcp", "agent_id": "file-agent"}))
        os.environ["MEMPALACE_HUB_URL"] = "https://env-should-lose.example/mcp"
        provider = MempalaceSharedBrainProvider()
        provider._resolve_config(self.hermes_home)
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
        provider._active = True
        provider._hub_url = "http://hub.example/mcp"
        provider._token = "tok"
        with self.assertRaises(NotImplementedError):
            provider.handle_tool_call("not_a_real_tool", {})

    def test_inactive_provider_returns_error_json_not_raise(self):
        provider = MempalaceSharedBrainProvider()
        provider._active = False
        result = json.loads(provider.handle_tool_call("mempalace_search", {"query": "x"}))
        self.assertIn("error", result)

    def test_diary_write_maps_content_to_entry_and_injects_agent_name(self):
        """The hub's mempalace_diary_write requires agent_name (not something the model
        should supply itself) and uses "entry" as its content field (not "content", which
        is only accepted as an alias) - confirmed against the hub's real schema, not
        assumed. This locks in that mapping.
        """
        provider = MempalaceSharedBrainProvider()
        provider._active = True
        provider._hub_url = "http://hub.example/mcp"
        provider._token = "tok"
        provider._agent_id = "unraid-hermes"
        captured = {}

        def fake_call_tool(tool_name, arguments):
            captured["tool_name"] = tool_name
            captured["arguments"] = arguments
            return {}

        provider._call_tool = fake_call_tool
        provider.handle_tool_call("mempalace_diary_write", {"content": "did a thing"})
        self.assertEqual(captured["tool_name"], "mempalace_diary_write")
        self.assertEqual(captured["arguments"]["agent_name"], "unraid-hermes")
        self.assertEqual(captured["arguments"]["entry"], "did a thing")
        self.assertNotIn("content", captured["arguments"])

    def test_diary_read_maps_limit_to_last_n_and_injects_agent_name(self):
        provider = MempalaceSharedBrainProvider()
        provider._active = True
        provider._hub_url = "http://hub.example/mcp"
        provider._token = "tok"
        provider._agent_id = "unraid-hermes"
        captured = {}

        def fake_call_tool(tool_name, arguments):
            captured["arguments"] = arguments
            return {}

        provider._call_tool = fake_call_tool
        provider.handle_tool_call("mempalace_diary_read", {"limit": 5})
        self.assertEqual(captured["arguments"]["agent_name"], "unraid-hermes")
        self.assertEqual(captured["arguments"]["last_n"], 5)

    def test_list_drawers_not_locked_to_own_wing(self):
        """Regression test for a real bug: handle_tool_call hardcoded
        wing=self._wing for mempalace_list_drawers, so querying a room that only
        exists in another wing on the shared brain always came back empty --
        confirmed live via a real Hermes call (room="feedback" returned 0 results
        despite mempalace_status showing 8 entries under a different wing).
        list_drawers must default to every wing, matching mempalace_search's
        scope, and pass through an explicit wing only when the caller asks.
        """
        provider = MempalaceSharedBrainProvider()
        provider._active = True
        provider._hub_url = "http://hub.example/mcp"
        provider._token = "tok"
        provider._wing = "hermes"
        captured = {}

        def fake_call_tool(tool_name, arguments):
            captured["arguments"] = arguments
            return {}

        provider._call_tool = fake_call_tool
        provider.handle_tool_call("mempalace_list_drawers", {"room": "feedback"})
        self.assertIsNone(captured["arguments"]["wing"])

        provider.handle_tool_call("mempalace_list_drawers", {"room": "feedback", "wing": "shane"})
        self.assertEqual(captured["arguments"]["wing"], "shane")


class ToolSchemaRegistrationTests(IsolatedConfigTestCase):
    def test_schemas_available_before_initialize(self):
        """Regression test: Hermes snapshots get_tool_schemas() BEFORE calling
        initialize(), to build its tool-name -> provider routing table. A provider
        that gates this on post-initialize state (e.g. self._active) never gets its
        tools registered, and every call then fails as "Unknown tool" without ever
        reaching handle_tool_call. Schemas must be available on a freshly
        constructed, never-initialized provider.
        """
        provider = MempalaceSharedBrainProvider()  # note: initialize() NOT called
        names = {schema["name"] for schema in provider.get_tool_schemas()}
        self.assertEqual(names, _BASE_TOOL_NAMES)

    def test_kg_write_and_coordination_tools_off_by_default(self):
        provider = MempalaceSharedBrainProvider()
        names = {schema["name"] for schema in provider.get_tool_schemas()}
        self.assertFalse(names & _KG_WRITE_TOOL_NAMES)
        self.assertFalse(names & _COORDINATION_TOOL_NAMES)

    def _write_config(self, **flags):
        cfg = {"hub_url": "http://mempalace:8765/mcp"}
        cfg.update(flags)
        (Path(self.hermes_home) / "mempalace_sharedbrain.json").write_text(json.dumps(cfg))

    def test_kg_write_tools_appear_when_enabled_via_config_file(self):
        self._write_config(enable_kg_write=True)
        provider = MempalaceSharedBrainProvider()
        names = {schema["name"] for schema in provider.get_tool_schemas()}
        self.assertTrue(_KG_WRITE_TOOL_NAMES.issubset(names))
        self.assertFalse(names & _COORDINATION_TOOL_NAMES)

    def test_coordination_tools_appear_when_enabled_via_config_file(self):
        self._write_config(enable_coordination=True)
        provider = MempalaceSharedBrainProvider()
        names = {schema["name"] for schema in provider.get_tool_schemas()}
        self.assertTrue(_COORDINATION_TOOL_NAMES.issubset(names))
        self.assertFalse(names & _KG_WRITE_TOOL_NAMES)


class GatedToolDispatchTests(unittest.TestCase):
    def _provider(self, enable_kg_write=False, enable_coordination=False):
        provider = MempalaceSharedBrainProvider()
        provider._active = True
        provider._hub_url = "http://hub.example/mcp"
        provider._token = "tok"
        provider._agent_id = "test-agent"
        provider._enable_kg_write = enable_kg_write
        provider._enable_coordination = enable_coordination
        return provider

    def test_kg_write_refused_when_disabled(self):
        provider = self._provider(enable_kg_write=False)
        result = json.loads(
            provider.handle_tool_call("mempalace_kg_add", {"subject": "a", "predicate": "b", "object": "c"})
        )
        self.assertIn("error", result)
        self.assertIn("enable_kg_write", result["error"])

    def test_coordination_refused_when_disabled(self):
        provider = self._provider(enable_coordination=False)
        result = json.loads(
            provider.handle_tool_call("mempalace_event_append", {"type": "task.request", "stream": "x", "room": "y"})
        )
        self.assertIn("error", result)
        self.assertIn("enable_coordination", result["error"])

    def test_coordination_args_inject_agent_identity_not_model_supplied(self):
        provider = self._provider(enable_coordination=True)
        args = provider._coordination_args(
            "mempalace_event_append", {"type": "task.request", "stream": "x", "room": "y", "from_agent": "spoofed"}
        )
        self.assertEqual(args["from_agent"], "test-agent")

    def test_kg_write_args_shape_per_tool(self):
        provider = self._provider(enable_kg_write=True)
        add_args = provider._kg_write_args("mempalace_kg_add", {"subject": "a", "predicate": "b", "object": "c"})
        self.assertEqual(add_args["subject"], "a")
        supersede_args = provider._kg_write_args(
            "mempalace_kg_supersede",
            {"subject": "a", "predicate": "uses_model", "old_object": "x", "new_object": "y"},
        )
        self.assertEqual(supersede_args["old_object"], "x")
        self.assertEqual(supersede_args["new_object"], "y")


class ConfigSchemaDefaultsTests(IsolatedConfigTestCase):
    def test_hub_url_default_reflects_current_config(self):
        """The dashboard shows get_config_schema()'s "default" for each field, so it
        must reflect what's actually configured right now, not a hardcoded hint --
        otherwise a working setup looks blank/unconfigured in the UI.
        """
        os.environ["MEMPALACE_HUB_URL"] = "http://mempalace:8765/mcp"
        os.environ["MEMPALACE_AGENT_ID"] = "unraid-hermes"
        provider = MempalaceSharedBrainProvider()
        schema = {field["key"]: field for field in provider.get_config_schema()}
        self.assertEqual(schema["hub_url"]["default"], "http://mempalace:8765/mcp")
        self.assertEqual(schema["agent_id"]["default"], "unraid-hermes")

    def test_every_field_warns_a_restart_is_needed(self):
        """Regression test: initialize() -- where config actually reaches the running
        provider -- only runs once per gateway process start, not per session. A saved
        config change (via the dashboard or hermes memory setup) has no effect until the
        gateway restarts, confirmed live (enable_kg_write didn't expose mempalace_kg_add
        in an active session until the container restarted). Every field must say so,
        not just the two toggles that happened to get tested first.
        """
        provider = MempalaceSharedBrainProvider()
        for field in provider.get_config_schema():
            self.assertIn(
                "restart",
                field["description"].lower(),
                msg=f"field {field['key']!r} description doesn't mention restarting the gateway",
            )

    def test_token_field_never_has_a_default(self):
        os.environ["MEMPALACE_MCP_HTTP_TOKEN"] = "super-secret-token"
        provider = MempalaceSharedBrainProvider()
        schema = {field["key"]: field for field in provider.get_config_schema()}
        self.assertNotIn("default", schema["token"])


def _search_envelope(*texts):
    """Build the hub's real MCP response shape: result.content[].text is itself
    a JSON-encoded string holding the tool's actual output."""
    return {
        "content": [
            {"type": "text", "text": json.dumps({"results": [{"text": t} for t in texts]})}
        ]
    }


class RecallStatusTests(unittest.TestCase):
    """The host (Hermes >= 0.21.0) calls recall_status() right after prefetch() to
    render a deterministic "recalled N memories" line that does not depend on the
    model choosing to mention it. The ABC's one hard requirement is that it reflect
    only the LAST prefetch, never a stale prior count.
    """

    def _ready_provider(self):
        provider = MempalaceSharedBrainProvider()
        provider._active = True
        provider._hub_url = "http://hub.example/mcp"
        provider._token = "tok"
        return provider

    def test_no_status_before_any_prefetch(self):
        self.assertIsNone(self._ready_provider().recall_status())

    def test_status_reports_count_label_and_glyph(self):
        provider = self._ready_provider()
        provider._prefetch_cache["s1"] = ("injected text", 3)
        provider.prefetch("anything", session_id="s1")
        status = provider.recall_status()
        self.assertIsNotNone(status)
        self.assertEqual(status.count, 3)
        self.assertEqual(status.provider_label, "MemPalace")
        self.assertEqual(status.glyph, plugin_module._MEMPALACE_GLYPH)

    def test_miss_clears_a_previous_turns_status(self):
        """The staleness hazard the ABC warns about: turn 1 recalls, turn 2 recalls
        nothing. Turn 2 must not still report turn 1's count.
        """
        provider = self._ready_provider()
        provider._prefetch_cache["s1"] = ("injected text", 2)
        provider.prefetch("q", session_id="s1")
        self.assertEqual(provider.recall_status().count, 2)

        provider.prefetch("q", session_id="s1")  # nothing cached this turn
        self.assertIsNone(provider.recall_status())

    def test_count_matches_the_memories_actually_injected(self):
        """The count and the injected block come from one extraction pass, so a hit
        the formatter drops (empty text) can never be counted as recalled.
        """
        provider = self._ready_provider()
        provider._call_tool = lambda *a, **kw: _search_envelope("fact one", "", "fact two")
        provider._do_prefetch("query", "s1")

        text = provider.prefetch("query", session_id="s1")
        self.assertIn("fact one", text)
        self.assertIn("fact two", text)
        self.assertEqual(provider.recall_status().count, 2)
        self.assertEqual(text.count("---"), 1)  # two hits, one separator

    def test_empty_search_result_injects_nothing_and_reports_nothing(self):
        provider = self._ready_provider()
        provider._call_tool = lambda *a, **kw: _search_envelope()
        provider._do_prefetch("query", "s1")

        self.assertEqual(provider.prefetch("query", session_id="s1"), "")
        self.assertIsNone(provider.recall_status())

    def test_falls_back_to_the_unkeyed_cache_entry(self):
        """queue_prefetch stores under "" when no session_id was supplied; a later
        prefetch that does carry one must still find it.
        """
        provider = self._ready_provider()
        provider._prefetch_cache[""] = ("injected text", 1)
        self.assertEqual(provider.prefetch("q", session_id="s1"), "injected text")
        self.assertEqual(provider.recall_status().count, 1)

    def test_prefetch_is_consume_once(self):
        """Preserved from the original pop() semantics: the cached block is injected
        into exactly one turn, and the status clears with it.
        """
        provider = self._ready_provider()
        provider._prefetch_cache["s1"] = ("injected text", 1)
        self.assertEqual(provider.prefetch("q", session_id="s1"), "injected text")
        self.assertEqual(provider.prefetch("q", session_id="s1"), "")
        self.assertIsNone(provider.recall_status())


class UnavailableReasonTests(IsolatedConfigTestCase):
    """is_available() gates initialization, so a provider that reports unavailable is
    never initialized and anything it would log from initialize() is unreachable.
    This hook is the only place a usable hint can reach the operator.
    """

    def _reason_with(self, hub_url=None, token=None):
        os.environ.pop("MEMPALACE_HUB_URL", None)
        os.environ.pop("MEMPALACE_MCP_HTTP_TOKEN", None)
        if hub_url:
            os.environ["MEMPALACE_HUB_URL"] = hub_url
        if token:
            os.environ["MEMPALACE_MCP_HTTP_TOKEN"] = token
        return MempalaceSharedBrainProvider().unavailable_reason()

    def test_empty_when_fully_configured(self):
        self.assertEqual(self._reason_with("http://hub.example/mcp", "tok"), "")

    def test_names_hub_url_when_only_that_is_missing(self):
        reason = self._reason_with(token="tok")
        self.assertIn("hub_url", reason)
        self.assertNotIn("MEMPALACE_MCP_HTTP_TOKEN", reason)

    def test_names_the_token_env_var_when_only_that_is_missing(self):
        reason = self._reason_with(hub_url="http://hub.example/mcp")
        self.assertIn("MEMPALACE_MCP_HTTP_TOKEN", reason)
        self.assertNotIn("hub_url", reason)

    def test_names_both_when_nothing_is_configured(self):
        reason = self._reason_with()
        self.assertIn("hub_url", reason)
        self.assertIn("MEMPALACE_MCP_HTTP_TOKEN", reason)

    def test_never_echoes_the_token_back(self):
        """The reason string is surfaced in host warnings and logs. Whatever else it
        says, it must not carry the secret it is complaining about.
        """
        reason = self._reason_with(token="super-secret-token")
        self.assertNotIn("super-secret-token", reason)

    def test_mentions_restarting_the_gateway(self):
        """Same reason every config-schema field says so: config is read once per
        gateway process start, so fixing the file alone changes nothing.
        """
        self.assertIn("restart", self._reason_with().lower())

    def test_resolves_config_without_is_available_being_called_first(self):
        """The hook must not depend on is_available() having run to populate state."""
        os.environ["MEMPALACE_HUB_URL"] = "http://hub.example/mcp"
        os.environ["MEMPALACE_MCP_HTTP_TOKEN"] = "tok"
        provider = MempalaceSharedBrainProvider()
        self.assertEqual(provider._hub_url, "")  # nothing resolved yet
        self.assertEqual(provider.unavailable_reason(), "")


if __name__ == "__main__":
    unittest.main()
