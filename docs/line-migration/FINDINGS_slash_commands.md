# 子任務 5 調查：slash commands 對應

調查日期：2026-07-31

## 結論摘要

遷移後不需要再造一套排程引擎或對話 session 引擎：Hermes 原生 agent 可以取得 `cronjob` 工具，而 gateway 的 `/new`（別名 `/reset`）會把目前聊天切到新的 session。建議對使用者改成：

- 建立排程：**不要再打 `/定期任務`**，直接用自然語言，例如「每天晚上九點彙整 AI 新聞並傳回這個 LINE 對話」。
- 清除目前對話上下文：改打 **`/new`**（或 `/reset`），再依 LINE 上的文字提示回覆 `/approve`；`/stop` 不是清除記憶。

不過，這兩條替代路徑的體驗都不是舊行為的逐字等價：自然語言排程多了一層「主模型要先判斷應呼叫工具」；LINE 的 `/new` 預設多一次文字確認。原始碼可以證明工具與路徑可達，**不能單憑靜態閱讀證明目前主模型對任意中文排程敘述的準確率**。正式切流前仍應用目前 production 模型做中文案例驗收。

目前沒有為了保留既有能力而「確定必補」的新 core/plugin command。若 Murray 要求舊指令字串零學習成本相容，才建議在 LINE plugin 邊緣加薄 alias/rewrite，不要複製 channel_gw 的排程建立與 session 儲存邏輯。

## 調查範圍與限制

- 已完整閱讀 `docs/line-migration/PLAN.md`。
- 舊行為以唯讀檔 `/home/murray/ai-stack/code/home_gateway/channel_gw/main.py` 為準。
- 先以 codebase-memory 查詢：`murray-home-gateway` 的兩個 handler 都存在，但 registry 動態派送沒有形成靜態 caller 邊；因此以下結論回到實檔的 `COMMAND_REGISTRY` 與 `process_message()` 查證。codebase-memory 目前沒有索引 `hermes-agent`，Hermes 端全部以實檔核對，沒有假稱圖譜已驗證。
- 本 checkout 與 `/home/murray/.hermes` 都沒有可讀的實際部署 `config.yaml`，而本任務禁止進容器。因此「目前部署的 `platform_toolsets.line` 指向 `hermes-line`」採用遷移規格已記錄的現況（`docs/line-migration/PLAN.md:17-21`）；程式端另外驗證即使 plugin platform 沒有明列設定，也會推導預設 composite 名稱 `hermes-line`（`hermes_cli/tools_config.py:2151-2158`）。若部署檔把 `line` 明確設成空清單、排除 `cronjob`，或在 `agent.disabled_toolsets` 關掉它，結果會不同，切流前應讀實際部署檔再確認一次（全域 subtraction 見 `hermes_cli/tools_config.py:2392-2400`）。
- 另以本機 resolver 做了不寫檔驗證：`_get_platform_tools({}, "line")` 與 `_get_platform_tools({"platform_toolsets": {"line": ["hermes-line"]}}, "line")` 的結果都包含 `cronjob`。環境缺 `httpx`，載入幾個無關 plugin 時有 warning，但不影響這兩次 LINE toolset 解析結果。
- 嘗試執行既有單元測試，但此 checkout 沒有 `.venv`/`venv`，系統也沒有 `pytest`（`pytest: command not found`）；未為了調查安裝依賴。既有 command parser 測試仍可作為原始碼證據（`tests/gateway/test_platform_base.py:95-112`）。

## 舊 channel_gw 的精確行為

### `/定期任務 <描述>`

1. `parse_slash_command()` 只要文字以 `/` 開頭，就拆出命令名稱與參數（`/home/murray/ai-stack/code/home_gateway/channel_gw/main.py:770-778`）。
2. `process_message()` 在進 Hermes 前查 `COMMAND_REGISTRY` 並直接呼叫 handler；所以是否進入排程流程是確定的字串路由，不由聊天模型判斷（同檔 `1059-1074`）。
3. `定期任務` 註冊到 `handle_periodic_task`（同檔 `924-928`）。handler 先用 LLM 從描述抽出 `topic`、五欄 cron 與中文頻率名稱（同檔 `799-844`、`893-907`），再直接建立 job（同檔 `847-890`）。

因此舊路徑也不是完全規則式：**時間與主題仍由 LLM 抽取**；它確定化的是「這一則一定是排程意圖」，避開弱意圖分類器的誤判。

### `/清除記憶`

`清除記憶` 註冊到 `handle_clear_memory`（同檔 `929-933`）。handler 會清掉 channel/employee 的 Redis history window、旋轉 Hermes transcript/session id，並清 pending state（同檔 `910-921`）。它的使用者可觀察契約是「下一輪從空的對話上下文開始」，不是把所有歷史資料與長期個人記憶做實體抹除。

## 原生已涵蓋

### 1. Agent loop 有建立排程所需的原生能力

證據鏈如下：

1. `hermes-line` composite 直接包含 `_HERMES_CORE_TOOLS`（`toolsets.py:473-476`），而 core tools 明列 `cronjob`（`toolsets.py:67-68`）。獨立的 `cronjob` toolset 也把 `cronjob` 暴露為工具（`toolsets.py:188-191`）。
2. Gateway 每一輪會依 `source.platform` 讀取 `config.yaml` 的 platform toolsets（`gateway/run.py:23166-23172`），並把結果傳給 `AIAgent`（`gateway/run.py:23393-23417`、`gateway/run.py:4450-4460`）。
3. Agent 初始化會把 enabled toolsets 解析成實際 API tool schemas（`agent/agent_init.py:1382-1405`；解析 composite 的實作見 `model_tools.py:377-402`），API request 使用 `agent.tools`（`agent/chat_completion_helpers.py:1123-1139`）。
4. 模型回傳 tool call 後，conversation loop 會執行它（`agent/conversation_loop.py:6058-6105`）。
5. `cronjob` schema 明確告訴模型可用 `action='create'` 建立 job（`tools/cronjob_tools.py:940-976`）；create 實作要求 schedule 與 prompt/skill，最後呼叫 `create_job()`（`tools/cronjob_tools.py:623-670`、`700-705`）。工具在 gateway session 可用，不需要外部 crontab（`tools/cronjob_tools.py:1049-1068`），並以 `check_fn` 註冊（`tools/cronjob_tools.py:1071-1103`）。此外 gateway startup 無條件設定 `HERMES_EXEC_ASK=1`（`gateway/run.py:2145-2149`），正好滿足該 `check_fn` 的其中一個 truthy 條件（`tools/cronjob_tools.py:1062-1068`），所以不是只有 schema 名稱被選中、卻在 runtime gate 被拿掉。
6. 未指定 `deliver` 時，schema 要求自動回送目前 chat/topic（`tools/cronjob_tools.py:986-988`）；LINE platform 已註冊原生 cron delivery 與 standalone sender（`plugins/platforms/line/adapter.py:1702-1717`）。

所以，當 LINE 的 enabled toolsets 含 `cronjob` 時，普通自然語言會進主 agent；主模型可選擇 `cronjob(action='create', schedule=..., prompt=...)`，不需要 `/定期任務` 專屬 handler。

遷移指引：請使用者直接說「每週一上午九點整理上週銷售業績並傳回這個 LINE 對話」之類的完整自然語言，且**不要保留開頭 `/定期任務`**。Hermes 對真正未知的 slash command 會回 unknown-command 提示，並要求拿掉 `/` 重送（`gateway/run.py:14829-14849`）。

### 2. LINE 的 `/new`、`/stop` 能被原生 gateway 正確辨識

證據鏈如下：

1. LINE adapter 對文字訊息原樣取 `message.text`，建成 `MessageEvent`，再呼叫共用 `handle_message()`（`plugins/platforms/line/adapter.py:995-1003`、`1027-1045`）。
2. 共用 `handle_message()` 先呼叫 `coerce_plaintext_gateway_command()`（`gateway/platforms/base.py:5535-5547`）。這個 helper **只**把 DM 中精確的「restart gateway」類普通文字改成 `/restart`；若原文已以 `/` 開頭會直接略過（`gateway/platforms/base.py:2166-2198`）。換言之，`/new` 不需要也不會被它破壞。
3. `MessageEvent.get_command()` 對 `/` 開頭的文字取第一個 token、去掉 `/` 並轉小寫，故 `/new` 與 `/stop` 都可直接解析（`gateway/platforms/base.py:2127-2144`）；既有單元測試也明確覆蓋 `/new`（`tests/gateway/test_platform_base.py:95-112`）。
4. 即使同一 session 正在跑 agent，base adapter 也會讓 `/stop`、`/new`、`/reset` 走 interrupt-then-dispatch，不會排進普通訊息佇列（`gateway/platforms/base.py:5568-5601`）。
5. Gateway registry 宣告 `/new`（alias `/reset`）與 `/stop`（`hermes_cli/commands.py:101-107`、`135-136`），runner 解析 alias 後分派：`/new` 到 reset handler、`/stop` 到 stop handler（`gateway/run.py:14186-14199`、`14288-14302`、`14343-14344`）。

注意：`/stop` 只中止當前執行，且刻意保留 session 讓使用者繼續對話（`gateway/slash_commands.py:1348-1358`）；它不是 `/清除記憶` 的替代品。

### 3. 原生 `/new` 涵蓋「讓目前對話從空上下文重新開始」

`/new` 的 reset handler 會清理舊 agent、所有 conversation-scoped state，然後呼叫 `reset_session()`（`gateway/slash_commands.py:119-184`、`216-233`）。session store 會結束舊 session、生成新的 session id，並用同一個 LINE session key 指向新 entry（`gateway/session.py:2704-2745`、`2747-2773`）。這符合舊 `/清除記憶` 的使用者可觀察效果：下一輪不再帶入舊 transcript。

它**不等於實體刪除 SessionDB 裡的舊 transcript，也不等於清除 memory provider 的長期個人記憶**。但舊 handler 本身也是「清 Redis window + rotate transcript id」，不是刪除全部長期記憶，所以就本次規格所稱的「目前 channel 對話歷史」而言，`/new` 是原生等效。

## 原生有但體驗會變差

### 1. 排程少了明確意圖邊界

舊 `/定期任務` 以 registry 確保一定進排程抽取；新做法要主模型先從自然語言決定呼叫 `cronjob`，再正確填 schedule/prompt。新架構的優點是不用記命令、也能在同一段自然語言補問細節；缺點是比舊路徑多一個模型判斷點。

目前原始碼與測試只能證明工具 schema 已提供且 agent loop 能執行，沒有針對 production 現用模型的繁中排程意圖 eval。因此不能把「現在主模型不同」直接推論成「誤判率已可接受」。切流前最低限度應用 production route 實測下列類型，並查 jobs storage 確認 schedule、prompt、deliver：

- 明確週期與時間：「每天晚上九點彙整 AI 新聞並傳回 LINE」。
- 排除干擾時間：「每天晚上九點傳送；早上九點已彙整的內容排除」。
- 含週別：「每週一上午九點傳上週銷售週報」。
- 時間不完整：「每天整理新聞」——應先澄清，不應自行猜時間。
- 非排程敘述：「請說明如何安排每週報告」——不應誤建 job。

若這組驗收穩定，就不要繼承舊 command。若不穩定，優先在 LINE plugin 用很薄的 `/定期任務` rewrite，把參數改寫成明確的普通 user prompt（例如要求 agent 建立 cronjob），仍由原生 cronjob tool 建 job；不要搬回 docker exec、第二套排程 schema或 channel_gw callback。

### 2. `/new` 在 LINE 預設多一次文字確認

`/new` 被當成 destructive slash command；`approvals.destructive_slash_confirm` 預設為 `true`（`gateway/run.py:19409-19445`）。有原生按鈕的 adapter 可顯示三按鈕，但 LINE adapter 沒有 override `send_slash_confirm()`；base implementation 回報不支援，gateway 因而走文字 fallback，要求下一則回 `/approve`、`/always` 或 `/cancel`（`gateway/platforms/base.py:3726-3759`、`gateway/run.py:19479-19495`、`19540-19563`）。

相較舊 `/清除記憶` 一次完成，預設流程變成：

1. 使用者傳 `/new`。
2. Bot 回確認文字。
3. 使用者傳 `/approve` 才真正 reset。

這是明確的額外摩擦。遷移初期可先在使用者說明中寫清楚。若實際使用證明很困擾，建議補 LINE 的 `send_slash_confirm()`（LINE Template Message/Postback）來提供按鈕，而不是新增另一個清 session handler。也可由使用者選 `/always` 永久關閉未來確認；程式會持久設定 `approvals.destructive_slash_confirm: false`（`gateway/run.py:19449-19469`）。

### 3. 舊中文指令名稱不相容

Hermes registry 沒有 `/定期任務`、`/清除記憶`；真正未知的 slash command 不會送進 LLM，而會直接回 unknown-command（`gateway/run.py:14829-14849`）。因此遷移公告必須明寫：

- `/定期任務 …` → 拿掉命令前綴，改成完整自然語言。
- `/清除記憶` → `/new`（或 `/reset`），並完成確認。
- `/stop` → 只在要停止目前正在執行的工作時使用，不能拿來清上下文。

## 原生沒有、確定要補

**目前沒有。** 就規格要求的兩個能力而言：

- 建立排程已有原生 `cronjob` tool 與 LINE delivery registration。
- 清除目前對話上下文已有 `/new`/`/reset` 與 session rotation。

以下是「原生沒有，但不是本次確定必補」的相容/UX 選項：

- 舊中文命令字串的 alias：只有在不接受使用者遷移公告、必須零學習成本時才補；應在 plugin 邊緣 rewrite 到原生能力。
- LINE 原生 slash-confirm 按鈕：能改善 `/new` 的三步文字 UX，值得後續做，但不阻擋能力遷移。
- 實體刪除舊 transcript 或清除長期 memory provider：這超出舊 `/清除記憶` 的可觀察契約；若法遵/隱私另有「right to erasure」需求，應另立規格，不能把 `/new` 當資料刪除 API。

## 切流前驗收建議

1. 在不碰真實 OA/nginx 的前提下，用合成 LINE webhook 傳 `/new`，確認收到文字確認；再傳 `/approve`，確認同一 LINE session key 映射到新的 session id，下一句不帶入舊對話。
2. 在 agent 閒置與執行中各測一次 `/stop`，確認只中止工作、後續仍沿用原 session。
3. 用上節五組繁中排程案例走 production model route；逐一驗證 job 是否建立、schedule/prompt 是否正確、`deliver` 是否回到該 LINE origin。
4. 傳 `/定期任務 每天九點…` 與 `/清除記憶`，確認會得到 unknown-command，據此校對遷移公告措辭。
5. 切流當下再唯讀確認實際部署 `config.yaml` 的 `platform_toolsets.line` 確實包含能解析出 `cronjob` 的 toolset，且 `agent.disabled_toolsets` 未將它停用。
