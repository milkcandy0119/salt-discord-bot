# 階段 9 七天保守試跑

## 安全邊界

migration 與重新啟動 Bot 都不會自動開始試跑。只有本機執行固定確認文字後，才會保存基準並
開始七天計時。基準一旦建立就不能刪除重建來取得第二份額度。

試跑同時受以下四個條件限制：

1. 永久全域 US$10。
2. 永久背景 US$3。
3. 試跑新增全域 US$1。
4. 試跑新增背景 US$0.25。

任一條件不足就不送出新的付費 AI 請求。提醒、訊息保存及免費管理不受影響。

## Docker 驗收後開始

先確認：

```powershell
docker compose ps
docker compose run --rm --no-deps backup python -m app.backup_cli verify
docker compose exec bot python -m app.healthcheck
```

Bot 應為 `healthy`，備份與健康檢查應成功。開始前免費查看狀態：

```powershell
docker compose exec bot python -m app.trial_cli status
```

第一次應顯示：

```json
{"status": "not_started"}
```

明確開始：

```powershell
docker compose exec bot python -m app.trial_cli start --confirmation "確認啟動階段 9 七天保守試跑"
```

成功輸出會包含 `status: active`、開始／結束時間、兩個增量額度及目前 0 用量。

## 日常查看與評價

本機完整彙總：

```powershell
docker compose exec bot python -m app.trial_cli status
docker compose exec bot python -m app.trial_cli report
```

Discord 管理員可私密使用：

```text
/bot trial-status
/trial feedback message_id:訊息ID category:too_formal
```

Discord 訊息 ID 可在開發者模式下對訊息按右鍵選擇「複製訊息 ID」。不要把訊息正文貼進
`message_id`。評價分類為：

- `good`：回覆合適。
- `too_formal`：太官腔或過度求證。
- `wrong_memory`：使用了錯誤或混淆的記憶。
- `unwanted_reply`：不應加入這段對話。
- `missed_reply`：應該回覆卻保持安靜。
- `other`：其他需要人工查看的案例。

## 暫停、恢復與結束

發現疑似隱私、記憶混用、重複回覆或品質異常時，先免費暫停：

```powershell
docker compose exec bot python -m app.trial_cli pause
```

暫停會阻擋新的付費預留，但 Bot 仍保存白名單訊息並提供免費功能。確認安全後恢復：

```powershell
docker compose exec bot python -m app.trial_cli resume --confirmation "確認恢復階段 9 試跑"
```

七天期滿或要提前結束：

```powershell
docker compose exec bot python -m app.trial_cli finish --confirmation "確認結束階段 9 試跑"
docker compose exec bot python -m app.trial_cli report
```

結束後不得恢復或重設基準，新付費預留維持停止。若未來要進行第二輪付費試跑，必須先設計
新的階段與明確額度，不能直接清除資料表。
