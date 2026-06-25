# XMemo Hermes Agent Memory Provider Plugin

[XMemo](https://xmemo.dev) is an identity-aware memory layer for AI agents: one persistent, user-owned memory space that works across ChatGPT, Copilot, Claude, Codex, Gemini, and here — Hermes Agent. Every memory entry knows who wrote it, so you can recall, audit, and control context without confusion or spillover.

This plugin connects Hermes Agent to your XMemo account, giving the agent durable memory that survives sessions and long-running workflows, with per-profile isolation.

## Agent-readable summary

Use this section when a setup agent, package index, marketplace crawler, or
Hermes-aware assistant needs to classify the integration quickly.

| Field | Value |
|------|-------|
| Package | `hermes-xmemo` |
| Hermes plugin name | `xmemo` |
| Runtime role | Native Hermes memory provider |
| Recommended mode | `hermes_plugin_preferred` |
| XMemo service | `https://xmemo.dev` |
| XMemo MCP fallback | `https://xmemo.dev/mcp` |
| Agent discovery | `https://xmemo.dev/.well-known/agent-discovery.json` |
| Hermes config endpoint | `https://xmemo.dev/v1/mcp/config/hermes` |
| Repository | `https://github.com/yonro/hermes-xmemo-plugin` |
| ChatGPT bridge | ChatGPT can connect to XMemo through the hosted MCP/OAuth path; user-approved memories saved there can be recalled by Hermes through this plugin. |

If the active runtime is Hermes Agent, prefer this native plugin over raw MCP
configuration. The plugin participates in Hermes' memory lifecycle: pre-turn
recall, native memory mirroring, working state, optional workflow tools, and
session-end snapshots. Use the hosted MCP endpoint only as a portable fallback
or for non-Hermes MCP clients.

## Shared memory with ChatGPT

XMemo gives Hermes a way to work with the same user-owned cloud memory that can
also be connected from ChatGPT. When a user saves useful ChatGPT brainstorms,
project decisions, preferences, research notes, or handoff context into XMemo,
Hermes can later recall that context through this native memory provider.

This is especially useful for long-running work: ChatGPT can help shape the
idea, XMemo stores the durable context under the user's account, and Hermes can
continue with the same memory layer during agent runs. This does not mean the
plugin reads ChatGPT's built-in native memory directly. It reads XMemo memories
that the user explicitly saved or authorized through the XMemo connection.
Marketplace or official-connection claims should be made only when supported by
public listing or review evidence.

## What it does

- **Cross-agent recall** — Hermes prefetches relevant XMemo context from all visible memories in your XMemo account, including memories written by other connected agents.
- **Semantic search** — ask the agent to search saved facts with natural language (`xmemo_search`); results include provenance such as `self` or `other_agent` when XMemo returns it.
- **Durable fact storage** — store explicit facts, preferences, and decisions (`xmemo_remember`).
- **Working state with TTL** — save active task, next action, or blocker so future sessions can resume (`xmemo_update_state`).
- **Built-in memory mirroring** — when Hermes' native `memory` tool writes, the same fact is mirrored to XMemo.
- **Session snapshots** — captures a restart snapshot at session end for later restoration by compatible XMemo workflows.
- **Reminders & timeline** — optional workflow tools for TODOs and timeline events (opt-in via config).
- **Hermes write scoping** — new Hermes-authored memories are written to the configured Hermes scope so provenance stays clear.
- **Resilient by default** — circuit breaker pauses API calls after consecutive failures; background prefetch/sync never block the chat.

## Install

Hermes looks for memory provider plugins in `$HERMES_HOME/plugins/<name>/`. The default `HERMES_HOME` is `~/.hermes`.

### pip install (recommended)

```bash
pip install hermes-xmemo
hermes-xmemo install
hermes memory setup xmemo
```

To install to a non-default Hermes home:

```bash
HERMES_HOME=/path/to/your/hermes-home hermes-xmemo install
```

### One-liner (no pip)

```bash
curl -fsSL https://raw.githubusercontent.com/yonro/hermes-xmemo-plugin/main/install-remote.sh | bash
hermes memory setup xmemo
```

### Using install.sh (if you already cloned)

```bash
cd hermes-xmemo-plugin
bash install.sh
```

### Manual install

```bash
# Remove any previous install first to avoid nested directories.
rm -rf "${HERMES_HOME:-$HOME/.hermes}/plugins/xmemo"
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins"
cp -r src/hermes_xmemo/xmemo "${HERMES_HOME:-$HOME/.hermes}/plugins/"
```

### Configure

```bash
hermes memory setup xmemo
```

The setup wizard only asks for your XMemo token.

Or manually:

```bash
hermes config set memory.provider xmemo
echo "XMEMO_KEY=your-token" >> "${HERMES_HOME:-$HOME/.hermes}/.env"
```

## Requirements

- Hermes Agent (the plugin is loaded by Hermes at runtime)
- `httpx` (already a dependency of Hermes)
- XMemo service token from [xmemo.dev](https://xmemo.dev)

## How it works

1. **Before the turn** — Hermes calls `prefetch()` to retrieve relevant XMemo context across visible memories in your XMemo account and injects it into the conversation.
2. **During the turn** — the agent can call explicit XMemo tools (`xmemo_search`, `xmemo_remember`, etc.).
3. **After the turn** — high-signal turns can be recorded to the XMemo timeline if `capture_timeline` is enabled.
4. **Session end** — a restart snapshot is captured for later restoration by compatible XMemo workflows.

Automatic prefetch, turn sync, and session snapshots run in the background. Explicit tool calls and memory mirroring use bounded timeouts, with a circuit breaker protecting Hermes from repeated failures.

## Configure

Most users do not need this section. Advanced settings are available in
`$HERMES_HOME/xmemo.json`:

| Key | Default | Description |
|-----|---------|-------------|
| `agent_id` | `hermes` | Agent family identifier |
| `agent_instance_id` | auto-generated | Stable, opaque install identifier |
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

### Environment variables

You can override most settings via environment variables (useful for CI or containerized setups):

| Variable | Overrides |
|----------|-----------|
| `XMEMO_KEY` | API key (required; `MEMORY_OS_API_KEY` is also accepted) |
| `XMEMO_AGENT_ID` | `agent_id` |
| `XMEMO_AGENT_INSTANCE_ID` | `agent_instance_id` |
| `XMEMO_BUCKET` | `bucket` |
| `XMEMO_SCOPE` | `scope` |
| `XMEMO_READ_BUCKET` | `read_bucket` |
| `XMEMO_READ_SCOPE` | `read_scope` |
| `XMEMO_TIMEOUT_SECONDS` | `timeout_seconds` |
| `XMEMO_PREFETCH_MAX_ITEMS` | `prefetch_max_items` |
| `XMEMO_PREFETCH_MAX_TOKENS` | `prefetch_max_tokens` |

## Tools

### Default tools

These tools are always available:

| Tool | Description |
|------|-------------|
| `xmemo_recall_context` | Build a bounded, ranked context pack |
| `xmemo_search` | Semantic search over XMemo memories |
| `xmemo_remember` | Save a durable fact |
| `xmemo_update_state` | Save active task / next action / blocker with TTL |

### Optional workflow tools

Set `enable_workflow_tools: true` in `xmemo.json` to expose:

| Tool | Description |
|------|-------------|
| `xmemo_record_event` | Append a timeline event or milestone |
| `xmemo_create_reminder` | Create a TODO / action item |
| `xmemo_list_reminders` | List open or completed reminders |
| `xmemo_complete_reminder` | Mark a reminder as completed |

### Optional destructive tool

Set `enable_destructive_tools: true` to expose:

| Tool | Description |
|------|-------------|
| `xmemo_forget` | Delete a memory by exact id |

## Privacy & security

- API keys live in `$HERMES_HOME/.env`, never in `xmemo.json`.
- `xmemo_forget` requires an exact memory id and is disabled by default.
- Automatic timeline writes are disabled by default. When enabled, only high-signal turns (decisions, preferences, blockers, etc.) are recorded.
- Built-in `memory` tool writes are mirrored to XMemo only when the provider is active.
- Prefetch cache is isolated per session, so concurrent gateway sessions cannot cross-contaminate recall context.

## Reliability, Local Cache & Outbox

To improve availability and reduce data-loss risk during temporary XMemo API outages, the plugin implements a local reliability layer using a SQLite database (`xmemo_cache.db` located in `$HERMES_HOME`).

### 1. Read Cache Fallback
- **Authoritative Cloud Priority**: When online, the plugin always queries the XMemo cloud first to ensure absolute freshness.
- **Lightweight Caching**: Successful query results (`xmemo_search`, `xmemo_recall_context`, and background prefetches) are cached locally with a **5-minute fresh TTL**.
- **Stale Fallback**: If the XMemo cloud is unreachable (transient connection failures, timeouts, or HTTP 5xx errors), the plugin automatically falls back to the local cache, provided the cached data is less than **24 hours old** (`max_stale_until`). Fallback results are returned with `stale: true` and `source: "cache"` markers to inform the agent of their status.

### 2. Write Outbox & Idempotency
- **Failover Queueing**: When a write operation (e.g., saving a fact, updating state, recording an event) fails due to a transient network error, it is automatically enqueued into a local outbox.
- **Outbox Status Routing**:
  - **Idempotent Writes** (`xmemo_remember`, `xmemo_update_state`): Enqueued as `'pending'` and automatically replayed by a background sync daemon using exponential backoff (up to 5 retries, capped at a maximum delay of 1 hour).
  - **Non-Idempotent Writes** (`xmemo_record_event`, `xmemo_create_reminder`, etc.): By default, these enter a `'held'` status and are **not** replayed automatically (unless `enable_non_idempotent_replay: true` is set in `xmemo.json`), protecting the server from duplicate entries.
- **Idempotency Standards**: Every write operation generates a stable, unique idempotency key (UUID) at initiation. This key is preserved and sent in the `Idempotency-Key` and `X-Idempotency-Key` headers, as well as the JSON body, on all retries and replays, allowing the XMemo server to deduplicate requests.
- **Dead-Lettering**: If an outbox write fails with a permanent non-transient error (e.g., HTTP 4xx validation or auth errors) or exhausts all 5 retry attempts, it is immediately moved to `'failed'` (dead-lettered) status with the error message recorded in the database.

### 3. Privacy, Retention & Diagnostics
- **Payload Security**:
  > [!WARNING]
  > The local `xmemo_cache.db` database file stores copies of cached recall responses and queued outbox payloads in plain-text JSON. No credentials or API keys are ever cached or written to this database. To protect this sensitive data, secure the `$HERMES_HOME` directory and restrict file access to the owner process.
- **Database Retention & Cleanup**:
  - Successfully synchronized writes (`'sent'`) are automatically pruned from the outbox after **24 hours**.
  - Failed/dead-lettered outbox writes (`'failed'`) are automatically deleted after **7 days**, and the dead-letter queue is capped at a maximum of **100 records** to prevent database bloating.
- **Diagnostic Controls**:
  If you need to clear the local cache or outbox (for troubleshooting or privacy reasons), the plugin provides programmatic controls via the `XMemoLocalCache` class:
  - `local_cache.clear_cache()`: Safe to call at any time. Instantly clears all cached read queries.
  - `local_cache.clear_outbox()`: Instantly purges all queued write operations.
    > [!CAUTION]
    > Wiping the outbox will permanently delete any pending or held write operations that have not yet synchronized to the XMemo cloud.

## Uninstall or disable

To disable XMemo without removing files:

```bash
hermes config set memory.provider ""
```

To remove the plugin entirely:

```bash
rm -rf "${HERMES_HOME:-$HOME/.hermes}/plugins/xmemo"
```

## Learn more

- [xmemo.dev](https://xmemo.dev) — XMemo home
- [XMemo agent discovery](https://xmemo.dev/.well-known/agent-discovery.json)
- [Hermes XMemo config](https://xmemo.dev/v1/mcp/config/hermes)
- [XMemo hosted MCP fallback](https://xmemo.dev/mcp)
- [Hermes Agent docs: Memory Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers)

## License

MIT
