"""AI 用量記錄與週報（2026-11-04 Justin「用量週報照建議做」）。

每次呼叫 Claude／Gemini 成功後記一筆到 DATA_DIR/ai_usage.jsonl：時間、服務、模型、功能、token、估算美元。
不記訊息內容、照片、使用者 ID、金鑰。記錄失敗一律吞掉，不影響原本功能。
管理員私訊「AI用量」看上週；每週一 08:05 私訊管理員（main.py 的 usage_weekly_job）。
"""
import json
import os
from datetime import date, datetime, timedelta

import pytz

from paths import data_path

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
FILENAME = "ai_usage.jsonl"
PRICE_DATE = "2026-11-04"
# 美元／百萬 tokens（輸入, 輸出）；換模型或官方調價就改這裡和 PRICE_DATE
PRICES = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "gemini-2.5-flash": (0.30, 2.50),
}
USD_TWD = 32


def _path(path=None) -> str:
    return path or data_path(FILENAME)


def estimate_usd(model: str, input_tokens: int, output_tokens: int):
    price = PRICES.get(model)
    if not price:
        return None
    return round(input_tokens * price[0] / 1e6 + output_tokens * price[1] / 1e6, 6)


def claude_tokens(message) -> tuple[int, int]:
    usage = getattr(message, "usage", None)
    return (getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0) if usage else (0, 0)


def gemini_tokens(response) -> tuple[int, int]:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return 0, 0
    def n(field):
        return getattr(meta, field, None) or 0
    return n("prompt_token_count"), n("candidates_token_count") + n("thoughts_token_count")


def record(service: str, model: str, feature: str, input_tokens: int = 0, output_tokens: int = 0,
           now: datetime | None = None, path: str | None = None) -> dict:
    """記一筆；任何錯誤回 {}，不丟例外。"""
    try:
        row = {
            "time": (now or datetime.now(TAIPEI_TZ)).isoformat(timespec="seconds"),
            "service": service,
            "model": model,
            "feature": feature,
            "in": int(input_tokens or 0),
            "out": int(output_tokens or 0),
            "usd": estimate_usd(model, int(input_tokens or 0), int(output_tokens or 0)),
        }
        with open(_path(path), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row
    except Exception:
        return {}


def load(path: str | None = None) -> tuple[list[dict], int]:
    """回傳 (紀錄, 讀不懂的行數)；檔案不存在就是 ([], 0)。"""
    rows, bad = [], 0
    try:
        with open(_path(path), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                    date.fromisoformat(str(r["time"])[:10])
                    rows.append(r)
                except (ValueError, KeyError, TypeError):
                    bad += 1
    except OSError:
        pass
    return rows, bad


def _sum(rows, start: date, end: date) -> dict:
    s = {"usd": 0.0, "n": 0, "by_feature": {}, "unknown": {}}
    for r in rows:
        d = date.fromisoformat(str(r["time"])[:10])
        if not start <= d <= end:
            continue
        s["n"] += 1
        usd = r.get("usd")
        if usd is None:
            s["unknown"][r.get("model", "?")] = s["unknown"].get(r.get("model", "?"), 0) + 1
            usd = 0.0
        s["usd"] += usd
        f = s["by_feature"].setdefault(r.get("feature", "?"), {"n": 0, "usd": 0.0})
        f["n"] += 1
        f["usd"] += usd
    return s


def summary_text(days: int = 7, today: date | None = None, path: str | None = None) -> str:
    """上週（今天往前 days 天，不含今天）的用量摘要，給私訊用。"""
    today = today or datetime.now(TAIPEI_TZ).date()
    end, start = today - timedelta(days=1), today - timedelta(days=days)
    rows, bad = load(path)
    week = _sum(rows, start, end)
    prev = _sum(rows, start - timedelta(days=days), start - timedelta(days=1))
    lines = [f"📊 AI 用量（{start:%m/%d}～{end:%m/%d}，估算）"]
    if week["n"] == 0:
        lines.append("・上週沒有呼叫 AI（0 元）")
    else:
        lines.append(f"・合計約 {_usd(week['usd'])}（{_twd(week['usd'])}），共 {week['n']} 次")
        for name, f in sorted(week["by_feature"].items(), key=lambda kv: -kv[1]["usd"]):
            lines.append(f"・{name} {f['n']} 次 {_usd(f['usd'])}")
    lines.append(f"・前一週 {_usd(prev['usd'])}")
    if week["unknown"]:
        lines.append("・單價未知（沒算進合計）：" + "、".join(f"{m} {n} 次" for m, n in week["unknown"].items()))
    if bad:
        lines.append(f"・讀不懂的紀錄 {bad} 行（已略過）")
    lines.append(f"（只算這個小幫手；單價 {PRICE_DATE}，金額是估算）")
    return "\n".join(lines)


def _usd(x: float) -> str:
    """小金額多留幾位，避免顯示成 0.00。"""
    return f"US${x:.4f}" if 0 < x < 0.01 else f"US${x:.2f}"


def _twd(x: float) -> str:
    return "不到 NT$1" if 0 < x * USD_TWD < 1 else f"約 NT${x * USD_TWD:,.0f}"


def is_usage_command(text: str) -> bool:
    return (text or "").strip().replace(" ", "").lower() == "ai用量"
