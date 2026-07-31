# Subtask 6: Cron Push (F3 Daily Group Summary) — Implementation Summary

## Date
2026-07-31

## Branch
`feature/line-cron-delivery` (worktree: `/home/murray/hermes-agent-line-cron`)

---

## 1. Verification Conclusions (with evidence)

### 1.1 Native cron delivery mechanism

**How `cron_deliver_env_var` and `standalone_sender_fn` work:**

The LINE plugin registers both on its `PlatformEntry` at
`plugins/platforms/line/adapter.py:1715-1716`:

```python
cron_deliver_env_var="LINE_HOME_CHANNEL",
standalone_sender_fn=_standalone_send,
```

- **`cron_deliver_env_var`** (`cron/scheduler.py:981-997`): When a cron job
  has `deliver=line`, the scheduler calls `_plugin_cron_env_var("line")`
  which looks up the platform registry for the `cron_deliver_env_var` field.
  It returns `"LINE_HOME_CHANNEL"`. The scheduler then reads
  `os.getenv("LINE_HOME_CHANNEL")` to get the **single** target chat ID
  (`cron/scheduler.py:1026-1036`).

- **`standalone_sender_fn`** (`tools/send_message_tool.py:747-771`): When
  cron runs in a separate process from the gateway, there is no live
  in-process adapter. The scheduler falls back to the plugin's
  `standalone_sender_fn`, which is `_standalone_send` in
  `adapter.py:1615-1657`. This function creates an ephemeral `_LineClient`
  and calls `client.push(chat_id, messages)` — always to the **single**
  `chat_id` passed by the caller.

**Can it specify an arbitrary chat_id?**

Yes, but only via the `deliver=line:<chat_id>` syntax. The resolution path
(`cron/scheduler.py:1156-1224`) shows:

- `deliver=line` → reads `LINE_HOME_CHANNEL` env var (single target)
- `deliver=line:C16015a313fd45352dc63fdb63a1f45de` → resolves to the
  explicit chat_id `C16015a313fd45352dc63fdb63a1f45de`

So the native cron delivery **can** target an arbitrary chat_id, but only
**one** per cron job. There is no mechanism to fan out to multiple dynamic
targets from a single `deliver=` value.

### 1.2 line_ingestion tracked group list

**Where the group list is stored:**

The `line_ingestion` SQLite database at
`~/ai-data/line-harness/line_ingestion.sqlite3` has a
`raw_line_messages` table with a `group_id` column. The distinct group IDs
are the dynamically-tracked groups:

```sql
SELECT DISTINCT group_id FROM raw_line_messages;
```

Verified against the live DB — currently 7 groups:
`C16015a313fd45352dc63fdb63a1f45de`, `C4a20563c82e8a59f4c8ec179e1f3fe5f`,
`Cdf78243becd0d9e07906fa4c0d0a6451`, `Ce5a3a3ef45a113d55faa41480cc27b3a`,
`Ctest-phase-b-cutover`, `public-test-group`, `test-group`.

New groups appear automatically as soon as their first message is
ingested — no config change required.

**Existing deduplication:**

The `line_summary_pushes` table (`store.py:168-176`) tracks
`(source_ref, local_day, target_group_id)` with a primary key on
`(source_ref, local_day)`, so retries do not double-push.

### 1.3 Existing F3 daily summary generation

**Where it lives:**

`line_ingestion/summary_service.py` — `DailySummaryService.run_once()`
(line 230-264). The flow:

1. Iterates `self.config.group_targets` (a **static** dict from
   `LINE_SUMMARY_GROUP_TARGETS` env var — `config.py:92-94`).
2. For each group, calls `self.store.events_for_local_day()` to read
   extracted events from the `extracted_events` table
   (`extraction_store.py:188-236`).
3. Composes a Traditional-Chinese summary via `LiteLLMSummaryComposer`
   (`summary_service.py:64-108`) — calls LiteLLM's
   `/v1/chat/completions` endpoint.
4. Pushes via `LinePushClient.push_text()` (`summary_push.py:55-78`) —
   direct HTTP POST to `https://api.line.me/v2/bot/message/push`.
5. Records the push in `SummaryPushLog` for deduplication
   (`summary_service.py:155-208`).

**Key difference from the new module:** The existing service uses a
**static** `group_targets` config. The new module reads the group list
**dynamically** from the `raw_line_messages` table.

### 1.4 channel_gw `/internal/report` endpoint

`channel_gw/main.py:1791-1909` — accepts `employee_id`, `title`,
`content`, `format`, etc. Resolves the LINE user_id via
`identity.reverse_resolve("line", employee_id)` and pushes to that
user's 1:1 chat. This is for **personal** cron reports (from
`/定期任務`), not group summaries. When channel_gw is retired, this
endpoint will be gone.

The hermes-agent native cron `deliver=line` mechanism replaces this for
personal reports. For F3 group summaries, the new `cron_delivery.py`
module replaces the `line_ingestion/summary_service.py` standalone
process.

---

## 2. Design Rationale

### Why a standalone module?

The task requires:
- Dynamic multi-group fan-out (not a single home channel)
- 429 graceful degradation
- Failures must be discoverable

The native cron `deliver=line` mechanism only supports a single target
per cron job. While `deliver=line:<chat_id>` can target an arbitrary
group, it cannot fan out to all tracked groups dynamically.

**Solution:** A standalone module that:
1. Reads the group list dynamically from the DB
2. Generates summaries per group
3. Pushes to each group via LINE Push API
4. Is triggered by a hermes-agent cron job (no new scheduler)

### Why not extend `standalone_sender_fn`?

The `standalone_sender_fn` signature is
`async (pconfig, chat_id, message, ...) -> dict` — it delivers to a
**single** `chat_id`. Extending it to fan out to multiple groups would
break the contract used by all other platforms (Discord, Slack, etc.).
The standalone module approach keeps the change localized to the LINE
plugin.

### Why urllib instead of aiohttp?

The `line_ingestion/summary_push.py` already uses `urllib`. The
`_standalone_send` in `adapter.py` uses `aiohttp`, but that requires
the gateway to be running. The cron delivery module runs as a cron job
script — it should work even when the gateway is not in the same
process. Using `urllib` (stdlib) avoids the `aiohttp` dependency.

### Architecture

```
hermes-agent cron job (deliver=local, script=run_f3_daily_summary)
        │
        ▼
cron_delivery.py::run_f3_daily_summary()
        │
        ├── get_tracked_groups(db_path)           ← raw_line_messages
        │
        ├── For each group:
        │   ├── events_for_local_day(db_path)      ← extracted_events
        │   ├── LiteLLMSummaryComposer.compose()   ← LiteLLM /v1/chat/completions
        │   ├── LinePushClient.push_text()         ← LINE Push API
        │   └── SummaryPushLog.record_push()       ← line_summary_pushes (dedup)
        │
        └── SummaryRunResult (logged + returned)
```

### Files created

| File | Purpose |
|------|---------|
| `plugins/platforms/line/cron_delivery.py` | Main module: group tracking, summary generation, LINE push, 429 handling |
| `tests/plugins/platforms/line/test_cron_delivery.py` | 47 pytest tests covering all functions |

### Files NOT modified

- `plugins/platforms/line/adapter.py` — no changes needed. The existing
  `standalone_sender_fn` and `cron_deliver_env_var` remain for personal
  cron reports (`deliver=line`). The new module is a separate path for
  F3 daily group summaries.
- `cron/scheduler.py` — no changes. The module is triggered via a cron
  job's `script` field.
- `tools/send_message_tool.py` — no changes.

---

## 3. How Failures Are Discovered

### 3.1 429 (quota exhausted)

When the LINE Push API returns 429:
- A `LinePushError` is raised with `status_code=429` and the
  `Retry-After` header value.
- The group is **skipped** (not retried in this run).
- A warning is logged: `"Group <id>: LINE push quota exhausted (429) —
  skipping. Retry-After: <seconds>"`.
- The error is added to `SummaryRunResult.errors`.
- The push is **not** recorded in `line_summary_pushes` (so the next
  cron run will retry).
- The cron job output includes the error in its printed summary.

### 3.2 Push failure (non-429)

When the LINE Push API returns any other error (500, 401, etc.):
- A `LinePushError` is raised.
- The group is counted as `failed`.
- An error is logged with `exc_info=True`.
- The error is added to `SummaryRunResult.errors`.
- The push is **not** recorded (retry on next run).
- The cron job output includes the error.

### 3.3 Summary composition failure

When LiteLLM fails to generate a summary:
- The exception is caught and logged with `exc_info=True`.
- The group is counted as `failed`.
- The error is added to `SummaryRunResult.errors`.
- No push is attempted.

### 3.4 No tracked groups

When the DB doesn't exist or has no groups:
- A warning is logged.
- An error is added to `SummaryRunResult.errors`:
  `"No tracked groups found in line_ingestion DB"`.
- The cron job output shows `checked=0`.

### 3.5 Cron job output

The `cron_script_main()` function prints a human-readable summary:

```
F3 daily summary: checked=7 pushed=5 skipped_empty=1 skipped_already=1 failed=0 skipped_429=0
```

If there are errors, they are listed below. This output is captured by
hermes-agent's cron job execution and stored in `last_output`, visible
via `hermes cron list` and `hermes cron show <id>`.

### 3.6 Logging

All results are logged via the `hermes.line_cron_delivery` logger at
INFO level (success) or WARNING/ERROR level (failures). In a production
deployment, these logs are captured by hermes-agent's logging system
(`~/.hermes/logs/agent.log`).

---

## 4. How to Use

### 4.1 Create a cron job

```bash
hermes cron add \
  --name "F3 daily group summary" \
  --schedule "0 10 * * *" \
  --script "python -m plugins.platforms.line.cron_delivery" \
  --deliver local
```

Or via the cronjob tool:

```
/cronjob create name="F3 daily group summary" schedule="0 10 * * *" script="python -m plugins.platforms.line.cron_delivery" deliver="local"
```

### 4.2 Environment variables

The `cron_script_main()` function reads:

| Variable | Default | Description |
|----------|---------|-------------|
| `LINE_INGESTION_DB` | `data/line_ingestion.sqlite3` | Path to line_ingestion SQLite DB |
| `LINE_CHANNEL_ACCESS_TOKEN` | (required) | LINE Push API token |
| `LINE_SUMMARY_LITELLM_BASE_URL` | `http://localhost:4000` | LiteLLM base URL |
| `LINE_SUMMARY_LITELLM_MODEL` | `qwen36-27b` | LiteLLM model for summary |
| `LINE_SUMMARY_LITELLM_API_KEY` | (empty) | Optional LiteLLM API key |
| `LINE_SUMMARY_SKIP_EMPTY_DAY` | `true` | Skip groups with no events |
| `LINE_SUMMARY_DISCLOSURE_TEXT` | `本訊息由 AI 助理自動產生。` | Footer text |

### 4.3 Programmatic use

```python
from plugins.platforms.line.cron_delivery import run_f3_daily_summary

result = run_f3_daily_summary(
    db_path=Path("/path/to/line_ingestion.sqlite3"),
    line_channel_access_token="your-token",
    litellm_base_url="http://localhost:4000",
    litellm_model="qwen36-27b",
)
print(result)
```

---

## 5. Suggested Validation

### 5.1 Unit tests (already passing)

```bash
/home/murray/.venvs/memory-hub/bin/python -m pytest \
  tests/plugins/platforms/line/test_cron_delivery.py -v
```

47 tests covering: group tracking, push log, event extraction, LINE push
client (success/429/500), message builders, summary composition, and the
main `run_f3_daily_summary` entry point.

### 5.2 Integration test (requires real services)

1. Start LiteLLM on port 4000.
2. Set `LINE_CHANNEL_ACCESS_TOKEN` to a real token.
3. Set `LINE_INGESTION_DB` to the real DB path.
4. Run:
   ```bash
   LINE_CHANNEL_ACCESS_TOKEN=... \
   LINE_INGESTION_DB=~/ai-data/line-harness/line_ingestion.sqlite3 \
   LINE_SUMMARY_LITELLM_BASE_URL=http://localhost:4000 \
   LINE_SUMMARY_LITELLM_MODEL=qwen36-27b \
   python -m plugins.platforms.line.cron_delivery
   ```
5. Verify:
   - Each tracked group receives a summary in LINE.
   - The `line_summary_pushes` table has new entries.
   - Re-running skips already-pushed groups.

### 5.3 Cron job integration test

1. Create a cron job with the script.
2. Wait for the scheduled time.
3. Check `hermes cron show <id>` for the output.
4. Verify LINE groups received the summary.

### 5.4 What requires real hardware

- **LINE Push API calls** — requires a real `LINE_CHANNEL_ACCESS_TOKEN`
  and a real LINE channel. Cannot be tested without real credentials.
- **LiteLLM summary composition** — requires a running LiteLLM instance
  with a working model. The unit tests mock this.
- **Real group data** — the unit tests use a temp DB with sample data.
  Integration tests should use the real `~/ai-data/line-harness/`
  DB.

### 5.5 What cannot be verified without real infrastructure

- Whether the summary content quality meets expectations (requires
  human review of actual LINE messages).
- Whether the LINE Push API rate limits are hit in production (requires
  real usage over time).
- Whether the `Retry-After` header is correctly parsed by the LINE API
  (requires a real 429 response).

---

## 6. Key Design Decisions

1. **No new scheduler** — Uses hermes-agent's native cron system. The
   module is triggered via a cron job's `script` field.

2. **Dynamic group list** — Reads from `raw_line_messages` table at
   run time. New groups are picked up automatically.

3. **Idempotent** — Uses the existing `line_summary_pushes` table for
   deduplication. Retries do not double-push.

4. **429 graceful degradation** — 429 errors are caught, logged, and
   the group is skipped. No crash, no retry loop. The push is not
   recorded, so the next cron run will retry.

5. **No heavy dependencies** — Only stdlib (sqlite3, urllib, json,
   logging, dataclasses). No aiohttp or line-bot-sdk.

6. **No changes to adapter.py** — The existing `standalone_sender_fn`
   and `cron_deliver_env_var` remain for personal cron reports. The new
   module is a separate path for F3 daily group summaries.

7. **No changes to cron/scheduler.py** — The module is triggered via a
   cron job's `script` field, not via the `deliver=` mechanism.

8. **Failure visibility** — Every result is logged and returned in
   `SummaryRunResult`. The cron job output includes a summary and
   error list.
