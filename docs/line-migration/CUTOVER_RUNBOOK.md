# LINE OA 切換正式 Runbook：channel_gw → hermes-agent 原生 LINE Plugin

> **文件狀態**：定稿  
> **建立日期**：2026-07-31  
> **適用分支**：`fix/line-qa-findings`（所有程式碼已整合驗收通過）  
> **母規格文件**：[PLAN.md](file:///home/murray/hermes-agent/docs/line-migration/PLAN.md)  
> **驗收清單**：[QA_CHECKLIST.md](file:///home/murray/hermes-agent/docs/line-migration/QA_CHECKLIST.md)  
> **實作筆記**：[IMPLEMENTATION_NOTES.md](file:///home/murray/hermes-agent/docs/line-migration/IMPLEMENTATION_NOTES.md)  

---

## 0. 前言

本 Runbook 僅適用於**所有子任務驗收完全通過**後的生產切換階段。切換期間**絕對禁止**提前修改 nginx 或停用 channel_gw — channel_gw 是目前唯一已知能運作的 LINE 路徑。

### 名詞對照

| 名詞 | 說明 |
|------|------|
| `hermes-agent-sm121` | Docker container name，hermes-agent 生產容器 |
| `hermes-nginx` | Docker container name，nginx 反向代理容器 |
| `172.22.0.1` | Docker bridge 網關地址（host.docker.internal 等效） |
| `LINE_PORT` | hermes-agent LINE webhook 監聽端口，預設 8646 |
| `line_ingestion` | LINE 群組事件 ingestion 服務，運行於 host，監聽 172.22.0.1:8092 |
| `admin.db` | Admin Panel 共享 SQLite 資料庫，唯讀掛載 |
| `state.db` | hermes-agent 內部 SQLite session store，位於容器 `/opt/data/state.db` |

---

## 1. 切換前的設定變更

> **⚠️ 此節為驗收期間發現的部署缺口，切換前必須補齊。**  
> 目前 `docker-compose.yml` 中的 `hermes-agent` service **缺少**以下設定。

### 1.1 挂载 admin.db（唯讀）

**實際主機路徑**：`/home/murray/ai-data/admin-panel/admin.db`

**容器內掛載點**：`/home/murray/ai-data/admin-panel/admin.db`（與主機路徑相同）

**修改目標**：`docker-compose.yml` → `services.hermes-agent.volumes`

**加入行**：
```yaml
      - /home/murray/ai-data/admin-panel/admin.db:/home/murray/ai-data/admin-panel/admin.db:ro
```

**驗證指令**：
```bash
docker exec hermes-agent-sm121 ls -la /home/murray/ai-data/admin-panel/admin.db
```
預期輸出：檔案存在且權限可讀。

### 1.2 設定 ADMIN_DB_PATH

**修改目標**：`docker-compose.yml` → `services.hermes-agent.environment`

**加入行**：
```yaml
      - ADMIN_DB_PATH=/home/murray/ai-data/admin-panel/admin.db
```

**說明**：`identity.py` 的 `DEFAULT_ADMIN_DB` 預設為 `~/ai-data/admin-panel/admin.db`，但容器內的 `~` 可能與主機不同，**必須**明確設定此環境變數。若未設定且預設路徑不存在，plugin 會發出 `WARNING` 日誌，所有使用者都會被判定為未綁定 → bot 對誰都不回話。

**驗證指令**：
```bash
docker exec hermes-agent-sm121 python -c "
from plugins.platforms.line.identity import IdentityResolver
r = IdentityResolver()
print('admin_db:', r._admin_db)
print('exists:', r._admin_db.exists())
"
```
預期輸出：`exists: True`，且無 `WARNING` 日誌。

### 1.3 設定 LINE_GROUP_QA_ALLOWLIST

**修改目標**：`docker-compose.yml` → `services.hermes-agent.environment`

**加入行**：
```yaml
      - LINE_GROUP_QA_ALLOWLIST=C16015a313fd45352dc63fdb63a1f45de
```

**說明**：此值來源為 `channel_gw` 現行的 `LINE_GROUP_QA_ALLOWLIST`（F4 群組問答的允許清單）。群組訊息只有在此清單內**且** @到 bot 本身時才會觸發 agent 回覆。空值採 fail-closed，不會讓任何群組觸發 agent。

**取得方式**：從 `~/ai-stack/config/rendered/svc1/channel-gw/channel-gw.env` 讀取 `LINE_GROUP_QA_ALLOWLIST` 的值。

### 1.4 設定 LINE_INGESTION_FORWARD_URL

**修改目標**：`docker-compose.yml` → `services.hermes-agent.environment`

**加入行**：
```yaml
      - LINE_INGESTION_FORWARD_URL=http://172.22.0.1:8092/internal/line-events
```

**說明**：群組/room 事件轉發目標。此值與 channel_gw 現行設定一致（`172.22.0.1:8092` 是 line_ingestion 在 docker bridge 上的地址）。hermes-agent 容器與 line_ingestion 都在同一個 docker bridge 網路（172.22.0.0/16），因此可以直接透過 `172.22.0.1` 存取。

**取得方式**：從 `~/ai-stack/config/rendered/svc1/channel-gw/channel-gw.env` 讀取 `LINE_INGESTION_FORWARD_URL` 的值。**不要將此值寫死在文件內**，而是從 env 檔取得。

### 1.5 設定 LINE_INGESTION_INTERNAL_SECRET

**修改目標**：`docker-compose.yml` → `services.hermes-agent.environment`

**加入行**：
```yaml
      - LINE_INGESTION_INTERNAL_SECRET=<從 channel-gw.env 或 vault 取得>
```

**說明**：轉發時用於 `X-Internal-Secret` header 的驗證 secret。**絕對不要將 secret 明文寫進文件或 docker-compose.yml**。

**取得方式**：從 `~/ai-stack/config/rendered/svc1/channel-gw/channel-gw.env` 讀取 `LINE_INGESTION_INTERNAL_SECRET` 的值，或從 Murray 的 vault 取得。

### 1.6 設定 LINE_BOT_USER_ID（選填，fallback）

**修改目標**：`docker-compose.yml` → `services.hermes-agent.environment`

**加入行**：
```yaml
      - LINE_BOT_USER_ID=Uf60a97fd41e8fb5264f4db0d9adecb17
```

**說明**：@mention 檢測的 fallback bot user ID。正常情況下 adapter 會在連線時從 LINE API 取得 bot 的 user ID；此值僅在 API 取得失敗時使用。

**取得方式**：從 `~/ai-stack/config/rendered/svc1/channel-gw/channel-gw.env` 讀取 `LINE_BOT_USER_ID` 的值。

### 1.7 設定 ADMIN_PANEL_URL（選填）

**修改目標**：`docker-compose.yml` → `services.hermes-agent.environment`

**加入行**：
```yaml
      - ADMIN_PANEL_URL=http://172.22.0.1:8888
```

**說明**：Admin Panel 的 `/api/bind` endpoint 基礎 URL。**注意**：channel_gw 的 env 使用 `http://localhost:8888`，但在 docker container 內 `localhost` 指的是容器本身而非 host，因此必須改為 `http://172.22.0.1:8888`（docker bridge 網關）。

### 1.8 確認 LINE_PORT=8646 讓 nginx 能連到

**目前狀況**：`docker-compose.yml` 中 `hermes-agent` 的 `ports:` 僅發佈 `3001:9119` 和 `8001:8642`，**未發佈 8646**。

**說明**：這是**刻意不發佈**到 host 的設計，nginx 透過 docker 內部網路（172.22.0.0/16）直接連到 `hermes-agent-sm121:8646`，而**不需要**開放 host port。LINE 的 webhook URL 是 `https://ai.murray.tw/webhook/line`，LINE 伺服器連到 nginx 的 80/443，nginx 再轉發到容器內的 8646。

**驗證指令**：
```bash
docker exec hermes-nginx wget -qO- --spider http://hermes-agent-sm121:8646/line/webhook/health 2>&1
```
預期輸出：`200 OK`（或 health endpoint 回應）。

### 1.9 移除 LINE_ALLOWED_USERS（選填）

**修改目標**：`docker-compose.yml` → `services.hermes-agent.environment`

**說明**：目前 `docker-compose.yml` 中有一個臨時的 `LINE_ALLOWED_USERS` 環境變數（標註「TEMPORARY for Phase B.3 live-traffic smoke test」）。切換到正式環境時，身分綁定閘門（identity binding gate）已提供真正的 allowlist 檢查，此臨時變數應移除。

**加入行**（註解掉或刪除）：
```yaml
      # REMOVED: LINE_ALLOWED_USERS — 由 identity binding gate 取代
```

---

## 2. nginx 切換步驟

> **⚠️ 此節為不可逆操作。執行前請確認所有驗收通過。**  
> **⚠️ nginx.conf 是單檔 bind-mount 的 inode 陷阱：修改後必須 `docker restart hermes-nginx`，不能只 `reload`。**

### 2.1 確認當前狀況

**檔案**：`nginx.conf`

**目前 `/webhook/line` 代理目標**（**兩處 server block**）：
1. **`:80` server block**（`ai.murray.tw`）：`proxy_pass http://channel_gw/webhook/line;`
2. **`:443` server block**（default）：`proxy_pass http://channel_gw/webhook/line;`

**hermes_line upstream**：nginx.conf 中**尚未定義** `hermes_line` upstream。此 upstream 需要新增，指向 `hermes-agent-sm121:8646`。

### 2.2 步驟 1 — 新增 hermes_line upstream

**修改目標**：`nginx.conf` → `http` block → upstreams section

**在 `upstream channel_gw { ... }` 之後加入**：
```nginx
    upstream hermes_line {
        server hermes-agent-sm121:8646;
    }
```

### 2.3 步驟 2 — 修改 :80 server block 的 `/webhook/line`

**修改目標**：`nginx.conf` → `:80` server block → `location /webhook/line`

**將**：
```nginx
        location /webhook/line {
            proxy_pass http://channel_gw/webhook/line;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
        }
```

**改為**：
```nginx
        location /webhook/line {
            proxy_pass http://hermes_line/line/webhook;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
        }
```

**說明**：nginx 的 `proxy_pass` 包含 URI（`/line/webhook`）時，會將 matched location prefix（`/webhook/line`）替換為 proxy_pass 中的 URI。因此 `/webhook/line` → `http://hermes_line/line/webhook`。hermes-agent 的 LINE plugin 在 `/line/webhook` 監聽 webhook。

### 2.4 步驟 3 — 修改 :443 server block 的 `/webhook/line`

**修改目標**：`nginx.conf` → `:443` server block → `location /webhook/line`

**將**：
```nginx
        location /webhook/line {
            proxy_pass http://channel_gw/webhook/line;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
```

**改為**：
```nginx
        location /webhook/line {
            proxy_pass http://hermes_line/line/webhook;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
```

### 2.5 步驟 4 — 重啟 nginx（不可只 reload）

```bash
docker restart hermes-nginx
```

**等待指令**：
```bash
docker wait hermes-nginx && echo "nginx restarted"
```

**驗證指令**：
```bash
docker exec hermes-nginx nginx -t 2>&1
```
預期輸出：`syntax is ok` / `test is successful`。

```bash
docker logs hermes-nginx --since 30s | grep -i "error\|fail" | tail -5
```
預期輸出：無 error/fail 訊息。

---

## 3. LINE 平台後台需要改的設定

### 3.1 Webhook URL 是否需要變更？

**不需要**。LINE 平台後台的 webhook URL 保持不變：`https://ai.murray.tw/webhook/line`。

**理由**：nginx 的路由路徑 `/webhook/line` 不變，只有 proxy_pass 的後端從 `channel_gw` 換到 `hermes_line`。LINE 平台只看 URL，無需修改。

### 3.2 確認事項

執行切換後，在 LINE Developers Console（https://developers.line.biz/console/）確認：

1. **Webhook URL** 仍為 `https://ai.murray.tw/webhook/line`
2. **Use webhook** 為啟用（✓）
3. **Channel secret** 與 `docker-compose.yml` 中的 `LINE_CHANNEL_SECRET` 一致

**驗證指令**（從 LINE API 驗證 webhook 連線）：
```bash
curl -X POST "https://api.line.me/v2/bot/channel/webhook/test" \
  -H "Authorization: Bearer ${LINE_CHANNEL_ACCESS_TOKEN}" \
  -H "Content-Type: application/json"
```
預期回應：`{"success":true,...}`

---

## 4. 切換後的煙霧測試

> **每項測試都必須有可執行的檢查指令。測試順序務必依序執行。**

### 4.1 測試 1：1:1 真實訊息能收到回覆

**步驟**：
1. 使用已綁定的 LINE 帳號，傳送任意文字訊息給小助理。
2. 等待 10-30 秒，確認收到 agent 回覆。

**檢查指令**：
```bash
docker logs hermes-agent-sm121 --since 2m 2>&1 | grep "LINE:" | tail -20
```
預期：日誌中出現 `LINE: dispatching message` 或類似字眼，表示 webhook 收到訊息。

```bash
docker exec hermes-agent-sm121 sqlite3 /opt/data/state.db \
  "SELECT session_id, created_at FROM sessions WHERE session_id LIKE 'line:dm:%' ORDER BY created_at DESC LIMIT 5;"
```
預期：出現對應 user_id 的 session，格式如 `line:dm:U...`。

```bash
curl -s -X GET "https://api.line.me/v2/bot/message/{$REPLY_TOKEN}" \
  -H "Authorization: Bearer ${LINE_CHANNEL_ACCESS_TOKEN}"
```
（若有 reply token 可用）預期：回覆內容存在。

### 4.2 測試 2：群組訊息有進 line_ingestion

**步驟**：
1. 在 F4 群組（`C16015a313fd45352dc63fdb63a1f45de`）傳送普通文字訊息（**不** @bot）。
2. 等待 5 秒。

**檢查指令**：
```bash
docker exec -it line-ingestion-container psql -h 172.22.0.1 -U line_user -d line_ingestion \
  -c "SELECT message_id, group_id, created_at FROM raw_line_messages WHERE group_id = 'C16015a313fd45352dc63fdb63a1f45de' AND created_at > NOW() - INTERVAL '5 minutes' ORDER BY created_at DESC LIMIT 5;"
```
預期：出現剛發送的訊息記錄。

> **注意**：`line-ingestion-container` 需替換為實際的 line_ingestion container name 或 host 上的 psql。若 line_ingestion 直接運行在 host，請使用 `psql` 直接連接。

### 4.3 測試 3：@bot 有回應

**步驟**：
1. 在 F4 群組內 @ 小助理 發送問題。
2. 等待 10-30 秒，確認收到 agent 回覆。

**檢查指令**：
```bash
docker logs hermes-agent-sm121 --since 2m 2>&1 | grep "LINE:" | tail -20
```
預期：日誌中出現 `mention` 或 `isSelf` 相關字眼。

```bash
docker exec hermes-agent-sm121 sqlite3 /opt/data/state.db \
  "SELECT session_id FROM sessions WHERE session_id LIKE 'grp:C16015a313fd45352dc63fdb63a1f45de' ORDER BY created_at DESC LIMIT 5;"
```
預期：出現 `grp:C16015a313fd45352dc63fdb63a1f45de` 的 session。

**⚠️ 安全防線檢查**：確認群組對話**未**混入個人 1:1 session：
```bash
docker exec hermes-agent-sm121 sqlite3 /opt/data/state.db \
  "SELECT session_id FROM sessions WHERE session_id LIKE 'line:dm:%' AND created_at > NOW() - INTERVAL '5 minutes';"
```
預期：**不應**出現與此群組對話相關的 `line:dm:` session。

### 4.4 測試 4：未綁定用戶被正確攔截

**步驟**：
1. 使用**未綁定**的 LINE 帳號，傳送任意文字訊息。
2. 確認收到「請輸入 Admin Panel Nickname 進行身分綁定」的提示。
3. 確認**沒有** LLM 回應。

**檢查指令**：
```bash
docker logs hermes-agent-sm121 --since 2m 2>&1 | grep -i "binding\|unbound\|identity\|awaiting_nickname" | tail -10
```
預期：日誌中出現 `awaiting_nickname` 或 `identity` 相關字眼，表示綁定流程觸發。

```bash
docker exec hermes-agent-sm121 sqlite3 /opt/data/state.db \
  "SELECT COUNT(*) FROM sessions WHERE session_id LIKE 'line:dm:%' AND created_at > NOW() - INTERVAL '5 minutes';"
```
預期：**0**（未綁定用戶不應建立任何 session）。

**LLM 檢查**：
```bash
docker logs hermes-agent-sm121 --since 2m 2>&1 | grep -i "chat.completions\|LLM\|LiteLLM\|token" | tail -10
```
預期：**無**任何 LLM 呼叫記錄。

---

## 5. 回滾程序

> **⚠️ 回滾必須在幾分鐘內完成。channel_gw 在整個回滾期間保持運行。**  
> **⚠️ 回滾是安全的、可逆的操作。**

### 5.1 步驟 1 — 恢復 nginx.conf

**修改目標**：`nginx.conf`

**撤銷 2.2 的變更** — 刪除 `hermes_line` upstream：
```nginx
    upstream hermes_line {
        server hermes-agent-sm121:8646;
    }
```

**撤銷 2.3 的變更** — `:80` server block 的 `/webhook/line`：
```nginx
        location /webhook/line {
            proxy_pass http://channel_gw/webhook/line;
            ...
        }
```

**撤銷 2.4 的變更** — `:443` server block 的 `/webhook/line`：
```nginx
        location /webhook/line {
            proxy_pass http://channel_gw/webhook/line;
            ...
        }
```

### 5.2 步驟 2 — 重啟 nginx

```bash
docker restart hermes-nginx
```

### 5.3 步驟 3 — 驗證 channel_gw 恢回

```bash
docker exec hermes-nginx nginx -t 2>&1
```
預期：`syntax is ok` / `test is successful`。

```bash
docker exec -it channel-gw-container curl -s http://172.22.0.1:5000/health 2>&1
```
（或 `docker exec channel_gw curl -s http://172.22.0.1:5000/health`）  
預期：`200 OK` 或 health endpoint 回應。

```bash
docker logs hermes-nginx --since 30s | grep "channel_gw" | tail -5
```
預期：nginx 日誌顯示代理到 `channel_gw` 的記錄。

### 5.4 回滾完成

回滉後，所有 LINE 流量自動恢復到 channel_gw，無需其他操作。hermes-agent 的 LINE plugin 保持運行，不會接收公網流量。

---

## 6. 退役 channel_gw（回滾窗口過後才執行）

> **⚠️ 此節為不可逆操作。執行前必須確認穩定運行 48 小時以上。**  
> **⚠️ 退役採「disable 不 remove」原則，保留所有資料與程式碼。**

### 6.1 穩定性觀察期

**觀察時間**：**48 小時**（至少）

**觀察指標**：

| 指標 | 目標 | 檢查指令 |
|------|------|----------|
| 訊息回覆成功率 | ≥ 99% | `docker logs hermes-agent-sm121 --since 48h 2>&1 \| grep "LINE:" \| grep -c "dispatch"` |
| 群組轉發成功率 | ≥ 99% | `docker exec line-ingestion-container psql -c "SELECT COUNT(*) FROM raw_line_messages WHERE created_at > NOW() - INTERVAL '48 hours';"` |
| 未綁定攔截成功率 | 100% | `docker logs hermes-agent-sm121 --since 48h 2>&1 \| grep -c "awaiting_nickname"` |
| 無 5xx 錯誤 | 0 次 | `docker logs hermes-nginx --since 48h 2>&1 \| grep -c "502\|503\|504"` |
| 用戶無投訴 | 0 通 | 向 Murray 確認是否有用戶反映問題 |

### 6.2 退役步驟

#### 步驟 1 — disable channel_gw service

**如果 channel_gw 是 systemd service**：
```bash
sudo systemctl stop channel-gw
sudo systemctl disable channel-gw
```

**如果 channel_gw 是 docker container**：
```bash
docker stop channel_gw
docker rm channel_gw
```

**如果 channel_gw 是 docker-compose service**：
```bash
cd ~/ai-stack/code/home_gateway/channel_gw
docker compose stop
```

#### 步驟 2 — 保留資料與程式碼

**資料保留**：
- `~/ai-stack/code/home_gateway/channel_gw/` — 程式碼保留
- `~/ai-stack/config/rendered/svc1/channel-gw/` — config 保留
- `~/ai-data/channel-gw/runtime.db` — runtime 資料保留

**說明**：保留資料與程式碼以便未來回滉。回滉只需要重新啟動 channel_gw 並恢復 nginx.conf 的 proxy_pass。

#### 步驟 3 — 清理 nginx.conf 中的 channel_gw 參考（可選）

**修改目標**：`nginx.conf`

**刪除 `upstream channel_gw`**：
```nginx
    upstream channel_gw {
        server 172.22.0.1:5000;
    }
```

**刪除 `/files/` location**（如果確定 hermes-agent 有對等的檔案服務）：
```nginx
        location /files/ {
            proxy_pass http://channel_gw/files/;
            proxy_set_header Host $host;
        }
```

**說明**：此步驟可選，僅清理不再使用的 upstream 定義。建議在退役後 1 週確認無問題後執行。

#### 步驟 4 — 更新 docker-compose.yml

**修改目標**：`docker-compose.yml`

**刪除 `CHANNEL_GW_URL` 環境變數**（如果不再需要）：
```yaml
      - CHANNEL_GW_URL=http://172.22.0.1:5000
```

---

## 7. 緊急聯絡

| 問題類型 | 聯絡方式 |
|----------|----------|
| LINE webhook 接收失敗 | 檢查 `docker logs hermes-nginx` + `docker logs hermes-agent-sm121` |
| 群組轉發失敗 | 檢查 `LINE_INGESTION_FORWARD_URL` + line_ingestion 服務狀態 |
| 身分綁定失敗 | 檢查 `ADMIN_DB_PATH` + `admin.db` 是否可讀 |
| 回滉緊急 | 執行 §5 回滉程序，channel_gw 保持運行即可 |

---

## 附錄 A. docker-compose.yml 完整修改範例

以下是 `hermes-agent` service 在切換前需要修改的完整 `environment` + `volumes` 區段：

```yaml
  hermes-agent:
    build: .
    image: hermes-agent-hermes-agent:latest
    container_name: hermes-agent-sm121
    ports:
      - "3001:9119"
      - "8001:8642"
    environment:
      - HERMES_HOME=/opt/data
      - HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist
      - PYTHONUNBUFFERED=1
      - HERMES_DASHBOARD=1
      - HERMES_DASHBOARD_HOST=0.0.0.0
      - HERMES_DASHBOARD_PORT=9119
      - HERMES_DASHBOARD_BASIC_AUTH_USERNAME=${HERMES_DASHBOARD_BASIC_AUTH_USERNAME:?set in .env}
      - HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=${HERMES_DASHBOARD_BASIC_AUTH_PASSWORD:?set in .env}
      - INTENTION_ROUTER_API_KEY=${LITELLM_MASTER_KEY:?set in .env}
      - INTENTION_ROUTER_BASE_URL=http://192.168.101.7:4000/v1
      - CHANNEL_GW_URL=http://172.22.0.1:5000
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - GOOGLE_API_KEY=${GOOGLE_API_KEY:-}
      # LINE platform plugin (Phase B)
      - LINE_CHANNEL_ACCESS_TOKEN=${LINE_CHANNEL_ACCESS_TOKEN:-}
      - LINE_CHANNEL_SECRET=${LINE_CHANNEL_SECRET:-}
      - LINE_PORT=8646
      - LINE_PUBLIC_URL=https://ai.murray.tw
      - LINE_BOT_USER_ID=Uf60a97fd41e8fb5264f4db0d9adecb17
      - LINE_GROUP_QA_ALLOWLIST=C16015a313fd45352dc63fdb63a1f45de
      - LINE_INGESTION_FORWARD_URL=http://172.22.0.1:8092/internal/line-events
      - LINE_INGESTION_INTERNAL_SECRET=${LINE_INGESTION_INTERNAL_SECRET:?set in .env}
      - ADMIN_DB_PATH=/home/murray/ai-data/admin-panel/admin.db
      - ADMIN_PANEL_URL=http://172.22.0.1:8888
    volumes:
      - hermes-data:/opt/data
      - /home/murray/enterprise-skills:/opt/data/skills/enterprise:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /home/murray/ai-data/admin-panel/admin.db:/home/murray/ai-data/admin-panel/admin.db:ro
    command: ["gateway", "run", "--accept-hooks"]
    restart: unless-stopped
```

---

## 附錄 B. nginx.conf 完整修改範例

以下是 `nginx.conf` 中需要修改的部分：

```nginx
    # --- Upstreams ---
    upstream hermes_agent {
        server hermes-agent-sm121:9119;
    }
    upstream hermes_api {
        server hermes-agent-sm121:8642;
    }
    # ... (其他 upstreams 保持不變) ...
    upstream channel_gw {
        server 172.22.0.1:5000;
    }
    upstream hermes_line {          # ← 新增
        server hermes-agent-sm121:8646;
    }
    # ... (其他 upstreams 保持不變) ...

    # ai.murray.tw — Cloudflare Flexible SSL sends HTTP; do NOT redirect
    server {
        listen 80;
        server_name ai.murray.tw;

        location /webhook/line {
            proxy_pass http://hermes_line/line/webhook;   # ← 修改
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
        }
        # ... (其他 location 保持不變) ...
    }

    # ... (HTTP → HTTPS redirect 保持不變) ...

    server {
        listen 443 ssl;
        server_name _;
        # ... (SSL 設定保持不變) ...

        # ... (Web UIs 保持不變) ...

        # Channel Gateway LINE webhook (:5000 on host)
        location /webhook/line {
            proxy_pass http://hermes_line/line/webhook;   # ← 修改
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }

        # ... (其他 location 保持不變) ...
    }
```
