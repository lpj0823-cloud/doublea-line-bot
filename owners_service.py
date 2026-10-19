"""公司版：專案 → 負責人清單（存在 DATA_DIR/project_owners.json）。
在 LINE 用「+負責 淡水林宅 淨雯」新增、「負責清單」查看、「刪負責 淡水林宅」刪除；
同事貼週報時會自動更新（update_from_report），每人每案記最後回報日，兩週沒回報會標出來。"""
import json
import re
from datetime import date, timedelta

from paths import data_path

OWNERS_FILE = data_path("project_owners.json")
_OWNER_SPLIT = re.compile(r"[、,，/／]")


def _load() -> dict:
    try:
        with open(OWNERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {"owners": dict(data.get("owners", {})), "reported": dict(data.get("reported", {}))}


def _save(data: dict) -> None:
    with open(OWNERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def _names(owner: str) -> list[str]:
    return [n.strip() for n in _OWNER_SPLIT.split(owner) if n.strip()]


def _norm(name: str) -> str:
    return re.sub(r"[\s　·・\-_()（）]", "", name).lower()


def _similar(a: str, b: str) -> bool:
    """同一個專案的不同寫法：字有 75% 以上重疊（兩邊都至少 4 個字），例如「龍潭鍾政文學園區」和「龍潭鍾肇政園區再造」。"""
    x, y = set(_norm(a)), set(_norm(b))
    return min(len(x), len(y)) >= 4 and len(x & y) / min(len(x), len(y)) >= 0.75


def load_project_owners() -> dict[str, str]:
    return _load()["owners"]


def set_project_owner(project: str, owner: str) -> None:
    data = _load()
    data["owners"][project] = owner
    today = date.today().isoformat()
    for name in _names(owner):
        data["reported"][f"{project}|{name}"] = today
    _save(data)


def remove_project_owner(project: str) -> bool:
    data = _load()
    if project not in data["owners"]:
        return False
    del data["owners"][project]
    data["reported"] = {k: v for k, v in data["reported"].items() if not k.startswith(f"{project}|")}
    _save(data)
    return True


def projects_by_owner() -> dict[str, list[str]]:
    """每個人手上的專案（照加入順序）：{負責人: [專案, ...]}；兩人合作寫「育鳳、蕙綺」，兩人底下都會列。"""
    grouped: dict[str, list[str]] = {}
    for project, owner in load_project_owners().items():
        for name in _names(owner):
            grouped.setdefault(name, []).append(project)
    return grouped


def update_from_report(owner: str, projects: list[str], today: str) -> list[str]:
    """同事的週報：週報裡的專案記到這個人名下、更新回報日；回傳新加進清單的專案。
    名稱和清單上的只差空格、括號、一個包含另一個，或字幾乎一樣，就算同一個專案。"""
    data = _load()
    owners, added = data["owners"], []
    for project in (p.strip() for p in projects):
        if not project:
            continue
        key = (next((k for k in owners if _norm(k) == _norm(project)), None)
               or next((k for k in owners if _norm(project) and (_norm(k) in _norm(project) or _norm(project) in _norm(k))), None)
               or next((k for k in owners if _similar(k, project)), None))
        if key is None:
            owners[project], key = owner, project
            added.append(project)
        elif owner not in _names(owners[key]):
            owners[key] = f"{owners[key]}、{owner}"
        data["reported"][f"{key}|{owner}"] = today
    _save(data)
    return added


def stale_pairs(today: str, days: int = 14) -> set[tuple[str, str]]:
    """超過 days 天沒在週報裡提到的（專案, 負責人）；沒有回報日的不算。"""
    cutoff = date.fromisoformat(today) - timedelta(days=days)
    return {tuple(k.split("|", 1)) for k, v in _load()["reported"].items()
            if "|" in k and date.fromisoformat(v) <= cutoff}
