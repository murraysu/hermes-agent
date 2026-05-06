# SM121 System — AI Agent Stack 安裝完成！

## ✅ 已完成

### 1. LiteLLM (LLM Proxy Server)
- **狀態**: ✅ 運行中 (health: starting)
- **端口**: 4000
- **容器**: litellm-sm121
- **圖像**: litellm-sm121:latest (自構建 ARM64)
- **功能**: 統一 LLM API 代理，支援 OpenAI、Anthropic、Google Gemini

### 2. Redis (Cache & Session Storage)
- **狀態**: ✅ 運行中 (healthy)
- **端口**: 6379
- **容器**: hermes-redis
- **功能**: LiteLLM 快取、Hermes-Agent 會話存儲

### 3. CLI-Anything (CLI Package Manager)
- **狀態**: ✅ 運行中
- **容器**: cli-anything-sm121
- **圖像**: cli-anything-sm121:latest (自構建)
- **功能**: 51+ 個 agent-native CLI 界面
- **可用分類**: 3D, AI, 圖像, 視頻, 網絡, 遊戲等 27 個分類

### 4. Hermes-Agent (AI Agent Hub)
- **狀態**: ⏳ Dockerfile 已準備好，待構建
- **端口**: 3000 (Web Dashboard), 8000 (API Gateway)
- **功能**: 自改進 AI 代理，可創建和改善技能

### 5. Agency-Agents (AI Specialists)
- **狀態**: ✅ 已克隆到 /home/murray/agency-agents
- **集成**: 將作為只讀卷掛載到 Hermes-Agent 容器
- **分類**: Engineering, Design, Marketing, Finance, Sales, Strategy 等

## 📋 下一步

### 1. 配置 API 密鑰
```bash
cd /home/murray/hermes-agent
nano .env
```
填寫您的 API 密鑰：
- OPENAI_API_KEY
- ANTHROPIC_API_KEY
- GOOGLE_API_KEY

### 2. 構建 Hermes-Agent (需要 10-15 分鐘)
```bash
cd /home/murray/hermes-agent
docker compose build hermes-agent
```

### 3. 啟動所有服務
```bash
cd /home/murray/hermes-agent
docker compose up -d
```

### 4. 驗證安裝
```bash
# 檢查所有服務狀態
docker compose ps

# 訪問 Hermes-Agent Web 儀表板
open http://localhost:3000

# 測試 LiteLLM
curl http://localhost:4000/model/info

# 瀏覽 CLI-Anything
docker exec -it cli-anything-sm121 bash
cli-hub list
```

## 📁 檔案結構

```
/home/murray/
├── hermes-agent/
│   ├── Dockerfile                  # Hermes-Agent Dockerfile
│   ├── docker-compose.yml          # 所有服務配置
│   ├── nginx.conf                  # Nginx 反向代理配置
│   ├── .env.template               # 環境變數模板
│   ├── .env                        # 環境變數 (需填寫 API 密鑰)
│   ├── start.sh                    # 快速啟動腳本
│   ├── INSTALLATION.md             # 安裝指南
│   └── docker/
│       └── entrypoint.sh           # 容器啟動腳本
│
├── CLI-Anything/
│   ├── Dockerfile                  # CLI-Anything Dockerfile
│   ├── docker-compose.yml          # CLI-Anything compose 配置
│   └── cli-hub/                    # CLI 套件管理器源碼
│
└── agency-agents/
    ├── README.md
    ├── scripts/                    # 安裝腳本
    ├── engineering/                # 工程代理
    ├── design/                     # 設計代理
    ├── marketing/                  # 營銷代理
    └── ...                         # 其他代理分類
```

## 🚀 快速命令參考

### 服務管理
```bash
# 啟動所有服務
docker compose up -d

# 停止所有服務
docker compose down

# 重啟所有服務
docker compose restart

# 查看日誌
docker compose logs -f

# 查看特定服務日誌
docker compose logs -f hermes-agent
docker compose logs -f litellm
```

### CLI-Anything 命令
```bash
# 進入容器
docker exec -it cli-anything-sm121 bash

# 列出所有可用 CLIs
docker exec cli-anything-sm121 cli-hub list

# 搜索特定 CLI
docker exec cli-anything-sm121 cli-hub search blender

# 安裝 CLI
docker exec cli-anything-sm121 cli-hub install <name>

# 更新所有 CLIs
docker exec cli-anything-sm121 cli-hub update
```

### Hermes-Agent 命令
```bash
# 進入容器
docker exec -it hermes-agent-sm121 bash

# 運行 Hermes CLI
docker exec hermes-agent-sm121 hermes --help

# 查看日誌
docker compose logs -f hermes-agent
```

## ⚠️ 注意事項

1. **Hermes-Agent 構建時間**: 由於需要下載 Playwright 瀏覽器，首次構建需要 10-15 分鐘
2. **API 密鑰**: 使用前需要在 `.env` 文件中配置 API 密鑰
3. **磁碟空間**: 確保有足夠的磁碟空間（建議至少 50GB）
4. **記憶體**: Hermes-Agent 需要至少 8GB 記憶體

## 🔗 有用連結

- **Hermes-Agent GitHub**: https://github.com/NousResearch/hermes-agent
- **CLI-Anything GitHub**: https://github.com/HKUDS/CLI-Anything
- **CLI-Hub Web**: https://hkuds.github.io/CLI-Anything/
- **LiteLLM 文檔**: https://docs.litellm.ai/
- **Agency-Agents GitHub**: https://github.com/msitarzewski/agency-agents

## 📞 支援

如果遇到問題，請查看：
1. `docker compose logs -f` 查看服務日誌
2. INSTALLATION.md 中的故障排除章節
3. 各專案的 GitHub Issues
