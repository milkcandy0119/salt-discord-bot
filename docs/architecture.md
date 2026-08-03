# 架構基線

## 目前完成範圍

階段 2 已在既有 Discord 訊息接收、安全閘門與 SQLite 持久化之上，加入完全不需付費 API
的對話段落引擎。程式在沒有 Discord 設定時仍以安全模式啟動；設定完整時才執行 migration、
補處理重啟前尚未切段的訊息，並啟動 Discord.py 用戶端。

本階段不包含 AI 回覆、付費語意切段、摘要、Embedding、歷史匯入、提醒或部署。

## 訊息處理順序

1. Discord.py 的 `on_message` 接收事件，只接受一般訊息與明確回覆事件。
2. `MessageHandler` 檢查 guild/channel 白名單，並忽略機器人自己的訊息。
3. `SensitiveFilter` 在資料庫寫入及任何通知前掃描訊息內容與作者顯示名稱。
4. 一般訊息保存原文；敏感訊息只保存遮罩內容、分類及通知狀態。
5. `MessageRepository` 先完成資料庫交易，再執行作者與管理員通知。
6. `ConversationSegmenter` 先採明確回覆鏈，再採保守的時間與參與者規則切段。
7. Discord message ID 是唯一鍵；重送與並發重送不會產生第二筆資料。

敏感通知使用不含 `content` 欄位的 `SensitiveNotice`，因此通知配接層無法取得原始訊息。
通知失敗時，已保存訊息不會回滾，狀態會記為 `failed`。

## 模組邊界

- `app.config`：環境設定、祕密遮罩及 Discord ID 清單解析。
- `app.main`：安全啟動檢查、migration 與 Discord 用戶端生命週期。
- `app.bot.client`：Discord.py 事件轉換及固定安全通知。
- `app.bot.message_handler`：白名單、安全掃描、先保存後通知的流程編排。
- `app.conversations.segmenter`：回覆優先、保守續接、封存及重啟脈絡。
- `app.security.sensitive_filter`：完全在本機執行的確定性敏感資料規則。
- `app.security.access_policy`：擁有者與管理員的敏感事件查閱權限。
- `app.storage`：SQLAlchemy 非同步 session、資料模型與冪等 repository。
- `migrations`：Alembic schema 版本。

每個非同步工作使用自己的 `AsyncSession`，不在並發工作之間共用 session。Migration 是同步
管理工作，從事件迴圈以 `asyncio.to_thread` 執行。

## 目前資料模型

`messages` 至少保存：

- 唯一 Discord message ID、guild/channel/author ID。
- 經安全處理的內容與作者顯示名稱。
- Discord 建立時間及系統接收時間。
- 被回覆訊息的 Discord message ID。
- 作者是否為機器人。
- 敏感狀態、敏感分類、處理狀態。
- 作者及管理員通知狀態。
- 所屬對話段落 ID。

`conversation_segments` 保存 guild/channel、根訊息 ID、活動／封存狀態、建立時間、最後訊息
時間、封存時間與重啟時間。訊息透過 `segment_id` 成為段落成員。回覆 ID 仍保存為外部 ID，
因為被回覆訊息可能尚未匯入或來自很久以前。

## 階段 2 確定性切段規則

判斷順序固定如下：

1. 明確回覆已知訊息時，加入被回覆訊息的段落。
2. 即使該段落已封存，明確回覆仍會重啟原段落。
3. 沒有回覆時，只有同一作者在設定時間窗內剛參與且恰好只有一個候選活動段落，才會續接。
4. 回覆目標未知、候選為零或同時有多個候選時，一律建立新段落。
5. 活動段落滿 30 分鐘沒有新訊息便封存；Discord 執行期間每分鐘檢查一次。

這個版本刻意不從文字內容猜主題。未來的語意相似度只能作為沒有明確回覆時的輔助訊號，
且不能因歷史檢索而永久合併段落。

## 安全邊界

- Token、API key、密碼與私鑰不得寫入日誌。
- 敏感原文不寫入資料庫；只有遮罩內容與分類進入持久化資料。
- 不自動刪除 Discord 原訊息。
- 作者收到固定私訊；擁有者與指定管理員收到不含內容的事件資訊。
- 一般使用者不能透過應用程式查閱敏感攔截紀錄。
- OpenAI 整合即使已有環境金鑰，在本階段仍保持停用。

## 目前限制

- 敏感偵測是規則式工具，仍可能誤判或漏判；保守方案因此不自動刪除訊息。
- 只掃描文字內容及作者顯示名稱，尚未下載或掃描附件內容。
- 通知失敗會留下可重試狀態，但自動背景重試佇列屬後續階段。
- 階段 1 明確採用 SQLite；最終資料庫與向量儲存方案仍待後續部署決策。
- 沒有明確回覆時的保守規則可能建立較多短段落；這比錯誤合併不同話題更容易在未來修正。
- 30 分鐘封存目前只改變段落狀態；摘要／向量背景佇列要到階段 5 才建立。
