# XMemo Memory Provider for Hermes Agent

[XMemo](https://xmemo.dev) is an identity-aware memory layer for AI agents: one persistent, user-owned memory space that works across ChatGPT, Copilot, Claude, Codex, Gemini, and here — Hermes Agent. Every memory entry knows who wrote it, so you can recall, audit, and control context without confusion or spillover.

This plugin connects Hermes Agent to your XMemo account, giving the agent durable memory that survives sessions and long-running workflows, with per-profile isolation.

## What it does

- **Orchestrated recall** — Hermes prefetches relevant XMemo context before each turn, automatically.
- **Semantic search** — ask the agent to search saved facts with natural language (`xmemo_search`).
- **Durable fact storage** — store explicit facts, preferences, and decisions (`xmemo_remember`).
- **Working state with TTL** — save active task, next action, or blocker so future sessions can resume (`xmemo_update_state`).
- **Built-in memory mirroring** — when Hermes' native `memory` tool writes, the same fact is mirrored to XMemo.
- **Session snapshots** — captures a restart snapshot at session end for later restoration by compatible XMemo workflows.
- **Reminders & timeline** — optional workflow tools for TODOs and timeline events (opt-in via config).
- **Profile isolation** — each Hermes profile uses its own `xmemo.json` and scoped memories.
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

1. **Before the turn** — Hermes calls `prefetch()` to retrieve relevant XMemo context and injects it into the conversation.
2. **During the turn** — the agent can call explicit XMemo tools (`xmemo_search`, `xmemo_remember`, etc.).
3. **After the turn** — high-signal turns can be recorded to the XMemo timeline if `capture_timeline` is enabled.
4. **Session end** — a restart snapshot is captured for later restoration by compatible XMemo workflows.

Automatic prefetch, turn sync, and session snapshots run in the background. Explicit tool calls and memory mirroring use bounded timeouts, with a circuit breaker protecting Hermes from repeated failures.

## Configure

Edit `$HERMES_HOME/xmemo.json`:

| Key | Default | Description |
|-----|---------|-------------|
| `base_url` | `https://xmemo.dev` | XMemo service URL |
| `agent_id` | `hermes` | Agent family identifier |
| `agent_instance_id` | auto-generated | Stable, opaque install identifier |
| `bucket` | `work` | Storage namespace |
| `scope` | `hermes/default` | Project/session scope |
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
| `XMEMO_URL` | `base_url` (`MEMORY_OS_URL` is also accepted) |
| `XMEMO_AGENT_ID` | `agent_id` |
| `XMEMO_AGENT_INSTANCE_ID` | `agent_instance_id` |
| `XMEMO_BUCKET` | `bucket` |
| `XMEMO_SCOPE` | `scope` |
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
- [Hermes Agent docs: Memory Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers)

## License

MIT
