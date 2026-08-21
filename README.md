# Salt Discord Bot

Salt 是一個以 **maimai 的 Salt** 為原型製作的非官方 Discord AI 陪伴機器人。它能在指定頻道中聊天、理解圖片、保存短期對話脈絡，並提供個人記憶、提醒及管理功能。


## 主要功能

- AI 對話：支援提及、回覆及文字前綴觸發。
- 頻道模式：一般模式只回應明確觸發；陪伴模式可在適當時機參與聊天。
- 圖片理解：可選擇開啟靜態圖片、GIF 及 APNG 的內容理解。
- 個人記憶：使用者可自行管理 Salt 對自己的記憶。
- 提醒功能：支援單次、每日、每週及固定間隔提醒。
- 對話記憶：可選擇建立摘要與向量，讓 Salt 參考較早的對話。
- 管理功能：可管理允許使用的頻道、頻道模式與跨頻道記憶群組。
- 安全保護：偵測常見 Token、API Key、密碼及私鑰，敏感內容不會以原文保存或傳送給 AI。

## 使用需求

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Discord Bot Token
- OpenAI API Key（AI 回覆需要）
- 選用：Docker Engine 或 Docker Desktop

Discord Developer Portal 中必須開啟 **Message Content Intent**。Bot 在允許的頻道中需要查看頻道、讀取訊息、傳送訊息、讀取歷史訊息及使用 Slash Commands 的權限。

## 安裝與配置

安裝依賴：

```powershell
uv sync --locked
```

複製環境設定範例：

```powershell
Copy-Item .env.example .env
```

至少填寫以下項目：

```env
DISCORD_BOT_TOKEN=你的 Discord Bot Token
DISCORD_ALLOWED_GUILD_IDS=允許使用的伺服器 ID
DISCORD_ALLOWED_CHANNEL_IDS=允許使用的頻道 ID
DISCORD_COMPANION_CHANNEL_IDS=
DISCORD_OWNER_USER_ID=擁有者的使用者 ID
DISCORD_ADMIN_USER_IDS=
OPENAI_API_KEY=你的 OpenAI API Key
```

多個 ID 使用逗號分隔，例如 `123456789,987654321`。

| 設定 | 說明 |
| --- | --- |
| `DISCORD_ALLOWED_GUILD_IDS` | 允許完整功能運作的伺服器 |
| `DISCORD_ALLOWED_CHANNEL_IDS` | Salt 會接收及保存訊息的頻道 |
| `DISCORD_COMPANION_CHANNEL_IDS` | 啟用陪伴模式的頻道，必須同時位於允許頻道中；留空則全部使用一般模式 |
| `DISCORD_OWNER_USER_ID` | Bot 擁有者，只能填一個 ID |
| `DISCORD_ADMIN_USER_IDS` | 額外管理員，可留空 |
| `DISCORD_AI_COMMAND_PREFIX` | 文字觸發前綴，以 `.env.example` 的設定為準 |
| `DATABASE_URL` | SQLite 資料庫位置，預設為 `data/discord_assistant.db` |
| `AI_PERSONA_PATH` | 人設檔案，預設為 `personas/salt-zh-tw-v1.toml` |
| `AI_VISION_ENABLED` | 是否開啟圖片理解，預設 `false` |
| `BACKGROUND_AI_ENABLED` | 是否建立背景摘要與歷史記憶，預設 `false` |

其他模型、上下文、提醒、健康檢查及備份選項均已列在 [`.env.example`](.env.example) 中。請勿提交 `.env`，也不要在公開頻道貼出 Token 或 API Key。

## 啟動

本機啟動：

```powershell
uv run python -m app.main
```

啟動時會自動更新資料庫結構並連線 Discord；使用 `Ctrl+C` 停止。

使用 Docker：

```powershell
docker compose up -d --build
docker compose ps
```

更完整的 Docker、備份及還原設定請參考 [`docs/deployment.md`](docs/deployment.md)。

## 如何使用

在允許的頻道中，可以：

- 提及 Salt。
- 回覆 Salt 的訊息。
- 使用 `DISCORD_AI_COMMAND_PREFIX` 設定的文字前綴。
- 在陪伴模式頻道中正常聊天，Salt 會依頻道活動與冷卻時間判斷是否加入對話。

主要 Slash Commands：

| 指令 | 用途 |
| --- | --- |
| `/salt about` | 查看 Salt 的介紹與人設版本 |
| `/salt help` | 查看目前可用的功能 |
| `/salt privacy` | 查看隱私與資料使用說明 |
| `/salt ping` | 確認 Bot 是否在線 |
| `/memory menu` | 管理自己的個人記憶 |
| `/remind create` | 建立提醒 |
| `/remind manage` | 編輯或刪除提醒 |
| `/remind list` | 查看待處理提醒 |
| `/timezone view` | 查看提醒時區 |
| `/timezone set` | 設定提醒時區，例如 `Asia/Taipei` |
| `/admin menu` | 管理頻道白名單、模式及記憶群組（管理員限定） |
| `/bot status` | 查看健康、用量及工作狀態（管理員限定） |

提醒會以私訊傳送給建立者。個人記憶及多數管理指令使用只有操作本人可見的私密回覆。

## 資料與費用

- 訊息與設定資料預設保存在本機 SQLite 資料庫。
- 只有通過觸發及安全檢查的非敏感內容，才可能傳送至 OpenAI。
- 沒有設定 `OPENAI_API_KEY` 時，不會產生 AI API 呼叫。
- 圖片理解與背景記憶預設關閉，開啟後可能增加 OpenAI API 費用。
- 專案具有一次性 AI 預算限制及用量通知，避免無限制呼叫。
- 偵測到真實憑證外洩時，仍應立即刪除原訊息並撤銷或更換憑證。

## 其他文件

- [`docs/deployment.md`](docs/deployment.md)：Docker 部署、備份與還原
- [`docs/trial.md`](docs/trial.md)：試跑與正式運行管理
- [`docs/architecture.md`](docs/architecture.md)：系統架構與資料流程
- [`docs/decisions.md`](docs/decisions.md)：安全與功能設計決策
