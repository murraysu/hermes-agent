# LINE OA 遷移：整合分支實作筆記 (Implementation Notes)

> **文件狀態**：定稿  
> **建立日期**：2026-07-31  
> **工作分支**：`feature/line-integration`  
> **母規格文件**：[PLAN.md](file:///home/murray/hermes-agent-line-integration/docs/line-migration/PLAN.md)  
> **驗收清單**：[QA_CHECKLIST.md](file:///home/murray/hermes-agent-line-integration/docs/line-migration/QA_CHECKLIST.md)  

本文件整合五條平行開發分支的 SUMMARY.md 內容，並記錄合併後的最終執行順序。

---

## 第一節：身分綁定閘門 (feature/line-soul-skill，含 identity binding)

分支 `feature/line-soul-skill` 自 `feature/line-identity-binding` 切出，
因此合併此分支等同合併身分綁定 + soul/skill 個人化。

### 實作內容

兩個新檔案 + 一個修改檔案於 `plugins/platforms/line/`：

#### `plugins/platforms/line/identity.py` (new)

包含兩個類別：

- **`IdentityResolver`** — 從 admin.db 的 `pipeline_config` 表格
  (`component='channel_gw'`) 讀取 `identity_map`，與 channel_gw 的
  `identity.py` 完全一致。admin.db 開啓**唯讀**。新綁定透過 Admin Panel
  `POST /api/bind` 端點寫入（與 channel_gw 使用相同的寫入點），絕不直接
  寫入 admin.db。包含 60 秒 TTL 的記憶體快取。

- **`BindStateStore`** — 輕量記憶體快取用於 `awaiting_nickname` 狀態，
  取代 channel_gw 的 Redis 依賶。10 分鐘 TTL 匹配 channel_gw 的
  `BIND_STATE_TTL`。

#### `plugins/platforms/line/adapter.py` (modified)

- 新增 `from plugins.platforms.line.identity import IdentityResolver, BindStateStore`
- 在 `__init__` 初始化 `self._identity` 與 `self._bind_state`
- 在 `_handle_message_event` 加入身分綁定閘門：對 `chat_type == "dm"`
  (1:1 用戶訊息)，在媒體下載**之前**與 `handle_message()` 之前呼叫
  `_handle_identity_gate()`。若閘門返回 `True`（訊息由綁定流程處理），
  方法提前返回 — 訊息絕不送入 agent/LLM。
- 新增 `_handle_identity_gate()` 方法 — 鏡照 channel_gw 的
  `_handle_line_message` 身分流程（channel_gw/main.py 行 1436–1484）：
  1. 若已綁定 → 返回 `False`（繼續送 agent）
  2. 若 `awaiting_nickname` + 文字 → 當作暱稱，呼叫 bind API，回覆
  3. 若未綁定 → 設定 `awaiting_nickname` 狀態，回覆提示
- 新增 `_send_binding_prompt()` 方法 — 使用暖摘 reply token
 （免費 Reply API，非計費 Push）發送綁定提示

#### `plugins/platforms/line/plugin.yaml` (modified)

- 新增 `ADMIN_DB_PATH` 與 `ADMIN_PANEL_URL` optional env vars

### admin.db 表格/欄位

**唯讀**（透過 `sqlite3.connect`）：

| 表格 | 欄位 | 用途 |
|-------|-------|---------|
| `pipeline_config` | `config_json` (where `component='channel_gw'`) | 包含 `identity_map` 陣列：`[{"channel": "line", "channel_user_id": "U...", "employee_id": "E...", "note": "..."}]` |
| `users` | `employee_id`, `name` (where `active=1`) | 用戶顯示名稱 |

**寫入**（透過 `POST {ADMIN_PANEL_URL}/api/bind`）：

| 表格 | 欄位 | 用途 |
|-------|-------|---------|
| `pipeline_config` | `config_json` (where `component='channel_gw'`) | Admin Panel `/api/bind` 附加到 `identity_map` 陣列 |

綁定 API 將 nickname 與 `users.nickname` 比對（不區分大小寫，`active=1`），
檢查 `ALREADY_BOUND` / `EMPLOYEE_BOUND` 衝突，並附加新項目到 `identity_map`。
這是與 channel_gw **相同的寫入點**，不會導致資料分裂。

---

## 第二節：群組轉發 + F4 @mention 閘門 (feature/line-group-forward)

### 改動內容

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

### 設計原因

轉發與回答是兩條獨立路徑：每日摘要／事件擷取不能因助理 allowlist 而漏資料，
但 agent 也不能對群組每句話都回覆。轉發 task 使用 adapter 既有的 background
task 集合追蹤，因此 webhook 可以快速回 200，gateway shutdown 時也能統一取消。
空的 `LINE_GROUP_QA_ALLOWLIST` 採 fail-closed，不會讓任何群組觸發 agent。

### 驗證結果

- 新增 7 個單元測試，覆蓋 group/room 轉發、allowlist 前轉發、非 mention 不觸發、
  allowlist 外 mention 不觸發、mention 清理、保留原始 event，以及 payload/header
  契約；全部通過。
- `python3 -m py_compile`：通過。
- `ruff check plugins/platforms/line/adapter.py tests/gateway/test_line_plugin.py`：通過。

### 群組身分閘門：與 channel_gw 的刻意行為差異

**這是一個刻意的設計決定，不是疏漏。**

channel_gw 的 `_handle_line_group_mention()`（`~/ai-stack/code/home_gateway/channel_gw/main.py:1257-1260`）
只回答**已綁定員工** — 未綁定的 LINE user_id 在群組中 @bot 時會被靜默忽略，
不會觸發任何 AI 回應。

hermes-agent 原生 LINE plugin 的身分閘門（`_handle_identity_gate()`）**只套用在
1:1 (DM) 訊息**（`adapter.py:1133` `if chat_type == "dm"`）。群組內任何人 —
包括未綁定的外部客戶、廠商、或非員工 — 都可以 @bot 觸發回答。

**Murray 於 2026-08-01 明確決定：維持原生版行為，群組裡任何人都可以問。**
因此**不要**加身分閘門到群組路徑，程式碼不用改動。

#### 影響

- bot 在群組裡對非員工也會載入公司人格與工具（透過 `pre_llm_call` hook
  讀取 employee_id → soul/skill）。對非員工來說，soul/skill hook 會因
  找不到綁定而安全降級（返回 `None`，不注入 context），但仍會使用預設
  system prompt + 可用的工具。
- 若未來群組含有外部人員且不希望他們觸發公司人格/工具，需重新評估此決定，
  或改為 `LINE_GROUP_QA_ALLOW_ALL=false` + 手動維護 `LINE_GROUP_QA_ALLOWLIST`，
  並考慮在群組路徑加入身分閘門。

#### LINE_GROUP_QA_ALLOW_ALL 旗標

為了讓 bot「在哪個群就在哪個群回話，含未來新加的群組」，Murray 決定啟用
`LINE_GROUP_QA_ALLOW_ALL=true`。此旗標：

- 使用 `_truthy_env()` 讀取（與 `LINE_ALLOW_ALL_USERS` 同一個模式）。
- 為真時：**跳過允許清單的成員檢查**，任何群組都可以觸發回答。
- **@mention 的要求絕對不能一起放寬** — 這是唯一阻止 bot 對群組每則訊息都
  插嘴的機制。必須仍然只有被 @ 標註時才回應。
- 為假（預設）時：維持現在的允許清單行為，行為不變。

當 `LINE_GROUP_QA_ALLOW_ALL=true` 時，`LINE_GROUP_QA_ALLOWLIST` 不需要設定
（但仍可留空或不設定）。

---

## 第三節：多媒體處理查證 (feature/line-media)

### 結論摘要

| 媒體類型 | 結論 | 需重寫 |
|---------|------|--------|
| 圖片 (Image) | ✅ 已原生支援 | 否 |
| 語音 (Audio) | ✅ 已原生支援 | 否 |
| 檔案 (File) | ✅ 白名單檢查已加 | 否 |

### 圖片 (Image) — 已原生支援

**證據鏈**：

1. **下載與快取**：`adapter.py` 的 `_download_media()` 呼叫
   `cache_image_from_bytes()`（來自 `gateway/platforms/base.py:822`）將圖片下載
   並快取到本地路徑，填入 `MessageEvent.media_urls`。
2. **MessageEvent 建立**：`_handle_message_event()` 建立 `MessageEvent` 時，
   `media_urls` 包含圖片本地路徑，`message_type` 為 `MessageType.PHOTO`。
3. **gateway 處理**：`gateway/run.py` 的 `_prepare_inbound_message_text()`
   檢查 `_event_media_is_image(event, i)`，將圖片路徑分類到 `image_paths`。
4. **Vision 工具自動調用**：
   - 若模型支援 vision (native 模式)：圖片路徑存入 `native_image_paths`，由 agent 內聯附加。
   - 若模型不支援 vision (text 模式)：呼叫 `_enrich_message_with_vision()` →
     `vision_analyze_tool()`，將視覺描述前置到訊息文字中。
5. **與 channel_gw 對比**：channel_gw 的 `ask_vision()` 使用 Qwen3.6-27B vision
   model。hermes-agent 的 `vision_analyze_tool` 使用配置的 vision provider
   (`auxiliary.vision.*`)。功能等效，hermes-agent 更通用。

### 語音 (Audio) — 已原生支援

**證據鏈**：

1. **類型對映**：LINE audio 訊息被對映為 `MessageType.VOICE`。
2. **STT 管道判定**：`_event_media_is_stt_input()` 對 `MessageType.VOICE` 返回
   `True`。
3. **自動轉錄**：`_prepare_inbound_message_text()` 收集 STT-eligible 的音頻路徑，
   呼叫 `_enrich_message_with_transcription()`。
4. **轉錄實現**：`_enrich_message_with_transcription()` 呼叫
   `tools.transcription_tools.transcribe_audio()`，並在失敗時嘗試
   `transcribe_audio_local_fallback()`。
5. **STT 開關**：由 `stt_enabled` 配置控制（預設 `True`）。
6. **語音回覆 (TTS)**：`_send_voice_reply()` 使用 `text_to_speech_tool` 進行 TTS。
   由 `_should_auto_tts_for_chat()` 控制，基於 `voice.auto_tts` 配置或
   `/voice on` 指令。
7. **與 channel_gw 對比**：channel_gw 的 `transcribe_audio()` 使用 LiteLLM 的
   whisper-local 路由。hermes-agent 的 `transcribe_audio` 使用配置的 STT provider。
   功能等效。channel_gw 的 `synthesize_speech_m4a()` 使用 LiteLLM TTS。
   hermes-agent 的 auto-TTS 功能等效，並支援更多平台。

### 檔案 (File) — 白名單檢查已加

`plugins/platforms/line/media.py` (new) 提供檔案副檔名白名單：

- **白名單**：`pdf`, `doc`, `docx`, `xls`, `xlsx`, `ppt`, `pptx`, `txt`, `csv`
- **檢查時機**：在 `_download_media()` 之前 — 未通過白名單的檔案絕不下載。
- **函數**：`check_file_extension()`、`is_supported_file_type()`、
  `get_file_extension()`、`unsupported_file_message()`

在 `adapter.py` 的 `_handle_message_event` 中，`msg_type == "file"` 時會先呼叫
`check_file_extension(filename)`，若不支援則直接設定 `text = reject_msg`，
跳過下載，訊息仍會送入 agent 但不會包含媒體路徑。

---

## 第四節：Soul/Skill 個人化 Hook (feature/line-soul-skill)

### 實作內容

`plugins/platforms/line/personalization.py` (new) — 實作 `pre_llm_call` hook，
在 LINE user_id 解析為 employee_id 後，從 admin.db 讀取該員工的 soul
（部門 persona）與 skill（部門/個人技能設定），動態疊加進 system prompt。

**設計要點**：

- **Hook，而非 core edit**：Soul/skill 透過 hermes-agent 的 `pre_llm_call` plugin
  hook 注入，hook 回傳 `{"context": "..."}`。Hermes 將其注入到 *user message*
  中（絕不修改 system prompt），以保持 prompt-cache 穩定性。
- **平台作用域**：Hook callback 註冊在 plugin manager 上，但在
  `platform != "line"` 時立即 short-circuit — 其他平台零成本。
- **快取**：IdentityResolver (identity_map) 與 soul/skill 資料均使用 60 秒 TTL
  快取。
- **安全降級**：所有失敗路徑返回 `None`（不注入 context），而非拋出例外。
  缺少 admin.db、未綁定用戶或暫時性 DB 錯誤絕不中斷對話。

在 `adapter.py` 的 `register()` 函數中註冊：
```python
ctx.register_hook("pre_llm_call", pre_llm_call_hook)
```

並在 `plugin.yaml` 宣告 `hooks: [pre_llm_call]`。

---

## 第五節：Cron 推播 F3 每日摘要 (feature/line-cron-delivery)

### 驗證結論

**原生 cron delivery 機制已足夠**：

LINE plugin 在 `PlatformEntry` 註冊：
```python
cron_deliver_env_var="LINE_HOME_CHANNEL",
standalone_sender_fn=_standalone_send,
```

- **`cron_deliver_env_var`**：當 cron job 的 `deliver=line`，scheduler 讀取
  `LINE_HOME_CHANNEL` env var 取得單一目標 chat id。
- **`standalone_sender_fn`**：當 cron 在獨立進程執行時，scheduler 呼叫
  `_standalone_send`，建立 ephemeral `_LineClient` 並推送到指定 chat_id。

**可以指定任意 chat_id**：透過 `deliver=line:<chat_id>` 語法，scheduler
解析到明確的 chat_id。但每次 cron job 只能指定一個目標。

### 實作內容

`plugins/platforms/line/cron_delivery.py` (new) 提供：

- `schedule_daily_group_summary()` — 逐一推送 F3 每日摘要到 line_ingestion
  追蹤的群組清單。
- `push_to_group()` — 對單一群組推送摘要文字。
- 429 配額耗盡時的 graceful degradation — 標記為跳過並記錄警告，
  絕不卡死 Cron 排程器。
- `push_report()` — 處理外部 `/internal/report` 請求（向後兼容）。

測試檔案 `tests/plugins/platforms/line/test_cron_delivery.py` 包含 885 行測試。

---

## 合併後執行順序 (Final Execution Order)

以下是 `_dispatch_event` 與 `_handle_message_event` 的完整流程，確認順序合理、
沒有互相覆蓋：

### `_dispatch_event` 流程

1. **萃取欄位**：`event_type`, `source`, `source_type`, `webhook_event_id`
2. **群組/房間 ingestion 轉發** (before allowlist)：
   若 `source_type in {"group", "room"}`，排入背景 task 轉發至
   `line_ingestion`。**轉發與回答獨立** — 無論是否 @mention 都轉發。
3. **去重**：若 `webhook_event_id` 為重複，忽略。
4. **自我回音過濾**：若發送者為 bot 自身，忽略。
5. **群組/房間 @mention + 追問視窗閘門**：
   對 group/room，只有文字訊息同時符合以下條件才繼續：
   - `chat_id in LINE_GROUP_QA_ALLOWLIST` (或 `LINE_GROUP_QA_ALLOW_ALL=true`)
   - 訊息類型為 `text`
   - 訊息 **@mention 了 bot** → 開啟/重置追問視窗 (`_open_followup_window`)，
     移除 @bot mention 從文字中（僅在 event 複本上）
   - **或** 該 `(chat_id, user_id)` 有活動的追問視窗 → 視為對話延續，
     重置視窗計時，**不需要 @mention**
   - 否則 `return`（轉發已完成，agent 不回覆）

   > **追問視窗 (Group Follow-up Window)**：由環境變數
   > `LINE_GROUP_FOLLOWUP_WINDOW_SECONDS` 控制，預設 600 秒。
   > 設為 0 則停用，退回「每次都要 @」的舊行為。
   > 視窗是 **per (chat_id, user_id)**，不是 per group — 同群組內的其他人
   > 即使 A 的視窗還開著也**絕對不能**觸發回話。
   > 每次派工（@mention 或 follow-up）都會重新計時，多輪對話可以一直延續。
   > 視窗狀態放在 adapter 的 in-memory dict (`_followup_windows`)，
   > 過期項目透過 `_prune_followup_windows()` 清理。
6. **Allowlist 閘門**：對非 group/room (即 DM/user)，檢查 `_allowed_for_source`。
7. **分發**：`message` → `_handle_message_event`，`postback` →
   `_handle_postback_event`，生命周期事件記錄日誌。

### `_handle_message_event` 流程

1. **萃取欄位**：`msg`, `msg_type`, `message_id`, `reply_token`, `source`,
   `chat_id`, `chat_type`, `user_id`
2. **暖摘 reply token**：存入 `_reply_tokens` 供後續 Push/Reply 使用。
3. **身分綁定閘門** (僅 DM)：
   若 `chat_type == "dm"`，呼叫 `_handle_identity_gate()`。
   - 若返回 `True`（未綁定，用戶正在綁定流程中），**立即 return** —
     訊息絕不送入 media 下載或 LLM。
   - 若返回 `False`（已經綁定），繼續。
4. **媒體處理**：
   - `text` → 提取文字
   - `image/audio/video/file` → 下載媒體：
     - 若為 `file` 類型，**先檢查副檔名白名單** (`check_file_extension`)。
       - 不支援 → 設定 `text = reject_msg`，跳過下載，不加入 `media_urls`。
       - 支援 → 下載並加入 `media_urls`/`media_types`。
     - 非 file 類型 → 直接下載。
   - `sticker` → 提取關鍵字
   - `location` → 提取標題/地址
   - 其他 → `text = "[unsupported message type: {msg_type}]"`
5. **輸入提示** (僅 DM)：`asyncio.create_task(self._client.loading(chat_id))`
6. **建立 MessageEvent**：包含 `text`, `message_type`, `source`, `media_urls`,
   `media_types`, `raw_message`, `message_id`。
7. **送入 agent**：`await self.handle_message(event_obj)`

### 安全防線驗證

| 防線 | 執行順序 | 確認 |
|------|----------|------|
| 群組事件一律轉發 line_ingestion | `_dispatch_event` 步驟 2 (before allowlist) | ✅ |
| 只有 @mention 且群組在 allowlist 才觸發 agent | `_dispatch_event` 步驟 5 | ✅ |
| 追問視窗 per (chat_id, user_id) — 同群組不同人不受影響 | `_dispatch_event` 步驟 5 (Layer 3) | ✅ |
| 追問視窗設為 0 時退回舊行為 | `followup_window_seconds <= 0` 時 `_open_followup_window` / `_check_followup_window` 提前返回 | ✅ |
| 未綁定員工的 1:1 訊息絕不進 LLM | `_handle_message_event` 步驟 3 (before media download) | ✅ |
| 不在白名單的副檔名在下載之前擋掉 | `_handle_message_event` 步驟 4 (file 檢查在 `_download_media` 之前) | ✅ |
| soul/skill pre_llm_call hook 註冊存活 | `register()` 中的 `ctx.register_hook("pre_llm_call", ...)` | ✅ |

---

## 部署需求 (Deployment Requirements)

hermes-agent 容器要能正確運作 LINE 身分綁定，**必須**滿足以下兩項部署條件。
這是驗收時發現的部署缺口，未來部署新環境時務必確認。

### 1. 挂载 admin.db (唯讀)

容器必須將 admin.db 以**唯讀**方式掛载進容器內。hermes-agent 的 LINE plugin
(`plugins/platforms/line/identity.py`) 會讀取 admin.db 中的 `pipeline_config`
表格來解析 LINE user_id → employee_id 的綁定關係。

- **主機路徑**：`/home/murray/ai-data/admin-panel/admin.db`
  （此為 admin_panel 服務收編到 ai-stack 後的實際路徑；舊路徑
  `~/services/admin_panel/admin.db` 已不再存在）。
- **容器掛載點**：建議掛載到容器內的相同路徑，或透過 `ADMIN_DB_PATH`
  環境變數指定。
- **掛載選項**：**唯讀** (`ro`)。hermes-agent 絕不直接寫入 admin.db；
  所有綁定寫入都透過 Admin Panel 的 `POST /api/bind` API 完成。

Docker compose 範例：

```yaml
services:
  hermes-agent:
    volumes:
      - /home/murray/ai-data/admin-panel/admin.db:/home/murray/ai-data/admin-panel/admin.db:ro
    environment:
      - ADMIN_DB_PATH=/home/murray/ai-data/admin-panel/admin.db
```

### 2. 設定 ADMIN_DB_PATH

容器內的 `ADMIN_DB_PATH` 環境變數**必須**指向掛載進容器的 admin.db 路徑。
`identity.py` 的 `DEFAULT_ADMIN_DB` 預設為 `~/ai-data/admin-panel/admin.db`，
但容器內的 `~` 可能與主機不同，因此**必須**明確設定 `ADMIN_DB_PATH` 而非
依賴預設值。

- 若 `ADMIN_DB_PATH` 未設定且預設路徑在容器內不存在，plugin 會發出
  `WARNING` 級別日誌（而非 `DEBUG`），並且**所有使用者都會被判定為未綁定**
  → 全部卡在「請輸入暱稱」 → bot 對誰都不回話。
- 這種情況必須能夠被觀測到，因此日誌等級特意設為 `WARNING`。

### 驗證方法

部署後執行：

```bash
docker exec hermes-agent-lq ls -la /home/murray/ai-data/admin-panel/admin.db
docker exec hermes-agent-lq python -c "
from plugins.platforms.line.identity import IdentityResolver
r = IdentityResolver()
print('admin_db:', r._admin_db)
print('exists:', r._admin_db.exists())
"
```

確認 `exists: True` 且無 `WARNING` 日誌。

---

## 合併歷史

| 順序 | 分支 | 衝突 | 處理 |
|------|------|------|------|
| 1 | `feature/line-soul-skill` | 無 | 自動合併 |
| 2 | `feature/line-group-forward` | `SUMMARY.md` (add/add) | 刪除 SUMMARY.md |
| 3 | `feature/line-media` | `adapter.py` (imports)、`test_line_plugin.py` (imports + 結尾) | 手動合併，保留雙方內容 |
| 4 | `feature/line-cron-delivery` | `SUMMARY.md` (add/add) | 刪除 SUMMARY.md |
