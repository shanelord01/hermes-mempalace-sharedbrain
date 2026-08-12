# hermes-mempalace-sharedbrain

A Hermes Agent memory-provider plugin for [MemPalace](https://github.com/MemPalace/mempalace)'s
**shared-brain hub** (`mempalace serve`) - the real multi-machine, multi-agent shared memory
mode, not a local palace.

## Why this exists

MemPalace already has three Hermes integrations (the official in-tree one, and two
independent third-party repos). All three call MemPalace as an in-process library or wrap
the local `mempalace` CLI against `~/.mempalace/`. That means a running shared-brain hub has
no effect on any of them for reads, and only the CLI-wrapping one forwards *writes* to the hub
(via the CLI's own `mine` forwarding) while search still queries the empty local palace -
verified by reading their source, not assumed.

This plugin only ever talks to the hub, over HTTP, via its `/mcp` JSON-RPC endpoint. There is
no local palace and no `mempalace` package dependency - if the hub is unreachable, calls fail
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
| `hub_url` | `MEMPALACE_HUB_URL` | - (required) | The hub's `/mcp` endpoint |
| `agent_id` | `MEMPALACE_AGENT_ID` | `hermes` | This agent's identity on the shared brain (`<machine>-<harness>` convention) |
| `wing` | - | `hermes` | MemPalace wing (project/namespace) turns are filed under |
| `room` | - | `conversation` | MemPalace room (category) turns are filed under |
| `enable_kg_write` | - | `false` | Let this agent add/edit shared knowledge-graph facts, not just query them |
| `enable_coordination` | - | `false` | Let this agent send/receive delegated tasks and patches over the shared logstream |
| - | `MEMPALACE_MCP_HTTP_TOKEN` | - (required, secret) | Bearer token, `.env` only |

Config file wins over env vars, which win over defaults.

**Every field above needs a Hermes gateway restart to take effect, not just a save.**
`initialize()` -- where this config actually gets read into the running provider -- is
called once per gateway process start, not per session or per turn. Saving a change via
`hermes memory setup` or the dashboard updates the file immediately, but the running
gateway keeps using whatever it read at its own last startup until restarted. Confirmed
live: toggling `enable_kg_write` on did not expose `mempalace_kg_add` in an active
session until the gateway container was restarted.

## What it does

- `system_prompt_block()` - teaches the model the shared-brain etiquette (search before
  answering about past work, quote verbatim, file durable outcomes, never file secrets).
- `queue_prefetch()` / `prefetch()` - background `mempalace_search` call before each turn.
- `sync_turn()` - files each completed turn into the palace via a bounded background queue
  (`mempalace_add_drawer`); the agent loop never blocks on it.

### Tools exposed to the model

Always available (read-only, or self-contained writes to the agent's own diary):
`mempalace_search`, `mempalace_add_drawer`, `mempalace_list_drawers`, `mempalace_status`,
`mempalace_get_taxonomy`, `mempalace_kg_query`, `mempalace_get_drawer`, `mempalace_diary_write`,
`mempalace_diary_read`.

Opt-in via `enable_kg_write` - mutates the shared knowledge graph other agents rely on:
`mempalace_kg_add`, `mempalace_kg_invalidate`, `mempalace_kg_supersede`.

Opt-in via `enable_coordination` - the consequential one. Lets this agent send and receive
delegated tasks and code patches over the shared agent logstream, meaning it can be handed
work autonomously by another agent, not just asked to recall or file memory:
`mempalace_event_append`, `mempalace_event_list`, `mempalace_event_wait`, `mempalace_event_ack`,
`mempalace_artifact_put`, `mempalace_artifact_get`, `mempalace_patch_submit`.

Both toggles are off by default and surfaced in `hermes memory setup` / the dashboard, not
silently on. Identity fields (`from_agent`, `created_by`, `agent_name`) are always injected from
the plugin's own `agent_id` config, never accepted from the model, so a tool call can't spoof
another agent's identity on the shared brain.

## Wire format

JSON-RPC `tools/call` over HTTP with `Authorization: Bearer <token>`:

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "...", "arguments": {...}}}
```

The hub wraps every tool's real output as a JSON string inside the standard MCP content
envelope (`result.content[0].text`) - this plugin unwraps that automatically. Both the request
shape and the response shape were confirmed against a live hub, not assumed from docs.

## License

MIT
