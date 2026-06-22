import json
import os
from datetime import datetime

import pytz

USE_FIRESTORE = bool(os.environ.get("K_SERVICE") or os.environ.get("USE_FIRESTORE"))
NOTES_FILE = os.path.join(os.path.dirname(__file__), "notes.json")
TAIPEI_TZ = pytz.timezone("Asia/Taipei")
FIRESTORE_COLLECTION = "doublea"
FIRESTORE_DOC = "notespad"


def _get_db():
    from google.cloud import firestore
    return firestore.Client()


# ── Firestore backend ─────────────────────────────────────────────────────────

def _fs_load() -> dict:
    try:
        doc = _get_db().collection(FIRESTORE_COLLECTION).document(FIRESTORE_DOC).get()
        return doc.to_dict() or {"notes": [], "next_id": 1}
    except Exception as e:
        print(f"[DoubleA] Firestore notes load 失敗，fallback JSON：{e}")
        return _local_load()


def _fs_save(data: dict) -> None:
    try:
        _get_db().collection(FIRESTORE_COLLECTION).document(FIRESTORE_DOC).set(data)
    except Exception as e:
        print(f"[DoubleA] Firestore notes save 失敗，fallback JSON：{e}")
        _local_save(data)


# ── Local JSON backend ────────────────────────────────────────────────────────

def _local_load() -> dict:
    if os.path.exists(NOTES_FILE):
        with open(NOTES_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"notes": [], "next_id": 1}


def _local_save(data: dict) -> None:
    with open(NOTES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Public API ────────────────────────────────────────────────────────────────

def _load() -> dict:
    return _fs_load() if USE_FIRESTORE else _local_load()


def _save(data: dict) -> None:
    _fs_save(data) if USE_FIRESTORE else _local_save(data)


def add_note(content: str) -> dict:
    """新增筆記。回傳新筆記資料。"""
    data = _load()
    note = {
        "id": data["next_id"],
        "content": content.strip(),
        "created_at": datetime.now(TAIPEI_TZ).strftime("%m/%d %H:%M"),
    }
    data["notes"].append(note)
    data["next_id"] += 1
    _save(data)
    return note


def get_notes() -> list[dict]:
    """回傳全部筆記 [{"id", "content", "created_at"}, ...]。"""
    return _load()["notes"]


def delete_note_by_index(n: int) -> str | None:
    """刪除第 n 筆（1-based）筆記。回傳被刪除的內容，找不到回傳 None。"""
    data = _load()
    if 1 <= n <= len(data["notes"]):
        removed = data["notes"].pop(n - 1)
        _save(data)
        return removed["content"]
    return None
