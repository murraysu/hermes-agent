# (本檔原本只有 GitNexus 自動產生的區塊，已於 2026-08-02 退役時清空)

## 怎麼在 svc1 上跑測試

⚠️ **測試是跑得起來的。不要因為 `.venv` 是空的、或映像裡沒有 pytest/pip 就下結論說
「這台沒有 dev 環境」。** 2026-08-15 我就是這樣誤判，連續四次重建映像上線都沒跑測試，
其中一次（r5）把整個 dashboard 弄成全白。

`.venv` 沒有安裝專案依賴，直接 `python -m pytest` 一定失敗。用 `uv run --with` 疊上
臨時依賴即可，**不會動到 `.venv`**（重要：`uv sync` 必須保住 `--extra honcho`，
不要為了跑測試去 sync）。

### 後端

```bash
cd ~/hermes-agent
uv run --no-sync --with pytest --with fastapi --with httpx \
       --with pyyaml --with python-multipart \
       python -m pytest tests/hermes_cli/test_dashboard_auth_prefix.py -q
```

缺的依賴就這四個。純邏輯的測試（例如 `tests/test_orchestrator_toolsets.py` 只 import
`toolsets`）連 fastapi 都不用，`--with pytest` 就夠。

### 前端

```bash
cd ~/hermes-agent/web
npm ci      # node_modules 預設不存在；node 由 nvm 提供（v24）
npm test    # vitest run —— 20 檔 135 測試
```

`npm run build` 不必在本機跑：Docker build 裡已經包含前端建置。

### 部署前至少要跑的

改到 `hermes_cli/dashboard_auth/`、`web/`、`toolsets.py` 時：

```bash
uv run --no-sync --with pytest --with fastapi --with httpx --with pyyaml \
       --with python-multipart python -m pytest \
  tests/hermes_cli/test_dashboard_auth_password_login.py \
  tests/hermes_cli/test_dashboard_auth_prefix.py \
  tests/hermes_cli/test_dashboard_auth_401_reauth.py \
  tests/hermes_cli/test_dashboard_auth_cookies.py \
  tests/hermes_cli/test_dashboard_auth_middleware.py \
  tests/test_orchestrator_toolsets.py \
  tests/test_spa_index_head_injection.py -q
```

## 驗證前端改動時的陷阱

⚠️ **curl 對每個資源 URL 拿到 200，不代表瀏覽器會去要它。** r5 的全白事故裡，所有
資源 URL 都是好的——壞的是它們在 HTML 裡不再是標籤（被吞進註解）。要檢查的是**剝掉
HTML 註解之後標籤還在不在**，`tests/test_spa_index_head_injection.py` 就是在守這件事。

另一個有效的訊號：nginx access log 裡瀏覽器對 JS 發出**零個請求**（而不是 404），
代表 HTML 結構壞了，不是路徑錯了。
