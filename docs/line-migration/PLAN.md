# LINE OA 遷移：channel_gw → hermes-agent 原生 LINE plugin

## 背景（給接手的 agent 看，不要跳過）

Murray 的 LINE 助理「小助理」目前由 `~/ai-stack/code/home_gateway/channel_gw`
（一支獨立 FastAPI 服務）處理所有 LINE webhook。這支服務綁架了太多職責、體驗
不可靠（詳見 `~/.claude/plans/shimmying-rolling-lantern.md` Phase B 的記錄），
現在要整個換成 **hermes-agent 自帶的原生 LINE platform plugin**
（`~/hermes-agent/plugins/platforms/line/adapter.py`）。

**目前狀態（2026-07-31）**：
- hermes-agent 的原生 LINE plugin 已經是「連線中」狀態，但公網流量仍 100%
  走 channel_gw（nginx `/webhook/line` → channel_gw:5000）。原生 plugin 只能
  用合成 webhook 請求（見下方「怎麼測試」）驗證，不能碰真實 OA 流量——
  channel_gw 停用前，它是小助理唯一能正常運作的路徑，**任何人在完全驗收
  通過前都不准動 nginx 路由或停用 channel_gw**。
- 已驗證：原生 plugin 的基本 dispatch（收到 1:1 文字訊息→建立 session→
  agent 執行→試著回覆）是好的。已修過一個 bug：`config.yaml` 的
  `platform_toolsets.line` 原本指到不存在的 `hermes-line` toolset，已在
  `toolsets.py` 補上（commit `bc0e8401f`）。
- LINE push 配額（免費方案每月 200 則）已用罄，push 會回 429——這是 LINE
  平台外部限制，不是程式問題。**真實 reply token 走 Reply API 不算配額**
  （配額只計 push，不計 reply），所以正常情境下不受影響；只有 fallback
  推播（reply token 過期、或多氣泡超過 reply 視窗）才會撞到。

## 權威來源：channel_gw 現在做了什麼

**全部以 `~/ai-stack/code/home_gateway/channel_gw/main.py` 的行為為準**——
不要憑印象重新設計，任何行為差異都要先對照這支檔案。核心函式：

- `webhook_line()`（約 1534 行）：signature 驗證 → `_partition_line_events()`
  把 events 分成 group/room（ingestion）跟 user（assistant）兩組。
- `_partition_line_events()` / `_forward_line_ingestion_events()`（約
  1150-1186 行）：group/room 事件**一律**（不論有沒有 @mention）非同步轉發
  給 line_ingestion 的 `/internal/line-events`（`X-Internal-Secret` header，
  見 `LINE_INGESTION_FORWARD_URL`/`LINE_INGESTION_INTERNAL_SECRET` env）。
- `_line_group_mention_events()` / `_handle_line_group_mention()`（約
  1189-1292 行，F4）：**額外**（不影響上面的轉發），如果 `LINE_GROUP_QA_ENABLED`
  為真，group/room 文字訊息裡 @到 bot 本身、且該群組在 `LINE_GROUP_QA_ALLOWLIST`
  裡，才會用**獨立的 `grp:{groupId}` session 命名空間**（`history.session_id`
  + `ask_hermes(gscope, ...)`，`gscope = f"grp:{group_id}"`）呼叫 AI 回答，
  跟任何員工的 1:1 記憶物理隔離——這是 2026-07-01 記憶錯亂事故後刻意做的防線
  （見記憶 `line-bot-memory-contamination`），**遷移時這個隔離設計不能丟**。
- `_handle_line_message()`（約 1405-1531 行，user 1:1 路徑）：
  1. `__FOLLOW__`/`__UNFOLLOW__` 系統事件
  2. 不支援的訊息類型直接回覆說明
  3. **身分綁定流程**：`identity.resolve("line", user_id)` 查不到員工就進入
     `awaiting_nickname` 狀態，要求輸入 Admin Panel 的 Nickname 欄位；
     `identity.bind("line", user_id, nickname)` 比對成功才放行。未綁定的人
     **完全不會被送去給 AI**。
  4. 檔案格式白名單（pdf/doc/docx/xls/xlsx/ppt/pptx/txt/csv）
  5. 簡單問候語快速路徑（不建 job、不跑長記憶）
  6. 其餘一律建立 async job（`_create_job`）→ reply-token 先回一句「收到，
     處理完會通知」→ 背景 `_run_line_long_task`：
     - 檔案 → 解析文字（`line_adapter.extract_text`，見
       `_prepare_line_message_for_ai`）
     - 圖片 → `ask_vision()`（vision model）
     - 語音 → `transcribe_audio()`（whisper，經 LiteLLM）
     - 都轉成純文字後 → `process_message("line", ...)` → `ask_hermes()`
     - 語音訊息額外用 `synthesize_speech_m4a()` 產生 TTS 語音檔 push 回去
  7. Soul/skill：`process_message()` 內部會 `_load_soul(employee_id)` +
     `_load_skill(employee_id)`（讀 admin.db 的部門/個人技能表）疊加進
     system prompt。
- Slash commands（`COMMAND_REGISTRY`，另外兩個獨立指令，不在 webhook_line
  裡但同一支服務）：`/定期任務 <描述>` 建立排程報告、`/清除記憶` 清除
  目前 channel 的對話歷史。
- Cron 推播（F3 每日群組摘要 + `/定期任務` 產生的排程）：外部呼叫
  `POST /internal/report`（`?secret=channel-gw-internal-2026`）
  → `push_text()` 推到 LINE。這條路徑停掉 channel_gw 之後必須有替代，
  否則每日摘要跟排程報告會完全推不出去（不會噴錯，就是安靜地送不到）。

## 目標架構：全部遷進 hermes-agent 原生 LINE plugin

不要整包照抄 channel_gw 的實作方式——hermes-agent 本身是通用 agent 平台，
很多 channel_gw 手刻的東西（vision、file 解析、slash command、排程）
**hermes-agent 可能已經有更好的原生等效**，遷移的精神是「讓 LINE 這個平台
的訊息，用 hermes-agent 本來就有的能力去處理」，channel_gw 那套是
「gemma4-e4b 4B 分類器太弱只好手刻」年代的產物（見記憶
`project_enterprise_ai.md`「為什麼移除意圖偵測」一節）。**每個子任務動手前
先實測 hermes-agent 有沒有現成能力，不要預設沒有就重寫。**

### 子任務清單（對照 TaskCreate 任務、標明負責 CLI）

#### 1. 群組轉發 + F4 @mention 閘門（負責：codex）

**目標行為**：
- LINE plugin 的 `_dispatch_event()`（`plugins/platforms/line/adapter.py`
  約 944 行）在 allowlist 檢查*之前*，group/room 來源的事件一律非同步轉發
  給 line_ingestion 的 `/internal/line-events`（沿用 channel_gw 現有的
  forward 格式與 secret，不要重新設計 payload schema）。轉發跟後面要不要
  觸發 agent 回覆是兩件獨立的事。
- 只有 group/room 文字訊息裡 @到 bot 本身（LINE webhook 的
  `message.mention.mentionees[].isSelf`，見 channel_gw `_line_mentions_self`
  的判斷邏輯，直接搬過來）**且**該群組在新的允許清單環境變數（例如
  `LINE_GROUP_QA_ALLOWLIST`，逗號分隔 groupId）裡，才繼續往下呼叫
  `self.handle_message(event_obj)` 觸發 agent 回覆；否則轉發完就結束，
  不要讓 agent 對每一則群組訊息都回話。
- @mention 觸發時，去掉訊息裡的 @bot 字串（`_strip_self_mention` 邏輯）
  再送進 agent。

**為什麼要做**：現在原生 plugin 對所有 group/room 訊息一視同仁丟給
allowlist（沒設 `LINE_ALLOWED_GROUPS` 就整批拒收），完全沒有「先轉發、
再決定要不要回話」這個分層，切過去會讓 line_ingestion 的每日摘要/事件
擷取管線斷掉。

**驗收**：合成兩種 webhook 事件（純群組訊息 vs. @bot 群組訊息），確認
line_ingestion 都收到轉發（查 `raw_line_messages` 表），且只有 @mention
那則有觸發 `state.db` 新增 session、agent 有回覆。

#### 2. 身分綁定閘門（負責：opencode）

**目標行為**：LINE plugin 對 1:1（user）訊息，先查詢 admin.db 是否已有
`identity_map`（`pipeline_config[component=channel_gw].identity_map`，讀
`~/ai-stack/code/admin_panel` 用的同一張表/欄位——**先去讀 admin_panel 的
schema，不要臆測欄位名稱**）綁定該 LINE user_id。查不到就回覆要求輸入
Nickname 的訊息，並記住「這個 user_id 正在等輸入暱稱」的狀態（可以用
hermes 自己的 session/kv 機制存，不必依賴 channel_gw 的 redis）；下一則
文字訊息若命中暱稱、比對 admin.db 使用者表成功，就寫入綁定並放行；查無
此暱稱要重新提示。**只有綁定成功的員工訊息才繼續往下讓 agent 處理**——
未綁定訊息完全不進 LLM。

**驗收**：合成一個不在任何綁定裡的 LINE user_id 傳文字，確認回覆是綁定
提示、且 state.db 沒有幫這個 user_id 建立 session；再合成「輸入正確暱稱」
的下一則訊息，確認綁定寫入、且之後的訊息能正常觸發 agent。

#### 3. 語音/檔案/圖片是否已有原生等效（負責：先實測釐清，不指定 CLI）

`plugins/platforms/line/adapter.py` 已經有 `_download_media`（image/audio/
video/file 都會 cache 成本地檔並填進 `MessageEvent.media_urls/media_types`），
而 hermes-agent 核心本身有 vision/file 相關工具（見 `config.yaml` 的
`platform_toolsets.api_server` 就列了 `vision`、`file`）。**先合成一則帶
圖片/檔案/語音的 webhook 事件，實測 agent 會不會自動用核心工具處理**，
再決定：
- 如果已經work → 不用重寫，直接在這份文件記錄「已原生支援，見 XX 測試」。
- 如果沒 work（例如語音沒有自動轉錄成文字餵給 LLM）→ 才需要照 channel_gw
  的 `transcribe_audio`/`ask_vision`/`extract_text` 邏輯另外接。
語音的 TTS 回覆（`synthesize_speech_m4a` 那段）也一樣，先查
`config.yaml` 的 `auxiliary.tts_audio_tags` 跟 hermes 既有的 voice-mode
機制（`_sync_voice_mode_state_to_adapter`）是否已經覆蓋這個需求。

#### 4. Soul/skill 個人化 hook（負責：待第 2 項身分綁定完成後排）

一旦第 2 項能穩定解析出 `employee_id`，比照
`~/services/hermes-hooks/intention_router.py` 那種 hook 模式，寫一個
LINE 平台專用的 `pre_llm_call` hook：解析出 employee_id 後去讀該員工的
soul/skill（admin_panel 的 skills 表，見 channel_gw `_load_soul`/
`_load_skill` 的 SQL 當參考），動態疊加進這一輪的 system prompt。

#### 5. Slash commands 對應（負責：先實測釐清）

**先實測**：hermes-agent 的 agent loop 本身能不能透過自然語言呼叫
`cronjob` 工具建立排程（不需要使用者打 `/定期任務`），以及 LINE 訊息打
`/new` 之類 hermes 內建指令會不會被 `coerce_plaintext_gateway_command`/
`event.get_command()` 正確辨識、觸發跟 channel_gw `/清除記憶` 類似的效果。
如果原生機制已經涵蓋，**不需要重刻 slash command**，只要在文件裡記錄
「請用戶改用自然語言/`/new`」；如果體驗明顯比 channel_gw 差（例如
LLM 誤判排程意圖），再評估是否要加一層平台專屬的指令解析。

#### 6. Cron 推播（F3 每日摘要）對應方案（負責：待前面子系統穩定後排）

hermes-agent 的 LINE plugin 註冊時已經帶了
`cron_deliver_env_var="LINE_HOME_CHANNEL"`、`standalone_sender_fn`——這代表
原生 cron 系統可以推訊息給*一個*固定的 home channel（適合「Murray 個人」
這種單一目標），但 F3 需要對**每一個正在被 ingest 的群組**各自推當日摘要，
目標數量是動態的（新群組會增加）。需要設計：用 `standalone_sender_fn` 
或直接呼叫 LINE push API，搭配 line_ingestion 目前紀錄的「有在追蹤的
群組清單」，寫一個排程腳本/hermes cron job 逐一推送，取代
channel_gw 的 `/internal/report`。**這個任務排在最後**，因為前面的子系統
（尤其身分綁定、group 轉發）都還沒穩，且風險相對低（就算慢一點做，
最壞情況只是「當日摘要晚幾天恢復」，不影響即時對話體驗）。

## 怎麼測試（全程不要碰真實 OA/nginx）

容器內部直接打 `127.0.0.1:8646/line/webhook`，帶正確 HMAC 簽章（用真實
`LINE_CHANNEL_SECRET`，可以從 `docker inspect hermes-agent-sm121` 的 env
拿到，或問 Murray／查 vault），範例腳本：

```python
import hmac, hashlib, base64, json, urllib.request, time
secret = "<LINE_CHANNEL_SECRET>"
body = {
    "destination": "test-destination",
    "events": [{
        "type": "message",
        "webhookEventId": f"test-{int(time.time())}",
        "timestamp": int(time.time() * 1000),
        "source": {"type": "user", "userId": "<測試用真實 allowlist user_id>"},
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
```

（group/room 測試把 `source` 換成
`{"type": "group", "groupId": "C...", "userId": "U..."}`，並在
`message.mention.mentionees` 加 `isSelf: true` 來模擬 @bot。）

驗證完的合成 session 記得用 `hermes sessions delete <id> --yes` 清掉，
不要留在 Murray 的真實對話歷史/Honcho 記憶裡。

## 完成後才能做的事（不要提前做）

- nginx `/webhook/line` 從 channel_gw 切到 hermes-agent（`hermes-nginx`
  容器的 `nginx.conf`，兩處 server block 都要改，改完
  `docker restart hermes-nginx`）。
- 停用 channel_gw（`disable` 不 `remove`，資料/程式碼都留著，回滾成本
  只有改一行 nginx + reload）。
- 這兩件事都要等本文件列的子系統**全部**驗收通過才能做，任何一項沒過
  都不准動 nginx（channel_gw 是目前唯一能正常運作的路徑）。

## 相關記憶／文件（背景脈絡，需要時查）

- `~/.claude/plans/shimmying-rolling-lantern.md` — 整個收斂計畫的母文件
- 記憶 `line-bot-memory-contamination` — F4 group session 隔離的由來
- 記憶 `line-gateway-dispatch-not-completing` — 這次遷移之前的一段除錯史
- 記憶 `spec-0001-line` — line_ingestion/F1-F6 的架構背景
- 記憶 `project_enterprise_ai` — channel_gw 現有架構全貌
