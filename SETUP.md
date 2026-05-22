# DoubleA LINE Bot — 完整設定步驟

## 概覽

1. 建立 LINE Messaging API Channel
2. 建立 Google Cloud 專案 + 授權
3. 部署到 Railway
4. 設定 Webhook URL
5. 把 DoubleA 加進群組

---

## Step 1｜LINE Developer 設定

### 1-1 建立帳號與 Provider

1. 前往 [developers.line.biz](https://developers.line.biz/) 用你的 LINE 帳號登入
2. 點 **Create a new provider** → 輸入名稱（如 `Atticus Family`）
3. 點 **Create a new channel** → 選 **Messaging API**
4. 填寫：
   - Channel name：`DoubleA`
   - Channel description：家庭行事曆助理
   - Category：個人 / 家庭
5. 勾選同意條款，建立 Channel

### 1-2 取得憑證

在 Channel 設定頁面：

- **Basic settings** → 複製 `Channel secret`（等等要用）
- **Messaging API** → 點 **Issue** 產生 `Channel access token (long-lived)`

### 1-3 開啟加入群組功能

**Messaging API** 頁面 → **Allow bot to join group chats** → 開啟

---

## Step 2｜Google Cloud 設定

### 2-1 建立專案

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 點右上角的專案選單 → **New Project** → 名稱：`DoubleA Bot`
3. 啟用 **Google Calendar API**：
   - 搜尋 "Google Calendar API" → **Enable**

### 2-2 建立 OAuth 憑證

1. 左側選單 → **APIs & Services** → **Credentials**
2. **Create Credentials** → **OAuth client ID**
3. 如果還沒設定同意畫面：**Configure Consent Screen** → 選 **External** → 填好基本資料
4. 回到 Credentials → **Create Credentials** → **OAuth client ID**
   - Application type：**Desktop app**
   - 名稱：`DoubleA`
5. 下載 JSON → 改名為 `credentials.json` → 放到 `line-bot/` 資料夾

### 2-3 本機授權（只做一次）

```bash
cd line-bot
uv run auth_setup.py
```

瀏覽器會開啟 → 用 **atticus.wu@gmail.com** 登入 → 允許存取行事曆

完成後終端機會輸出一段 base64 字串，複製起來備用（Step 3 要用）。

---

## Step 3｜部署到 Railway

### 3-1 建立 GitHub Repo

```bash
# 在 line-bot/ 資料夾
git init
git add .
git commit -m "Initial DoubleA bot"
```

建一個新的 GitHub repo（例如 `doublea-line-bot`），push 上去。

> ⚠️ 確認 `.gitignore` 有排除 `credentials.json`、`token.json`、`.env`

### 3-2 Railway 部署

1. 前往 [railway.app](https://railway.app/) 用 GitHub 登入
2. **New Project** → **Deploy from GitHub repo** → 選你的 repo
3. Railway 會自動偵測 Python 專案並部署

### 3-3 設定環境變數

在 Railway 專案的 **Variables** 頁面，加入以下四個變數：

| 變數名稱 | 值 |
|---------|---|
| `LINE_CHANNEL_SECRET` | Step 1-2 複製的 Channel secret |
| `LINE_CHANNEL_ACCESS_TOKEN` | Step 1-2 的 Channel access token |
| `ANTHROPIC_API_KEY` | 你的 Anthropic API key |
| `GOOGLE_TOKEN_JSON` | Step 2-3 輸出的 base64 字串 |

設定完畢後 Railway 會自動重新部署。

### 3-4 取得你的 Webhook URL

Railway 部署成功後，在 **Settings** → **Domains** 取得網址，格式如：
```
https://doublea-bot-xxxx.railway.app
```

你的 Webhook URL 是：
```
https://doublea-bot-xxxx.railway.app/webhook
```

---

## Step 4｜設定 LINE Webhook

1. 回到 LINE Developers → Messaging API 設定頁
2. **Webhook URL** → 填入你的 Railway webhook URL
3. 點 **Verify** → 應該顯示 `Success`
4. 開啟 **Use webhook** 開關

---

## Step 5｜把 DoubleA 加進群組

1. 在 LINE Developers → **Messaging API** → 用 QR Code 加 DoubleA 為好友
2. 建立一個 LINE 群組，把你、Angel、DoubleA 都加進去
3. 發一則測試訊息：「明天下午3點要去看醫生」
4. DoubleA 應該回覆確認訊息，Angel 的 Gmail 也會收到行事曆邀請

---

## 測試範例

| 訊息 | 預期結果 |
|------|---------|
| `明天下午3點看醫生` | ✅ 加入行事曆 |
| `下週五晚上6點吃飯` | ✅ 加入行事曆 |
| `5/30 去台北出差` | ✅ 加入行事曆（09:00） |
| `我愛你老婆` | ❌ 無動作 |
| `記得買牛奶` | ❌ 無動作（無具體時間） |
| `今天好累` | ❌ 無動作 |

---

## 排錯

- **Webhook Verify 失敗**：確認 Railway 服務正在運行，試試 `/health` 端點
- **行事曆沒建立**：檢查 Railway logs，確認 `GOOGLE_TOKEN_JSON` 設定正確
- **DoubleA 沒回應**：確認 LINE Channel 的 Webhook 已開啟
