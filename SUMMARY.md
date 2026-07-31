# Sub-task 2: Identity Binding Gate (nickname → employee_id)

## What was implemented

Two new files and one modified file in `plugins/platforms/line/`:

### `plugins/platforms/line/identity.py` (new)

Contains two classes:

- **`IdentityResolver`** — reads the `identity_map` from admin.db's
  `pipeline_config` table (`component='channel_gw'`), exactly as
  channel_gw's `identity.py` does.  admin.db is opened **read-only**.
  New bindings are written through the Admin Panel `POST /api/bind`
  endpoint (the same write point channel_gw uses), never directly to
  admin.db.  Includes a 60-second in-memory cache with TTL.

- **`BindStateStore`** — lightweight in-memory store for the
  `awaiting_nickname` state, replacing channel_gw's Redis dependency.
  10-minute TTL matches channel_gw's `BIND_STATE_TTL`.

### `plugins/platforms/line/adapter.py` (modified)

- Added `from plugins.platforms.line.identity import IdentityResolver, BindStateStore`
- Initialized `self._identity` and `self._bind_state` in `__init__`
- Added identity binding gate in `_handle_message_event`: for `chat_type == "dm"`
  (1:1 user messages), calls `_handle_identity_gate()` **before** media download
  and **before** `handle_message()`.  If the gate returns `True` (message was
  handled by the binding flow), the method returns early — the message never
  reaches the agent/LLM.
- Added `_handle_identity_gate()` method — mirrors channel_gw's
  `_handle_line_message` identity flow (lines 1436–1484 of channel_gw/main.py):
  1. If bound → return `False` (proceed to agent)
  2. If `awaiting_nickname` + text → treat as nickname, call bind API, reply
  3. If not bound → set `awaiting_nickname` state, reply with prompt
- Added `_send_binding_prompt()` method — sends the binding prompt using the
  stashed reply token (free Reply API, not metered Push)

### `plugins/platforms/line/plugin.yaml` (modified)

- Added `ADMIN_DB_PATH` and `ADMIN_PANEL_URL` optional env vars

## admin.db tables / fields used

**Read-only** (via `sqlite3.connect`):

| Table | Field | Purpose |
|-------|-------|---------|
| `pipeline_config` | `config_json` (where `component='channel_gw'`) | Contains `identity_map` array: `[{"channel": "line", "channel_user_id": "U...", "employee_id": "E...", "note": "..."}]` |
| `users` | `employee_id`, `name` (where `active=1`) | Employee display names |

**Write** (via `POST {ADMIN_PANEL_URL}/api/bind`):

| Table | Field | Purpose |
|-------|-------|---------|
| `pipeline_config` | `config_json` (where `component='channel_gw'`) | Admin Panel `/api/bind` appends to `identity_map` array |

The bind API matches nickname against `users.nickname` (case-insensitive,
`active=1`), checks for `ALREADY_BOUND` / `EMPLOYEE_BOUND` conflicts, and
appends a new entry to `identity_map`.  This is the **same write point**
channel_gw uses — no new tables or schema changes.

## Why this design

1. **Mirror channel_gw exactly** — same SQL queries, same API endpoint,
   same error codes (`NOT_FOUND`, `ALREADY_BOUND`, `EMPLOYEE_BOUND`,
   `DUPLICATE_NICKNAME`), same user-facing messages.  This ensures
   behavioral parity when traffic is cut over from channel_gw to the
   native plugin.

2. **admin.db is read-only** — we only `SELECT` from it.  All writes go
   through the Admin Panel API, which handles the `UPDATE pipeline_config`
   transaction.  No risk of schema divergence or direct DB corruption.

3. **In-memory bind state** — no Redis dependency.  The `awaiting_nickname`
   state is ephemeral by design (10-min TTL), matching channel_gw.  If the
   process restarts, the user simply gets re-prompted — the identity_map
   in admin.db is the source of truth.

4. **Gate before media download** — unbound users' media is never
   downloaded, saving bandwidth and avoiding unnecessary API calls.

5. **Gate before `handle_message`** — unbound messages never reach the
   agent/LLM, satisfying the core requirement: "只有綁定成功的訊息才
   繼續往下讓 agent 處理".

## How to verify with the synthetic webhook script

From the hermes-agent container, send a webhook for an unbound LINE user_id:

```python
import hmac, hashlib, base64, json, urllib.request, time

secret = "<LINE_CHANNEL_SECRET>"
user_id = "<unbound-test-user-id>"  # not in any identity_map

# 1. First message — should get binding prompt
body = {
    "destination": "test-destination",
    "events": [{
        "type": "message",
        "webhookEventId": f"test-{int(time.time())}",
        "timestamp": int(time.time() * 1000),
        "source": {"type": "user", "userId": user_id},
        "replyToken": "dummy-not-real",
        "mode": "active",
        "message": {"id": "1", "type": "text", "text": "測試文字"},
    }],
}
raw = json.dumps(body).encode()
sig = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
req = urllib.request.Request(
    "http://127.0.0.1:8646/line/webhook", data=raw,
    headers={"Content-Type": "application/json", "X-Line-Signature": sig}, method="POST")
print(urllib.request.urlopen(req, timeout=10).read())
# Expected: 200 OK, and the user receives the binding prompt
# Verify: no session created in state.db for this user_id

# 2. Second message — input correct nickname
body2 = dict(body)
body2["events"] = [{
    "type": "message",
    "webhookEventId": f"test-{int(time.time())}-2",
    "timestamp": int(time.time() * 1000),
    "source": {"type": "user", "userId": user_id},
    "replyToken": "dummy-not-real-2",
    "mode": "active",
    "message": {"id": "2", "type": "text", "text": "<correct-nickname>"},
}]
raw2 = json.dumps(body2).encode()
sig2 = base64.b64encode(hmac.new(secret.encode(), raw2, hashlib.sha256).digest()).decode()
req2 = urllib.request.Request(
    "http://127.0.0.1:8646/line/webhook", data=raw2,
    headers={"Content-Type": "application/json", "X-Line-Signature": sig2}, method="POST")
print(urllib.request.urlopen(req2, timeout=10).read())
# Expected: 200 OK, and the user receives "綁定成功！"
# Verify: identity_map in admin.db now contains this user_id → employee_id

# 3. Third message — should now reach the agent
body3 = dict(body)
body3["events"] = [{
    "type": "message",
    "webhookEventId": f"test-{int(time.time())}-3",
    "timestamp": int(time.time() * 1000),
    "source": {"type": "user", "userId": user_id},
    "replyToken": "dummy-not-real-3",
    "mode": "active",
    "message": {"id": "3", "type": "text", "text": "你好"},
}]
raw3 = json.dumps(body3).encode()
sig3 = base64.b64encode(hmac.new(secret.encode(), raw3, hashlib.sha256).digest()).decode()
req3 = urllib.request.Request(
    "http://127.0.0.1:8646/line/webhook", data=raw3,
    headers={"Content-Type": "application/json", "X-Line-Signature": sig3}, method="POST")
print(urllib.request.urlopen(req3, timeout=10).read())
# Expected: 200 OK, and the agent processes the message (session created)

# Cleanup: delete the test session
# hermes sessions delete <session_id> --yes
```

Also test the NOT_FOUND path: send a wrong nickname and verify the user
gets re-prompted (state is NOT cleared, so they can try again).

---

# Sub-task 4: Soul/skill personalization hook

## What was built

A standalone module `plugins/platforms/line/personalization.py` that
injects employee soul (department persona) and skill (department/personal
skill settings) from `admin.db` into the hermes-agent conversation via a
`pre_llm_call` plugin hook.

## Files changed

| File | Change |
|------|--------|
| `plugins/platforms/line/personalization.py` | **New** — soul/skill loaders + `pre_llm_call` hook callback |
| `plugins/platforms/line/adapter.py` | **2 lines added** — import `pre_llm_call_hook` + `ctx.register_hook("pre_llm_call", ...)` in `register()` |
| `plugins/platforms/line/plugin.yaml` | **3 lines added** — `hooks: [pre_llm_call]` declaration |

## Design rationale

### Why a `pre_llm_call` hook (not a core edit)

The spec references `hermes-hooks/intention_router.py`'s hook pattern.
Hermes-agent's `pre_llm_call` plugin hook is the native equivalent: it
receives the conversation context, returns `{"context": "..."}`, and
hermes injects that into the user message. This is the **smallest
footprint** path — no core files touched, no config.yaml changes, no
shell-hook configuration. The hook is registered in the LINE plugin's
`register()` function and is automatically scoped to the LINE platform
(via a `platform == "line"` guard inside the callback).

### Why context goes into the user message, not the system prompt

Hermes-agent's hook system **always** injects `pre_llm_call` context into
the user message, never the system prompt. This is deliberate — it
preserves the prompt-cache prefix across turns (the system prompt stays
byte-identical), which is a hard requirement in this codebase. The
soul/skill content is functionally equivalent to a system prompt
addition: the agent sees it as part of the current turn's context.

### Why a module-level IdentityResolver singleton

The `IdentityResolver` caches the `identity_map` from `admin.db` with a
60-second TTL. Without a singleton, every `pre_llm_call` invocation would
create a fresh resolver (empty cache) and reload the identity_map from
admin.db — defeating the cache. The singleton ensures the cache is reused
across messages from the same employee.

### Safe degradation

Every failure path returns `None` (no context injected):
- Platform is not `"line"` → immediate return
- `sender_id` is empty → return
- Identity resolution fails (unbound user) → return
- `admin.db` not found → return
- Any DB error → `try/except` returns `""`, which means no parts → return

A missing admin.db or an unbound user never breaks the conversation.

### Caching

Two layers of caching, both with 60-second TTL:
1. **IdentityResolver singleton** — caches `identity_map` (channel_user_id → employee_id)
2. **SoulSkillCache** — caches `(soul, skill)` per employee_id

A burst of 10 messages from the same employee within 60 seconds results
in at most 1 admin.db query for the identity_map + 1 for soul/skill.

## admin.db tables & columns read

Mirrors channel_gw's `_load_soul` (main.py:414-443) and `_load_skill`
(main.py:466-503) **exactly**:

### `_load_soul`
- `users` — `employee_id`, `department_id`, `active` (lookup dept)
- `system_settings` — `key`, `value` (lookup `base_soul`)
- `departments` — `id`, `soul_overlay`, `active` (lookup dept overlay)

### `_load_skill`
- `users` — `employee_id`, `department_id`, `active` (lookup dept)
- `skills` — `content`, `scope`, `active`, `sort_order`, `id` (global + dept + user)
- `department_skills` — `skill_id`, `department_id` (dept skill join)
- `user_skills` — `skill_id`, `employee_id` (user skill join)

**Join order**: global → dept → user (same as channel_gw).
**Separator**: soul parts joined with `\n\n`; skill parts joined with `\n\n---\n\n`.

**admin.db is read-only** — `sqlite3.connect()` is used for reads only;
no `INSERT`/`UPDATE`/`DELETE` or schema changes are performed.

## How the hook fires

1. LINE webhook → `LineAdapter._handle_message_event()` → `handle_message()`
2. Gateway creates `AIAgent` with `user_id=ctx.source.user_id` (the LINE user_id)
3. Agent's conversation loop calls `pre_llm_call` hook (in `agent/turn_context.py:1050`)
4. `pre_llm_call_hook` receives `sender_id` (= `agent._user_id` = LINE user_id)
5. Hook resolves user_id → employee_id via `IdentityResolver`
6. Hook loads soul/skill from admin.db (cached)
7. Hook returns `{"context": "soul\n\nskill"}` → injected into user message

## Suggested verification

1. **Synthetic webhook test** (per PLAN.md §"怎麼測試"):
   ```python
   import hmac, hashlib, base64, json, urllib.request, time
   secret = "<LINE_CHANNEL_SECRET>"
   body = {
       "destination": "test-destination",
       "events": [{
           "type": "message",
           "webhookEventId": f"test-{int(time.time())}",
           "timestamp": int(time.time() * 1000),
           "source": {"type": "user", "userId": "<bound_LINE_user_id>"},
           "replyToken": "dummy-not-real",
           "mode": "active",
           "message": {"id": "1", "type": "text", "text": "你好"},
       }],
   }
   raw = json.dumps(body).encode()
   sig = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
   req = urllib.request.Request(
       "http://127.0.0.1:8646/line/webhook", data=raw,
       headers={"Content-Type": "application/json", "X-Line-Signature": sig}, method="POST")
   print(urllib.request.urlopen(req, timeout=10).read())
   ```

2. **Check agent.log** for the debug line:
   ```
   LINE personalization: injected soul/skill for employee=<eid> (soul=N chars, skill=N chars)
   ```

3. **Verify safe degradation**: send a webhook with an unbound `userId`
   — the agent should still respond normally (no crash, no soul/skill
   injection).

4. **Verify cache**: send 5 rapid messages from the same bound user —
   admin.db should be queried at most twice (identity_map + soul/skill),
   not 5 times.

5. **Clean up**: `hermes sessions delete <id> --yes` to remove test
   sessions from Honcho memory.
