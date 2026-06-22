import json
import os
import re

USE_FIRESTORE = bool(os.environ.get("K_SERVICE") or os.environ.get("USE_FIRESTORE"))
BIRTHDAYS_FILE = os.path.join(os.path.dirname(__file__), "birthdays.json")
FIRESTORE_COLLECTION = "doublea"
FIRESTORE_DOC = "birthdays"


def _get_db():
    from google.cloud import firestore
    return firestore.Client()


# ── Firestore backend ─────────────────────────────────────────────────────────

def _fs_load() -> dict:
    doc = _get_db().collection(FIRESTORE_COLLECTION).document(FIRESTORE_DOC).get()
    return doc.to_dict() or {"birthdays": [], "next_id": 1}


def _fs_save(data: dict) -> None:
    _get_db().collection(FIRESTORE_COLLECTION).document(FIRESTORE_DOC).set(data)


# ── Local JSON backend ────────────────────────────────────────────────────────

def _local_load() -> dict:
    if os.path.exists(BIRTHDAYS_FILE):
        with open(BIRTHDAYS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"birthdays": [], "next_id": 1}


def _local_save(data: dict) -> None:
    with open(BIRTHDAYS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Shared ────────────────────────────────────────────────────────────────────

def _load() -> dict:
    return _fs_load() if USE_FIRESTORE else _local_load()


def _save(data: dict) -> None:
    _fs_save(data) if USE_FIRESTORE else _local_save(data)


# ── Date parsing ──────────────────────────────────────────────────────────────

def parse_birthday_date(date_str: str) -> tuple[int | None, int | None, int | None]:
    """Parse natural-language date. Returns (month, day, year). Year may be None."""
    s = date_str.strip()

    # year-first: 1990/3/15  1990-3-15  1990年3月15日
    m = re.match(r"(\d{4})[/\-年](\d{1,2})[/\-月](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return mo, d, y

    # month/day only: 3/15  3-15  3月15日  3月15號
    m = re.match(r"(\d{1,2})[/\-月](\d{1,2})", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return mo, d, None

    return None, None, None


# ── Public API ────────────────────────────────────────────────────────────────

def add_birthday(name: str, month: int, day: int, year: int | None = None) -> dict:
    """新增生日記錄。回傳新增的資料。"""
    data = _load()
    entry: dict = {"id": data["next_id"], "name": name.strip(), "month": month, "day": day}
    if year:
        entry["year"] = year
    data["birthdays"].append(entry)
    data["next_id"] += 1
    _save(data)
    return entry


def get_birthdays() -> list[dict]:
    """回傳所有生日記錄，依月份日期排序。"""
    bds = _load()["birthdays"]
    return sorted(bds, key=lambda b: (b["month"], b["day"]))


def delete_birthday_by_index(n: int) -> str | None:
    """刪除第 n 筆（1-based，依排序後順序）。回傳被刪除的姓名。"""
    data = _load()
    sorted_bds = sorted(data["birthdays"], key=lambda b: (b["month"], b["day"]))
    if 1 <= n <= len(sorted_bds):
        target_id = sorted_bds[n - 1]["id"]
        data["birthdays"] = [b for b in data["birthdays"] if b["id"] != target_id]
        _save(data)
        return sorted_bds[n - 1]["name"]
    return None


def get_todays_birthdays(month: int, day: int) -> list[dict]:
    """回傳今天生日的所有人。"""
    return [b for b in _load()["birthdays"] if b["month"] == month and b["day"] == day]
