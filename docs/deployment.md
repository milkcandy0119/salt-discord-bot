# 階段 8 部署、備份與還原

## 範圍與安全邊界

這份流程先在本機 Docker 環境驗收，再原樣搬到 Ubuntu 24.04 LTS VPS。Docker 建置會下載
Python、uv、Restic image 與 Python 套件，但不會呼叫 OpenAI。`DISCORD_BOT_TOKEN`、
`OPENAI_API_KEY` 與 Restic 密碼都不得寫入 image 或提交 Git。

備份目錄若和正式資料位於同一顆 VPS 磁碟，只能防止部分人為錯誤，不能抵抗整顆磁碟或 VPS
遺失。正式上線時應把 `BACKUP_DIRECTORY` 指向另一顆掛載磁碟，或把
`RESTIC_REPOSITORY` 換成受控的 Restic S3 後端。

## 第一次準備

先停止目前直接以 Python 執行的機器人，避免同一個 Discord Token 同時登入兩個程序。

PowerShell：

```powershell
New-Item -ItemType Directory -Force data,runtime,backups,restore,secrets
[Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48)) |
    Set-Content -NoNewline -Encoding ascii secrets/restic_password.txt
Copy-Item .env.example .env  # 只有還沒有 .env 時才執行
```

既有 `.env` 不要被 `.env.example` 覆寫。備份密碼檔建立後應另外保存一份到密碼管理器；遺失
密碼就無法還原備份。不要把密碼內容貼到聊天、Issue 或日誌。

Linux VPS：

```bash
mkdir -p data runtime backups restore secrets
chmod 700 data runtime backups restore secrets
openssl rand -base64 48 > secrets/restic_password.txt
chmod 600 secrets/restic_password.txt
```

`.env` 中確認：

```env
APP_ENV=production
BACKUP_SECRET_FILE=./secrets/restic_password.txt
BACKUP_DAILY_TIME_UTC=19:00
BACKUP_KEEP_LAST=7
CONTAINER_UID=1000
CONTAINER_GID=1000
APP_DATA_DIRECTORY=./data
APP_RUNTIME_DIRECTORY=./runtime
BACKUP_DIRECTORY=./backups
```

Linux 的 UID／GID 應以 `id -u`、`id -g` 實際結果取代。`19:00 UTC` 對應臺灣時間隔日
`03:00`。

## 建置與第一次加密備份

```bash
docker compose build --pull
docker compose run --rm --no-deps backup python -m app.backup_cli init
docker compose run --rm --no-deps backup python -m app.backup_cli run
docker compose run --rm --no-deps backup python -m app.backup_cli verify
```

`init` 對同一個儲存庫只執行一次；已初始化時再次執行會安全失敗，不會重設密碼或清除備份。
`run` 的成功條件包括 SQLite 一致性快照、Restic 加密、完整讀取、實際解密還原、SQLite
完整性檢查、verified 標記及最後的 7 份輪替。

## 啟動與健康檢查

```bash
docker compose up -d
docker compose ps
docker compose logs --tail 100 bot
docker compose logs --tail 100 backup
```

成功時 `bot` 應顯示 `healthy`；`backup` 會顯示下一次 UTC 備份時間。Bot 的 migration 仍會在
每次啟動時自動執行。若 `bot` 顯示 `unhealthy`，可手動執行：

```bash
docker compose exec bot python -m app.healthcheck
```

健康檢查不連線 OpenAI，只檢查 Discord ready 心跳與 SQLite。Discord Token 錯誤、網路中斷、
事件迴圈卡住、心跳過期或 SQLite 無法讀取都會使容器不健康。

## 隔離還原演練

先確認 `restore/discord_assistant.db` 不存在，再執行：

```bash
docker compose run --rm --no-deps \
  -v ./restore:/restore \
  backup python -m app.backup_cli restore \
  --snapshot latest --target /restore/discord_assistant.db

docker compose run --rm --no-deps \
  -v ./restore:/restore:ro \
  -e DATABASE_URL=sqlite+aiosqlite:////restore/discord_assistant.db \
  bot python -m app.history_cli status
```

還原工具刻意拒絕覆寫既有檔案，也拒絕把目標設成正式資料庫。上述命令只驗收隔離副本；若
真的要用備份取代正式資料庫，必須先停止 `bot`、再備份現況，確認正確快照及路徑後才人工
置換，不應把這個破壞性步驟自動化。

## 搬移到 VPS

將 repository 複製到 VPS，安全建立新的 `.env` 與 Restic 密碼檔，再還原加密備份。不要把
開發電腦的 `.env` 或祕密檔透過 Git 傳送。容器啟動後先完成以下驗收，才停止舊主機：

1. `docker compose ps` 顯示 bot healthy。
2. `/bot status` 僅管理員可私密查看。
3. 建立一筆兩分鐘後的提醒並成功收到私訊。
4. 手動執行一次備份、verify 與隔離 restore。
5. 確認新舊主機不會同時使用相同 Discord Token 運行。
