<div align="center">
  <a href="https://xmemo.dev">
    <img src="https://cdn.jsdelivr.net/gh/yonro/hermes-xmemo-plugin@main/assets/icon.png" alt="XMemo" width="112" />
  </a>

  <h1>XMemo for Hermes Agent</h1>

  <p><strong>Native, user-owned long-term memory for Hermes Agent.</strong></p>
  <p>
    Recall across sessions, preserve working state, and keep Hermes connected to
    the same private memory layer as your other AI agents.
  </p>

  <p>
    <a href="https://github.com/yonro/hermes-xmemo-plugin/actions/workflows/pypi-publish.yml"><img alt="Release" src="https://img.shields.io/github/actions/workflow/status/yonro/hermes-xmemo-plugin/pypi-publish.yml?event=push&amp;style=flat-square&amp;logo=githubactions&amp;logoColor=white&amp;label=release" /></a>
    <a href="https://pypi.org/project/hermes-xmemo/"><img alt="PyPI" src="https://img.shields.io/pypi/v/hermes-xmemo?style=flat-square&amp;logo=pypi&amp;logoColor=white&amp;label=PyPI" /></a>
    <a href="https://pypi.org/project/hermes-xmemo/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/hermes-xmemo?style=flat-square&amp;logo=python&amp;logoColor=white" /></a>
    <a href="LICENSE"><img alt="License" src="https://img.shields.io/pypi/l/hermes-xmemo?style=flat-square&amp;label=license" /></a>
    <a href="https://github.com/yonro/hermes-xmemo-plugin/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/yonro/hermes-xmemo-plugin?style=flat-square&amp;logo=github" /></a>
  </p>

  <p>
    <img alt="Hermes native provider" src="https://img.shields.io/badge/Hermes-native%20provider-5B5BD6?style=flat-square" />
    <img alt="XMemo Cloud" src="https://img.shields.io/badge/XMemo-Cloud-10B9B0?style=flat-square" />
    <img alt="Privacy first" src="https://img.shields.io/badge/privacy-first-334155?style=flat-square" />
    <img alt="Tools" src="https://img.shields.io/badge/tools-4%20default%20%2B%204%20workflow-EC4899?style=flat-square" />
  </p>

  <p>
    <a href="#quick-start">Quick start</a> ·
    <a href="#architecture">Architecture</a> ·
    <a href="#tools">Tools</a> ·
    <a href="#configuration">Configuration</a> ·
    <a href="#reliability">Reliability</a> ·
    <a href="#security">Security</a>
  </p>
</div>

---

`hermes-xmemo` is the native XMemo memory provider for
[Hermes Agent](https://hermes-agent.nousresearch.com/). It joins Hermes'
memory lifecycle directly: pre-turn recall, explicit memory tools, built-in
memory mirroring, per-session working context, and session-end snapshots.

> [!NOTE]
> This provider reads memories that you saved or authorized in XMemo. It does
> not read the private built-in memory of ChatGPT, Claude, or another client.

## At a glance

| | |
|---|---|
| Package | [`hermes-xmemo`](https://pypi.org/project/hermes-xmemo/) |
| Hermes provider | `xmemo` |
| Runtime role | Native memory provider |
| Configuration | `$HERMES_HOME/xmemo.json` |
| Cloud service | [`https://xmemo.dev`](https://xmemo.dev) |
| Python | 3.10+ |
| License | MIT |

### Why a native provider?

- **First-class lifecycle** — recall starts before a turn and snapshots happen
  when a session ends.
- **Cross-agent continuity** — Hermes can recall user-approved XMemo memories
  written by other connected agents.
- **Bounded context** — recall is ranked and limited before it reaches the
  prompt.
- **Write isolation** — Hermes-authored entries use a dedicated bucket and
  scope while reads can span all memories visible to the account.
- **Graceful degradation** — timeouts, a circuit breaker, local read cache, and
  a write outbox keep memory failures from blocking the conversation.

## Quick start

### 1. Install with XMemo CLI (recommended)

```bash
npm install -g @xmemo/client
xmemo login
xmemo setup hermes
```

This installs or updates the Python package, deploys the native provider into
Hermes, and reuses your user-scoped XMemo credential. Hosted MCP is not added by
default.

For a custom Hermes home:

```bash
xmemo setup hermes --hermes-home /path/to/.hermes
```

### 2. Install directly from PyPI

```bash
pip install hermes-xmemo
hermes-xmemo install
hermes memory setup xmemo
```

### 3. Install without cloning

```bash
curl -fsSL https://raw.githubusercontent.com/yonro/hermes-xmemo-plugin/main/install-remote.sh | bash
hermes memory setup xmemo
```

The native Hermes setup wizard remains supported. If `xmemo login` has already
stored a shared credential, the wizard can reuse it; otherwise it asks for an
XMemo token.

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/yonro/hermes-xmemo-plugin@main/assets/hermes-setup-flow.svg" alt="XMemo for Hermes setup flow" width="920" />
</p>

## Architecture

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/yonro/hermes-xmemo-plugin@main/assets/hermes-architecture.svg" alt="XMemo for Hermes architecture" width="980" />
</p>

The provider runs inside Hermes and communicates with XMemo over bounded HTTPS
requests. Its local SQLite reliability layer belongs to the Hermes profile; it
is not a second source of truth.

### Lifecycle

1. **Before a turn** — Hermes starts a background prefetch and injects a
   bounded XMemo context pack when it is ready.
2. **During a turn** — the agent can search, remember, or update working state
   with explicit tools. Writes through Hermes' built-in `memory` tool are
   mirrored to XMemo while this provider is active.
3. **After a turn** — high-signal timeline capture is available as an opt-in.
4. **At session end** — the provider captures a restart snapshot and schedules
   a final outbox sync.

Prefetch state is isolated by profile and session, preventing one concurrent
Hermes session from receiving another session's recall context.

### Native provider or hosted MCP?

| | Native provider | Hosted MCP |
|---|---|---|
| Best for | Hermes Agent | MCP-only clients |
| Hermes lifecycle hooks | Yes | No |
| Pre-turn context injection | Yes | Client-dependent |
| Built-in memory mirroring | Yes | No |
| Local cache and outbox | Yes | Client-dependent |
| Endpoint | Installed plugin | `https://xmemo.dev/mcp` |

Use the native provider for Hermes. Add `xmemo setup hermes --with-mcp` only
when you deliberately want the portable MCP fallback as well.

## Tools

### Default

| Tool | Purpose |
|---|---|
| `xmemo_recall_context` | Build a bounded, ranked context pack |
| `xmemo_search` | Search durable memories semantically |
| `xmemo_remember` | Save a durable fact, preference, or decision |
| `xmemo_update_state` | Save active task, next action, or blocker with TTL |

### Optional workflow tools

Set `"enable_workflow_tools": true` in `xmemo.json`:

| Tool | Purpose |
|---|---|
| `xmemo_record_event` | Append a timeline event or milestone |
| `xmemo_create_reminder` | Create a TODO or action item |
| `xmemo_list_reminders` | List open or completed reminders |
| `xmemo_complete_reminder` | Mark a reminder complete |

### Optional destructive tool

Set `"enable_destructive_tools": true` to expose `xmemo_forget`. Deletion
requires an exact memory ID and stays disabled by default.

## Configuration

Most installations need no manual configuration. Non-secret settings live in
`$HERMES_HOME/xmemo.json`:

| Key | Default | Purpose |
|---|---|---|
| `agent_id` | `hermes` | Agent family identifier |
| `agent_instance_id` | generated | Stable, opaque installation identifier |
| `bucket` | `work` | Namespace for new Hermes-authored writes |
| `scope` | `hermes/default` | Scope for new Hermes-authored writes |
| `read_bucket` | `%` | Recall/search bucket filter (`%` means all visible) |
| `read_scope` | unset | Recall/search scope filter (unset means all visible) |
| `timeout_seconds` | `5.0` | REST request timeout |
| `prefetch_max_items` | `5` | Maximum recalled items |
| `prefetch_max_tokens` | `900` | Maximum recalled context tokens |
| `enable_workflow_tools` | `false` | Expose reminder and event tools |
| `enable_destructive_tools` | `false` | Expose `xmemo_forget` |
| `capture_timeline` | `false` | Record high-signal turns |
| `enable_non_idempotent_replay` | `false` | Auto-replay non-idempotent queued writes |

Example:

```json
{
  "bucket": "work",
  "scope": "hermes/default",
  "read_bucket": "%",
  "prefetch_max_items": 5,
  "prefetch_max_tokens": 900,
  "enable_workflow_tools": false
}
```

### Environment overrides

| Variable | Overrides |
|---|---|
| `XMEMO_KEY` | API key |
| `XMEMO_URL` | Service base URL |
| `XMEMO_AGENT_ID` | `agent_id` |
| `XMEMO_AGENT_INSTANCE_ID` | `agent_instance_id` |
| `XMEMO_BUCKET` | `bucket` |
| `XMEMO_SCOPE` | `scope` |
| `XMEMO_READ_BUCKET` | `read_bucket` |
| `XMEMO_READ_SCOPE` | `read_scope` |
| `XMEMO_TIMEOUT_SECONDS` | `timeout_seconds` |
| `XMEMO_PREFETCH_MAX_ITEMS` | `prefetch_max_items` |
| `XMEMO_PREFETCH_MAX_TOKENS` | `prefetch_max_tokens` |

Legacy `MEMORY_OS_API_KEY`, `MEMORY_OS_MCP_TOKEN`, and `MEMORY_OS_URL`
variables remain accepted.

### Credential resolution

Credentials are resolved in this order:

1. `XMEMO_KEY`, then the supported legacy environment variables.
2. The user-scoped credential saved by `xmemo login`.

Secrets are never read from or written to `xmemo.json`. The setup flow can sync
the active token to `$HERMES_HOME/.env` for Hermes compatibility.

## Reliability

The plugin maintains `$HERMES_HOME/xmemo_cache.db` so temporary service or
network failures degrade safely.

| Layer | Behavior |
|---|---|
| Fresh read cache | Reuses matching recall/search results for 5 minutes |
| Stale fallback | Returns marked cache results during transient failures, up to 24 hours old |
| Write outbox | Queues transiently failed writes with stable idempotency keys |
| Replay | Retries idempotent writes up to 5 times with exponential backoff capped at 1 hour |
| Non-idempotent writes | Held by default to avoid duplicate reminders or events |
| Dead letters | Permanent failures and exhausted retries are retained for diagnosis |
| Retention | Sent entries: 24 hours; failed entries: 7 days; failed queue capped at 100 |

Fallback responses are explicitly marked with `stale: true` and
`source: "cache"` so the agent does not confuse an offline cache result with
fresh cloud state.

> [!WARNING]
> `xmemo_cache.db` contains cached memory responses and queued write payloads as
> plain-text JSON. It never stores API credentials, but `$HERMES_HOME` should
> still be readable only by the owning user.

## Security

| Control | Default |
|---|---|
| Credentials outside project configuration | Enabled |
| Destructive memory tool | Disabled |
| Automatic timeline capture | Disabled |
| Exact ID required for deletion | Enabled |
| Per-session prefetch isolation | Enabled |
| Bounded network timeout | 5 seconds |
| Circuit breaker | Enabled |

Avoid committing `$HERMES_HOME/.env`, `xmemo.json`, or runtime cache files.
Treat the XMemo token like any other service credential.

## Operations

Disable XMemo without removing files:

```bash
hermes config set memory.provider ""
```

Re-enable it:

```bash
hermes config set memory.provider xmemo
```

Remove the installed provider:

```bash
rm -rf "${HERMES_HOME:-$HOME/.hermes}/plugins/xmemo"
```

## Development

```bash
git clone https://github.com/yonro/hermes-xmemo-plugin.git
cd hermes-xmemo-plugin
python -m pip install -e .
python -m pytest -q
python -m build
```

The implementation shipped to Hermes lives in
`src/hermes_xmemo/xmemo/`. Keep its bundled README aligned with this repository
README whenever installation or configuration behavior changes.

## Agent-readable metadata

| Field | Value |
|---|---|
| Package | `hermes-xmemo` |
| Provider name | `xmemo` |
| Recommended mode | `hermes_plugin_preferred` |
| Agent discovery | `https://xmemo.dev/.well-known/agent-discovery.json` |
| Hermes configuration | `https://xmemo.dev/v1/mcp/config/hermes` |
| MCP fallback | `https://xmemo.dev/mcp` |
| Repository | `https://github.com/yonro/hermes-xmemo-plugin` |

## Links

- [XMemo](https://xmemo.dev)
- [PyPI package](https://pypi.org/project/hermes-xmemo/)
- [Hermes memory-provider documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers)
- [Agent discovery](https://xmemo.dev/.well-known/agent-discovery.json)
- [Issue tracker](https://github.com/yonro/hermes-xmemo-plugin/issues)

## License

[MIT](LICENSE) © XMemo contributors.
