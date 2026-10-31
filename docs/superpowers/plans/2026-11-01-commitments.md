# 承諾追蹤 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 力口小幫手（公司版）從公司群組的對話抓出「答應客戶或同事的事＋期限」和「主動補位」，到期沒結案就每天早上私訊提醒管理者（Justin）。

**Architecture:** 新模組 `commitments_service.py`（純函式＋JSON 檔，好測）負責關鍵字預篩、AI 回覆解析、存檔、到期分類、清單文字、指令解析；`main.py` 只接線：群組裡「沒叫小幫手」的訊息先預篩，像承諾才丟背景工作叫 Claude 摘要；每天 08:30 排程私訊管理者；私訊「承諾」看清單、「承諾 完成 3」結案。

**Tech Stack:** Python 3、FastAPI、APScheduler、anthropic（沿用 event_parser 的 Claude 呼叫）、unittest。

**Spec:** Justin 11/01 07:2x 回「DoubleA 加承諾追蹤」，核可的內容是總指揮 07:20 那則：從群組對話抓答應客戶的事，到期沒做私訊提醒 Justin，也記下誰主動補位；只私訊不發群組；對話只送現有服務；上線要 Justin 說可以（`justin-commander/手機收件匣` 雲端版 `2026-11-01_0720_學習_OpenAI公司的未來_承諾追蹤.md`）。

## Global Constraints

- 只在公司版開（有 ALLOWED_GROUP_ID）；FEATURES_OFF 列 `commitments` 可關。家用版完全不動。
- 不在群組發言；提醒只私訊 ADMIN_USER_IDS；沒設 ADMIN_USER_IDS 就不推（只記）。
- 不存訊息全文，只存 AI 摘的一句（60 字內）；log 只印筆數和錯誤類型，不印訊息內容（威脅模型 5）。
- AI 回覆只當資料：只寫進承諾清單，不觸發刪除、轉發、改設定（威脅模型 6）。
- 花錢：只有通過關鍵字預篩的句子才叫 AI；每個群組每小時最多 20 次（rate_limiter 新增 `commitment`）（威脅模型 7）。
- 清單、結案指令只在私訊用；群組打「承諾」只回一句「請私訊我」。
- 合併上線要 Justin 說可以；先在私訊自己測，不在公司群組測。

## Review Focus

1. 一般聊天（「明天見」「下週再說」）不該被記成承諾：預篩要同時有「期限詞」和「動作詞」，AI 也被要求沒有明確承諾就回空清單。
2. AI 回錯格式、亂給日期（過去很久、一年後）要丟掉，不能讓程式掛掉。
3. 同一件事在群組被講兩次，不要記成兩筆。
4. 叫小幫手的指令（例如「小幫手 明天下午開會」）不能被當成承諾重複處理：只看沒叫小幫手的句子。
5. 沒有管理者 ID 時不能推給群組或亂推：只記不推。

---

### Task 1: commitments_service.py
- [x] 寫測試 `tests/test_commitments_service.py`：預篩、解析（含 ``` 包住、壞 JSON、錯 kind、壞日期、太遠的日期、太長截斷）、存檔去重、到期分類、清單與提醒文字、指令解析、enabled。
- [x] 實作，跑 `python -m unittest discover -s tests -v` 全過。

### Task 2: main.py 接線
- [x] `_commitments_on()`；webhook 群組訊息 `_addressed_text` 回 None 時預篩→`process_commitment` 背景工作。
- [x] `process_commitment`：限流→名字（MEMBERS／LINE 名稱）→`extract`（呼叫 Claude）→`add_items`；log 只印筆數。
- [x] `commitment_reminder_job` 08:30；lifespan 註冊（公司版且有 ADMIN_USER_IDS）。
- [x] `handle_command`：私訊「承諾」「承諾 完成 3」「承諾 取消 3」「承諾 說明」；群組回請私訊；管理者限定。
- [x] 選單一行、自我檢查一行、公司版隱私權頁補一段。
- [x] 冒煙測試：假環境變數載入 main，模擬群組訊息→記一筆→私訊清單→結案→提醒文字。

### Task 3: 文件
- [x] FEATURES.md、LINE_使用說明.txt、CLAUDE.md 檔案地圖各補一行。
- [x] `python -m py_compile *.py`、unittest 全過；PR 不合併，等 Justin 說可以。
