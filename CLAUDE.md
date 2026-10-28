# DoubleA（LINE 小幫手）

> 給 Claude 的工作說明，一律繁體中文。2026-10-24 建立（Justin 在總指揮待辦板選「要，寫好給我看」）。

## 這是什麼
- 一份程式碼，兩個 LINE 官方帳號：
  - 家用版「培正家AI小幫手」：培正家群組（總樹狀圖 個人 7.3）。
  - 公司版「力口小幫手」：力口全組（公司 6.5.1），設 `BOT_NAME=力口小幫手` 和 `ALLOWED_GROUP_ID`。
- 部署在 Railway，兩個服務各一份環境變數。README 寫的 Cloud Run 是早期做法。資料檔放 Railway Volume，`DATA_DIR=/data`。
- Python＋FastAPI，入口 `main.py`。合併到 main 後 Railway 會自動部署，等於直接上線。

## 檔案地圖
| 檔 | 做什麼 |
|---|---|
| main.py | Webhook、指令判斷、排程端點、自我檢查、重新授權頁 |
| event_parser.py | 用 Claude 和 Gemini 把一句話判斷成行程或待辦（模型由環境變數設定） |
| calendar_service.py、todo_service.py | Google 行事曆、Google Tasks |
| google_auth.py、reauth_service.py、auth_setup.py | Google 授權、網頁版重新授權、本機一次性授權 |
| state_service.py、paths.py | 狀態與資料檔位置 |
| progress_service.py、owners_service.py | 公司版：同事專案進度表、專案負責人 |
| contacts_service.py | 公司版：關係人經驗名錄（「名錄 姓名：內容」記、「名錄 姓名」查；本機＋小幫手自建試算表，CONTACTS_SHEET 沒設就跟 PROGRESS_SHEET） |
| bible_plan_service.py、proverbs_service.py、birthday_service.py | 讀經進度、箴言、生日 |
| finance_service.py、shopping_service.py、notes_service.py | 記帳、購物清單、筆記 |
| restaurant_service.py、weather_service.py | 附近餐廳、天氣 |
| rate_limiter.py | 記憶體限流（圖片辨識等） |
| selftest.py | 自我檢查，只回報正常或異常，不印金鑰 |

## 端點與保護
| 路徑 | 保護 |
|---|---|
| POST /webhook | LINE 簽章（X-Line-Signature，HMAC-SHA256），驗過才讀內容 |
| POST /morning-briefing、/daily-reminder、/check-reminders | `token`：SELFTEST_TOKEN，或「重新授權」給的 15 分鐘一次性碼 |
| GET /selftest | `?token=SELFTEST_TOKEN`；沒設 SELFTEST_TOKEN 就停用 |
| GET、POST /reauth | 一次性連結；設了 ADMIN_USER_IDS 就只給這些人 |
| GET /health、/、/privacy | 公開，不放任何資料 |

## 兩個版本靠環境變數分開
- `ALLOWED_GROUP_ID`：只讓這些群組和群組裡的人使用（公司版）。沒設就是誰都能用（家用版）。
- `ADMIN_USER_IDS`：能用「重新授權」的人。沒設就不限。
- `FEATURES_OFF`、`GROUP_TRIGGER`、`MEMBERS`、`CALENDAR_*`、`PROGRESS_SHEET` 等，說明寫在 main.py 各函式的註解。
- 金鑰只放 Railway 環境變數，`.env.example` 只有名稱：LINE_CHANNEL_SECRET、LINE_CHANNEL_ACCESS_TOKEN、GEMINI_API_KEY、ANTHROPIC_API_KEY、GOOGLE_TOKEN_JSON、GOOGLE_CLIENT_ID、GOOGLE_CLIENT_SECRET、SELFTEST_TOKEN、WEATHER_API_KEY、GOOGLE_MAPS_API_KEY。

## 威脅模型：改程式前先想這 7 條
1. 任何人都能加官方帳號好友、私訊它。家用版沒設 ALLOWED_GROUP_ID 時，陌生人也能用指令。家用版的「重新授權」要不要限制，見總指揮 ISSUES 10/19，待 Justin 決定。
2. 偽造 Webhook：一定先驗簽章再讀內容，不要加不驗章的路徑。
3. 排程端點會對群組發訊息：一定要有通行碼；token 不印進 log、不放進回覆。
4. 金鑰外洩：10/22 有 Railway 使用者被盜刷。程式、log、回覆都不印金鑰；懷疑外洩就照總指揮 `後台/LINE資安自檢清單.md` 輪替。
5. 訊息內容是家人和同事的資料：log 不印訊息全文（10/24 修補）；對話只送 Claude、Gemini 和 Google，不送其他服務。
6. AI 判斷出來的結果是資料，不是指令：不要讓模型的輸出直接變成刪除、轉發或改設定。
7. 花錢：AI 呼叫、圖片辨識要限流；推播量受 LINE 方案限制，晚間推播、活動提醒預設關閉。

## 改程式的規矩
- 照總指揮的流程：writing-plans → systematic-debugging → verification-before-completion。
- 測試只有 `tests/`（目前是名錄）：`python -m unittest discover -s tests -v`。改完至少跑 `python -m py_compile *.py`；先在私訊自己測，不在公司群組測。
- 上線後開 `/selftest?token=…`，或在 Railway 跑 `python selftest.py`，全部正常才算完成。
- 公司群組看得到的文字（行事曆標題、推播）先在私訊試；標題只用成員名字（MEMBERS），不寫同事家人的名字。
- 合併前一定要 Justin 說可以；不刪群組資料、不改別人的行事曆、不替 Justin 發訊息給同事或家人。

## 相關文件
README.md（早期安裝步驟）、SETUP.md、FEATURES.md（功能清單）、LINE_使用說明.txt（給家人同事的指令說明）、DISCUSSION_NOTES.md。
