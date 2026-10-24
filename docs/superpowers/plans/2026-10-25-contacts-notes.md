# 關係人經驗名錄（力口小幫手）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 公司群組打「名錄 王○○：備註」記一筆、「名錄 王○○」查，讓審查、會勘、交屋的經驗留在公司裡。

**Architecture:** 新檔 `contacts_service.py` 管資料：本機 `DATA_DIR/contacts.json` 是正本，PROGRESS_SHEET（或 CONTACTS_SHEET）開時同步到小幫手自己建的 Google 試算表（drive.file 權限只動自己建的檔），試算表手改的列也查得到。`main.py` 只做指令判斷與回覆，只在有 ALLOWED_GROUP_ID（公司版）且 FEATURES_OFF 沒關 contacts 時啟用。

**Tech Stack:** Python 3、FastAPI 既有架構、google-api-python-client（Sheets v4）、unittest。

**Spec:** Justin 10/25「學起來，運用」（Donna 律所關係人名錄）＋總指揮 AI工具清單 ㉑-4；欄位與「怎麼寫」規則照雲端硬碟範本「力口主言_關係人經驗名錄」。

## Global Constraints
- 欄位：姓名｜單位（機關／公司／社區）｜角色｜類別｜相關案件（樹狀圖編號＋案名）｜經驗備註（具體做法，不寫評語）｜標籤｜寫的人｜日期｜狀態。
- log 不印備註內容與姓名（威脅模型第 5 條）；只印錯誤類型。
- 不用 AI 判斷，指令直接處理（威脅模型第 6、7 條）。
- 家用版（沒有 ALLOWED_GROUP_ID）完全不出現這個功能。
- 合併前一定要 Justin 說可以。

## Review Focus
1. 「名錄」後面沒有名字，或只有冒號沒有內容 → 回用法，不記空白。
2. 全形冒號「：」、半形「:」、名字前後有空白都要認得。
3. 查的字是單位或標籤（例：名錄 估驗）也要找得到。
4. 試算表連不到（沒授權、被刪）→ 本機照記、回覆附一句原因；被刪就重建。
5. 同一人很多則 → 只列最新 10 則，附「還有 N 則」與表格連結。

---

### Task 1: contacts_service.py（資料與試算表同步）

**Files:**
- Create: `contacts_service.py`
- Test: `tests/test_contacts_service.py`

**Interfaces:**
- Produces:
  - `parse_command(text: str) -> tuple[str, str, str] | None`：回 ("add", 名字, 備註)、("find", 查詢字, "")、("help", "", "")；不是名錄指令回 None。
  - `add_note(name: str, note: str, author: str, day: str) -> dict`：備註裡的「#標籤」拆到 tags（用「、」接），回這筆。
  - `find(query: str) -> list[dict]`：名字、單位、標籤含查詢字（不分大小寫），新的在前。
  - `format_results(query: str, rows: list[dict], url: str = "", limit: int = 10) -> str`
  - `sync() -> str | None`、`sheet_url() -> str`、`status_text() -> str`、`sheet_enabled() -> bool`
  - `enabled(has_allowed_group: bool, features_off: str) -> bool`：公司版而且 FEATURES_OFF 沒有 contacts 才開。

- [ ] Step 1：寫測試 `test_parse_add_fullwidth_colon`（「名錄 王○○：估驗前一週要先寄估驗表」→ ("add","王○○","估驗前一週要先寄估驗表")）、`test_parse_add_halfwidth_colon`、`test_parse_find`（「名錄 估驗」→ ("find","估驗","")）、`test_parse_help_and_empty_note`（「名錄」、「名錄 王○○：」→ ("help","","")）、`test_add_and_find_by_name_unit_tag`、`test_format_limits_and_marks_former`（12 則只列 10、附「還有 2 則」；狀態前任標「（前任）」）、`test_sync_disabled_returns_none`、`test_enabled_requires_allowed_group`。
- [ ] Step 2：跑 `python -m unittest tests.test_contacts_service -v`，預期 ImportError。
- [ ] Step 3：照 Interfaces 實作；試算表用 progress_service 的 `_service`、`_status`、`_error_text`；建表兩頁「名錄」「怎麼寫」；未送出的列用 values.append；同步後把名錄整頁讀回存成 `sheet_rows` 供查詢；404 重建一次。
- [ ] Step 4：再跑測試，預期全部 PASS；`python -m py_compile *.py` 無輸出。
- [ ] Step 5：commit。

### Task 2: main.py 接指令、選單、自我檢查；說明文件

**Files:**
- Modify: `main.py`（import、`_feature_on` 說明、`_NO_TRIGGER_PREFIX`、`handle_command` 公司版區塊、`_menu_lines`、自我檢查）
- Modify: `LINE_使用說明.txt`、`FEATURES.md`、`CLAUDE.md`（檔案地圖加 contacts_service.py、環境變數 CONTACTS_SHEET）

**Interfaces:**
- Consumes：Task 1 全部函式。
- Produces：`_contacts_on() -> bool`（有 ALLOWED_GROUP_ID 且 `_feature_on("contacts")`）。

- [ ] Step 1：實作（條件判斷用 Task 1 的 `enabled`，已有測試）；寫的人＝`_member_in(_display_name(...))`，對不到用 LINE 名稱；記完回「✅ 已記進名錄」＋那一則；查之前先 sync。
- [ ] Step 2：`python -m unittest discover -s tests -v` 全 PASS；`python -m py_compile *.py`；`git diff` 自己再讀一次（log 沒有印備註）。
- [ ] Step 3：commit、push，更新 PR #16 標題與說明（不合併）。
