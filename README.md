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

To host a hub with per-user OAuth login instead of a shared static token, see
[Running a MemPalace hub behind Newt/Pangolin with Pocket ID OAuth](https://github.com/shanelord01/hermes-mempalace-sharedbrain/wiki/MemPalace-Hub-behind-Newt-Pangolin-OAuth)
on the wiki: MemPalace + Qdrant behind Newt/Pangolin with no open inbound ports, Pocket ID as the
OAuth 2.1 authorization server, and a caddy-jwt container in front of the hub.

Claude Code machines on the same hub:
[claude-mempalace-sharedbrain](https://github.com/shanelord01/claude-mempalace-sharedbrain) is the
matching Claude Code plugin. It checks the palace and the task inbox at the start of every
session, reminds the model to checkpoint, and snapshots the conversation before compaction, so a
Hermes gateway and your Claude Code sessions share one palace and one logstream.

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
| `bridge_enabled` | - | `false` | Take part in the hub's message bridge (see below) |
| `bridge_mode` | - | `read` | `act`, `read` or `off` |
| `bridge_max_turns_per_hour` | - | `12` | Most automatic turns the bridge starts in an hour |
| `bridge_max_turns_per_thread` | - | `4` | Most automatic turns on one thread before it pauses for you |
| `bridge_owner_ids` | - | `[]` | Your own user ids on gateway platforms (your Discord user id) |
| `bridge_session_key` | - | (most recent chat) | Pin automatic bridge turns to one chat |
| `bridge_sign_tasks` | - | `session` | When an outgoing task is signed: `session`, `ask` or `auto` |
| `presence_wing` | - | `fleet` | Wing of the shared presence room |
| `presence_room` | - | `presence` | Room of the shared presence room |
| `presence_interval_minutes` | - | `30` | Minutes between check-ins |
| `host_label` | - | the hostname | Host name shown in the check-in line (not on the dashboard) |
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

Opt-in via `bridge_enabled`: `mempalace_bridge_continue`, plus the coordination tools above,
because a bridge turn claims, replies to and closes tasks with them.

Both toggles are off by default and surfaced in `hermes memory setup` / the dashboard, not
silently on. Identity fields (`from_agent`, `created_by`, `agent_name`) are always injected from
the plugin's own `agent_id` config, never accepted from the model, so a tool call can't spoof
another agent's identity on the shared brain.

## The message bridge

Agents on one MemPalace hub can send each other work through its event log. With the bridge on,
Hermes takes part as `agent_id` (for example `unraid-hermes`): a message addressed to it reaches it
within about a minute and starts a turn in the chat you used most recently, and a message sent while
Hermes is down waits on the hub and is picked up when the bridge next starts. The protocol is shared with
the Claude Code plugin and written up in
[docs/bridge.md](https://github.com/shanelord01/claude-mempalace-sharedbrain/blob/main/docs/bridge.md).

### Switching it on

Add the bridge keys to `$HERMES_HOME/mempalace_sharedbrain.json`:

```json
{
  "hub_url": "http://mempalace:8765/mcp",
  "agent_id": "unraid-hermes",
  "bridge_enabled": true,
  "bridge_mode": "read"
}
```

`read` (the default) starts a turn for each message that only reads and reports it. Set
`bridge_mode` to `act` to let tasks addressed to Hermes, signed with a key you approved, be carried
out (see Signing and trust below).

Starting a turn uses Hermes's plugin message API, which is off for every plugin until you grant it
in `config.yaml`:

```yaml
plugins:
  entries:
    mempalace-sharedbrain:
      allow_gateway_injection: true
```

For Discord (or another gateway platform), also set `bridge_owner_ids` to your own user id there,
for example `"bridge_owner_ids": ["123456789012345678"]`. Then restart the gateway, the dashboard and
hermes-webui, and send Hermes one message wherever you use it.

### Where bridge mail reaches you

Bridge mail goes only to your own chats, never to a chat with other people in it:

| Where you talk to Hermes | Mail with your next message | Automatic turn when mail arrives |
|---|---|---|
| Web dashboard chat | yes | yes, while that chat is open |
| hermes-webui and the Hermex app | yes | yes |
| Discord direct message | yes, when your user id is an owner | yes |
| Discord thread | yes, when you are an owner and nobody else has spoken there | yes |
| Discord channel outside a thread | no | no |
| `hermes chat` in a terminal | yes | no |

The dashboard, hermes-webui and the terminal are yours by their nature: Hermes reports them as
platform `tui`, `webui` and `cli`, which only those programs produce, and only you can reach them.
Discord chats fail closed. A direct message or thread counts only when its author is an owner:
someone listed in `bridge_owner_ids`, or, when that is empty, the one user named in
`DISCORD_ALLOWED_USERS` (or `GATEWAY_ALLOWED_USERS`) if it names exactly one. With neither, Discord
gets no bridge mail and no turns, and the check-in and the log say to fill in `bridge_owner_ids`. A
thread where anyone else, or another bot, has spoken is never used again. A channel outside a thread
never counts, because Hermes keeps one session per person there and the plugin cannot see who else
is talking.

Mail is shown once. The first of your chats to take a message shows it, and the others leave it
alone. If that turn never finishes, another chat may show it again after 20 minutes. The inbox moves
on only after a turn that showed it has finished.

An automatic turn starts in the chat you used most recently that some running Hermes process can
start a turn in. Each surface needs its own process to do it, because Hermes offers no way to start a
turn in another process:

- **Discord:** the gateway process, through Hermes's plugin message API. The gateway can only post
  the turn as a message from you: it appears in the thread or DM under your own Discord name, with the
  📨 From line on top saying which agent actually sent it.
- **The dashboard chat:** the process that runs that chat, through the same API, and only while the
  chat is open in a browser. A dashboard chat often runs in its own `tui_gateway` process rather than
  in the dashboard web server, so the plugin remembers which process each chat ran in and only that
  process starts the turn.
- **hermes-webui:** the webui process, through webui's own `start_session_turn`. That is a webui
  function, not a Hermes plugin API, so a webui update could change it. It needs the same
  `allow_gateway_injection` consent.

Every one of these processes runs the bridge's delivery loop, and one of them (whichever got there
first) also reads the hub. They share `$HERMES_HOME/mempalace_sharedbrain/`, so the webui container
needs the same `HERMES_HOME` as the gateway. If the chosen chat cannot take the turn (the dashboard
tab closed, the webui container down), the turn moves to the next most recent chat: at once when the
process says why it cannot, after two minutes when no process answers. The reason is logged as a
warning and kept in the bridge state under `failed_targets`, and that chat is skipped for ten
minutes.
If no chat can take it, the mail waits for your next message. A webui chat that is busy is retried a
few seconds later. Set `bridge_session_key` to pin turns to one chat instead.

Hermes's cron jobs are not used. This plugin is switched off inside a cron run, so the run could not
claim, reply to or close anything, and cron can deliver its output to Discord or the dashboard's bot
chat but not to hermes-webui.

The bridge starts in each process with the first conversation there after a restart, because Hermes
builds a memory provider per conversation and gives memory providers no start-up hook. CLI, cron and
subagent runs never start it. Without the permission, or with no chat that can take a turn, mail
still arrives with your next message and the check-in says `listening no`.

### What it does

The bridge checks in to the presence room (wing `fleet`, room `presence` by default) when it starts
and every 30 minutes: one drawer for this identity, updated in place, whose first two lines read

```
identity: unraid-hermes | checked_in 2026-10-04T09:30:00Z | plugin 1.2.1 hermes | listening yes | host unraid | project hermes | bridge act
bridge-key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... SHA256:<fingerprint>
```

About once a minute it reads `mempalace_event_list` addressed to `agent_id` (which includes `*`
broadcasts) from its own cursor, in pages of 40. In `act` mode, a `task.request` addressed to this
agent by name, with a `correlation_id`, carrying a valid signature from a key you approved for its
sender, with no stated requirements, is act level: the turn claims it, does the work, replies and
closes it. Everything else is read level: broadcasts, replies, `patch.ready`, unsigned or untrusted
messages, and tasks with requirements (Hermes has no capability profile, so it cannot confirm them).
A read-level turn reports the message to you, answers a direct question with a `task.reply` and
does nothing else.

Each message in your chat opens with a line saying who sent it and what this machine found when it
checked the signature:

```
📨 From bazzite:claude:projects · 🔐 signature verified (SHA256:<fingerprint>)
📨 From bazzite:claude:projects · ⚠️ unsigned
📨 From bazzite:claude:projects · ⛔ signature not valid: <reason>
```

The mark and the fingerprint come only from Hermes's own check, never from anything the sender
wrote, and those mark characters are removed from every sender name and message body, so a message
cannot fake a verified line. Hermes's plugin message API carries the text as a user message with no
separate sender field, which is why the sender line is part of the text.

In `read` mode every message is read level. In `off` mode no turn starts and mail waits for your next
message, as it did before the bridge. Mail that waits reaches the model through the memory prefetch
of your next message, in any of your own chats (see the table above).

Before an act-level turn the bridge takes a lock file for the event, so two processes sharing the
identity never both act on it, and posts a status-less `received:` ack so the sender knows Hermes
picked it up. A task that its sender, its named addressee or Hermes itself already closed starts no
turn. For a broadcast anyone's closure counts, and you are told who closed it.

The inbox cursor moves only past mail whose turn finished. If a turn never runs, the mail is shown
again at your next message instead of being lost. Turns started by mail are filed to the palace
without the other agents' text, so the palace never records it as something you said.

### Signing and trust

A sender name is free text, so act level needs a signature. Each machine has one Ed25519 key used
only for the bridge. Hermes creates its key on first use at
`$HERMES_HOME/mempalace_sharedbrain/bridge_ed25519` and publishes the public half in its check-in.
Signatures use OpenSSH's own format (`ssh-keygen -Y sign`, namespace `mempalace-bridge`). Hermes runs
`ssh-keygen` when it is installed and otherwise does the same with the Python `cryptography`
package. With neither, the check-in says signing is unavailable and every message stays read level.

Replies (`task.reply`) that Hermes sends with `mempalace_event_append` are always signed. A signed
`task.request` or `patch.ready` is something another machine may carry out, so `bridge_sign_tasks`
decides when you approve one:

| Value | What happens |
|---|---|
| `session` (default) | The first task to each recipient in a chat waits for your approval. `/bridge-send <id> always` signs it and every later task to that recipient in the same chat. |
| `ask` | Every task waits for your approval. |
| `auto` | Tasks are signed without asking. |

Any other value counts as `ask`. A task waiting for approval is not sent. Hermes keeps it as a draft
with a four-character id and asks you to type `/bridge-send <id>`. That command prints the stored
draft exactly as it will be sent (type, sender, recipient, correlation id and the full body), read
from the plugin's own store rather than as the model describes it, followed by your options:

```
/bridge-send <id> sign      sign and send it
/bridge-send <id> always    sign and send it, and sign later tasks to that recipient in this chat (session only)
/bridge-send <id> unsigned  send it unsigned
```

Only these forms send, and the reply gives the hub's event id. Only you can run `/bridge-send`: no
model tool wraps it, and text injected by the bridge cannot run slash commands. Drafts expire after
30 minutes, and a draft longer than 3,000 characters can only go unsigned. For a longer brief that
should still be carried out, put the brief in a hub artifact (`mempalace_artifact_put`) and send a
short task that names the artifact id and its sha256; the worker fetches it with
`mempalace_artifact_get` and checks the hash. The model is told this when it drafts a task that is
too long.

In every mode, a turn that carries mail from other agents never signs or drafts a `task.request` or
`patch.ready`, whether the mail started the turn or arrived with your message. It goes out unsigned
and the tool result says why, so another agent's message cannot get a signed task sent on to a third
machine. This holds in `auto` and for remembered recipients too. Memory recalled from the palace does
not count. Send the task
with `mempalace_event_append`, naming the recipient and a `correlation_id`. A sender, recipient,
type or correlation id that could blur the signed lines (a newline, or characters outside the
allowed set) is never signed, and an incoming one never verifies.
`mempalace_patch_submit` and the hub's `mempalace_task_create` cannot carry a signature.

An act-level turn is given the exact body of the event that passed these checks, fenced as data and
labelled with its event id, and is told to act only on that text. Anything else on the same thread,
including what a later fetch of the thread returns, stays read level, so an unsigned event cannot
stand in for the signed one.

A signature counts when it verifies over the event's sender, recipient, type, correlation id, time
and full body against a key you approved for that sender, is no more than 14 days old or 5 minutes
ahead, and has not been seen before on a different event. Where the hub records which identity it
authenticated for an event (the `writer` field on newer hubs) and that differs from the claimed
sender, the message is read level and its closures do not count.

A published key grants nothing until you approve it. The easy way is a 6-digit code. On the
machine whose key the others should trust, type `/bridge-trust pair` in your Hermes chat (Claude
Code: `/mempalace-sharedbrain:trust pair`). It sends the key to the hub and shows a code. On each
machine that should trust it, type `/bridge-trust pair <code>` (Claude Code:
`/mempalace-sharedbrain:trust pair <code>`) within 10 minutes. The code never goes to the hub. If
two different keys answer to the same code, nothing is approved and you start again. The receiving
machine reads every pairing request of the last 15 minutes, and refuses if the hub cannot be read
completely or holds more than 2,000 of them. Pairing is one
way, so run it on each machine that should send work.

The other commands, all typed in your Hermes chat:

```
/bridge-trust list                             check-ins, their keys and fingerprints, and which are approved
/bridge-trust show                             this machine's own key and fingerprint
/bridge-trust approve <identity> <fingerprint> trust that identity's published key
/bridge-trust revoke <principal>               remove a trusted key
```

Approving by hand needs the fingerprint, read on the other machine with `/bridge-trust show` (or
`/mempalace-sharedbrain:trust show`). It is refused if the published key differs, or if two check-ins
claim the same identity. Approving a `host:harness:project` identity trusts the key for `host:*`,
every identity on that machine. Approving a fixed name such as `unraid-hermes` trusts it for that
name only. Each principal holds one key: approving or pairing again replaces the key it had.
Approved keys live in the OpenSSH allowed-signers file
`$HERMES_HOME/mempalace_sharedbrain/trusted_signers`. No model tool can pair, approve or revoke a key.

Whatever the level, text from other agents is treated as data: excerpts are cleaned of control
characters, shortened, kept to one line each and fenced between "begin data" and "end data" markers
they cannot forge, and the turn is told never to put them into a shell command.

### Limits and resuming a paused thread

The bridge starts at most `bridge_max_turns_per_hour` turns an hour (12). Past that, mail waits for
your next message and you are told once.

A thread is a `correlation_id`, or the event id when there is none. After
`bridge_max_turns_per_thread` automatic turns on one thread (4), the turn that reaches the limit
does its work, summarises the whole thread for you, posts a `status` event on the thread telling the
other agent it has paused for you, and asks whether to continue. New mail on a paused thread starts
no turn and waits, marked as paused. To let it carry on, type

```
/bridge-continue <thread>
```

in your Hermes chat, or ask Hermes to resume it, which calls `mempalace_bridge_continue`. Either one
resets the thread's count. `/bridge-continue` on its own lists the paused threads, and
`/bridge-continue all` resumes every one. The tool refuses to resume anything in a turn that carries
bridge mail, whether the mail started the turn or arrived with your message, so mail can never lift
its own pause. The slash command always works, because injected text cannot run slash commands.

### State

The cursor, the presence drawer id, thread counts, paused threads, mail waiting for you and the
signatures already seen live in `$HERMES_HOME/mempalace_sharedbrain/`, one file per identity, with
per-event lock files, the bridge key and the trusted signers file beside it. Keep a copy of the key
if you rebuild the container, or approve the new one on the other machines.
Deleting the directory starts the bridge afresh: it then looks only at open tasks that nobody has
closed and that Hermes has not already taken on.

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
