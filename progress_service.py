"""公司版：同事專案進度表（存在 DATA_DIR/progress.json；PROGRESS_SHEET=on 時也存一份在 Google 試算表，雙向同步）。
同事貼週報時記下每個專案的進度原文（record_report）；試算表兩頁：
「最新進度」可以直接改、刪、加列，下次同步小幫手就照著改；「回報紀錄」每次週報加列，只增不改。"""
import json
import os
import threading
from datetime import date, datetime, timedelta

import pytz

from paths import data_path

PROGRESS_FILE = data_path("progress.json")
LATEST, LOG = "最新進度", "回報紀錄"
LATEST_HEAD = ["負責人", "專案", "最新進度", "回報日", "狀態"]
LOG_HEAD = ["日期", "負責人", "專案", "進度"]
STALE_DAYS = 14
STALE_MARK = "⚠️ 兩週沒更新"
LOG_LIMIT = 2000  # 本機最多留幾筆回報紀錄
_LOCK = threading.Lock()


def sheet_enabled() -> bool:
    return os.environ.get("PROGRESS_SHEET", "").strip().lower() in ("1", "true", "yes", "on")


def _today() -> date:
    return datetime.now(pytz.timezone("Asia/Taipei")).date()


def _load() -> dict:
    try:
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {"sheet_id": data.get("sheet_id", ""), "sheet_url": data.get("sheet_url", ""),
            "rows": dict(data.get("rows", {})), "changed": list(data.get("changed", [])),
            "deleted": list(data.get("deleted", [])),
            "log": list(data.get("log", [])), "log_sent": int(data.get("log_sent", 0)),
            "last_sync": data.get("last_sync", ""), "last_error": data.get("last_error", "")}


def _save(data: dict) -> None:
    tmp = PROGRESS_FILE + ".tmp"  # 先寫暫存檔再換掉，別的執行緒不會讀到寫一半的檔
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PROGRESS_FILE)


def _key(owner: str, project: str) -> str:
    return f"{owner}|{project}"


def record_report(owner: str, items: list[tuple[str, str]], day: str) -> None:
    """週報裡每個（專案, 進度原文）記成這個人這案的最新進度，並加一筆回報紀錄（還沒寫到試算表的先留著）。"""
    with _LOCK:
        data = _load()
        for project, progress in items:
            k = _key(owner, project)
            data["rows"][k] = {"owner": owner, "project": project, "progress": progress, "date": day}
            if k not in data["changed"]:
                data["changed"].append(k)
            data["log"].append([day, owner, project, progress])
        drop = max(0, len(data["log"]) - LOG_LIMIT)
        data["log"], data["log_sent"] = data["log"][drop:], max(0, data["log_sent"] - drop)
        _save(data)


def remove_project(project: str) -> int:
    """專案結束（「刪負責」）：每個人這案的進度都拿掉，下次同步試算表也刪掉；回傳拿掉幾列。"""
    with _LOCK:
        data = _load()
        gone = [k for k, r in data["rows"].items() if r["project"] == project]
        for k in gone:
            del data["rows"][k]
            data["changed"] = [c for c in data["changed"] if c != k]
            if k not in data["deleted"]:
                data["deleted"].append(k)
        _save(data)
        return len(gone)


def latest() -> list[dict]:
    return list(_load()["rows"].values())


def sheet_url() -> str:
    return _load()["sheet_url"]


def is_stale(day: str) -> bool:
    try:
        return date.fromisoformat(day) <= _today() - timedelta(days=STALE_DAYS)
    except (TypeError, ValueError):
        return False  # 手改成看不懂的日期就不標


def _service():
    from googleapiclient.discovery import build

    from google_auth import get_credentials
    return build("sheets", "v4", credentials=get_credentials(), cache_discovery=False)


def _status(e: Exception) -> int | None:
    return getattr(getattr(e, "resp", None), "status", None) or getattr(e, "status_code", None)


def _error_text(e: Exception) -> str:
    s = str(e)
    if _status(e) == 403 or "insufficient" in s.lower() or "scope" in s.lower():
        return "還沒授權：請私訊小幫手「重新授權」，用力口信箱登入（要多一個建試算表的權限）"
    return f"{type(e).__name__}：{s[:120]}"


def _member_rank(owner: str) -> int:
    members = [m.strip() for m in os.environ.get("MEMBERS", "").split(",") if m.strip()]
    return members.index(owner) if owner in members else len(members)


def _create(svc) -> tuple[str, str]:
    title = os.environ.get("PROGRESS_SHEET_TITLE", "").strip() or "同事專案進度表"
    book = svc.spreadsheets().create(
        body={"properties": {"title": title}, "sheets": [{"properties": {"title": LATEST}}, {"properties": {"title": LOG}}]},
        fields="spreadsheetId,spreadsheetUrl").execute()
    sid = book["spreadsheetId"]
    svc.spreadsheets().values().update(spreadsheetId=sid, range=f"'{LOG}'!A1", valueInputOption="RAW",
                                       body={"values": [LOG_HEAD]}).execute()
    return sid, book.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{sid}"


def _cells(values: list) -> list[list[str]]:
    """試算表讀回來的格子補齊成 5 欄文字（API 會省略列尾的空格）。"""
    return [([str(c) for c in v] + [""] * 5)[:5] for v in values]


def _rows_from(table: list[list[str]]) -> dict:
    """「最新進度」第 2 列起 → {負責人|專案: 列}；負責人或專案空白的列跳過。"""
    rows = {}
    start = 1 if table and table[0][0].strip() == LATEST_HEAD[0] else 0  # 標題列被刪了，第一列就是資料
    for owner, project, progress, day, _ in table[start:]:
        owner, project = owner.strip(), project.strip()
        if owner and project:
            rows[_key(owner, project)] = {"owner": owner, "project": project, "progress": progress, "date": day.strip()}
    return rows


def _table(rows: dict) -> list[list[str]]:
    ordered = sorted(rows.values(), key=lambda r: _member_rank(r["owner"]))
    return [LATEST_HEAD] + [[r["owner"], r["project"], r["progress"], r["date"], STALE_MARK if is_stale(r["date"]) else ""]
                            for r in ordered]


def sync() -> str | None:
    """和試算表同步：先讀試算表（別人手改的、刪的、加的都算數），本機還沒寫上去的變更蓋過去，再整頁寫回、補上回報紀錄。
    沒開 PROGRESS_SHEET 回 None；成功回 None；失敗回原因（本機資料留著，下次再同步）。試算表被刪了就重建一份。"""
    if not sheet_enabled():
        return None
    with _LOCK:
        data = _load()
        for _ in range(2):
            try:
                svc = _service()
                if not data["sheet_id"]:
                    data["sheet_id"], data["sheet_url"] = _create(svc)
                    data["changed"], data["deleted"], data["log_sent"] = list(data["rows"]), [], 0
                    _save(data)
                    current = []
                else:
                    current = _cells(svc.spreadsheets().values().get(
                        spreadsheetId=data["sheet_id"], range=f"'{LATEST}'!A1:E").execute().get("values", []))
                merged = _rows_from(current)
                for k in data["changed"]:
                    if k in data["rows"]:
                        merged[k] = data["rows"][k]
                for k in data["deleted"]:
                    merged.pop(k, None)
                table, values = _table(merged), svc.spreadsheets().values()
                if table != current:  # 沒變就不重寫，不會蓋到別人正在改的格子
                    values.clear(spreadsheetId=data["sheet_id"], range=f"'{LATEST}'!A1:E", body={}).execute()
                    values.update(spreadsheetId=data["sheet_id"], range=f"'{LATEST}'!A1", valueInputOption="RAW",
                                  body={"values": table}).execute()
                if data["log"][data["log_sent"]:]:
                    values.append(spreadsheetId=data["sheet_id"], range=f"'{LOG}'!A1", valueInputOption="RAW",
                                  insertDataOption="INSERT_ROWS", body={"values": data["log"][data["log_sent"]:]}).execute()
                data.update(rows=merged, changed=[], deleted=[], log_sent=len(data["log"]), last_error="",
                            last_sync=datetime.now(pytz.timezone("Asia/Taipei")).strftime("%m/%d %H:%M"))
                _save(data)
                return None
            except Exception as e:
                if _status(e) == 404 and data["sheet_id"]:
                    print(f"[DoubleA] 進度表找不到（可能被刪了），重建一份：{data['sheet_id']}")
                    data["sheet_id"], data["sheet_url"] = "", ""
                    continue
                data["last_error"] = _error_text(e)
                _save(data)
                print(f"[DoubleA] 進度表同步失敗（本機已記）：{e}")
                return data["last_error"]
        return data["last_error"] or "進度表重建失敗"


def status_text() -> str:
    """自我檢查用的一句話。"""
    data = _load()
    if not sheet_enabled():
        return f"只存在小幫手（{len(data['rows'])} 筆；PROGRESS_SHEET 沒開）"
    if data["last_error"]:
        return data["last_error"]
    if data["sheet_id"]:
        return f"已連到 Google 試算表（{len(data['rows'])} 筆，最後同步 {data['last_sync']}）"
    return "還沒建立（下一次週報或查詢會自動建立）"
