# LINE OA 遷移至 hermes-agent 原生 LINE Plugin：遷移紀錄與維護者指南 (Migration Log & Maintainer Guide)

> **文件狀態**：動態更新中（In Progress）  
> **建立日期**：2026-07-31  
> **母規格文件**：[PLAN.md](file:///home/murray/hermes-agent/docs/line-migration/PLAN.md)  
> **驗收測試規格**：[QA_CHECKLIST.md](file:///home/murray/hermes-agent/docs/line-migration/QA_CHECKLIST.md)  

---

## 1. 為什麼要做這次遷移 (Background & Motivation)

### 1.1 `channel_gw` 現有問題與架構瓶頸
Murray 的 LINE 官方帳號（OA）「小助理」原本是由一支獨立的 FastAPI 服務 `~/ai-stack/code/home_gateway/channel_gw` 處理所有 LINE Webhook。這支服務在早期承載了過多職責：
- Webhook 簽章驗證與事件分流
- 身分綁定 (`identity_map`) 與權限檢查
- 多媒體檔案（圖片、語音、PDF/Office 文件）下載與解析
- 外接 Vision 模型、Whisper 轉錄與 CosyVoice TTS 語音合成
- 手刻意圖辨識與 Slash Commands（例如 `/定期任務`、`/清除記憶`）
- 內部報告與 Cron 推播轉發 (`POST /internal/report`)

這種「過度綑綁」的架構帶來了高維護成本與不穩定的維運體驗。

### 1.2 體驗不可靠的背景
依據 `~/.claude/plans/shimmying-rolling-lantern.md`（Phase B 記錄）以及 2026-07-30 深夜的真實流量測試：
1. **派工與連線問題**：過去曾發生 Honcho 記憶服務連線端點硬編碼為 `127.0.0.1` 導致 `ConnectError`，以及 Gateway 派工隊列鎖定造成訊息靜默無視。
2. **回應體驗不穩定**：用戶發送訊息後常面臨佇列阻塞或延遲過高，導致 LINE bot 體驗不可靠，這也是過往使用率偏低的核心技術根因。
3. **系統收斂戰略**：2026-07-27 rethink 決定收斂「多人產品線/多重 Gateway」，改以 `hermes-agent` 為個人 AI 中樞。讓 LINE Platform 直接由 `hermes-agent` 原生 Platform Plugin (`plugins/platforms/line/adapter.py`) 驅動，能簡化系統層級、降低延遲並提升穩定度。

---

## 2. 架構決策 (Architectural Decisions)

### 2.1 原始 `channel_gw` 與原生 LINE Plugin 比較

| 架構維度 | 舊架構 (`channel_gw`) | 新架構 (`hermes-agent` 原生 LINE Plugin) |
| :--- | :--- | :--- |
| **服務形式** | 獨立 FastAPI 服務 (Port 5000)，需維護獨立容器/進程 | `hermes-agent` 內部原生的 Platform Adapter (Port 8646) |
| **訊息派工** | 自製 Async Job 隊列 + 靜態意圖路由 | 整合 `hermes-agent` 通用 Agent Loop 與背景任務派工 |
| **能力提供** | 於 Gateway 層手刻 Vision/Whisper/檔案解析/TTS 邏輯 | 善用 `hermes-agent` 內建 Core Toolsets (vision, file, cronjob 等) |
| **身分與 Soul** | 手刻 SQL 查詢 `admin.db` 並組裝 Prompt | 身分綁定閘門 + 專用 `pre_llm_call` Hook 動態載入 Soul/Skill |
| **維護成本** | 需同時維護 channel_gw 與 hermes 兩套代碼 | 統一於 `hermes-agent` 框架下維護 |

### 2.2 刻意放棄與簡化的歷史包袱

#### 捨棄手刻 Slash Commands (`/定期任務`、`/清除記憶`)
- **歷史脈絡**：當年使用 `gemma4-e4b` 4B 小型分類器時，因為語意理解能力不足，LLM 無法精確辨識用戶是否想要設定定時任務或重置對話，因此必須在 `channel_gw` 開闢手刻的 Slash Commands 靜態導航路徑。
- **新架構做法**：`hermes-agent` 本身的 Agent Loop 搭配強大主模型已具備成熟的工具調用能力。用戶可以直接使用自然語言要求 Agent 設定排程（Agent 自動調用內建 `cronjob` 工具），或發送內建指令（如 `/new`）直接重置 Session。不需要在 LINE 平台層重刻一套專屬的靜態指令解析器。

#### 簡化中繼多媒體處理解析
- 原生 LINE Plugin 的 `adapter.py` 已提供 `_download_media` 快取機制，並能將媒體填入 `MessageEvent.media_urls`。整合 `hermes-agent` 的原生 Vision 與 File 工具，免除舊有 `channel_gw` 手寫大量轉碼與臨時檔清理邏輯。

---

## 3. 關鍵環境陷阱 (Critical Environment Pitfalls)

在遷移與後續派工過程中，必須特別注意以下兩個環境陷阱：

> [!WARNING]
> ### 坑一：本機沒有可跑 pytest 的 venv
> - **現象與原因**：`hermes-agent` 的完整執行與測試 Python 環境封裝於生產容器內（`/opt/hermes/.venv`）。宿主機（Host）環境以及各 Git Worktree 目錄下皆**未建立 Python virtualenv**，因此無法在宿主機直接執行 `pytest` 運行單元測試。
> - **對策規範**：**嚴禁在宿主機嘗試安裝或執行 pytest**。所有功能與防退步驗證，一律嚴格遵照 [PLAN.md](file:///home/murray/hermes-agent/docs/line-migration/PLAN.md) 與 [QA_CHECKLIST.md](file:///home/murray/hermes-agent/docs/line-migration/QA_CHECKLIST.md) 提供的合成 Webhook 腳本 (`send_synthetic_webhook.py`)，直接對容器內部的 Mock Endpoint (`http://127.0.0.1:8646/line/webhook`) 發送請求進行驗證。

> [!CAUTION]
> ### 坑二：Worktree 誤寫主 Repo 的風險
> - **現象與教訓**：在多 Subagent/Worktree 並行派工時，opencode 第一輪開發除了在其專屬 Worktree commit 之外，誤將同一批修改檔案寫入主 Repo (`/home/murray/hermes-agent`) 並執行了 `git add` (staged)。
> - **嚴重性與防範**：主 Repo (`/home/murray/hermes-agent`) 是用於 `docker compose build` 建置生產容器的分支。若未察覺主 Repo 被污染就執行 rebuild，未經過審查與驗收的開發中程式碼將會直接上線。
> - **派工指令規範**：雖然該次誤寫已被清理，但後續派工（無論是 Subagent 或獨立 CLI 工具）**必須明確禁止寫入主 Repo**。另外，在使用 `codex` 等 CLI 派工時，須注意其預設 sandbox 設定（例如 codex 預設唯讀 sandbox 會導致檔案寫入失敗而白工，必須顯式帶入 `--sandbox workspace-write`）。

---

## 4. 現況進度表格 (Current Migration Progress)

截至 **2026-07-31** 的真實狀態如下（已照實記錄，無美化）：

| 子任務 / 項目 | 狀態 | 負責分支 / Commit | 詳細說明與注意事項 |
| :--- | :--- | :--- | :--- |
| **子任務 1**<br>群組轉發 + F4 mention 閘門 | 進行中 | `feature/line-group-forward`<br>(codex 開發中) | 實作群組訊息無條件轉發 `line_ingestion` 與 F4 `@bot` 門檻。**註記**：第一次派工因 codex 預設唯讀 sandbox 導致寫入無效，調整為 `--sandbox workspace-write` 後重新開發中。 |
| **子任務 2**<br>身分綁定閘門 | 程式碼已完成<br>(**未審查**) | `feature/line-identity-binding`<br>(commit `c9c98d4f5`) | 已完成未綁定用戶攔截與暱稱綁定邏輯，但**尚未經過 QA 審查與測試驗收**。 |
| **子任務 3**<br>語音/檔案/圖片原生等效驗證 | 尚未開始 | - | 待子任務 1 & 2 穩定後，以合成 Webhook 驗證原生 vision/file/whisper 工具等效性。 |
| **子任務 4**<br>Soul/Skill 個人化 Hook | 進行中 | `feature/line-soul-skill`<br>(opencode 開發中) | 從 `feature/line-identity-binding` 切出，實作 `pre_llm_call` 載入員工 Soul/Skill 邏輯。 |
| **子任務 5**<br>Slash Commands 對應 | 尚未開始 | - | 評估自然語言與 `/new` 原生機制，確認無須重刻舊有手刻指令。 |
| **子任務 6**<br>Cron 推播 F3 每日摘要 | 尚未開始 | - | 設計多目標群組推播機制，取代舊 `channel_gw` 的 `/internal/report`。 |
| **QA 驗收清單** | 已完成 | [QA_CHECKLIST.md](file:///home/murray/hermes-agent/docs/line-migration/QA_CHECKLIST.md) | 已撰寫包含合成 Webhook 測試腳本、SQL 驗證標準與防退步規範之完整清單。 |
| **Nginx 切換與 channel_gw 退役** | **未執行** | `main` | **生產零風險**。真實 LINE OA 流量仍 100% 走 `channel_gw`（Nginx `/webhook/line` 指向 Port 5000）。 |

---

## 5. 給下一個接手者的快速上手指南 (Maintainer Quickstart Guide)

### 5.1 建議閱讀順序
1. **[PLAN.md](file:///home/murray/hermes-agent/docs/line-migration/PLAN.md)**：瞭解全貌、`channel_gw` 現有行為對照表、新 Plugin 目標架構與測試方法。
2. **[QA_CHECKLIST.md](file:///home/murray/hermes-agent/docs/line-migration/QA_CHECKLIST.md)**：參照合成 Webhook Python 腳本、各子任務判斷標準（SQL/Log）與安全防線。
3. **[MIGRATION_LOG.md](file:///home/murray/hermes-agent/docs/line-migration/MIGRATION_LOG.md)**（本文件）：掌握目前進度、分支狀況與環境坑位。

### 5.2 三大絕對不可退步的安全防線 (Critical Safety Rules)
1. **F4 群組記憶物理隔離**：
   - 群組觸發之 Agent Session 命名空間必須為 **`grp:{groupId}`**。
   - **絕不可**混入個人 `usr:{userId}` 記憶，亦不可寫入 Honcho 員工個人長期記憶庫（源自 2026-07-01 記憶錯亂事故教訓）。
2. **未綁定用戶零 LLM 暴露**：
   - 未在 `admin.db` 完成綁定 (`identity_map`) 的用戶訊息，必須在 Gateway / Adapter 層直接攔截並提示綁定，**絕對禁止**送入 LLM 核心。
3. **生產環境零風險控制**：
   - 在所有子任務於合成測試完全通過驗收前，**絕不可以動 Nginx 路由設定**，亦**不可關閉或停用 channel_gw**。

### 5.3 關鍵記憶與背景文件參照
- **`~/.claude/plans/shimmying-rolling-lantern.md`**：收斂計畫母文件（Phase B 詳細記錄）。
- **記憶 `line-bot-memory-contamination`**：F4 群組 Session 隔離設計的起源事故。
- **記憶 `line-gateway-dispatch-not-completing`**：遷移前夕的 Gateway 除錯經驗。
- **記憶 `spec-0001-line`**：`line_ingestion` 與 F1-F6 的背景脈絡。
- **記憶 `project_enterprise_ai`**：`channel_gw` 現有架構全貌與移除意圖偵測的決策背景。
