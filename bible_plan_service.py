"""每日讀經進度：半年（182 天）讀完新舊約一次，只給中文和合本。

- 舊約 929 章、新約 260 章，依章數平均分到 182 天（舊約每天約 5 章、新約約 1～2 章）。
- 起算日：環境變數 BIBLE_PLAN_START（YYYY-MM-DD）；沒設就用第一次使用的那天，存進 state。
- 第 182 天讀完後，隔天自動開始下一輪。
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pytz

from proverbs_service import _fetch_fhl_chapter, ZH_VERSION
from state_service import load_state, save_state

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
PLAN_DAYS = 182

# (書名, 信望愛站簡稱, 章數)
OLD_TESTAMENT = [
    ("創世記", "創", 50), ("出埃及記", "出", 40), ("利未記", "利", 27), ("民數記", "民", 36),
    ("申命記", "申", 34), ("約書亞記", "書", 24), ("士師記", "士", 21), ("路得記", "得", 4),
    ("撒母耳記上", "撒上", 31), ("撒母耳記下", "撒下", 24), ("列王紀上", "王上", 22), ("列王紀下", "王下", 25),
    ("歷代志上", "代上", 29), ("歷代志下", "代下", 36), ("以斯拉記", "拉", 10), ("尼希米記", "尼", 13),
    ("以斯帖記", "斯", 10), ("約伯記", "伯", 42), ("詩篇", "詩", 150), ("箴言", "箴", 31),
    ("傳道書", "傳", 12), ("雅歌", "歌", 8), ("以賽亞書", "賽", 66), ("耶利米書", "耶", 52),
    ("耶利米哀歌", "哀", 5), ("以西結書", "結", 48), ("但以理書", "但", 12), ("何西阿書", "何", 14),
    ("約珥書", "珥", 3), ("阿摩司書", "摩", 9), ("俄巴底亞書", "俄", 1), ("約拿書", "拿", 4),
    ("彌迦書", "彌", 7), ("那鴻書", "鴻", 3), ("哈巴谷書", "哈", 3), ("西番雅書", "番", 3),
    ("哈該書", "該", 2), ("撒迦利亞書", "亞", 14), ("瑪拉基書", "瑪", 4),
]
NEW_TESTAMENT = [
    ("馬太福音", "太", 28), ("馬可福音", "可", 16), ("路加福音", "路", 24), ("約翰福音", "約", 21),
    ("使徒行傳", "徒", 28), ("羅馬書", "羅", 16), ("哥林多前書", "林前", 16), ("哥林多後書", "林後", 13),
    ("加拉太書", "加", 6), ("以弗所書", "弗", 6), ("腓立比書", "腓", 4), ("歌羅西書", "西", 4),
    ("帖撒羅尼迦前書", "帖前", 5), ("帖撒羅尼迦後書", "帖後", 3), ("提摩太前書", "提前", 6),
    ("提摩太後書", "提後", 4), ("提多書", "多", 3), ("腓利門書", "門", 1), ("希伯來書", "來", 13),
    ("雅各書", "雅", 5), ("彼得前書", "彼前", 5), ("彼得後書", "彼後", 3), ("約翰一書", "約一", 5),
    ("約翰二書", "約二", 1), ("約翰三書", "約三", 1), ("猶大書", "猶", 1), ("啟示錄", "啟", 22),
]


def _flatten(books):
    return [(name, abbr, ch) for name, abbr, n in books for ch in range(1, n + 1)]


OT_CHAPTERS = _flatten(OLD_TESTAMENT)   # 929
NT_CHAPTERS = _flatten(NEW_TESTAMENT)   # 260


def allowed_user_ids() -> set[str]:
    """只有這些 LINE userId 能用（培正的兩支 LINE）。空白＝尚未設定。"""
    raw = os.environ.get("BIBLE_READER_IDS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def plan_start(today: date) -> date:
    raw = os.environ.get("BIBLE_PLAN_START", "").strip()
    if raw:
        return date.fromisoformat(raw)
    state = load_state()
    saved = state.get("bible_plan_start")
    if saved:
        return date.fromisoformat(saved)
    state["bible_plan_start"] = today.isoformat()
    save_state(state)
    return today


def day_index(today: date, start: date) -> int:
    """0 起算；讀完一輪（182 天）後從頭開始。起算日之前一律當第 1 天。"""
    return max((today - start).days, 0) % PLAN_DAYS


def _slice(chapters: list, idx: int) -> list:
    n = len(chapters)
    return chapters[idx * n // PLAN_DAYS:(idx + 1) * n // PLAN_DAYS]


def portion(idx: int) -> tuple[list, list]:
    """第 idx 天（0 起算）的 (舊約章, 新約章)。"""
    return _slice(OT_CHAPTERS, idx), _slice(NT_CHAPTERS, idx)


def describe(chapters: list) -> str:
    """[(創世記,創,1)…(創世記,創,5)] → 「創世記 1–5章」；跨書卷用「、」連起來。"""
    if not chapters:
        return "（今天沒有）"
    groups: list[list] = []
    for name, _abbr, ch in chapters:
        if groups and groups[-1][0] == name:
            groups[-1][2] = ch
        else:
            groups.append([name, ch, ch])
    return "、".join(f"{n} {a}章" if a == b else f"{n} {a}–{b}章" for n, a, b in groups)


def _chapter_text(item: tuple) -> str:
    name, abbr, ch = item
    try:
        ok, records = _fetch_fhl_chapter(ch, ZH_VERSION, book=abbr)
    except Exception as e:
        print(f"[DoubleA] 讀經 {name} {ch} 取得失敗：{e}")
        ok, records = False, []
    if not ok or not records:
        return f"【{name} {ch}章】⚠️ 暫時無法取得，請稍後再試。"
    lines = [f"【{name} {ch}章】"]
    for rec in records:
        sec = rec.get("sec", "")
        text = rec.get("bible_text", "").strip()
        if sec and text:
            lines.append(f"{sec} {text}")
    return "\n".join(lines)


def get_daily_reading(now: datetime = None) -> list[str]:
    """回傳要送出的文字段落：[今日範圍, 舊約全文, 新約全文]。"""
    if now is None:
        now = datetime.now(TAIPEI_TZ)
    today = now.date()
    idx = day_index(today, plan_start(today))
    ot, nt = portion(idx)

    head = (
        f"📖 {today.month}月{today.day}日 每日讀經（第 {idx + 1}／{PLAN_DAYS} 天）\n\n"
        f"舊約：{describe(ot)}\n"
        f"新約：{describe(nt)}"
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        ot_texts = list(pool.map(_chapter_text, ot))
        nt_texts = list(pool.map(_chapter_text, nt))
    parts = [head]
    if ot_texts:
        parts.append("📜 舊約（和合本）\n\n" + "\n\n".join(ot_texts))
    if nt_texts:
        parts.append("✝️ 新約（和合本）\n\n" + "\n\n".join(nt_texts))
    return parts
