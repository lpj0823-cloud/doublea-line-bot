import json
import os

USE_FIRESTORE = bool(os.environ.get("K_SERVICE") or os.environ.get("USE_FIRESTORE"))
SHOPPING_FILE = os.path.join(os.path.dirname(__file__), "shopping_list.json")
FIRESTORE_COLLECTION = "doublea"
FIRESTORE_DOC = "shopping"


def _get_db():
    from google.cloud import firestore
    return firestore.Client()


# ── Firestore backend ─────────────────────────────────────────────────────────

def _fs_load() -> dict:
    doc = _get_db().collection(FIRESTORE_COLLECTION).document(FIRESTORE_DOC).get()
    return doc.to_dict() or {"items": []}


def _fs_save(data: dict) -> None:
    _get_db().collection(FIRESTORE_COLLECTION).document(FIRESTORE_DOC).set(data)


# ── Local JSON backend ────────────────────────────────────────────────────────

def _local_load() -> dict:
    if os.path.exists(SHOPPING_FILE):
        with open(SHOPPING_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"items": []}


def _local_save(data: dict) -> None:
    with open(SHOPPING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Public API ────────────────────────────────────────────────────────────────

def _load() -> dict:
    return _fs_load() if USE_FIRESTORE else _local_load()


def _save(data: dict) -> None:
    _fs_save(data) if USE_FIRESTORE else _local_save(data)


def add_items(names: list[str]) -> list[str]:
    """新增品項。回傳成功加入的品項名稱清單。"""
    data = _load()
    added = []
    for name in names:
        name = name.strip()
        if name:
            data["items"].append({"name": name, "done": False})
            added.append(name)
    if added:
        _save(data)
    return added


def get_items() -> list[dict]:
    """回傳全部品項 [{"name", "done"}, ...]。"""
    return _load()["items"]


def mark_done_by_index(n: int) -> str | None:
    """標記第 n 筆（1-based）待買品項為已買。回傳品項名稱，找不到回傳 None。"""
    data = _load()
    pending = [it for it in data["items"] if not it["done"]]
    if 1 <= n <= len(pending):
        target = pending[n - 1]["name"]
        for it in data["items"]:
            if it["name"] == target and not it["done"]:
                it["done"] = True
                break
        _save(data)
        return target
    return None


def mark_done_by_keyword(keyword: str) -> str | None:
    """標記第一個含關鍵字的待買品項為已買。"""
    data = _load()
    for it in data["items"]:
        if not it["done"] and keyword in it["name"]:
            it["done"] = True
            _save(data)
            return it["name"]
    return None


def clear_done() -> int:
    """移除所有已買品項。回傳移除數量。"""
    data = _load()
    before = len(data["items"])
    data["items"] = [it for it in data["items"] if not it["done"]]
    _save(data)
    return before - len(data["items"])
