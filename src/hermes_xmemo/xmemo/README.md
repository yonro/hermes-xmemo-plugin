# XMemo Memory Provider for Hermes

Native, user-owned long-term memory for Hermes Agent.

This directory is the provider bundle installed under
`$HERMES_HOME/plugins/xmemo/`. It participates directly in Hermes' memory
lifecycle and connects the active profile to [XMemo](https://xmemo.dev).

## Discovery

| Resource | URL |
|---|---|
| Agent discovery | `https://xmemo.dev/.well-known/agent-discovery.json` |
| Hermes configuration | `https://xmemo.dev/v1/mcp/config/hermes` |
| Hosted MCP fallback | `https://xmemo.dev/mcp` |
| Source repository | `https://github.com/yonro/hermes-xmemo-plugin` |

For Hermes, prefer this native provider. The hosted MCP endpoint is a portable
fallback for MCP-only clients and does not provide Hermes lifecycle hooks.

## Setup

Recommended:

```bash
npm install -g @xmemo/client
xmemo login
xmemo setup hermes
```

Direct PyPI installation:

```bash
pip install hermes-xmemo
hermes-xmemo install
hermes memory setup xmemo
```

The setup flow:

- installs the provider under `$HERMES_HOME/plugins/xmemo/`;
- sets `memory.provider` to `xmemo`;
- stores non-secret settings in `$HERMES_HOME/xmemo.json`;
- reuses the user-scoped credential from `xmemo login` when available;
- can sync the token to `$HERMES_HOME/.env` as `XMEMO_KEY` for compatibility.

Hosted MCP is not configured by default. Use
`xmemo setup hermes --with-mcp` only when that fallback is intentional.

## Lifecycle

1. **Before a turn** — prefetch a bounded context pack from memories visible to
   the current XMemo account.
2. **During a turn** — expose explicit search, recall, remember, and working
   state tools. Hermes built-in `memory` writes are mirrored while XMemo is the
   active provider.
3. **After a turn** — optionally capture high-signal timeline events.
4. **At session end** — capture a restart snapshot and schedule a final outbox
   sync.

Prefetch state is isolated per Hermes profile and session.

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
| `xmemo_list_reminders` | List reminders |
| `xmemo_complete_reminder` | Complete a reminder |

Set `"enable_destructive_tools": true` to expose `xmemo_forget`. It requires an
exact memory ID and is disabled by default.

## Configuration

Non-secret configuration lives in `$HERMES_HOME/xmemo.json`:

| Key | Default | Purpose |
|---|---|---|
| `agent_id` | `hermes` | Agent family identifier |
| `agent_instance_id` | generated | Stable, opaque installation identifier |
| `bucket` | `work` | Namespace for new Hermes-authored writes |
| `scope` | `hermes/default` | Scope for new Hermes-authored writes |
| `read_bucket` | `%` | Recall/search bucket filter |
| `read_scope` | unset | Recall/search scope filter |
| `timeout_seconds` | `5.0` | REST request timeout |
| `prefetch_max_items` | `5` | Maximum recalled items |
| `prefetch_max_tokens` | `900` | Maximum recalled context tokens |
| `enable_workflow_tools` | `false` | Expose reminder and event tools |
| `enable_destructive_tools` | `false` | Expose `xmemo_forget` |
| `capture_timeline` | `false` | Record high-signal turns |
| `enable_non_idempotent_replay` | `false` | Auto-replay queued non-idempotent writes |

Supported environment overrides:

```text
XMEMO_KEY
XMEMO_URL
XMEMO_AGENT_ID
XMEMO_AGENT_INSTANCE_ID
XMEMO_BUCKET
XMEMO_SCOPE
XMEMO_READ_BUCKET
XMEMO_READ_SCOPE
XMEMO_TIMEOUT_SECONDS
XMEMO_PREFETCH_MAX_ITEMS
XMEMO_PREFETCH_MAX_TOKENS
```

Legacy `MEMORY_OS_API_KEY`, `MEMORY_OS_MCP_TOKEN`, and `MEMORY_OS_URL`
variables remain accepted.

## Credentials

Credential priority is:

1. `XMEMO_KEY`, then supported legacy environment variables.
2. The user-scoped credential stored by `xmemo login`.

API keys are never read from or written to `xmemo.json`. Do not place tokens in
git-tracked files, logs, or shell history.

## Reliability

The provider stores its local reliability state in
`$HERMES_HOME/xmemo_cache.db`:

- successful recall/search results remain fresh for 5 minutes;
- marked stale results can be used for up to 24 hours during transient
  failures;
- transiently failed writes enter an outbox with stable idempotency keys;
- idempotent writes retry up to 5 times with exponential backoff;
- non-idempotent writes are held by default to prevent duplicates;
- sent records are retained for 24 hours, failed records for 7 days, and the
  failed queue is capped at 100.

The SQLite database stores cached responses and queued payloads as plain-text
JSON. It never stores credentials, but the Hermes home directory should still
be protected as private user data.

## Privacy boundaries

- The provider reads XMemo memories the user saved or authorized. It does not
  read another client's built-in private memory.
- New Hermes-authored writes use the configured Hermes bucket and scope.
- Timeline capture and destructive tools are disabled by default.
- A circuit breaker and bounded timeouts keep service failures from blocking
  the conversation.

## Disable

```bash
hermes config set memory.provider ""
```

Re-enable:

```bash
hermes config set memory.provider xmemo
```

Full project documentation:
<https://github.com/yonro/hermes-xmemo-plugin>
