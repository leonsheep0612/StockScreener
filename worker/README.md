# LINE 選股小幫手（Cloudflare Worker）

```
LINE 圖文選單 ──▶ Cloudflare Worker ──▶ GitHub（config.json / data/results.json / 觸發 Actions）
 ⚙️ 條件設定   → LIFF 設定頁：從四種策略範本新增組合、加減條件區塊、設定推播日
 📊 查看結果   → Flex 卡片（每個組合一張，左右滑）：看結果／改條件／立即篩選
 📋 用量與排程 → 本月推播額度、各組合推播日
```

不需要 Render：Worker 不會休眠、回應快，免費額度每天 10 萬次。

## 四種策略（依《用公開資訊從選股到精準進場》）

| 策略 | 條件區塊 | 資料來源 |
|---|---|---|
| 📈 長期成長股（CAN SLIM） | 單季 EPS／營收年增、年度 EPS 年增、近 4 季 ROE、累季 EPS；①～④ 強度一鍵套用 | 公開資訊觀測站綜合損益表（每季） |
| ⚡ 中短線強勢股 | +DI／−DI／ADX、布林 %b 與帶寬、日週月 KD、5MA>20MA、成交量、千張大戶不減少 | 證交所／櫃買中心每日行情（開高低收） |
| 🐳 主力操作股 | 千張大戶週增、法人連續買超、三個月累計同步、當月外資投信佔比、單日外資大買 | 集保股權分散表、三大法人買賣超 |
| 💰 高殖利率價值股 | 現金股利、殖利率、本益比、淨值比、季線之上且上揚 | 證交所／櫃買中心本益比殖利率 |

- 任何組合都能自由加入其他區塊，多個區塊可選「分開列出」或「只列同時符合」（交集，例如成長股 ∩ 三個月法人同步）。
- 尚未支援：書中成長股第 ⑤ 組的自由現金流條件；ROE 以「近 4 季 EPS ÷ 每股淨值」估算。

## 推播額度怎麼省

- 每個交易日 17:00 由 Worker 的 cron 觸發篩選**所有**組合，17:30 再補一次（17:00 成功就略過；GitHub 自己的排程常延遲好幾小時，只當 18:00 的備援），結果存在 `data/results.json`。
- 「查看結果」的卡片和結果都是**回覆訊息**，免費、不限次數。
- 預設只有「主力操作股」每個交易日推播（延續原本的每日推播，約 22 則／月），其他組合要推播再到設定頁勾選；同一天的組合會合併成 1 則，多勾不會多花額度。
- 推播只送給 `LINE_USER_ID` 這一個人，其他人加好友不會增加用量。
- 沒有安排推播的日子，連錯誤通知也不推（執行紀錄可到 GitHub Actions 查看）。
- 「立即篩選」會推播 1 則，按下前會再確認一次。
- 推播前會查詢額度，剩不到 3 則就不推播（結果仍可點選查看）。

## 設定步驟

已經有的（之前接 Messaging API 時設好）：GitHub Secrets 的 `LINE_CHANNEL_ACCESS_TOKEN`、`LINE_USER_ID`。
以下是還需要做的：

### 1. GitHub token（讓 Worker 能改設定、觸發篩選）
GitHub → 右上頭像 → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token
- Repository access：Only select repositories → `StockScreener`
- Repository permissions：**Contents** = Read and write、**Actions** = Read and write
- 複製 token（只會顯示一次）

### 2. LINE Developers 後台（https://developers.line.biz/console/）
1. 進入官方帳號所在的 **Provider** → 原本的 **Messaging API channel**
   - Basic settings → 複製 **Channel secret**
2. 同一個 Provider → Create a new channel → **LINE Login**（App types 勾 Web app）
   - 必須和官方帳號在**同一個 Provider**，userId 才會相同
   - Basic settings → 複製 **Channel ID**
   - LIFF 分頁 → Add：Size **Full**、Endpoint URL 先填 `https://example.com`（第 5 步部署後再改）、Scopes 勾 **openid**
   - 複製 **LIFF ID**（像 `2001234567-AbCdEfGh`）

### 3. Cloudflare（Worker 由 GitHub Actions 部署，不用在本機裝任何東西）
1. 註冊 https://dash.cloudflare.com （免費），左側 **Workers & Pages** 點進去一次，依提示設定你的 `workers.dev` 子網域
2. 右上頭像 → **My Profile** → **API Tokens** → Create Token → 範本 **Edit Cloudflare Workers** → Use template
   - Account Resources 選你的帳號、Zone Resources 選 All zones → Continue → Create Token，複製 token
3. **Account ID**：Workers & Pages 頁面右側（或網址 `dash.cloudflare.com/` 後面那串）

### 4. GitHub Secrets（repo → Settings → Secrets and variables → Actions → New repository secret）
| 名稱 | 值 |
|---|---|
| `CLOUDFLARE_API_TOKEN` | 第 3 步的 API token |
| `CLOUDFLARE_ACCOUNT_ID` | 第 3 步的 Account ID |
| `LINE_CHANNEL_SECRET` | Messaging API channel 的 Channel secret |
| `LIFF_ID` | LIFF ID |
| `LIFF_CHANNEL_ID` | LINE Login channel 的 Channel ID |
| `WORKER_GITHUB_TOKEN` | 第 1 步的 GitHub token（GitHub 不允許以 `GITHUB_` 開頭命名） |

`LINE_CHANNEL_ACCESS_TOKEN`、`LINE_USER_ID` 已經有了，不用重設。

### 5. 部署
GitHub → **Actions** → **Deploy LINE Worker** → Run workflow（勾選上傳圖文選單）。
完成後在「部署 Cloudflare Worker」步驟的紀錄裡找到網址，例如 `https://stock-screener-line.<子網域>.workers.dev`，回 LINE Developers：
- LINE Login channel → LIFF → Endpoint URL 改成這個網址（結尾加 `/`）
- Messaging API channel → Webhook URL 填 `網址/webhook`，按 **Verify**，打開 **Use webhook**
- LINE Official Account Manager → 回應設定 → 關閉「自動回應訊息」

之後 `worker/` 或 `strategies.json` 有改動推上 GitHub 時會自動重新部署。

### 6. 第一次篩選
GitHub → Actions → Stock Screener Schedule → Run workflow。第一次會回補約一年的行情（日／週／月 KD 需要），大約 30～40 分鐘；之後每天只抓當天。

## 本機預覽
```bash
node -e "const fs=require('fs');fs.writeFileSync('worker/preview.html',fs.readFileSync('worker/src/settings.html','utf8').replace('__STRATEGIES__',fs.readFileSync('strategies.json','utf8')))"
```
用瀏覽器開 `worker/preview.html`，沒有 LIFF ID 時會以預覽模式顯示四種策略。
