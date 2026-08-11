# Discord Assistant

這是一個分階段建立、可長期執行的 Discord 助手。目前完成到「階段 9：保守試跑工具」。
Discord 訊息接收、安全持久化、免費切段、交易式預算控制、normal／companion 頻道模式、
受控 AI 回覆、貼圖名稱中繼資料，以及可選的背景摘要與同頻道歷史檢索已可運作；歷史匯入、
提醒、管理、容器健康檢查、Restic 加密備份及受控試跑觀測已實作；七天試跑預設未啟動，
實際 VPS 尚未建立。

## 需求

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- 選用：Docker Engine／Docker Desktop（階段 8 部署驗收）

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
DISCORD_COMPANION_CHANNEL_IDS=345678901
DISCORD_OWNER_USER_ID=456789012
DISCORD_ADMIN_USER_IDS=567890123,678901234
DISCORD_AI_COMMAND_PREFIX=!ai
DATABASE_URL=sqlite+aiosqlite:///data/discord_assistant.db
CONVERSATION_IMPLICIT_CONTINUATION_MINUTES=5
COMPANION_OBSERVATION_SECONDS=5
COMPANION_COOLDOWN_SECONDS=120
OPENAI_API_KEY=
BACKGROUND_AI_ENABLED=false
```

ID 清單以逗號分隔。`DISCORD_ADMIN_USER_IDS` 可以留空；擁有者仍會收到敏感事件通知。
`DISCORD_OWNER_USER_ID` 也用於免費的聊天身分對照：本人發言、被提及，或對話詢問主人／開發者
時，程式會先按 Discord ID 確認，再把角色關係提供給模型，不依賴可變的暱稱。

Discord Developer Portal 必須同時開啟 **Message Content Intent**。程式端也已明確啟用此
intent，否則一般訊息的 `content` 會是空字串。Bot 只需查看白名單頻道及接收訊息所需權限；
保守試跑方案不會刪除訊息，因此不需要「管理訊息」權限。

設定完成後執行：

```powershell
uv run python -m app.main
```

程式會先自動執行 Alembic migration，再連線 Discord。停止程式可使用 `Ctrl+C`。

## 全域公開指令

Bot 啟動時只把以下免費、唯讀且全部採私密回覆的 `/salt` 指令同步為 Discord Global
Commands；它們只允許在伺服器內使用，不開放私訊：

- `/salt about`：非官方身分聲明與人設版本。
- `/salt help`：使用方式，以及目前伺服器是否已啟用完整功能。
- `/salt privacy`：不讀取實際資料的固定隱私摘要。
- `/salt ping`：只顯示連線狀態與 Discord 延遲。

這四個指令不呼叫 OpenAI、不讀取聊天或個人記憶，也不顯示預算、佇列或管理資料。既有
`/bot`、`/memory`、`/remind`、`/timezone`、`/trial` 仍只同步到
`DISCORD_ALLOWED_GUILD_IDS`，不得因全域同步而公開。

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

階段 4 已接入真實 AI 服務，但只有設定 `OPENAI_API_KEY` 且訊息通過觸發與安全規則時才可能
產生費用；自動化測試全部使用假 client。

## 頻道模式與 AI 回覆

- `normal`：只在提及機器人、回覆機器人或使用 `!ai` 指令時回覆。
- `companion`：不要求提及，但會先用免費規則評估對話、參與者、訊息速度、冷卻與預算，
  不會對每則訊息直接呼叫 AI。
- 陪伴模式的非明確觸發會等待頻道安靜 5 秒；有新訊息便重新計時。提及、回覆機器人及指令
  不需要等待。
- 陪伴模式在多位真人密集交談時不插話；會回應問題／求助、延續機器人最近參與的對話，
  或在 Salt 超過最近參與時間後，由單一真人的一句自然文字重新喚醒。純網址、單純 Emoji
  與 Discord 標記不會重新喚醒；自動回覆成功後冷卻 120 秒。
- 明確提問、提及、回覆機器人或指令會使用 Discord 回覆；Salt 自動加入日常話題、發表看法
  或接梗時改用一般頻道訊息，不把每次參與都掛在某一位成員的訊息下面。
- 第一版只在有人發言後判斷是否回覆，不會在無人發言時主動開啟話題，也不使用付費模型
  判斷「該不該回覆」。
- 頻道以 Discord channel ID 設定；人設獨立且有版本，只能影響表達方式，不能覆蓋安全、
  權限、隱私與預算限制。
- 視覺理解預設關閉。管理員明確設定 `AI_VISION_ENABLED=true` 後，只有已通過原有回覆觸發、
  安全檢查與前景預算預留的目前訊息，或使用者明確回覆的目標訊息，才可能處理靜態 Discord
  圖片，或從一個 GIF／APNG 動畫擷取最多四張代表畫面。Unicode Emoji 仍是文字；Lottie
  只讀名稱，影片與一般外部網址不處理。
- 模型輸出會移除重複的 Salt／ソルト說話者前綴，避免和 Discord 顯示名稱重複；人設允許
  偶爾使用一次簡短動作描寫，但不得讓每則訊息都變成角色扮演小說。

AI 使用官方 OpenAI Responses API，預設聊天模型為 `gpt-5.6-luna`、低推理強度、最多
12,000 字元上下文與 800 個輸出 Token。上下文先放明確回覆鏈，再補入同頻道最近 10 分鐘、
最多 12 則的非敏感對話（最多 4,000 字元），讓多人輪流發言仍保留短期話題；接著才補發言者
本人及最多三位被提及成員的近期訊息。模型只回覆標示的目前目標，其他內容只作背景。若背景
記憶已明確啟用且同頻道已有向量，會以目前問題、被回覆目標與近期話題建立查詢，暫時補入最
多 3 筆歷史摘要。所有補充都不會永久合併對話段落。人設預設載入 `personas/salt-zh-tw-v1.toml`。

沒有 `OPENAI_API_KEY` 或額度不足時，每次明確觸發只會回覆固定維護訊息。設定金鑰後，通過
觸發、安全檢查及預算預留的訊息會產生真實付費 API 呼叫。

### Salt 視覺理解第一階段

圖片下載只允許目前 Discord 事件提供或可由事件中的自訂表情 ID 建立的 Discord CDN 資源，
不接受 Tenor、Giphy 或聊天中的任意網址。下載前後都會檢查數量、宣告大小、實際格式、像素、
動畫狀態與逾時；合格圖片會移除 EXIF、校正方向、轉為 RGB 並縮小。原始位元組與 Base64
只存在單次處理記憶體，不寫入 SQLite、日誌、錯誤訊息或歷史向量。

資料庫只保存原本文字以及安全 metadata（資源種類、Discord 資源 ID、檔名、Content-Type、
大小與可能動畫狀態）。檔名與文字會先經既有敏感規則；目前不做 OCR，所以敏感檢查不能看到
圖片內文字。視覺費用屬於前景 US$10 帳本，會在下載前保守加入每張圖片 Token 預留，並依
Responses API 實際 usage 結算；預算不足時不下載圖片。

保守啟用範例：

```env
AI_VISION_ENABLED=true
AI_VISION_MAX_IMAGES_PER_MESSAGE=1
AI_VISION_MAX_DOWNLOAD_BYTES=8388608
AI_VISION_MAX_PIXELS=20000000
AI_VISION_DOWNLOAD_TIMEOUT_SECONDS=10
AI_VISION_DETAIL=low
AI_VISION_MAX_RESERVED_TOKENS_PER_IMAGE=1200
AI_VISION_MAX_DIMENSION=1536
AI_VISION_MAX_ANIMATIONS_PER_MESSAGE=1
AI_VISION_MAX_FRAMES_PER_ANIMATION=4
AI_VISION_MAX_ANIMATION_FRAMES=300
AI_VISION_MAX_ANIMATION_TOTAL_PIXELS=80000000
AI_VISION_ANIMATION_PROCESSING_TIMEOUT_SECONDS=3
AI_VISION_MAX_ANIMATION_DURATION_SECONDS=30
AI_VISION_ANIMATION_DUPLICATE_THRESHOLD=3
```

`normal` 頻道仍必須提及、回覆或使用既有指令；圖片不會繞過觸發。`companion` 只在「你看」
等免費視覺對話訊號或機器人最近已參與時，把圖片當候選，仍受 5 秒觀察、多人成員、冷卻、
每日上限與預算規則限制，因此不會每張圖都呼叫 AI。圖片打不開且沒有可繼續處理的文字時，
只回固定文字，不呼叫模型。

### Salt 視覺理解第二階段

GIF、APNG、Discord GIF 貼圖、Discord APNG 貼圖與動態自訂表情會在本機由 Pillow 解碼，
不會把動畫原檔直接當成單張圖片傳送。系統先沿整段動畫時間軸均勻選擇候選時間點，再以
縮小 RGB 畫面的平均差異去除幾乎相同的畫面，最多留下四張，按時間順序傳入 Responses API。
每張都附有順序及約略毫秒，模型也會收到「這些是同一動畫的連續代表畫面」說明。

每則訊息最多下載及處理一個動畫。檔案仍受 8 MiB 限制，另有單一畫布像素、動畫總像素、
最多 300 影格、30 秒動畫長度及 3 秒本機處理上限。預算下載前會以最多四張圖片保守預留，
成功後仍依 API 實際 usage 結算。動畫原檔、解碼影格及擷取畫面不寫入磁碟或資料庫。

Lottie 暫不下載或渲染，只保存貼圖名稱及 metadata。加入 Lottie 需額外原生 rlottie／Cairo
或 Node／Chromium 渲染器，會增加 Docker image、建置複雜度及不受信任向量動畫的 CPU、
記憶體與解析器攻擊面，因此必須另外評估並取得同意。Tenor、Giphy 與任意外部 URL 仍不在
範圍內。

## 階段 5 背景記憶

階段 5 採保守方案 A，預設 `BACKGROUND_AI_ENABLED=false`。保持關閉時，不啟動摘要排程、不建立
新背景工作、不做聊天查詢 Embedding，也不產生階段 5 費用。改為 `true` 後：

- 每分鐘封存檢查只替「這次新封存」的段落建立工作，不回填啟用前已封存的舊段落。
- 背景工作預設每 5 分鐘執行；pending 數達 20 時可提前跑一批，每批最多 10 筆。
- 工作持久化於 SQLite，最舊優先、冪等；程序中斷後可回收逾時 claim。
- 可安全重試的錯誤採 1、2、4、8…分鐘退避，上限 1 小時，預設 5 次後隔離。
- 額度不足或沒有金鑰時保留 pending，且不呼叫付費 API；用量不明時隔離工作，避免自動重送
  造成重複計費。
- 摘要使用 `gpt-5.4-nano-2026-03-17`、`reasoning=none`、最多 300 輸出 Token。
- Embedding 使用 `text-embedding-3-small` 的完整 1536 維，向量保存為 SQLite BLOB；小型資料量
  由 Python 精確計算餘弦相似度，不需要外部向量服務。
- 摘要與向量都是可從原始訊息重建的衍生資料，不會永久合併對話段落。

可用以下唯讀 SQL 檢查背景狀態：

```sql
SELECT status, COUNT(*) FROM background_jobs GROUP BY status;

SELECT id, segment_id, source_message_count, model_name, prompt_version, created_at
FROM segment_summaries ORDER BY id DESC LIMIT 20;

SELECT summary_id, chunk_index, model_name, dimension, length(vector_blob) AS bytes
FROM summary_embeddings ORDER BY id DESC LIMIT 20;
```

## 階段 6 歷史分析與受控正式匯入

正式匯入前必須先執行唯讀分析。分析工具會登入 Discord 並讀取白名單頻道歷史，但不傳送或
修改 Discord 訊息、不寫入資料庫、不建立背景工作，也不呼叫 OpenAI：

```powershell
uv run python -m app.history_cli analyze --limit-per-channel 10000
```

可用 `--after 2026-01-01T00:00:00+08:00` 限定起始時間。報告只輸出數量、文字容量、附件中繼
資料、切段估算、版本化模型價格與預算餘額，不輸出訊息內容或作者名稱。若某頻道達到上限，
其 ID 會出現在 `truncated_channel_ids`；此時不得把報告視為完整歷史估算。

`estimated_total_cost_microusd` 使用免費切段模擬，`maximum_total_cost_microusd` 則保守假設每則
合格新訊息各自形成一段。兩者都包含資料庫中已封存但尚未摘要的段落。分析結果不是正式匯入
授權；保存新歷史訊息、排摘要工作及向量化仍須再次明確確認。

取得明確確認後，正式匯入命令仍會重新讀取同一份歷史快照並估價。最新最壞成本高於批准值、
歷史被截斷或背景／全域預算不足時，會在資料庫寫入與 OpenAI 呼叫前停止：

```powershell
.\.tools\bin\uv.exe run --offline --cache-dir .uv-cache python -m app.history_cli import-history `
  --limit-per-channel 10000 `
  --confirmation "確認執行階段 6 正式匯入" `
  --maximum-approved-cost-microusd 17952 `
  --approval-baseline-global-committed-microusd 213225
```

上面的兩個數值只是這次測試伺服器報告中的批准上限與 `global_committed_microusd`，換伺服器、
頻道、資料庫或訊息範圍時不得照抄，必須先重新執行免費分析並取得新的明確批准。基準值讓同一
次匯入即使程序中斷後重跑，仍只能共用原來的總費用上限。正式匯入不要求把
`BACKGROUND_AI_ENABLED` 改成 `true`；它會在這次命令內受控處理既有封存段落。Discord message
ID、背景工作與摘要／向量唯一鍵都具有冪等保護，中斷後可用相同參數重跑。匯入舊敏感訊息時
仍會遮罩內容，但不會補發舊事件通知。

完成後可用唯讀狀態命令驗收，不會讀取 Discord 或呼叫 OpenAI：

```powershell
.\.tools\bin\uv.exe run --offline --cache-dir .uv-cache python -m app.history_cli status
```

## 階段 6.1 基本個人記憶

個人記憶以 Discord guild ID 與 user ID 隔離，不依賴可變更的暱稱。日常聊天只有明確使用
「請記得我……」或「記住我……」時，才使用本機規則建立記憶；一般聊天不會被 AI 猜測成
永久個人資料。成功時機器人會用固定免費文字回覆記憶編號，不呼叫聊天模型。

使用者也可以使用只對自己可見的 Slash Commands：

- `/memory menu`：以私密選單查看、新增、修改或刪除自己的記憶。

命令不接受目標使用者參數，因此不能查看、修改或刪除別人的記憶。每筆最多 200 字，可能含
API key、Token、密碼或私鑰的內容會被拒絕。聊天時最多加入
`AI_PERSONAL_MEMORY_CONTEXT_CHARACTERS` 個字元的目前發言者記憶；這些資料只供個人化，不具
系統指令權限，也不當成已證實的客觀事實。啟動機器人時會把命令同步到白名單伺服器。

個人記憶僅能由建立者透過選單管理。

## 階段 7 提醒與管理功能

提醒完全不依賴 AI。預設時區為 `Asia/Taipei`，使用標準 `tzdata` 支援 IANA 時區；只接受
`YYYY-MM-DD` 與 `HH:MM` 明確時間，不猜測自然語言。所有命令結果均為 ephemeral：

- `/timezone view`：查看自己的提醒時區。
- `/timezone set timezone:Asia/Taipei`：設定自己在目前伺服器的時區。
- `/remind create`：開啟私密選單，選擇一次、每天、每週或固定間隔提醒，再以表單填寫資料。
- `/remind manage`：選擇最多 25 個待處理提醒，可批量更新內容或取消。
- `/remind list`：列出自己的待處理提醒。
- `/remind cancel reminder_id:編號`：取消自己的單一提醒。
- `/bot status`：只有擁有者及指定管理員可查看健康、總用量、背景用量、背景工作、提醒及
  70%／90% 預算通知狀態。

到期提醒只私訊建立者，停用 mentions。機器人離線時提醒留在 SQLite，重啟後會補處理。禁止
私訊或找不到使用者時保留 `failed` 紀錄，不在公開頻道補發；暫時性 Discord 錯誤才依上限
退避重試。提醒內容最多 500 字，可能含祕密時拒絕保存。

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

## 持久化白名單與跨頻道記憶範圍

擁有者及 `DISCORD_ADMIN_USER_IDS` 指定管理員可在白名單伺服器使用以下私密指令：

- `/admin menu`：以私密選單管理白名單頻道，以及查看、建立、刪除記憶群組、加入或移出群組頻道。首次啟動時才會把
  `.env` 的頻道清單移入資料庫；此後管理員移除的頻道不會在重啟後自動恢復。

所有管理操作都採 ephemeral 回覆並留下不含訊息內容的稽核紀錄。移除白名單或刪除分組不會刪除
既有 Discord 訊息、SQLite 原始資料、摘要或向量。

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

階段 8 的完整部署與還原操作請看 [`docs/deployment.md`](docs/deployment.md)。階段 9 已加入
額外 US$1／背景 US$0.25 閘門、每日 companion 20 次上限、內容最小化觀測、固定分類評價及
免費報告；啟動、結束與切換正式運行的流程請看 [`docs/trial.md`](docs/trial.md)。
