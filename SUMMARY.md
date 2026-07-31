# LINE 群組轉發與 F4 @mention 閘門

## 改動內容

- `plugins/platforms/line/adapter.py` 現在會在任何 allowlist 判斷之前，將已通過
  LINE signature 驗證的 group/room 原始事件排入背景 task，轉發至
  `LINE_INGESTION_FORWARD_URL`。
- 轉發沿用 `channel_gw/main.py` 的契約：payload 是 `events` 陣列，原 webhook
  有 `destination` 時一併保留，認證 header 是
  `X-Internal-Secret: $LINE_INGESTION_INTERNAL_SECRET`。timeout 沿用
  `LINE_INGESTION_FORWARD_TIMEOUT`（預設 5 秒）。
- group/room 不再由一般 `LINE_ALLOWED_GROUPS`／`LINE_ALLOWED_ROOMS` 決定是否
  觸發 agent。只有文字訊息同時符合下列條件才呼叫 `handle_message()`：

  1. chat id 位於逗號分隔的 `LINE_GROUP_QA_ALLOWLIST`；
  2. `message.mention.mentionees` 內有 `isSelf: true`，或 mentionee `userId`
     符合 bot id（連線時取得的 id，`LINE_BOT_USER_ID` 是 fallback）。
- 送入 agent 前依 LINE mention 的 `index`／`length` 移除 bot mention。這是在
  event 的複本上完成，line_ingestion 仍收到未修改的原始文字。
- 群組訊息由 Hermes 原生 `line:group:<chat id>:...` session namespace 處理，與
  `line:dm:<user id>` 的 1:1 session key 物理分離。

## 設計原因

轉發與回答是兩條獨立路徑：每日摘要／事件擷取不能因助理 allowlist 而漏資料，
但 agent 也不能對群組每句話都回覆。轉發 task 使用 adapter 既有的 background
task 集合追蹤，因此 webhook 可以快速回 200，gateway shutdown 時也能統一取消。
空的 `LINE_GROUP_QA_ALLOWLIST` 採 fail-closed，不會讓任何群組觸發 agent。

## 驗證結果

- 新增 7 個單元測試，覆蓋 group/room 轉發、allowlist 前轉發、非 mention 不觸發、
  allowlist 外 mention 不觸發、mention 清理、保留原始 event，以及 payload/header
  契約；全部通過。
- `python3 -m py_compile`：通過。
- `ruff check plugins/platforms/line/adapter.py tests/gateway/test_line_plugin.py`：通過。
- 使用可取得但不完整的 pytest venv 執行整個 `test_line_plugin.py`：37 passed，
  唯一失敗是既有 dual-stack async 測試，原因是該 venv 沒有 `pytest-asyncio`；
  repo 的 canonical runner 也確認本 worktree 沒有含 pytest 的開發 venv。

## 建議的合成 webhook 驗證

先在待驗證的 Hermes runtime 設定以下環境變數，再依 `PLAN.md` 的 HMAC 腳本從
容器內送到 `127.0.0.1:8646/line/webhook`（不要切 nginx）：

```text
LINE_INGESTION_FORWARD_URL=http://<line-ingestion>/internal/line-events
LINE_INGESTION_INTERNAL_SECRET=<既有 internal secret>
LINE_GROUP_QA_ALLOWLIST=<測試 groupId>
```

連續送兩個具有不同 `webhookEventId`／`message.id` 的事件，source 都使用同一個
allowlisted groupId：

1. 純文字：`message = {"type": "text", "text": "forward only", ...}`。
2. mention：文字例如 `@小助理 請回答測試題`，並加入
   `"mention": {"mentionees": [{"index": 0, "length": 4, "isSelf": true}]}`；
   `length` 要與合成文字中 mention token 的實際長度一致。

驗收方式：

- 在 `LINE_INGESTION_DB`（預設相對 line_ingestion 工作目錄的
  `data/line_ingestion.sqlite3`）查 `raw_line_messages`，兩個不同 message id
  都應存在，而且 mention 那筆保留原始 `@小助理` 文字。
- 比較合成請求前後的 `hermes sessions list`／`state.db`：純文字不應新增 LINE
  group session；mention 才應新增 session 並嘗試回覆。dummy reply token 造成的
  Reply API 失敗屬預期，可能接著撞到已用罄的 push 配額 429，不代表 agent 未觸發。
- 驗證後用 `hermes sessions delete <id> --yes` 清除合成 session，避免污染真實
  transcript／Honcho 記憶。

本變更沒有修改主 repo、channel_gw、容器、nginx 或任何遠端狀態。
