# Discord Assistant

這是一個分階段建立、可長期執行的 Discord 助手。目前完成「階段 3：一次性預算帳本與
付費呼叫閘門」。Discord 訊息接收、安全持久化、免費切段及交易式預算控制已可運作；真實
AI 回覆、摘要、向量、提醒及部署尚未啟用。

## 需求

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)

## 安裝與驗證

```powershell
uv sync --locked
uv run ruff check .
uv run pytest
uv run python -m app.main
```

沒有 Discord 設定時，最後一個命令會以安全模式正常結束，並只列出缺少的設定名稱。

## Discord 設定

將 `.env.example` 複製成不受 Git 追蹤的 `.env`，填入：

```env
DISCORD_BOT_TOKEN=
DISCORD_ALLOWED_GUILD_IDS=123456789
DISCORD_ALLOWED_CHANNEL_IDS=234567890,345678901
DISCORD_OWNER_USER_ID=456789012
DISCORD_ADMIN_USER_IDS=567890123,678901234
DATABASE_URL=sqlite+aiosqlite:///data/discord_assistant.db
CONVERSATION_IMPLICIT_CONTINUATION_MINUTES=5
```

ID 清單以逗號分隔。`DISCORD_ADMIN_USER_IDS` 可以留空；擁有者仍會收到敏感事件通知。

Discord Developer Portal 必須同時開啟 **Message Content Intent**。程式端也已明確啟用此
intent，否則一般訊息的 `content` 會是空字串。Bot 只需查看白名單頻道及接收訊息所需權限；
保守試跑方案不會刪除訊息，因此不需要「管理訊息」權限。

設定完成後執行：

```powershell
uv run python -m app.main
```

程式會先自動執行 Alembic migration，再連線 Discord。停止程式可使用 `Ctrl+C`。

## 對話段落

- 明確回覆會加入被回覆訊息的段落，即使該段落已封存。
- 沒有回覆時，只在同一作者近期只有一個候選活動段落時續接。
- 無法可靠判斷或同時符合多個段落時建立新段落。
- 段落滿 30 分鐘沒有新訊息會自動封存。
- 程式重啟會補處理已保存但尚未完成切段的訊息。

這些規則不讀取訊息語意，也不呼叫任何付費 API。

## 一次性 AI 預算

- 全域永久上限：US$10。
- 背景摘要與 Embedding 上限：US$3。
- 前景聊天可以使用全部尚未使用的全域額度。
- 金額使用整數微美元，不使用浮點數。
- 每次未來的付費呼叫都必須先預留，完成後依實際 Token 結算。
- API 超時且用量不明時保留預留，不會假設費用為零。
- 實際花費達 70%／90% 時各私訊擁有者一次。
- 不會自動補額、按月重置或提供手動重置。

目前沒有任何真實 AI 服務接入，因此帳本初始值全部是零，也不會自行產生費用。

可用以下唯讀 SQL 查看實際資料庫狀態：

```sql
SELECT global_spent_microusd, global_reserved_microusd,
       background_spent_microusd, background_reserved_microusd
FROM budget_state WHERE id = 1;

SELECT reservation_id, purpose, model_name, price_version,
       reserved_cost_microusd, actual_cost_microusd, status
FROM paid_ai_calls ORDER BY created_at DESC LIMIT 20;

SELECT threshold_percent, status, attempts, sent_at
FROM budget_threshold_notifications ORDER BY threshold_percent;
```

## 敏感資料政策

收到訊息後會先掃描常見 Discord Token、OpenAI API key、具名 API key／Token／密碼及私鑰：

- 一般訊息保存原文。
- 敏感訊息只保存遮罩內容與分類，不保存原始祕密。
- 不自動刪除 Discord 原訊息。
- 私訊作者固定提醒，並通知擁有者與指定管理員。
- 日誌及通知不包含原始訊息內容或祕密。

敏感偵測仍可能誤判或漏判。發現真實憑證外洩時，仍應立即手動刪除訊息並撤銷或輪替憑證。

## 資料庫

階段 1 使用 SQLite，預設位於 `data/discord_assistant.db`，且 `data/` 不受 Git 追蹤。
Discord message ID 具有唯一限制，因此事件重送與並發重送不會建立重複資料。Migration 也可
手動執行：

```powershell
uv run alembic upgrade head
```

## 文件

- `docs/architecture.md`：訊息流程、模組邊界、資料模型與限制。
- `docs/decisions.md`：已確認的保守試跑政策及後續待確認項目。

下一步是階段 4「AI 觸發、上下文與回覆」。進入前必須先確認觸發條件、聊天模型、價格表、
系統提示、維護訊息與最大上下文。
