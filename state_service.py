"""
State persistence:
- 本機開發：使用 chat_state.json
- Cloud Run / Railway（設定 USE_FIRESTORE=true）：使用 Google Firestore
"""
import json
import os

from paths import data_path
import time
from datetime import datetime

USE_FIRESTORE = bool(os.environ.get("K_SERVICE") or os.environ.get("USE_FIRESTORE"))

# 診斷用：開機時印出判斷依據，方便確認 Railway 上的環境變數是否真的被移除了。
print(
    f"[DoubleA] state_service 啟動判斷：K_SERVICE={os.environ.get('K_SERVICE')!r} "
    f"USE_FIRESTORE(env)={os.environ.get('USE_FIRESTORE')!r} → USE_FIRESTORE(flag)={USE_FIRESTORE}"
)

CHAT_STATE_FILE = data_path("chat_state.json")
FIRESTORE_COLLECTION = "doublea"
FIRESTORE_STATE_DOC = "state"
FIRESTORE_REMINDERS = "reminders"


# ── Firestore backend ─────────────────────────────────────────────────────────

def _get_db():
    from google.cloud import firestore
    return firestore.Client()


def _fs_load_state() -> dict:
    try:
        doc = _get_db().collection(FIRESTORE_COLLECTION).document(FIRESTORE_STATE_DOC).get()
        return doc.to_dict() or {}
    except Exception as e:
        print(
            f"[DoubleA] Firestore load_state 失敗（USE_FIRESTORE(flag)={USE_FIRESTORE}, "
            f"env USE_FIRESTORE={os.environ.get('USE_FIRESTORE')!r}, K_SERVICE={os.environ.get('K_SERVICE')!r}），"
            f"fallback JSON：{e}"
        )
        return _local_load_state()


def _fs_save_state(data: dict) -> None:
    try:
        _get_db().collection(FIRESTORE_COLLECTION).document(FIRESTORE_STATE_DOC).set(
            data, merge=True
        )
    except Exception as e:
        print(
            f"[DoubleA] Firestore save_state 失敗（USE_FIRESTORE(flag)={USE_FIRESTORE}, "
            f"env USE_FIRESTORE={os.environ.get('USE_FIRESTORE')!r}, K_SERVICE={os.environ.get('K_SERVICE')!r}），"
            f"fallback JSON：{e}"
        )
        _local_save_state(data)


# ── Local file backend ────────────────────────────────────────────────────────

def _local_load_state() -> dict:
    if os.path.exists(CHAT_STATE_FILE):
        with open(CHAT_STATE_FILE) as f:
            return json.load(f)
    return {}


def _local_save_state(data: dict) -> None:
    state = _local_load_state()
    state.update(data)
    with open(CHAT_STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── Public API ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    return _fs_load_state() if USE_FIRESTORE else _local_load_state()


def save_state(data: dict) -> None:
    if USE_FIRESTORE:
        _fs_save_state(data)
    else:
        _local_save_state(data)


def load_chat_id() -> str | None:
    return load_state().get("group_chat_id")


def save_chat_id(chat_id: str) -> None:
    save_state({"group_chat_id": chat_id})


def load_last_event() -> dict | None:
    return load_state().get("last_event")


def save_last_event(event_id: str, event_data: dict) -> None:
    save_state({
        "last_event": {
            "id": event_id,
            "title": event_data["title"],
            "start": event_data["start"],
            "end": event_data["end"],
        }
    })


# ── Reminder persistence (Firestore only; local falls back to in-memory) ─────

_local_reminders: list = []


def add_reminder(chat_id: str, event_data: dict, reminder_dt: datetime) -> None:
    payload = {
        "chat_id": chat_id,
        "title": event_data["title"],
        "start": event_data["start"],
        "reminder_time": reminder_dt.isoformat(),
        "sent": False,
    }
    if USE_FIRESTORE:
        try:
            doc_id = f"{event_data['start']}_{chat_id}".replace(":", "-").replace("+", "p")
            _get_db().collection(FIRESTORE_REMINDERS).document(doc_id).set(payload)
            return
        except Exception as e:
            print(f"[DoubleA] Firestore add_reminder 失敗，fallback 記憶體：{e}")
    # 本機／Railway：存進 chat_state.json（放在 DATA_DIR 的永久磁碟），重新部署不會遺失
    reminders = [r for r in (_local_load_state().get("reminders") or []) if not r.get("sent")]
    payload["_id"] = f"{event_data['start']}_{chat_id}_{event_data['title']}"
    if not any(r.get("_id") == payload["_id"] for r in reminders):
        reminders.append(payload)
    _local_save_state({"reminders": reminders})


def get_due_reminders(now: datetime) -> list:
    if USE_FIRESTORE:
        try:
            docs = (
                _get_db()
                .collection(FIRESTORE_REMINDERS)
                .where("sent", "==", False)
                .stream()
            )
            due = []
            for doc in docs:
                data = doc.to_dict()
                data["_doc_id"] = doc.id
                if datetime.fromisoformat(data["reminder_time"]) <= now:
                    due.append(data)
            return due
        except Exception as e:
            print(f"[DoubleA] Firestore get_due_reminders 失敗，fallback 記憶體：{e}")
    return [
        r for r in (_local_load_state().get("reminders") or [])
        if not r.get("sent") and datetime.fromisoformat(r["reminder_time"]) <= now
    ]


def mark_reminder_sent(doc_id) -> None:
    if USE_FIRESTORE:
        try:
            _get_db().collection(FIRESTORE_REMINDERS).document(doc_id).update({"sent": True})
            return
        except Exception as e:
            print(f"[DoubleA] Firestore mark_reminder_sent 失敗，fallback 記憶體：{e}")
    reminders = _local_load_state().get("reminders") or []
    # 已送出的直接移除，避免檔案越長越大
    _local_save_state({"reminders": [r for r in reminders if r.get("_id") != doc_id]})


# ── Pending edit state ────────────────────────────────────────────────────────

def save_pending_edit(chat_id: str, data: dict) -> None:
    save_state({f"pending_edit_{chat_id}": data})


def load_pending_edit(chat_id: str) -> dict | None:
    val = load_state().get(f"pending_edit_{chat_id}")
    return val if isinstance(val, dict) else None


def clear_pending_edit(chat_id: str) -> None:
    save_state({f"pending_edit_{chat_id}": None})


# ── Pending image state（圖片＋文字說明才建立行事曆）────────────────────────────
# 使用者傳照片後，若沒有接著輸入觸發語（例如「加到行事曆」），
# 這張照片就只會走收據辨識／記帳提醒，不會自動建立行程。

def save_pending_image(chat_id: str, message_id: str) -> None:
    save_state({
        f"pending_image_{chat_id}": {
            "message_id": message_id,
            "ts": time.time(),
        }
    })


def load_pending_image(chat_id: str) -> dict | None:
    val = load_state().get(f"pending_image_{chat_id}")
    return val if isinstance(val, dict) else None


def clear_pending_image(chat_id: str) -> None:
    save_state({f"pending_image_{chat_id}": None})
