# 子任務 3：語音/檔案/圖片原生等效查證報告

分支：`feature/line-media`（自 `main` 切出，位於 `/home/murray/hermes-agent-line-media`）

---

## 1. 圖片 (Image)

### 結論：✅ 已原生支援 — 不需重寫

### 證據鏈

1. **下載與快取**：`plugins/platforms/line/adapter.py` 的 `_download_media()` (line 1106-1142) 會呼叫 `cache_image_from_bytes()` (來自 `gateway/platforms/base.py:822`) 將圖片下載並快取到本地路徑，然後填入 `MessageEvent.media_urls`。

2. **MessageEvent 建立**：`_handle_message_event()` (line 979) 建立 `MessageEvent` 時，`media_urls` 包含圖片的本地路徑，`message_type` 為 `MessageType.PHOTO` (adapter.py:170 `_LINE_MESSAGE_TYPES`)。

3. **gateway 處理**：`gateway/run.py` 的 `_prepare_inbound_message_text()` (line ~15085) 檢查 `_event_media_is_image(event, i)` (run.py:2506)，將圖片路徑分類到 `image_paths`。

4. **Vision 工具自動調用**：
   - 若模型支援 vision (native 模式)：圖片路徑存入 `native_image_paths` (run.py:15111-15113)，由 agent 內聯附加。
   - 若模型不支援 vision (text 模式)：呼叫 `_enrich_message_with_vision()` (run.py:20398) → `vision_analyze_tool()` (來自 `tools.vision_tools`)，將視覺描述前置到訊息文字中。

5. **與 channel_gw 的對比**：channel_gw 的 `ask_vision()` (main.py:648) 使用 Qwen3.6-27B vision model。hermes-agent 的 `vision_analyze_tool` 使用配置的 vision provider (可透過 `auxiliary.vision.*` 配置)。功能等效，hermes-agent 更通用。

### 結論
圖片訊息經過 `_download_media` 快取後，gateway 會自動調用 vision 工具進行圖像分析。**無需重寫**。

---

## 2. 語音 (Voice/Audio)

### 結論：✅ 已原生支援 — 不需重寫

### 證據鏈

1. **類型對映**：LINE audio 訊息被對映為 `MessageType.VOICE` (adapter.py:172 `_LINE_MESSAGE_TYPES`)。

2. **STT 管道判定**：`_event_media_is_stt_input()` (run.py:2529) 對 `MessageType.VOICE` 返回 `True`，對 `MessageType.AUDIO` 和 `MessageType.DOCUMENT` 返回 `False`。

3. **自動轉錄**：`_prepare_inbound_message_text()` (run.py:~15091) 收集 STT-eligible 的音頻路徑，呼叫 `_enrich_message_with_transcription()` (run.py:15149)。

4. **轉錄實現**：`_enrich_message_with_transcription()` (run.py:20467) 呼叫 `tools.transcription_tools.transcribe_audio()` (run.py:20513)，並在失敗時嘗試 `transcribe_audio_local_fallback()`。

5. **STT 開關**：由 `stt_enabled` 配置控制 (預設 `True`，run.py:20491)。來自 `gateway/config.py:914` 的 `PlatformConfig.stt_enabled`。

6. **轉錄回傳**：若 `stt_echo_transcripts` 為 `True` (預設 `True`，run.py:18280 `_should_echo_stt_transcripts`)，轉錄文字會回傳到聊天中 (run.py:15157-15171)。

7. **語音回覆 (TTS)**：`_send_voice_reply()` (run.py:18284) 使用 `text_to_speech_tool` (來自 `tools.tts_tool`) 進行 TTS。由 `_should_auto_tts_for_chat()` (base.py:3120) 控制，基於 `voice.auto_tts` 配置或 `/voice on` 指令。

8. **與 channel_gw 的對比**：channel_gw 的 `transcribe_audio()` (main.py:586) 使用 LiteLLM 的 whisper-local 路由。hermes-agent 的 `transcribe_audio` 使用配置的 STT provider (OpenAI Whisper 等)。功能等效。channel_gw 的 `synthesize_speech_m4a()` (main.py:609) 使用 LiteLLM TTS。hermes-agent 的 auto-TTS 功能等效，並支援更多平台 (Telegram, Signal, WhatsApp 等需要 Ogg/Opus 格式的平台)。

### 結論
LINE 語音訊息會自動透過 STT 轉錄成文字餵給 LLM，並支援 TTS 語音回覆。**無需重寫**。

---

## 3. 檔案 (File/Document)

### 結論：⚠️ 部分原生支援 — 有缺口需補

### 已支援的部分

1. **下載與快取**：LINE file 訊息被對映為 `MessageType.DOCUMENT` (adapter.py:173)。`_download_media()` (adapter.py:1106) 下載文件並快取到本地路徑。

2. **agent 可存取**：文件路徑透過 `MessageEvent.media_urls` 傳遞給 gateway。`_prepare_inbound_message_text()` (run.py:15220) 為非媒體文件建立 `_build_document_context_note()` (run.py:2570)，告知 agent 文件已保存的路徑，並建議 agent 使用 `terminal` 工具或 `ocr-and-documents` skill 來提取文字。

3. **agent 有讀取工具**：hermes-agent 核心有 `file` 工具 (read_file, search_files 等) 和 `ocr-and-documents` skill，可讀取 PDF/Word/Excel 等格式。

### 缺口

1. **❌ 檔案副檔名白名單**：LINE adapter 的 `_download_media()` **不過濾副檔名** — 任何文件類型都會被下載並快取。channel_gw 的 `_prepare_line_message_for_ai()` (main.py:1300) 執行 `SUPPORTED_EXTS = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv"}` 過濾，並拒絕非白名單格式 (main.py:1304-1305)。QA_CHECKLIST §4.4 要求「僅允許 `pdf`, `doc`, `docx`, `xls`, `xlsx`, `ppt`, `pptx`, `txt`, `csv`」，「其餘可執行或腳本副檔名（如 `exe`, `sh`, `bat`, `py`, `js`）必須被硬性拒絕」。

2. **❌ 未預先提取文字**：channel_gw 在 `_prepare_line_message_for_ai()` 中使用 `line_adapter.extract_text()` (adapters/line.py:373) 預先提取 PDF/Word/Excel 的文字內容，並內聯到訊息文字中。hermes-agent 僅將文件路徑傳給 agent，要求 agent 自行提取。這是設計上的不同（hermes-agent 將提取工作交給 agent），但 QA_CHECKLIST §4.3 Case 3.2 期望「調用 `file` / `extract_text`」並「根據 PDF 提取內容回答問答」。目前 agent 需要自行決定如何提取，可能導致行為不一致。

3. **⚠️ 媒體快取清理**：hermes-agent 有 `cleanup_image_cache` / `cleanup_audio_cache` / `cleanup_video_cache` / `cleanup_document_cache` (base.py:935, 1056, 1100, 1909)，每小時透過 gateway housekeeping loop 執行清理 (run.py:24801-24807)，預設清理 24 小時前的檔案。LINE adapter 在 `disconnect()` 時清理 `_media_temp_paths` (adapter.py:889-894)。但**沒有 per-session 清理** — 檔案在 24 小時過期前不會被清理。QA_CHECKLIST §4.4 要求「所有下載之語音/圖片/檔案暫存檔必須於對話 Session 結束或背景 Job 處理完成後被自動清理」。

### 補的程式碼

1. **新增 `plugins/platforms/line/media.py`**：包含 `SUPPORTED_FILE_EXTENSIONS` 白名單（與 channel_gw 保持一致）、`check_file_extension()` 等輔助函數。

2. **修改 `plugins/platforms/line/adapter.py`**：在 `_handle_message_event()` 中，對 `file` 類型訊息進行副檔名檢查 (line 1007-1010)。若副檔名不在白名單內，設定 `text` 為「不支援該檔案格式」提示，**不下載文件**、**不加入 `media_urls`**，確保惡意檔案不會觸達 cache 或 agent 的工具鏈。

### 未補的部分（設計決定）

- **預先提取文字**：hermes-agent 的設計是將文件路徑傳給 agent，讓 agent 使用 `terminal` / `ocr-and-documents` skill 自行提取。這與 channel_gw 的預先提取不同，但 agent 擁有足夠的工具來完成提取。若要完全對齊 channel_gw 的行為，需要在 gateway 新增一個預提取步驟，這會增加額外的依賴 (pypdf, python-docx, openpyxl) 到核心。建議在實機驗證後決定是否需要此改動。

- **per-session 快取清理**：目前的 24 小時 TTL 清理機制已能防止硬碟爆滿。per-session 清理需要在 session 結束時觸發清理回呼，這需要修改 gateway 的 session lifecycle。建議在實機驗證後評估是否需要。

---

## 驗證方式

### 圖片驗證
1. 發送合成 LINE 圖片 webhook (Case 3.1)
2. 檢查 `/home/murray/.hermes/cache/images/` 存在快取的圖片
3. 檢查 gateway log 包含 `Auto-analyzing user image` 或 `Image routing: native`
4. 確認 agent 回應包含對圖片視覺內容的描述

### 語音驗證
1. 發送合成 LINE 語音 webhook (Case 3.3)
2. 檢查 `/home/murray/.hermes/cache/audio/` 存在快取的音頻
3. 檢查 gateway log 包含 `Transcribing user voice`
4. 確認 agent 收到轉錄文字並回應
5. 若 `stt_echo_transcripts: true`，檢查聊天中收到 `🎙️ "transcript"` 回傳

### 檔案驗證
1. **支援格式** (Case 3.2): 發送合成 LINE PDF webhook，確認文件被下載到 cache，agent 收到文件路徑提示
2. **不支援格式** (Case 3.4): 發送合成 LINE `.exe` webhook，確認回覆「不支援 .exe 格式，目前支援：PDF、Word、Excel、PPT、TXT、CSV。」，且 cache 中無對應檔案

### 測試指令
```bash
/home/murray/.venvs/memory-hub/bin/python -m pytest tests/gateway/test_line_plugin.py -q
```
