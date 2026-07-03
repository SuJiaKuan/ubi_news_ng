# UBI Taiwan 每日新聞 Slack 推播工具

每天自動抓取 UBI（無條件基本收入）相關新聞，去重、用 AI 判斷相關度並產生台灣繁體中文總結，
推播到指定 Slack 頻道。

## 運作方式

1. 讀取 `config/sources.yaml`，抓取所有 RSS 來源與 Google News 關鍵字搜尋。
2. 解析 Google News 的轉址、去除追蹤參數，正規化成原文網址。
3. 比對 `state/seen.json`，過濾掉已經推播過的網址，並只保留最近 `LOOKBACK_DAYS` 天內的項目。
4. 呼叫 Gemini API，同時完成「相關度判斷、標題翻譯、總結」，不相關者丟棄。
5. 用 Slack Block Kit 組裝訊息並推播（超過 50 個 block 會自動分批）。
6. 把本次成功推播的網址寫回 `state/seen.json` 並 commit 回 repo。

## 部署設定

### GitHub Secrets（`Settings → Secrets and variables → Actions → Secrets`）

| 名稱 | 必填 | 說明 |
|------|------|------|
| `GEMINI_API_KEY` | ✅ | Gemini API 金鑰 |
| `SLACK_WEBHOOK_URL` | ✅ | 目標 Slack 頻道的 Incoming Webhook URL |

### GitHub Variables（`Settings → Secrets and variables → Actions → Variables`，非必填）

| 名稱 | 預設值 | 說明 |
|------|--------|------|
| `LLM_MODEL` | `gemini-2.5-flash` | Gemini 模型名稱 |
| `LOOKBACK_DAYS` | `3` | 只處理最近幾天內發布的項目 |
| `POST_WHEN_EMPTY` | `false` | 當天無新消息時，是否仍推播一則提示訊息 |

### Workflow 權限

`.github/workflows/daily-ubi-news.yml` 需要 `permissions: contents: write`，
用來把更新後的 `state/seen.json` commit 回 repo（已在 workflow 中設定好）。

## 如何新增 / 移除新聞來源

不需要改程式碼，直接編輯 `config/sources.yaml`：

- **RSS 來源**：在 `rss_sources` 底下新增一筆 `name` / `url`。
- **Google News 關鍵字搜尋**：在 `google_news` 底下新增一筆 `name` / `query` / `lang` / `geo` / `ceid`。
- 若某個來源長期失效，可以把該筆整段用 `#` 註解掉，或直接刪除。

## 已知事項與設計取捨

- **GitHub Actions 排程精準度**：`schedule` cron 在尖峰時段可能延遲數分鐘，請不要依賴精準到分。
- **60 天無 commit 會停用排程**：本工具每次執行只要有推播就會 commit `state/seen.json`，
  正常運作下不會觸發這個問題。
- **Scott Santens 官網 RSS 已停用**：`https://www.scottsantens.com/rss/`、`/feed/` 目前都回傳
  404（該站已改版，不再是 Ghost 平台），因此 `config/sources.yaml` 中此來源已註解掉。若未來
  官網恢復 RSS，可自行取消註解並確認正確的 feed URL。
- **Google News 轉址解析**：Google News RSS 的 `<link>` 是 `news.google.com/rss/articles/...`
  的內部轉址，不會用一般 HTTP redirect 導向原文。`src/dedup.py` 會先嘗試離線 base64 解碼，
  失敗時再向 Google 換取簽章（signature/timestamp）後解出原文 URL；若整個解析流程失敗，
  會退回使用原始轉址（僅記警告 log，不中斷流程）。這個內部機制屬於 Google 未公開的行為，
  未來仍有可能失效；短時間內解析太多筆也可能被 Google 回傳 429（Too Many Requests）。
- **同一則新聞被兩個來源重複抓到時的保險機制**：當 Google News 轉址解析失敗（例如遇到上述
  429），該則新聞會退回使用未解析的 `news.google.com` 轉址網址，這個網址跟原站 RSS（例如
  BIEN）給的原文網址雜湊值不同，單靠 URL 去重會誤判成兩則不同新聞而重複推播。因此
  `src/main.py` 在建立候選清單時，額外用標題比對（`dedup.is_likely_same_story`）當作第二層
  保險：偵測到同一批候選項目標題相同（並容忍 Google News 在標題後面加上的
  `- <來源名稱>` 後綴）時，只保留其中一則，優先保留已成功解析出原文網址的那一份。
- **無發布日期的項目**：採保守策略，直接跳過不推播，避免把無法判斷新舊的文章誤當成當日
  最新消息（可在 `src/main.py` 的 `run()` 中調整此行為）。
- **Slack 送出失敗**：批次之間彼此獨立重試，即使某一批最終失敗也會繼續嘗試其他批次；
  只要有任何一批最終失敗，整個 job 會以非零 exit code 結束（Actions 會顯示紅燈），
  但已經送出成功的批次仍會寫入 `state/seen.json`，避免明天重複推播。

## 本機測試

```bash
pip install -r requirements.txt

export GEMINI_API_KEY=xxx
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/xxx
export LOOKBACK_DAYS=3
export POST_WHEN_EMPTY=false

python src/main.py
```

## 未來擴充（本版尚未實作）

- 多頻道 / 依主題分類推播。
- 每週彙整週報。
- 來源健康度告警（連續數日抓不到內容時通知）。
- 中英雙語切換或原文標題並陳。
