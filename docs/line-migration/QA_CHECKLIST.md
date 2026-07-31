# LINE OA 遷移至 hermes-agent 原生 LINE Plugin：驗收測試清單 (QA Checklist)

> **文件狀態**：定稿（Draft / Ready for Testing Execution）  
> **建立日期**：2026-07-31  
> **母規格文件**：[PLAN.md](file:///home/murray/hermes-agent/docs/line-migration/PLAN.md)  
> **原則提醒**：本測試清單包含每個遷移子任務的合成 Webhook 測試腳本、明確 SQL/DB 通過判斷標準以及防退步安全規範。**測試執行期間絕不碰真實 LINE OA 流量與正式 Nginx 路由。**

---

## 1. 測試環境與基礎合成 Webhook 腳本模板

所有驗收測試均在容器內部打本地 Mock Endpoint 進行：
- **Target URL**: `http://127.0.0.1:8646/line/webhook`
- **HMAC Header**: `X-Line-Signature` (由 `LINE_CHANNEL_SECRET` 對 Raw Body 進行 HMAC-SHA256 Base64 編碼)
- **環境變數來源**: `docker inspect hermes-agent-sm121` 取得之 `LINE_CHANNEL_SECRET`
- **Session 清除指令**: 驗收完成後執行 `hermes sessions delete <session_id> --yes` 清除合成 Session。

### 通用 Python Webhook 發送模組 (`send_synthetic_webhook.py`)
```python
import hmac
import hashlib
import base64
import json
import urllib.request
import time
import sys

def send_line_event(secret: str, events: list, port: int = 8646) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}/line/webhook"
    body = {
        "destination": "test-destination",
        "events": events
    }
    raw = json.dumps(body).encode('utf-8')
    sig = base64.b64encode(
        hmac.new(secret.encode('utf-8'), raw, hashlib.sha256).digest()
    ).decode('utf-8')
    
    req = urllib.request.Request(
        url,
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Line-Signature": sig
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8')

if __name__ == "__main__":
    secret_val = sys.argv[1] if len(sys.argv) > 1 else "TEST_SECRET"
    print("Testing base helper module...")
```

---

## 2. 子任務 1：群組轉發 + F4 @mention 閘門 (Group Forwarding & F4 Gate)

### 2.1 測試目標與行為定義
1. **無條件轉發**：`plugins/platforms/line/adapter.py` 的 `_dispatch_event()` 收到任何 `group` 或 `room` 訊息時，在 Allowlist 檢查前**必須非同步轉發**給 `line_ingestion` (`/internal/line-events`)。
2. **@mention 門檻與 Allowlist 過濾**：僅當群組訊息包含 `@bot`（`message.mention.mentionees[].isSelf == true`）**且**該 `groupId` 列於 `LINE_GROUP_QA_ALLOWLIST` 時，才觸發 Agent 回覆。
3. **去除 Mention 前綴**：送入 Agent 之前必須剝離 `@bot` 標籤 (`_strip_self_mention`)。

### 2.2 合成 Webhook 測試腳本

```python
import time, json, hmac, hashlib, base64, urllib.request

def test_subtask_1(secret: str):
    ts = int(time.time() * 1000)
    group_id_allowed = "C_TEST_ALLOWED_001"
    group_id_disallowed = "C_TEST_DISALLOWED_002"
    user_id = "U_TEST_USER_999"

    # Case 1.1: 非 Allowlist 群組普通訊息 (不應觸發 Agent，但須轉發)
    evt_1_1 = {
        "type": "message",
        "webhookEventId": f"evt-1-1-{ts}",
        "timestamp": ts,
        "source": {"type": "group", "groupId": group_id_disallowed, "userId": user_id},
        "replyToken": "dummy-token-1-1",
        "mode": "active",
        "message": {"id": f"msg-1-1-{ts}", "type": "text", "text": "群組聊天1"}
    }

    # Case 1.2: Allowlist 群組普通訊息（無 @bot，不應觸發 Agent，但須轉發）
    evt_1_2 = {
        "type": "message",
        "webhookEventId": f"evt-1-2-{ts}",
        "timestamp": ts,
        "source": {"type": "group", "groupId": group_id_allowed, "userId": user_id},
        "replyToken": "dummy-token-1-2",
        "mode": "active",
        "message": {"id": f"msg-1-2-{ts}", "type": "text", "text": "群組聊天2"}
    }

    # Case 1.3: Allowlist 群組 @bot 文字訊息 (必須轉發 + 觸發 Agent)
    evt_1_3 = {
        "type": "message",
        "webhookEventId": f"evt-1-3-{ts}",
        "timestamp": ts,
        "source": {"type": "group", "groupId": group_id_allowed, "userId": user_id},
        "replyToken": "dummy-token-1-3",
        "mode": "active",
        "message": {
            "id": f"msg-1-3-{ts}",
            "type": "text",
            "text": "@小助理 請進行專案簡報",
            "mention": {
                "mentionees": [
                    {"index": 0, "length": 4, "isSelf": True, "type": "user"}
                ]
            }
        }
    }

    for idx, evt in enumerate([evt_1_1, evt_1_2, evt_1_3], start=1):
        print(f"Sending Case 1.{idx}...")
        # 調用通用發送
```

### 2.3 明確通過 / 失敗判斷標準

| 測試案例 | `line_ingestion` DB (`raw_line_messages`) | Hermes `state.db` (`sessions` 表) | Agent 回覆行為 |
| :--- | :--- | :--- | :--- |
| **Case 1.1 (非 Allowlist 普通訊息)** | **PASS**: 存在 `msg-1-1-*` 紀錄 | **PASS**: **不存在**任何新 Session | 不發送 LINE reply/push |
| **Case 1.2 (Allowlist 普通無 @bot)** | **PASS**: 存在 `msg-1-2-*` 紀錄 | **PASS**: **不存在**任何新 Session | 不發送 LINE reply/push |
| **Case 1.3 (Allowlist + @bot)** | **PASS**: 存在 `msg-1-3-*` 紀錄 | **PASS**: 存在 Session ID **`grp:C_TEST_ALLOWED_001`** | **PASS**: 觸發 Agent 產生簡報回應並發送 |

#### 查詢資料庫驗證語法：
```sql
-- 1. 檢查 line_ingestion 轉發紀錄 (PostgreSQL)
SELECT message_id, group_id, raw_payload FROM raw_line_messages 
WHERE message_id IN ('msg-1-1-...', 'msg-1-2-...', 'msg-1-3-...');

-- 2. 檢查 hermes state.db Session 獨立性 (SQLite)
SELECT session_id, created_at FROM sessions WHERE session_id LIKE 'grp:%';
```

### 2.4 🚨 絕對不能退步的安全防線 (CRITICAL SAFETY)
> [!CAUTION]
> **F4 群組 Session 必須與 1:1 員工記憶物理隔離（2026-07-01 記憶錯亂事故教訓）**
> - **硬性規定**：群組觸發之 Session 命名空間必須且只能為 **`grp:{groupId}`**。
> - **禁止行為**：群組 @bot 對話內容**絕對禁止**混入發訊者個人 1:1 Session (`usr:{userId}` / `user_{userId}`)，亦**絕對禁止**寫入 Honcho 員工個人長期記憶庫。
> - **驗證方法**：檢查 Case 1.3 執行後，`usr:U_TEST_USER_999` 的 Session 對話紀錄與 Honcho memory 中完全不包含 Case 1.3 的對話內容。

---

## 3. 子任務 2：身分綁定閘門 (Identity Binding Gate)

### 3.1 測試目標與行為定義
1. **未綁定用戶防護**：對 1:1 訊息查詢 `admin.db` (`identity_map` / `employees`)。若 LINE `userId` 未綁定，回覆綁定提示訊息，將用戶置於 `awaiting_nickname` 狀態。
2. **零 LLM 暴露**：未綁定用戶的訊息**絕不可**傳遞給 LLM 處理。
3. **暱稱核對與寫入**：收到下一則訊息時，核對 Admin Panel Nickname。若比對成功，寫入 `identity_map` 綁定關係並放行後續對話。

### 3.2 合成 Webhook 測試腳本

```python
import time, json

def test_subtask_2(secret: str):
    ts = int(time.time() * 1000)
    unbound_user_id = "U_UNBOUND_TEST_888"
    valid_nickname = "MurrayTest"

    # Case 2.1: 未綁定用戶傳送任意訊息
    evt_2_1 = {
        "type": "message",
        "webhookEventId": f"evt-2-1-{ts}",
        "timestamp": ts,
        "source": {"type": "user", "userId": unbound_user_id},
        "replyToken": "dummy-token-2-1",
        "mode": "active",
        "message": {"id": f"msg-2-1-{ts}", "type": "text", "text": "你好，我想查詢機密資料"}
    }

    # Case 2.2: 未綁定用戶回應 Admin Panel 暱稱
    evt_2_2 = {
        "type": "message",
        "webhookEventId": f"evt-2-2-{ts}",
        "timestamp": ts,
        "source": {"type": "user", "userId": unbound_user_id},
        "replyToken": "dummy-token-2-2",
        "mode": "active",
        "message": {"id": f"msg-2-2-{ts}", "type": "text", "text": valid_nickname}
    }

    # Case 2.3: 綁定完成後發送正常對話
    evt_2_3 = {
        "type": "message",
        "webhookEventId": f"evt-2-3-{ts}",
        "timestamp": ts,
        "source": {"type": "user", "userId": unbound_user_id},
        "replyToken": "dummy-token-2-3",
        "mode": "active",
        "message": {"id": f"msg-2-3-{ts}", "type": "text", "text": "請幫我開會排程"}
    }
```

### 3.3 明確通過 / 失敗判斷標準

| 測試階段 | `admin.db` 綁定狀態 | Hermes `state.db` / LLM 呼叫 | 回覆內容檢查 |
| :--- | :--- | :--- | :--- |
| **Case 2.1 (未綁定嘗試)** | 查無 `U_UNBOUND_TEST_888` | **PASS**: **無** Session 生成，**零** LLM Token | **PASS**: 包含「請輸入 Admin Panel Nickname 進行身分綁定」文字 |
| **Case 2.2 (暱稱核對)** | **PASS**: `identity_map` 新增 `(line, U_UNBOUND_TEST_888) -> employee_id` | **PASS**: 完成綁定紀錄 | **PASS**: 包含「身分綁定成功」提示 |
| **Case 2.3 (綁定後對話)** | 存在綁定紀錄 | **PASS**: 建立 Session `usr:U_UNBOUND_TEST_888` 並調用 LLM | **PASS**: 正常回答排程相關內容 |

#### 查詢資料庫驗證語法：
```sql
-- 查驗 admin.db 綁定表
SELECT * FROM identity_map WHERE platform = 'line' AND platform_user_id = 'U_UNBOUND_TEST_888';
```

### 3.4 🚨 絕對不能退步的安全防線 (CRITICAL SAFETY)
> [!CAUTION]
> **未綁定用戶絕對不能送進 LLM 核心**
> - **硬性規定**：在 `identity.resolve("line", user_id)` 確認成功前，訊息處理流程必須在 Plugin Gateway 層級攔截並 return。
> - **禁止行為**：禁止為了便利將未綁定訊息先送入 LLM 再由 LLM 提示用戶綁定。
> - **驗證方法**：檢查 LiteLLM / LLM Gateway API log，確認 Case 2.1 觸發時**沒有任何 Request 送達 LLM**。

---

## 4. 子任務 3：語音 / 檔案 / 圖片 多媒體處理 (Media Handling)

### 4.1 測試目標與行為定義
1. **多媒體下載與快取**：`_download_media` 正確下載 Image/Audio/File 並填寫 `media_urls`/`media_types`。
2. **圖片處理**：自動整合 Vision 工具 (`ask_vision` 或原生 Vision tool) 進行圖文分析。
3. **檔案處理**：針對白名單檔案格式（`.pdf`, `.docx`, `.xlsx`, `.txt`, `.csv`）解析內文並餵給 Agent。
4. **語音處理**：調用 Whisper STT 轉錄文字，Agent 回應；若啟用 TTS 則回傳語音檔。
5. **不支援格式防護**：非白名單副檔名（如 `.exe`, `.sh`, `.zip`）予以安全攔截。

### 4.2 合成 Webhook 測試腳本

```python
import time, json

def test_subtask_3(secret: str, bound_user_id: str):
    ts = int(time.time() * 1000)

    # Case 3.1: 圖片訊息
    evt_3_1 = {
        "type": "message",
        "webhookEventId": f"evt-3-1-{ts}",
        "timestamp": ts,
        "source": {"type": "user", "userId": bound_user_id},
        "replyToken": "dummy-token-3-1",
        "mode": "active",
        "message": {"id": f"msg-img-{ts}", "type": "image", "contentProvider": {"type": "line"}}
    }

    # Case 3.2: 白名單 PDF 檔案訊息
    evt_3_2 = {
        "type": "message",
        "webhookEventId": f"evt-3-2-{ts}",
        "timestamp": ts,
        "source": {"type": "user", "userId": bound_user_id},
        "replyToken": "dummy-token-3-2",
        "mode": "active",
        "message": {
            "id": f"msg-file-{ts}",
            "type": "file",
            "fileName": "financial_report.pdf",
            "fileSize": 524288
        }
    }

    # Case 3.3: 語音訊息 (M4A)
    evt_3_3 = {
        "type": "message",
        "webhookEventId": f"evt-3-3-{ts}",
        "timestamp": ts,
        "source": {"type": "user", "userId": bound_user_id},
        "replyToken": "dummy-token-3-3",
        "mode": "active",
        "message": {"id": f"msg-audio-{ts}", "type": "audio", "duration": 4000, "contentProvider": {"type": "line"}}
    }

    # Case 3.4: 非白名單可執行檔 (.exe)
    evt_3_4 = {
        "type": "message",
        "webhookEventId": f"evt-3-4-{ts}",
        "timestamp": ts,
        "source": {"type": "user", "userId": bound_user_id},
        "replyToken": "dummy-token-3-4",
        "mode": "active",
        "message": {
            "id": f"msg-exe-{ts}",
            "type": "file",
            "fileName": "malware.exe",
            "fileSize": 1048576
        }
    }
```

### 4.3 明確通過 / 失敗判斷標準

| 測試案例 | 快取與轉錄檔檢查 | 工具調用紀錄 | 回覆標準 |
| :--- | :--- | :--- | :--- |
| **Case 3.1 (圖片)** | `/tmp/hermes/media/` 存在圖片快取 | 調用 `vision` / `ask_vision` | **PASS**: 回應包含對圖片視覺內容的正確描述 |
| **Case 3.2 (PDF 白名單)** | 本地存在 `.pdf` 檔與提取之 `.txt` 內文 | 調用 `file` / `extract_text` | **PASS**: 根據 PDF 提取內容回答問答 |
| **Case 3.3 (語音)** | 本地存在 `.m4a` 檔與 Whisper 轉錄結果 | 調用 `transcribe_audio` (Whisper) | **PASS**: 正確轉錄語音文字並回應 |
| **Case 3.4 (EXE 非白名單)** | **不允許**傳遞至 LLM 工具鏈 | 無相關解析工具執行 | **PASS**: 回覆「不支援該檔案格式」提示 |

### 4.4 🚨 絕對不能退步的安全防線 (CRITICAL SAFETY)
> [!CAUTION]
> **嚴格白名單過濾與多媒體快取自動清理**
> - **副檔名白名單限制**：僅允許 `pdf`, `doc`, `docx`, `xls`, `xlsx`, `ppt`, `pptx`, `txt`, `csv`。其餘可執行或腳本副檔名（如 `exe`, `sh`, `bat`, `py`, `js`）必須被硬性拒絕。
> - **磁碟爆滿防護**：所有下載之語音/圖片/檔案暫存檔必須於對話 Session 結束或背景 Job 處理完成後被自動清理，防止多媒體檔案擠爆硬碟。

---

## 5. 子任務 4：Soul / Skill 個人化 Hook (Soul & Skill Personalization)

### 5.1 測試目標與行為定義
1. **身分關聯載入**：用戶完成身分綁定後，`pre_llm_call` hook 提取對應的 `employee_id`。
2. **動態 Prompt 疊加**：從 `admin.db` 讀取該員工所屬部門之 Soul 指令與個人 Skill 專長，動態注入此輪 LLM 請求的 System Prompt 中。

### 5.2 合成 Webhook 測試腳本

```python
import time, json

def test_subtask_4(secret: str, bound_user_id: str):
    ts = int(time.time() * 1000)

    # Case 4.1: 已綁定員工發送特定領域專業問題
    evt_4_1 = {
        "type": "message",
        "webhookEventId": f"evt-4-1-{ts}",
        "timestamp": ts,
        "source": {"type": "user", "userId": bound_user_id},
        "replyToken": "dummy-token-4-1",
        "mode": "active",
        "message": {"id": f"msg-4-1-{ts}", "type": "text", "text": "請依照我的部門規範審核這筆報支"}
    }
```

### 5.3 明確通過 / 失敗判斷標準

| 檢查項目 | 通過標準 (PASS) | 失敗標準 (FAIL) |
| :--- | :--- | :--- |
| **System Prompt 檢查** | 於 debug 日誌或 LLM Gateway payload 中確認包含 `[Employee Soul: ...]` 與 `[Skills: ...]` | System Prompt 僅有預設通用 Prompt，未載入員工個人化配置 |
| **對話行為回應** | 答案呈現該員工設定之對話風格與部門審核原則 | 呈現通用無個性化回答 |

### 5.4 🚨 絕對不能退步的安全防線 (CRITICAL SAFETY)
> [!CAUTION]
> **員工 Soul/Skill 脈絡嚴格隔離**
> - **禁止越權/洩漏**：員工 A 進行對話時，System Prompt **絕不可**混入員工 B 的 Soul 指令或私有 Skill 權限。

---

## 6. 子任務 5：Slash Commands 與內建指令對應 (Slash Commands)

### 6.1 測試目標與行為定義
1. **記憶清除指令 (`/清除記憶` 或 `/new`)**：重置當前 Channel / Session 的歷史對話紀錄。
2. **排程任務指令 (`/定期任務 <描述>`)**：正確解析並呼叫 `cronjob` / 排程工具建立定時任務。

### 6.2 合成 Webhook 測試腳本

```python
import time, json

def test_subtask_5(secret: str, bound_user_id: str):
    ts = int(time.time() * 1000)

    # Case 5.1: 發送 /new 清除對話記憶
    evt_5_1 = {
        "type": "message",
        "webhookEventId": f"evt-5-1-{ts}",
        "timestamp": ts,
        "source": {"type": "user", "userId": bound_user_id},
        "replyToken": "dummy-token-5-1",
        "mode": "active",
        "message": {"id": f"msg-5-1-{ts}", "type": "text", "text": "/new"}
    }

    # Case 5.2: 發送 /定期任務 建立自動化排程
    evt_5_2 = {
        "type": "message",
        "webhookEventId": f"evt-5-2-{ts}",
        "timestamp": ts,
        "source": {"type": "user", "userId": bound_user_id},
        "replyToken": "dummy-token-5-2",
        "mode": "active",
        "message": {"id": f"msg-5-2-{ts}", "type": "text", "text": "/定期任務 每天早上9點發送專案進度摘要"}
    }
```

### 6.3 明確通過 / 失敗判斷標準

| 測試案例 | `state.db` 狀態變更 | 工具調用與回覆判斷 |
| :--- | :--- | :--- |
| **Case 5.1 (`/new` 或 `/清除記憶`)** | **PASS**: 舊 Session 狀態設為 archived，建立全新 Session ID | **PASS**: 回覆「對話記憶已重置/已開啟新對話」 |
| **Case 5.2 (`/定期任務`)** | **PASS**: 排程資料庫新增一筆定時任務紀錄 | **PASS**: 成功呼叫 `cronjob` 工具並回傳排程設定成功 |

### 6.4 🚨 絕對不能退步的安全防線 (CRITICAL SAFETY)
> [!CAUTION]
> **記憶清除範圍防護**
> - **隔離作用域**：`/new` 或 `/清除記憶` **只能清除發送者自己的 Session** (`usr:{userId}`) 或當前群組 (`grp:{groupId}`)。**絕對禁止**誤刪資料庫全域歷史或其他用戶的對話紀錄。

---

## 7. 子任務 6：Cron 推播 (F3 每日群組摘要與排程報告)

### 7.1 測試目標與行為定義
1. **動態群組多目標推送**：讀取 `line_ingestion` 追蹤的群組清單，逐一呼叫 LINE Push API 發送 F3 每日摘要。
2. **個人定時任務推播**：根據 `/定期任務` 排程時間到達時，推送文字/報告給指定用戶或 Channel。
3. **配額耗盡降級處理 (429 Rate Limit Handling)**：LINE 免費 Push 配額用罄 (429) 時，應優雅記錄 Log，禁止卡死或死循環重試。

### 7.2 模擬發送測試腳本

```python
import urllib.request, json

def test_subtask_6_cron_push(internal_secret: str):
    # 模擬外部 Cron 或排程器發起推送請求
    url = "http://127.0.0.1:8646/internal/report"
    payload = {
        "target_type": "group_summary",
        "secret": internal_secret
    }
    # 測試內部推送 Endpoint 回應狀態
```

### 7.3 明確通過 / 失敗判斷標準

| 測試指標 | 通過標準 (PASS) | 失敗標準 (FAIL) |
| :--- | :--- | :--- |
| **目標群組覆蓋率** | `line_ingestion` 所有活躍群組皆收到對應的動態摘要報告 | 漏推部分群組或推送到未註冊群組 |
| **LINE Push API 回應** | HTTP 200 (配額內) 或 429 降級捕獲並記錄告警 Log | 噴出 Unhandled Exception 或推播服務崩潰 |

### 7.4 🚨 絕對不能退步的安全防線 (CRITICAL SAFETY)
> [!CAUTION]
> **推播目標隔離與 429 配額保護**
> - **推送目標錯置防護**：群組 A 的摘要內容**絕不可**推送到群組 B 或個人用戶；Push 目標 ID 必須嚴格驗證。
> - **配額耗盡不崩潰**：撞到 LINE 429 (Monthly quota exceeded) 時，必須將任務標記為降級跳過並輸出警告，**絕不可拋出未捕獲例外卡死 Cron 排程器**。

---

## 8. 驗收總結對照矩陣 (Acceptance Summary Matrix)

| 子任務編號 | 核心驗收功能 | 合成測試案例數量 | 關鍵驗證資料庫 / 日誌 | 絕對不可退步之安全鐵則 |
| :---: | :--- | :---: | :--- | :--- |
| **1** | 群組轉發 + F4 Mention 閘門 | 3 | `line_ingestion.raw_line_messages`, `hermes.state.db` | **F4 群組記憶必須隔離在 `grp:{groupId}`，嚴禁洩漏至個人記憶** |
| **2** | 身分綁定閘門 | 3 | `admin.db (identity_map)`, LLM Request Log | **未綁定用戶絕對不允許進入 LLM 核心** |
| **3** | 多媒體處理 (圖/文/音/檔) | 4 | `/tmp/hermes/media/`, Whisper/Vision log | **副檔名白名單防護、多媒體快取自動清理** |
| **4** | Soul / Skill 個人化 Hook | 1 | LLM System Prompt Payload | **跨員工 Soul/Skill 配置嚴格隔離** |
| **5** | Slash Commands 對應 | 2 | `hermes.state.db`, Cron tool log | **記憶清除僅能作用於當前 Session，禁止全域誤刪** |
| **6** | Cron 推播 (F3 與排程報告) | 2 | LINE Push Log, Cron Task Scheduler | **推播目標 ID 嚴格校驗，429 配額耗盡優雅降級** |

---

> **簽核記錄**：本驗收清單已完成定稿，等待所有子任務程式碼完全落地後，將由 QA Agent 依照本文件進行全量實測。
