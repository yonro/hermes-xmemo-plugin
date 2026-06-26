# XMemo Memory Provider

[XMemo](https://xmemo.dev) is an identity-aware, user-owned memory layer for AI agents. This plugin connects Hermes Agent to your XMemo account so the agent can recall, store, and manage persistent memory across sessions.

## Discovery

Hermes-aware setup agents can start from:

- XMemo discovery: `https://xmemo.dev/.well-known/agent-discovery.json`
- Hermes config: `https://xmemo.dev/v1/mcp/config/hermes`
- Repository: `https://github.com/yonro/hermes-xmemo-plugin`

For Hermes Agent, prefer this native provider plugin. The hosted MCP endpoint
(`https://xmemo.dev/mcp`) remains available as a portable fallback for MCP-only
clients.

## Shared memory with ChatGPT

When ChatGPT is connected to XMemo through the hosted MCP/OAuth path, durable
brainstorms, project decisions, preferences, and handoff notes saved into XMemo
can later be recalled by Hermes through this provider. The provider does not
read ChatGPT's built-in native memory directly; it reads user-approved XMemo
memory in the same account.

## Requirements

- Hermes already depends on `httpx`.
- XMemo service token from [xmemo.dev](https://xmemo.dev).

## Setup

Recommended shared-credential setup:

```bash
npm install -g @xmemo/client
xmemo login
xmemo setup hermes
```

This installs/updates the native Hermes plugin, reuses the user-scoped
credential stored by `@xmemo/client`, and syncs it to Hermes' existing `.env`
location for compatibility. Hosted MCP is optional and uses
`xmemo setup hermes --with-mcp`.

The original Hermes-native setup remains supported:

```bash
hermes memory setup xmemo
```

If a shared `xmemo login` credential already exists, the setup wizard can reuse
it. Otherwise, it asks for your XMemo token as before.

This writes:

- `config.yaml` → `memory.provider = xmemo`
- `$HERMES_HOME/.env` → `XMEMO_KEY`
- `$HERMES_HOME/xmemo.json` → non-secret provider settings

The API key is never written to `xmemo.json`. Do not paste tokens into shell
history, logs, or git-tracked files.

## What it does

- **Cross-agent recall** — prefetches relevant XMemo context from all visible memories in your XMemo account, including memories written by other connected agents.
- **Semantic search** — natural-language search over durable facts, with provenance such as `self` or `other_agent` when XMemo returns it.
- **Durable fact storage** — explicit `xmemo_remember` tool.
- **Working state** — save active task / next action / blocker with TTL.
- **Built-in memory mirroring** — Hermes native `memory` writes are mirrored to XMemo.
- **Session snapshots** — capture restart snapshots at session end.
- **Reminders & timeline** — optional workflow tools (opt-in).
- **Hermes write scoping** — new Hermes-authored memories are written to the configured Hermes scope so provenance stays clear.
- **Resilient** — circuit breaker protects the chat from a slow/unavailable XMemo API.

## Config

Config file: `$HERMES_HOME/xmemo.json`

Most users can leave this file at its defaults.

| Key | Default | Description |
|-----|---------|-------------|
| `agent_id` | `hermes` | Agent family identifier |
| `agent_instance_id` | auto-generated | Stable install identifier (random UUID) |
| `bucket` | `work` | Storage namespace for new Hermes-authored writes |
| `scope` | `hermes/default` | Scope for new Hermes-authored writes |
| `read_bucket` | `%` | Bucket filter for recall/search (`%` = all visible buckets) |
| `read_scope` | unset | Scope filter for recall/search (unset = all visible scopes) |
| `timeout_seconds` | `5.0` | REST request timeout |
| `prefetch_max_items` | `5` | Max context items per recall |
| `prefetch_max_tokens` | `900` | Max context tokens per recall |
| `enable_workflow_tools` | `false` | Expose reminder/event tools |
| `enable_destructive_tools` | `false` | Expose `xmemo_forget` |
| `capture_timeline` | `false` | Record high-signal turns to timeline |

## Default tools

These tools are always available:

| Tool | Description |
|------|-------------|
| `xmemo_recall_context` | Build a bounded, ranked context pack |
| `xmemo_search` | Semantic search over XMemo memories |
| `xmemo_remember` | Save a durable fact |
| `xmemo_update_state` | Save active task / next action / blocker with TTL |

## Optional tools

Set `enable_workflow_tools: true` in `xmemo.json` to expose:

| Tool | Description |
|------|-------------|
| `xmemo_record_event` | Append a timeline event or milestone |
| `xmemo_create_reminder` | Create a TODO / action item |
| `xmemo_list_reminders` | List open or completed reminders |
| `xmemo_complete_reminder` | Mark a reminder as completed |

Set `enable_destructive_tools: true` to expose:

| Tool | Description |
|------|-------------|
| `xmemo_forget` | Delete a memory by exact id |

## Privacy and lifecycle notes

- `xmemo_forget` requires an exact memory id and is disabled by default.
- Automatic timeline writes are disabled by default. When `capture_timeline` is
  `true`, only high-signal turns (decisions, preferences, blockers, etc.) are
  recorded.
- Hermes built-in `memory` tool writes are mirrored to XMemo `remember`.
- Prefetch cache is isolated per session, so concurrent gateway sessions cannot
  cross-contaminate recall context.
