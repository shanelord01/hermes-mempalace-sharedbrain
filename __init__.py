"""MemPalace shared-brain memory provider for Hermes.

Implements the Hermes ``MemoryProvider`` ABC (``agent/memory_provider.py``) as a pure
stdlib HTTP client against a MemPalace hub (``mempalace serve``). Every operation goes
over the hub's ``/mcp`` JSON-RPC endpoint with bearer-token auth. There is no local
palace and no ``mempalace`` package dependency, unlike every other Hermes+MemPalace
integration that exists today (the official in-tree one, and two third-party repos) --
all three call MemPalace as an in-process/local-CLI library, which means a running hub
has no effect on them: writes and reads alike stay on whatever machine Hermes runs on.
This provider only ever talks over HTTP to the hub, so it cannot silently drift onto
local state -- if the hub is unreachable, calls fail loudly instead of quietly
succeeding against an empty local palace.

Configuration precedence: ``$HERMES_HOME/mempalace_sharedbrain.json`` is read first,
then env vars (``MEMPALACE_HUB_URL``, ``MEMPALACE_AGENT_ID``) override, then built-in
defaults. The bearer token is always read from ``MEMPALACE_MCP_HTTP_TOKEN`` (secret,
belongs in ``.env``, never written to the config file).

Wire format: the JSON-RPC ``tools/call`` envelope and the hub's response shape
(``result.content[].text`` holding a JSON-encoded string of the tool's real output)
were confirmed against a live hub, not assumed from documentation.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from agent.memory_provider import MemoryProvider  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Hermes not installed

    class MemoryProvider:  # type: ignore[no-redef]
        """Stub used when ``agent.memory_provider`` cannot be imported."""


logger = logging.getLogger("mempalace_sharedbrain.hermes")

_DEFAULT_WING = "hermes"
_DEFAULT_ROOM = "conversation"
_REQUEST_TIMEOUT_S = 10.0
_QUEUE_MAXSIZE = 64
_MAX_QUERY_CHARS = 250


def _config_path(hermes_home: str) -> Path:
    return Path(hermes_home) / "mempalace_sharedbrain.json"


def _read_config(hermes_home: str) -> Dict[str, Any]:
    path = _config_path(hermes_home)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("mempalace_sharedbrain: could not read %s", path)
        return {}


def _coerce_bool(value: Any) -> bool:
    """True Python bools pass through; the dashboard's "choices" dropdown submits the
    strings "true"/"false" instead (bool typed fields are silently dropped by Hermes's
    dashboard API -- see get_config_schema), and bool("false") is True in plain Python,
    so this can't be a bare bool() call.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


# Always available -- read-only or self-contained (own diary), safe by default.
_BASE_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "mempalace_search",
        "description": "Search the shared MemPalace memory (verbatim results, semantic search).",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords or a question (max 250 chars)"},
                "limit": {"type": "integer", "description": "Max results (default 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "mempalace_add_drawer",
        "description": "File a durable fact or decision verbatim into the shared MemPalace memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Verbatim content to store"},
                "room": {
                    "type": "string",
                    "description": "Category, e.g. decisions, facts (optional, defaults to conversation)",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "mempalace_list_drawers",
        "description": (
            "List drawers with pagination -- a reliable completeness check that doesn't depend "
            "on semantic similarity the way mempalace_search does. Use this when you need to "
            "confirm what's actually filed under a wing/room, not just what a query surfaces. "
            "Searches across every wing on the shared brain by default, not just this agent's "
            "own wing -- pass 'wing' to narrow to one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wing": {
                    "type": "string",
                    "description": (
                        "Filter by wing (optional). Omit to search every wing on the shared "
                        "brain, matching mempalace_search's default scope."
                    ),
                },
                "room": {"type": "string", "description": "Filter by room (optional)"},
                "limit": {"type": "integer", "description": "Max results per page (default 20, max 100)"},
                "offset": {"type": "integer", "description": "Offset for pagination (default 0)"},
            },
        },
    },
    {
        "name": "mempalace_status",
        "description": (
            "Palace overview: total drawers, wing/room counts, and the hub's own memory protocol "
            "reminder. Call this at the start of a session for orientation."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "mempalace_get_taxonomy",
        "description": "Full taxonomy: wing -> room -> drawer count, across the whole palace.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "mempalace_kg_query",
        "description": (
            "Query the knowledge graph for an entity's relationships -- typed facts with temporal "
            "validity (e.g. 'Max' -> child_of Alice, loves chess). Use for relational/temporal "
            "facts search alone won't reliably surface. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity to query (e.g. a person or project name)"},
                "as_of": {"type": "string", "description": "Date/datetime filter (optional)"},
                "direction": {
                    "type": "string",
                    "description": "outgoing (entity->?), incoming (?->entity), or both (default: both)",
                },
            },
            "required": ["entity"],
        },
    },
    {
        "name": "mempalace_get_drawer",
        "description": "Fetch one drawer's full content by its drawer_id (e.g. to follow up on a search result).",
        "parameters": {
            "type": "object",
            "properties": {"drawer_id": {"type": "string", "description": "The drawer_id to fetch"}},
            "required": ["drawer_id"],
        },
    },
    {
        "name": "mempalace_diary_write",
        "description": (
            "Write a session diary entry: what happened, what was learned, what matters. The "
            "hub's own protocol calls for this after each session, separate from filing drawers. "
            "Writes to this agent's own diary only."
        ),
        "parameters": {
            "type": "object",
            "properties": {"content": {"type": "string", "description": "Diary entry content"}},
            "required": ["content"],
        },
    },
    {
        "name": "mempalace_diary_read",
        "description": "Read back this agent's own recent diary entries.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Max entries (optional)"}},
        },
    },
]

# Opt-in: mutates the shared knowledge graph other agents rely on for entity facts.
# Gated behind enable_kg_write (default False) -- see get_config_schema().
_KG_WRITE_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "mempalace_kg_add",
        "description": (
            "Add a fact to the shared knowledge graph: subject -> predicate -> object, with an "
            "optional time window."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "The entity doing/being something"},
                "predicate": {"type": "string", "description": "The relationship type, e.g. 'works_on'"},
                "object": {"type": "string", "description": "The entity being connected to"},
                "valid_from": {"type": "string", "description": "When this became true (optional)"},
                "valid_to": {"type": "string", "description": "When this stopped being true (optional)"},
            },
            "required": ["subject", "predicate", "object"],
        },
    },
    {
        "name": "mempalace_kg_invalidate",
        "description": "Mark a shared knowledge-graph fact as no longer true (e.g. a job or project ended).",
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Entity"},
                "predicate": {"type": "string", "description": "Relationship"},
                "object": {"type": "string", "description": "Connected entity"},
                "ended": {"type": "string", "description": "When it stopped being true (optional, default: today)"},
            },
            "required": ["subject", "predicate", "object"],
        },
    },
    {
        "name": "mempalace_kg_supersede",
        "description": (
            "Atomically replace a shared fact with its successor (e.g. model, employer, address "
            "changed). Prefer this over separate kg_invalidate + kg_add for single-valued facts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "The entity whose fact is changing"},
                "predicate": {"type": "string", "description": "The relationship type"},
                "old_object": {"type": "string", "description": "The value being replaced"},
                "new_object": {"type": "string", "description": "The new value"},
            },
            "required": ["subject", "predicate", "old_object", "new_object"],
        },
    },
]

# Opt-in: lets this agent send/receive delegated tasks and code patches to/from other
# agents on the shared brain. Gated behind enable_coordination (default False) -- see
# get_config_schema(). This is the consequential one: an agent with this enabled can
# be handed work autonomously via the logstream, not just recall/file memory.
_COORDINATION_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "mempalace_event_append",
        "description": (
            "Append a coordination event (e.g. task.request, task.reply, patch.ready) to the "
            "shared agent logstream. Use to delegate work to another agent or reply to a request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "Event type, e.g. 'task.request', 'task.reply'"},
                "stream": {"type": "string", "description": "Logical stream, e.g. 'project/myapp'"},
                "room": {"type": "string", "description": "Sub-channel, e.g. 'delegation', 'patches'"},
                "to_agent": {"type": "string", "description": "Target agent, or '*' for broadcast (optional)"},
                "correlation_id": {"type": "string", "description": "Ties a request to its reply (optional)"},
                "status": {
                    "type": "string",
                    "description": "One of: open, claimed, ready, applied, blocked, failed, superseded (optional)",
                },
                "body": {"type": "string", "description": "Verbatim human-readable content (optional)"},
            },
            "required": ["type", "stream", "room"],
        },
    },
    {
        "name": "mempalace_event_list",
        "description": "List coordination events, e.g. check this agent's inbox for delegated tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "stream": {"type": "string", "description": "Filter by stream (optional)"},
                "to_agent": {"type": "string", "description": "Filter by target agent (optional)"},
                "correlation_id": {"type": "string", "description": "Filter by correlation id (optional)"},
                "since_event_id": {"type": "string", "description": "Only events after this id (optional)"},
                "limit": {"type": "integer", "description": "Max events to return (default 50)"},
            },
        },
    },
    {
        "name": "mempalace_event_wait",
        "description": "Block until a matching coordination event exists or the timeout expires (max 5 minutes).",
        "parameters": {
            "type": "object",
            "properties": {
                "stream": {"type": "string", "description": "Filter by stream (optional)"},
                "to_agent": {"type": "string", "description": "Filter by target agent (optional)"},
                "correlation_id": {"type": "string", "description": "Filter by correlation id (optional)"},
                "timeout_ms": {"type": "integer", "description": "Max wait in ms (default 60000, max 300000)"},
            },
        },
    },
    {
        "name": "mempalace_event_ack",
        "description": "Acknowledge a coordination event (e.g. claimed/applied/blocked/failed) back to its writer.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Id of the event to acknowledge"},
                "status": {
                    "type": "string",
                    "description": "One of: open, claimed, ready, applied, blocked, failed, superseded (optional)",
                },
                "body": {"type": "string", "description": "Verbatim ack notes (optional)"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "mempalace_artifact_put",
        "description": "Store exact artifact content (patch, file, log, json, note) for a handoff to another agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "One of: patch, file, log, json, note"},
                "content": {"type": "string", "description": "Exact artifact content"},
            },
            "required": ["kind", "content"],
        },
    },
    {
        "name": "mempalace_artifact_get",
        "description": "Fetch a coordination artifact by id -- exact content plus sha256 for verification.",
        "parameters": {
            "type": "object",
            "properties": {"artifact_id": {"type": "string", "description": "Artifact id to fetch"}},
            "required": ["artifact_id"],
        },
    },
    {
        "name": "mempalace_patch_submit",
        "description": "Store a patch artifact and append its patch.ready event in one call -- hand completed work to another agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Unified diff content"},
                "stream": {"type": "string", "description": "Logical stream, e.g. 'project/myapp'"},
                "to_agent": {"type": "string", "description": "Target agent or '*' (optional)"},
                "correlation_id": {"type": "string", "description": "Ties this patch to its request (optional)"},
                "body": {"type": "string", "description": "Verbatim notes (optional)"},
            },
            "required": ["content", "stream"],
        },
    },
]

_KG_WRITE_TOOL_NAMES = {schema["name"] for schema in _KG_WRITE_TOOL_SCHEMAS}
_COORDINATION_TOOL_NAMES = {schema["name"] for schema in _COORDINATION_TOOL_SCHEMAS}


class MempalaceSharedBrainProvider(MemoryProvider):
    """Hermes memory provider backed by a remote MemPalace shared-brain hub."""

    @property
    def name(self) -> str:
        return "mempalace-sharedbrain"

    def __init__(self) -> None:
        self._hub_url = ""
        self._token = ""
        self._agent_id = "hermes"
        self._wing = _DEFAULT_WING
        self._room = _DEFAULT_ROOM
        self._enable_kg_write = False
        self._enable_coordination = False
        self._active = False
        self._prefetch_cache: Dict[str, str] = {}
        self._write_queue: "queue.Queue[Dict[str, str]]" = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()

    # -- config resolution ----------------------------------------------------

    def _resolve_config(self, hermes_home: str) -> None:
        cfg = _read_config(hermes_home) if hermes_home else {}
        env_url = os.environ.get("MEMPALACE_HUB_URL", "").strip()
        env_agent = os.environ.get("MEMPALACE_AGENT_ID", "").strip()

        self._hub_url = (cfg.get("hub_url") or env_url or "").rstrip("/")
        self._agent_id = cfg.get("agent_id") or env_agent or "hermes"
        self._wing = cfg.get("wing") or _DEFAULT_WING
        self._room = cfg.get("room") or _DEFAULT_ROOM
        self._enable_kg_write = bool(cfg.get("enable_kg_write", False))
        self._enable_coordination = bool(cfg.get("enable_coordination", False))
        self._token = os.environ.get("MEMPALACE_MCP_HTTP_TOKEN", "").strip()

    def _current_hermes_home(self) -> str:
        return os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))

    # -- ABC: core lifecycle ----------------------------------------------------

    def is_available(self) -> bool:
        self._resolve_config(self._current_hermes_home())
        return bool(self._hub_url and self._token)

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = kwargs.get("hermes_home", "")
        if hermes_home:
            self._resolve_config(hermes_home)
        # Cron/flush turns are system-generated; writing them would corrupt the
        # shared representation of what the user actually said and decided.
        self._active = kwargs.get("agent_context") not in {"cron", "flush"} and kwargs.get("platform") != "cron"
        if not self._active:
            return
        self._worker = threading.Thread(
            target=self._writer_loop, daemon=True, name="mempalace-sharedbrain-writer"
        )
        self._worker.start()

    def system_prompt_block(self) -> str:
        if not self._active:
            return ""
        return (
            f"You share a MemPalace hub with other agents/machines (your identity: "
            f"{self._agent_id}). Before answering about past work, decisions, people, or "
            "projects, search the palace with the mempalace_search tool. Quote results "
            "verbatim -- never paraphrase stored content. If the palace has nothing "
            "relevant, say so; don't guess. File durable outcomes with mempalace_add_drawer. "
            "Never file secrets or tokens."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return self._prefetch_cache.pop(session_id, "") or self._prefetch_cache.pop("", "")

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._active or not query or not self._hub_url:
            return
        threading.Thread(
            target=self._do_prefetch, args=(query, session_id), daemon=True
        ).start()

    def _do_prefetch(self, query: str, session_id: str) -> None:
        try:
            result = self._call_tool("mempalace_search", {"query": query[:_MAX_QUERY_CHARS], "limit": 5})
        except Exception:
            logger.debug("mempalace_sharedbrain: prefetch failed", exc_info=True)
            return
        text = self._format_search_result(result)
        if text:
            self._prefetch_cache[session_id or ""] = text

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if not self._active or (not user_content and not assistant_content):
            return
        content = f"User: {user_content}\nAssistant: {assistant_content}"
        try:
            self._write_queue.put_nowait({"content": content, "room": self._room})
        except queue.Full:
            logger.warning("mempalace_sharedbrain: write queue full, dropping turn")

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Hermes' agent.memory_manager snapshots this BEFORE initialize() runs, to
        # build its tool-name -> provider routing table. Gating on self._active (only
        # set inside initialize()) means the dispatcher never learns these tool names
        # exist, and every call then fails as "Unknown tool" without ever reaching
        # handle_tool_call. Schemas describe the interface, not runtime readiness --
        # readiness is checked inside handle_tool_call instead. For the same reason,
        # resolve config fresh here rather than trusting self._enable_* to already be
        # set -- this method can run before initialize() ever does.
        self._resolve_config(self._current_hermes_home())
        schemas = list(_BASE_TOOL_SCHEMAS)
        if self._enable_kg_write:
            schemas.extend(_KG_WRITE_TOOL_SCHEMAS)
        if self._enable_coordination:
            schemas.extend(_COORDINATION_TOOL_SCHEMAS)
        return schemas

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._active:
            return json.dumps({"error": "mempalace-sharedbrain not active (cron/flush context)."})
        if not self._hub_url or not self._token:
            return json.dumps({"error": "mempalace-sharedbrain not configured (missing hub_url or token)."})
        if tool_name == "mempalace_search":
            result = self._call_tool(
                "mempalace_search",
                {"query": str(args.get("query", ""))[:_MAX_QUERY_CHARS], "limit": args.get("limit", 5)},
            )
            return json.dumps(self._unwrap(result))
        if tool_name == "mempalace_add_drawer":
            result = self._call_tool(
                "mempalace_add_drawer",
                {
                    "wing": self._wing,
                    "room": args.get("room") or self._room,
                    "content": str(args.get("content", "")),
                    "added_by": self._agent_id,
                },
            )
            return json.dumps(self._unwrap(result))
        if tool_name == "mempalace_list_drawers":
            # Unlike add_drawer (which always writes into this agent's own wing --
            # writes should never land somewhere the caller didn't ask for), listing
            # defaults to every wing on the shared brain, matching mempalace_search's
            # scope. Hardcoding self._wing here was a real bug: it silently limited
            # every completeness check to this agent's own wing, so querying a room
            # that only exists in another wing always came back empty. Confirmed
            # live -- Hermes reported exactly this (room="feedback" returning 0
            # results despite mempalace_status showing 8 entries under shane/feedback).
            result = self._call_tool(
                "mempalace_list_drawers",
                {
                    "wing": args.get("wing"),
                    "room": args.get("room"),
                    "limit": args.get("limit", 20),
                    "offset": args.get("offset", 0),
                },
            )
            return json.dumps(self._unwrap(result))
        if tool_name == "mempalace_status":
            return json.dumps(self._unwrap(self._call_tool("mempalace_status", {})))
        if tool_name == "mempalace_get_taxonomy":
            return json.dumps(self._unwrap(self._call_tool("mempalace_get_taxonomy", {})))
        if tool_name == "mempalace_kg_query":
            result = self._call_tool(
                "mempalace_kg_query",
                {
                    "entity": str(args.get("entity", "")),
                    "as_of": args.get("as_of"),
                    "direction": args.get("direction"),
                },
            )
            return json.dumps(self._unwrap(result))
        if tool_name == "mempalace_get_drawer":
            result = self._call_tool("mempalace_get_drawer", {"drawer_id": str(args.get("drawer_id", ""))})
            return json.dumps(self._unwrap(result))
        if tool_name == "mempalace_diary_write":
            result = self._call_tool(
                "mempalace_diary_write",
                {"agent_name": self._agent_id, "entry": str(args.get("content", ""))},
            )
            return json.dumps(self._unwrap(result))
        if tool_name == "mempalace_diary_read":
            result = self._call_tool(
                "mempalace_diary_read",
                {"agent_name": self._agent_id, "last_n": args.get("limit", 10)},
            )
            return json.dumps(self._unwrap(result))

        if tool_name in _KG_WRITE_TOOL_NAMES:
            if not self._enable_kg_write:
                return json.dumps({"error": f"{tool_name} requires enable_kg_write: true in config."})
            return json.dumps(self._unwrap(self._call_tool(tool_name, self._kg_write_args(tool_name, args))))

        if tool_name in _COORDINATION_TOOL_NAMES:
            if not self._enable_coordination:
                return json.dumps({"error": f"{tool_name} requires enable_coordination: true in config."})
            return json.dumps(self._unwrap(self._call_tool(tool_name, self._coordination_args(tool_name, args))))

        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def _kg_write_args(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name == "mempalace_kg_add":
            return {
                "subject": str(args.get("subject", "")),
                "predicate": str(args.get("predicate", "")),
                "object": str(args.get("object", "")),
                "valid_from": args.get("valid_from"),
                "valid_to": args.get("valid_to"),
            }
        if tool_name == "mempalace_kg_invalidate":
            return {
                "subject": str(args.get("subject", "")),
                "predicate": str(args.get("predicate", "")),
                "object": str(args.get("object", "")),
                "ended": args.get("ended"),
            }
        # mempalace_kg_supersede
        return {
            "subject": str(args.get("subject", "")),
            "predicate": str(args.get("predicate", "")),
            "old_object": str(args.get("old_object", "")),
            "new_object": str(args.get("new_object", "")),
        }

    def _coordination_args(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name == "mempalace_event_append":
            return {
                "type": str(args.get("type", "")),
                "stream": str(args.get("stream", "")),
                "room": str(args.get("room", "")),
                "from_agent": self._agent_id,
                "to_agent": args.get("to_agent"),
                "correlation_id": args.get("correlation_id"),
                "status": args.get("status"),
                "body": args.get("body"),
            }
        if tool_name == "mempalace_event_list":
            return {
                "stream": args.get("stream"),
                "to_agent": args.get("to_agent"),
                "correlation_id": args.get("correlation_id"),
                "since_event_id": args.get("since_event_id"),
                "limit": args.get("limit", 50),
            }
        if tool_name == "mempalace_event_wait":
            return {
                "stream": args.get("stream"),
                "to_agent": args.get("to_agent"),
                "correlation_id": args.get("correlation_id"),
                "timeout_ms": args.get("timeout_ms", 60000),
            }
        if tool_name == "mempalace_event_ack":
            return {
                "event_id": str(args.get("event_id", "")),
                "from_agent": self._agent_id,
                "status": args.get("status"),
                "body": args.get("body"),
            }
        if tool_name == "mempalace_artifact_put":
            return {
                "kind": str(args.get("kind", "")),
                "content": str(args.get("content", "")),
                "created_by": self._agent_id,
            }
        if tool_name == "mempalace_artifact_get":
            return {"artifact_id": str(args.get("artifact_id", ""))}
        # mempalace_patch_submit
        return {
            "content": str(args.get("content", "")),
            "from_agent": self._agent_id,
            "stream": str(args.get("stream", "")),
            "to_agent": args.get("to_agent"),
            "correlation_id": args.get("correlation_id"),
            "body": args.get("body"),
        }

    def shutdown(self) -> None:
        self._shutdown_event.set()
        if self._worker:
            self._worker.join(timeout=5.0)

    # -- optional hooks -----------------------------------------------------

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        # sync_turn already files every turn; nothing extra to extract here.
        # Flush is handled by shutdown() joining the writer thread.
        pass

    def backup_paths(self) -> List[str]:
        return []  # no local state -- everything lives on the hub

    def get_config_schema(self) -> List[Dict[str, Any]]:
        # Unlike the official MemPalace-Hermes integration (which uses static class
        # constants for "default"), read the actually-resolved current config here so
        # the dashboard shows what's really configured, not just a generic hint. Only
        # the secret field is left with no default -- never echo a token back.
        self._resolve_config(self._current_hermes_home())
        # Every field below is read fresh only inside initialize(), which Hermes calls
        # once per gateway process start, not per session or per turn -- confirmed live:
        # toggling enable_kg_write via the dashboard alone did not expose the new tools
        # until the gateway container was restarted. Restart applies to every field
        # here, not just the two enable_* toggles, since they all flow through the same
        # initialize()-gated instance state.
        _RESTART_NOTE = " Restart the Hermes gateway to apply."
        return [
            {
                "key": "hub_url",
                "description": (
                    "MemPalace hub /mcp URL, e.g. http://mempalace:8765/mcp (same-host "
                    "container network) or https://your-tailnet-host/mcp (remote)."
                    + _RESTART_NOTE
                ),
                "required": True,
                "default": self._hub_url or None,
            },
            {
                "key": "agent_id",
                "description": (
                    "This agent's identity on the shared brain, <machine>-<harness> format, "
                    "e.g. unraid-hermes." + _RESTART_NOTE
                ),
                "required": True,
                "default": self._agent_id or "unraid-hermes",
            },
            {
                "key": "wing",
                "description": "MemPalace wing (project/namespace) to file Hermes turns under." + _RESTART_NOTE,
                "default": self._wing or _DEFAULT_WING,
            },
            {
                "key": "room",
                "description": "MemPalace room (category) to file Hermes turns under." + _RESTART_NOTE,
                "default": self._room or _DEFAULT_ROOM,
            },
            {
                # No explicit "type" key -- confirmed against the actual Hermes-core Hindsight
                # plugin source (plugins/memory/hindsight/__init__.py, auto_recall/auto_retain
                # fields) that the dashboard infers "kind": "boolean" from the Python type of
                # "default" itself. Declaring "type": "boolean" explicitly (as the MemoryProvider
                # ABC docstring's own field-key list suggests) is what silently dropped these two
                # fields from the dashboard's config API entirely -- root-caused by reading
                # Hindsight's real source, not guessed.
                "key": "enable_kg_write",
                "description": (
                    "Let this agent add/edit facts in the shared knowledge graph "
                    "(mempalace_kg_add/invalidate/supersede), not just query it. Off by default -- "
                    "these mutate structured facts other agents rely on." + _RESTART_NOTE
                ),
                "default": self._enable_kg_write,
            },
            {
                "key": "enable_coordination",
                "description": (
                    "Let this agent send and receive delegated tasks/patches over the shared "
                    "agent logstream (mempalace_event_*, artifact_*, patch_submit). Off by "
                    "default -- this is the consequential toggle: with it on, this agent can be "
                    "handed work autonomously by another agent, not just asked to recall/file memory."
                    + _RESTART_NOTE
                ),
                "default": self._enable_coordination,
            },
            {
                "key": "token",
                "description": "Bearer token for the hub." + _RESTART_NOTE,
                "secret": True,
                "required": True,
                "env_var": "MEMPALACE_MCP_HTTP_TOKEN",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        path = _config_path(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        cfg = {
            "hub_url": values.get("hub_url", ""),
            "agent_id": values.get("agent_id", "unraid-hermes"),
            "wing": values.get("wing", _DEFAULT_WING),
            "room": values.get("room", _DEFAULT_ROOM),
            "enable_kg_write": _coerce_bool(values.get("enable_kg_write", False)),
            "enable_coordination": _coerce_bool(values.get("enable_coordination", False)),
        }
        path.write_text(json.dumps(cfg, indent=2))

    # -- HTTP/JSON-RPC transport ----------------------------------------------

    def _call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """POST one JSON-RPC tools/call request to the hub.

        Returns the parsed ``result`` object. Raises on transport failure, HTTP
        error, or a JSON-RPC-level ``error`` field -- callers decide whether that
        should be logged-and-dropped (background prefetch/writes) or surfaced
        (direct tool calls).
        """
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._hub_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
        )
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("error"):
            raise RuntimeError(f"mempalace hub error calling {tool_name}: {payload['error']}")
        return payload.get("result", {})

    @staticmethod
    def _unwrap(result: Dict[str, Any]) -> Any:
        """Unwrap the MCP content envelope: result.content[].text is itself JSON."""
        content = result.get("content")
        if not isinstance(content, list):
            return result
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if not texts:
            return result
        try:
            return json.loads(texts[0])
        except (json.JSONDecodeError, IndexError):
            return texts[0]

    def _format_search_result(self, result: Dict[str, Any]) -> str:
        parsed = self._unwrap(result)
        if not isinstance(parsed, dict):
            return ""
        hits = parsed.get("results")
        if not isinstance(hits, list) or not hits:
            return ""
        lines = [h.get("text", "") for h in hits if isinstance(h, dict) and h.get("text")]
        if not lines:
            return ""
        return "Relevant memory from the shared MemPalace hub:\n" + "\n---\n".join(lines)

    # -- background write worker ----------------------------------------------

    def _writer_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                item = self._write_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                result = self._call_tool(
                    "mempalace_add_drawer",
                    {
                        "wing": self._wing,
                        "room": item["room"],
                        "content": item["content"],
                        "added_by": self._agent_id,
                    },
                )
                parsed = self._unwrap(result)
                if isinstance(parsed, dict) and parsed.get("success") is False:
                    logger.info("mempalace_sharedbrain: drawer not filed (%s)", parsed.get("reason", "duplicate?"))
            except Exception:
                logger.warning("mempalace_sharedbrain: failed to file drawer", exc_info=True)
            finally:
                self._write_queue.task_done()
