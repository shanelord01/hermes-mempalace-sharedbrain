# hermes-mempalace-sharedbrain

A Hermes Agent memory-provider plugin for [MemPalace](https://github.com/MemPalace/mempalace)'s
**shared-brain hub** (`mempalace serve`) — the real multi-machine, multi-agent shared memory
mode, not a local palace.

## Why this exists

MemPalace already has three Hermes integrations (the official in-tree one, and two
independent third-party repos). All three call MemPalace as an in-process library or wrap
the local `mempalace` CLI against `~/.mempalace/`. That means a running shared-brain hub has
no effect on any of them for reads, and only the CLI-wrapping one forwards *writes* to the hub
(via the CLI's own `mine` forwarding) while search still queries the empty local palace —
verified by reading their source, not assumed.

This plugin only ever talks to the hub, over HTTP, via its `/mcp` JSON-RPC endpoint. There is
no local palace and no `mempalace` package dependency — if the hub is unreachable, calls fail
loudly instead of silently succeeding against nothing.

## Requirements

A running MemPalace hub (`mempalace serve`, see the
[Remote / Team Server guide](https://mempalaceofficial.com/guide/remote-server.html)) reachable
over HTTP, and its bearer token.

## Install

```bash
hermes plugins install shanelord01/hermes-mempalace-sharedbrain --enable
hermes memory setup   # select mempalace-sharedbrain
```

Or manually:

```bash
hermes config set memory.provider mempalace-sharedbrain
echo "MEMPALACE_MCP_HTTP_TOKEN=<hub bearer token>" >> ~/.hermes/.env
```

## Configuration

`$HERMES_HOME/mempalace_sharedbrain.json`:

```json
{
  "hub_url": "http://mempalace:8765/mcp",
  "agent_id": "unraid-hermes",
  "wing": "hermes",
  "room": "conversation"
}
```

| Key | Env override | Default | Purpose |
|---|---|---|---|
| `hub_url` | `MEMPALACE_HUB_URL` | — (required) | The hub's `/mcp` endpoint |
| `agent_id` | `MEMPALACE_AGENT_ID` | `hermes` | This agent's identity on the shared brain (`<machine>-<harness>` convention) |
| `wing` | — | `hermes` | MemPalace wing (project/namespace) turns are filed under |
| `room` | — | `conversation` | MemPalace room (category) turns are filed under |
| — | `MEMPALACE_MCP_HTTP_TOKEN` | — (required, secret) | Bearer token, `.env` only |

Config file wins over env vars, which win over defaults.

## What it does

- `system_prompt_block()` — teaches the model the shared-brain etiquette (search before
  answering about past work, quote verbatim, file durable outcomes, never file secrets).
- `queue_prefetch()` / `prefetch()` — background `mempalace_search` call before each turn.
- `sync_turn()` — files each completed turn into the palace via a bounded background queue
  (`mempalace_add_drawer`); the agent loop never blocks on it.
- Exposes `mempalace_search` and `mempalace_add_drawer` as Hermes tools directly, so the model
  can search/file proactively, not just through auto-injection.

## Wire format

JSON-RPC `tools/call` over HTTP with `Authorization: Bearer <token>`:

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "...", "arguments": {...}}}
```

The hub wraps every tool's real output as a JSON string inside the standard MCP content
envelope (`result.content[0].text`) — this plugin unwraps that automatically. Both the request
shape and the response shape were confirmed against a live hub, not assumed from docs.

## License

MIT
