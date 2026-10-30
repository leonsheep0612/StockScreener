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

- 每個交易日 16:30 排程會篩選**所有**組合，結果存在 `data/results.json`。
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
   - LIFF 分頁 → Add：Size **Full**、Endpoint URL 先填 `https://example.com`（第 3 步部署後再改）、Scopes 勾 **openid**
   - 複製 **LIFF ID**（像 `2001234567-AbCdEfGh`）

### 3. 部署 Worker
```bash
cd worker
npm install
npx wrangler login
```
`wrangler.toml` 填入 `LIFF_ID`，再依序設定密鑰（每個指令會要你貼上值）：
```bash
npx wrangler secret put LINE_CHANNEL_SECRET
npx wrangler secret put LINE_CHANNEL_ACCESS_TOKEN
npx wrangler secret put LINE_USER_ID
npx wrangler secret put LIFF_CHANNEL_ID
npx wrangler secret put GITHUB_TOKEN
npx wrangler deploy
```
`LINE_CHANNEL_ACCESS_TOKEN`、`LINE_USER_ID` 和 GitHub Secrets 是同一組值（GitHub Secrets 無法讀出，請從 LINE 後台再複製一次：access token 在 Messaging API 分頁最下方，userId 在 Basic settings 的 Your user ID）。

部署完會得到網址，例如 `https://stock-screener-line.<帳號>.workers.dev`：
- LIFF 的 Endpoint URL 改成這個網址（結尾加 `/`）
- Messaging API 分頁 → Webhook URL 填 `網址/webhook`，按 Verify，打開 **Use webhook**
- LINE Official Account Manager → 回應設定 → 關閉「自動回應訊息」

### 4. 上傳圖文選單
```bash
pip install pillow requests
LINE_CHANNEL_ACCESS_TOKEN=… LIFF_ID=… python richmenu/setup_richmenu.py upload
```

### 5. 第一次執行
GitHub → Actions → Stock Screener Schedule → Run workflow。第一次會回補約一年的行情（日／週／月 KD 需要），大約 30～40 分鐘；之後每天只抓當天。

## 本機預覽
```bash
node -e "const fs=require('fs');fs.writeFileSync('worker/preview.html',fs.readFileSync('worker/src/settings.html','utf8').replace('__STRATEGIES__',fs.readFileSync('strategies.json','utf8')))"
```
用瀏覽器開 `worker/preview.html`，沒有 LIFF ID 時會以預覽模式顯示四種策略。
