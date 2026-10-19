"""公司版：專案 → 負責人清單（存在 DATA_DIR/project_owners.json）。
在 LINE 用「+負責 淡水林宅 淨雯」新增、「負責清單」查看、「刪負責 淡水林宅」刪除。"""
import json

from paths import data_path

OWNERS_FILE = data_path("project_owners.json")


def load_project_owners() -> dict[str, str]:
    try:
        with open(OWNERS_FILE, encoding="utf-8") as f:
            return dict(json.load(f).get("owners", {}))
    except (OSError, ValueError):
        return {}


def _save(owners: dict[str, str]) -> None:
    with open(OWNERS_FILE, "w", encoding="utf-8") as f:
        json.dump({"owners": owners}, f, ensure_ascii=False, indent=1)


def set_project_owner(project: str, owner: str) -> None:
    owners = load_project_owners()
    owners[project] = owner
    _save(owners)


def remove_project_owner(project: str) -> bool:
    owners = load_project_owners()
    if project not in owners:
        return False
    del owners[project]
    _save(owners)
    return True
