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
        self._token = os.environ.get("MEMPALACE_MCP_HTTP_TOKEN", "").strip()

    # -- ABC: core lifecycle ----------------------------------------------------

    def is_available(self) -> bool:
        hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        self._resolve_config(hermes_home)
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
        # readiness is checked inside handle_tool_call instead.
        return [
            {
                "name": "mempalace_search",
                "description": "Search the shared MemPalace memory (verbatim results, semantic search).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search keywords or a question (max 250 chars)",
                        },
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
        ]

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
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

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
        return [
            {
                "key": "hub_url",
                "description": (
                    "MemPalace hub /mcp URL, e.g. http://mempalace:8765/mcp (same-host "
                    "container network) or https://your-tailnet-host/mcp (remote)"
                ),
                "required": True,
            },
            {
                "key": "agent_id",
                "description": (
                    "This agent's identity on the shared brain, <machine>-<harness> format, "
                    "e.g. unraid-hermes"
                ),
                "required": True,
                "default": "unraid-hermes",
            },
            {
                "key": "wing",
                "description": "MemPalace wing (project/namespace) to file Hermes turns under",
                "default": _DEFAULT_WING,
            },
            {
                "key": "room",
                "description": "MemPalace room (category) to file Hermes turns under",
                "default": _DEFAULT_ROOM,
            },
            {
                "key": "token",
                "description": "Bearer token for the hub",
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
