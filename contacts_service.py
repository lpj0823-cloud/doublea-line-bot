"""公司版：關係人經驗名錄（存在 DATA_DIR/contacts.json；CONTACTS_SHEET=on，或沒設而 PROGRESS_SHEET=on 時，
也同步到小幫手自己建的 Google 試算表）。群組打「名錄 王○○：估驗前一週要先寄估驗表」記一筆、「名錄 王○○」查。
學自 Donna 律所的法官／對造律師名錄：寫具體做法、不寫評語；換人標「前任」不刪；每則留寫的人和日期。
試算表「名錄」可以直接改、刪、加列，查的時候照表上的；小幫手記的新列只往下加，不會蓋掉表上的內容。"""
import json
import os
import re
import threading
from datetime import date, datetime

import pytz

from paths import data_path

TAB, RULES_TAB = "名錄", "怎麼寫"
HEAD = ["姓名", "單位（機關／公司／社區）", "角色", "類別", "相關案件（樹狀圖編號＋案名）",
        "經驗備註（具體做法，不寫評語）", "標籤", "寫的人", "日期", "狀態"]
RULES = [
    "關係人經驗名錄：怎麼寫",
    "目的：每一次審查、會勘、交屋的經驗都留在公司裡；換人承辦，經驗不會斷。",
    "1. 寫具體做法，不寫評語。✅「估驗前一週要先寄估驗表」　❌「很難搞」",
    "2. 一則寫一件事，寫上寫的人和日期（群組打「名錄 姓名：內容」會自動填）。",
    "3. 換承辦或人調走，狀態改「前任」，不要刪；之前的紀錄留著。",
    "4. 不寫私人資料：電話、住址、家人、健康、政治立場都不寫。名錄裡是別人的個資，只用在公司業務。",
    "5. 只給公司內部看：不建公開連結。",
    "6. 開會、會勘、審查、交屋前，先在群組打「名錄 姓名」或「名錄 標籤」查一下。",
]
SHOW_LIMIT = 10
_LOCK = threading.Lock()
_ADD = re.compile(r"^(?:記名錄|名錄)\s*([^:：\n]{1,20}?)\s*[:：]\s*(.*)$", re.S)
_FIND = re.compile(r"^(?:查名錄|名錄)\s*(\S.{0,19})$")
_TAG = re.compile(r"[#＃]([^\s#＃]{1,12})")


def enabled(has_allowed_group: bool, features_off: str) -> bool:
    """公司版（有 ALLOWED_GROUP_ID）而且 FEATURES_OFF 沒列 contacts 才開。"""
    return has_allowed_group and "contacts" not in {f.strip().lower() for f in features_off.split(",")}


def sheet_enabled() -> bool:
    v = os.environ.get("CONTACTS_SHEET", "").strip().lower()
    if v:
        return v in ("1", "true", "yes", "on")
    return os.environ.get("PROGRESS_SHEET", "").strip().lower() in ("1", "true", "yes", "on")


def parse_command(text: str) -> tuple[str, str, str] | None:
    """「名錄 王○○：內容」→ ("add", 名字, 內容)；「名錄 王○○」→ ("find", 查詢字, "")；
    只打「名錄」或沒寫內容 → ("help", "", "")；不是名錄指令 → None。"""
    t = text.strip().replace("　", " ")
    if t in ("名錄", "查名錄", "記名錄"):
        return ("help", "", "")
    m = _ADD.match(t)
    if m:
        name, note = m.group(1).strip(), m.group(2).strip()
        return ("add", name, note) if name and note else ("help", "", "")
    m = _FIND.match(t)
    if m:
        return ("find", m.group(1).strip(), "")
    return None


def _file() -> str:
    return data_path("contacts.json")


def _load() -> dict:
    try:
        with open(_file(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {"notes": list(data.get("notes", [])), "sent": int(data.get("sent", 0)),
            "sheet_id": data.get("sheet_id", ""), "sheet_url": data.get("sheet_url", ""),
            "sheet_rows": list(data.get("sheet_rows", [])),
            "last_sync": data.get("last_sync", ""), "last_error": data.get("last_error", "")}


def _save(data: dict) -> None:
    tmp = _file() + ".tmp"  # 先寫暫存檔再換掉，別的執行緒不會讀到寫一半的檔
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _file())


def _save_sheet_rows(rows: list[list]) -> None:
    with _LOCK:
        data = _load()
        data["sheet_rows"] = [_pad(r) for r in rows]
        _save(data)


def add_note(name: str, note: str, author: str, day: str) -> dict:
    """記一筆；內容裡的「#標籤」拆出來放標籤欄。"""
    tags = _TAG.findall(note)
    clean = re.sub(r"\s{2,}", " ", _TAG.sub("", note)).strip() or note.strip()
    item = {"name": name.strip(), "note": clean, "tags": "、".join(tags), "author": author, "date": day}
    with _LOCK:
        data = _load()
        data["notes"].append(item)
        _save(data)
    return item


def _pad(cells: list) -> list[str]:
    """試算表讀回來的格子補齊成 10 欄文字（API 會省略列尾的空格）。"""
    return ([str(c) for c in cells] + [""] * len(HEAD))[:len(HEAD)]


def _from_sheet(cells: list[str]) -> dict:
    name, unit, role, _cat, _case, note, tags, author, day, state = _pad(cells)
    return {"name": name.strip(), "unit": unit.strip(), "role": role.strip(), "note": note.strip(), "tags": tags.strip(),
            "author": author.strip(), "date": day.strip(), "state": state.strip()}


def _from_note(n: dict) -> dict:
    return {"name": n["name"], "unit": "", "role": "", "note": n["note"], "tags": n.get("tags", ""),
            "author": n.get("author", ""), "date": n.get("date", ""), "state": "現任"}


def _all_rows(data: dict) -> list[dict]:
    """有同步過試算表：表上的列（含別人手改的）＋小幫手還沒送上去的；沒有試算表：小幫手記的全部。"""
    if data["sheet_rows"]:
        rows = [_from_sheet(r) for r in data["sheet_rows"]] + [_from_note(n) for n in data["notes"][data["sent"]:]]
    else:
        rows = [_from_note(n) for n in data["notes"]]
    return [r for r in rows if r["name"] and r["note"]]


def find(query: str) -> list[dict]:
    """名字、單位、角色、標籤、內容含查詢字的都算；名字對到的排前面，同一組新的在前。"""
    q = query.strip().lower()
    if not q:
        return []
    rows = [r for r in _all_rows(_load())
            if any(q in r[k].lower() for k in ("name", "unit", "role", "tags", "note"))]
    rows.sort(key=lambda r: r["date"], reverse=True)
    rows.sort(key=lambda r: q not in r["name"].lower())
    return rows


def _md(day: str) -> str:
    try:
        return f"{date.fromisoformat(day):%-m/%-d}"
    except (TypeError, ValueError):
        return day


def format_results(query: str, rows: list[dict], url: str = "", limit: int = SHOW_LIMIT) -> str:
    if not rows:
        return (f"📇 名錄裡還沒有「{query}」。\n"
                f"記一筆：名錄 {query}：估驗前一週要先寄估驗表（寫具體做法，不寫評語）")
    lines = [f"📇 名錄「{query}」（{len(rows)} 則）"]
    shown = rows[:limit]
    for name in dict.fromkeys(r["name"] for r in shown):
        mine = [r for r in shown if r["name"] == name]
        who = next((f"{r['unit']} {r['role']}".strip() for r in mine if r["unit"] or r["role"]), "")
        lines.append(f"【{name}｜{who}】" if who else f"【{name}】")
        for r in mine:
            by = "，".join(x for x in (r["author"], _md(r["date"])) if x)
            lines.append(f"・{r['note']}" + (f"（{by}）" if by else "") + ("（前任）" if r["state"] == "前任" else ""))
    if len(rows) > limit:
        lines.append(f"還有 {len(rows) - limit} 則")
    return "\n".join(lines) + (f"\n📎 完整表格：{url}" if url else "")


def added_text(item: dict, sync_error: str | None = None) -> str:
    text = f"✅ 已記進名錄：{item['name']}\n・{item['note']}（{item['author']}，{_md(item['date'])}）"
    if item["tags"]:
        text += f"\n標籤：{item['tags']}"
    if sync_error:
        text += f"\n⚠️ 試算表這次沒同步：{sync_error}（小幫手裡已記下，下次再同步）"
    return text


def help_text(url: str = "") -> str:
    return ("📇 關係人經驗名錄（審查、會勘、交屋的經驗留在公司）\n"
            "・記：名錄 王○○：估驗前一週要先寄估驗表 #估驗\n"
            "・查：名錄 王○○（也能查單位、標籤，例：名錄 估驗）\n"
            "寫具體做法，不寫評語；不寫電話、住址、家人。換人時到表格把狀態改「前任」，不要刪。"
            + (f"\n📎 完整表格：{url}" if url else ""))


def _note_row(n: dict) -> list[str]:
    return [n["name"], "", "", "", "", n["note"], n.get("tags", ""), n.get("author", ""), n.get("date", ""), "現任"]


def _create(svc) -> tuple[str, str]:
    title = os.environ.get("CONTACTS_SHEET_TITLE", "").strip() or "關係人經驗名錄"
    book = svc.spreadsheets().create(
        body={"properties": {"title": title}, "sheets": [{"properties": {"title": TAB}}, {"properties": {"title": RULES_TAB}}]},
        fields="spreadsheetId,spreadsheetUrl").execute()
    sid, values = book["spreadsheetId"], svc.spreadsheets().values()
    values.update(spreadsheetId=sid, range=f"'{TAB}'!A1", valueInputOption="RAW", body={"values": [HEAD]}).execute()
    values.update(spreadsheetId=sid, range=f"'{RULES_TAB}'!A1", valueInputOption="RAW",
                  body={"values": [[line] for line in RULES]}).execute()
    return sid, book.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{sid}"


def sync() -> str | None:
    """小幫手還沒送上去的列加到試算表，再把整頁讀回來（別人手改、刪、加的都算數）。
    沒開試算表回 None；成功回 None；失敗回原因（本機資料留著，下次再同步）。試算表被刪了就重建一份、全部重送。"""
    if not sheet_enabled():
        return None
    from progress_service import _error_text, _service, _status  # 共用試算表連線與錯誤說明

    with _LOCK:
        data = _load()
        for _ in range(2):
            try:
                svc = _service()
                if not data["sheet_id"]:
                    data["sheet_id"], data["sheet_url"] = _create(svc)
                    data["sent"] = 0
                    _save(data)
                values = svc.spreadsheets().values()
                pending = data["notes"][data["sent"]:]
                if pending:
                    values.append(spreadsheetId=data["sheet_id"], range=f"'{TAB}'!A1", valueInputOption="RAW",
                                  insertDataOption="INSERT_ROWS", body={"values": [_note_row(n) for n in pending]}).execute()
                    data["sent"] = len(data["notes"])
                    _save(data)
                got = values.get(spreadsheetId=data["sheet_id"], range=f"'{TAB}'!A2:J").execute().get("values", [])
                data.update(sheet_rows=[_pad(r) for r in got], last_error="",
                            last_sync=datetime.now(pytz.timezone("Asia/Taipei")).strftime("%m/%d %H:%M"))
                _save(data)
                return None
            except Exception as e:
                if _status(e) == 404 and data["sheet_id"]:
                    print("[DoubleA] 名錄試算表找不到（可能被刪了），重建一份")
                    data["sheet_id"], data["sheet_url"], data["sent"], data["sheet_rows"] = "", "", 0, []
                    continue
                data["last_error"] = _error_text(e)
                _save(data)
                print(f"[DoubleA] 名錄同步失敗（本機已記）：{type(e).__name__}")
                return data["last_error"]
        return data["last_error"] or "名錄試算表重建失敗"


def sheet_url() -> str:
    return _load()["sheet_url"]


def status_text() -> str:
    """自我檢查用的一句話。"""
    data = _load()
    n = len(data["notes"])
    if not sheet_enabled():
        return f"只存在小幫手（{n} 則；試算表沒開）"
    if data["last_error"]:
        return data["last_error"]
    if data["sheet_id"]:
        return f"已連到 Google 試算表（小幫手記了 {n} 則，表上共 {len(data['sheet_rows'])} 列，最後同步 {data['last_sync']}）"
    return "還沒建立（下一次記名錄或查名錄會自動建立）"
