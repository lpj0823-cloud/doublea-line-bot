import json
import os
import uuid
from datetime import datetime
import pytz

from state_service import USE_FIRESTORE

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
FINANCE_FILE = os.path.join(os.path.dirname(__file__), "finance_data.json")


def _get_db():
    # 與 state_service.py 一致：使用 google-cloud-firestore 的 Client()，
    # 透過 Application Default Credentials 連線（Cloud Run 會自動用服務帳號）。
    # 原本改用 firebase_admin + credentials.Certificate(GOOGLE_TOKEN_JSON) 是錯的：
    #   1) firebase_admin 未列在 requirements.txt；
    #   2) GOOGLE_TOKEN_JSON 是使用者 OAuth token，不是服務帳號金鑰。
    from google.cloud import firestore
    return firestore.Client()


# ── 本機 JSON 備援（比照 state_service.py 的模式）───────────────────────────────
# USE_FIRESTORE=False（本機開發，或 Railway 沒設定服務帳號金鑰）時直接用這個；
# USE_FIRESTORE=True 但 Firestore 連線失敗時，也會自動 fallback 到這裡，
# 不會像之前一樣直接整個 raise 讓記帳功能掛掉。

def _local_load() -> dict:
    if os.path.exists(FINANCE_FILE):
        with open(FINANCE_FILE) as f:
            return json.load(f)
    return {"projects": {}, "expenses": {}}


def _local_save(data: dict) -> None:
    with open(FINANCE_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── 專案管理 ──────────────────────────────────────────────────────────────────

def create_project(name: str, description: str = "") -> dict:
    now = datetime.now(TAIPEI_TZ)
    project = {
        "name": name,
        "description": description,
        "created_at": now.strftime("%Y-%m-%d %H:%M"),
        "total": 0,
    }
    if USE_FIRESTORE:
        try:
            _get_db().collection("finance_projects").document(name).set(project)
            return project
        except Exception as e:
            print(f"[Finance] Firestore create_project 失敗，fallback 本機：{e}")
    data = _local_load()
    data["projects"][name] = project
    _local_save(data)
    return project


def get_project(name: str) -> dict | None:
    if USE_FIRESTORE:
        try:
            doc = _get_db().collection("finance_projects").document(name).get()
            return doc.to_dict() if doc.exists else None
        except Exception as e:
            print(f"[Finance] Firestore get_project 失敗，fallback 本機：{e}")
    return _local_load()["projects"].get(name)


def list_projects() -> list[dict]:
    if USE_FIRESTORE:
        try:
            docs = _get_db().collection("finance_projects").order_by(
                "created_at", direction="DESCENDING"
            ).stream()
            return [doc.to_dict() | {"id": doc.id} for doc in docs]
        except Exception as e:
            print(f"[Finance] Firestore list_projects 失敗，fallback 本機：{e}")
    projects = list(_local_load()["projects"].values())
    projects.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return [p | {"id": p["name"]} for p in projects]


def delete_project(name: str) -> bool:
    if USE_FIRESTORE:
        try:
            db = _get_db()
            expenses = db.collection("finance_expenses").where("project", "==", name).stream()
            for exp in expenses:
                exp.reference.delete()
            db.collection("finance_projects").document(name).delete()
            return True
        except Exception as e:
            print(f"[Finance] Firestore delete_project 失敗，fallback 本機：{e}")
    data = _local_load()
    data["expenses"] = {
        eid: e for eid, e in data["expenses"].items() if e.get("project") != name
    }
    data["projects"].pop(name, None)
    _local_save(data)
    return True


# ── 花費管理 ──────────────────────────────────────────────────────────────────

def parse_expense_input(raw: str) -> dict | None:
    """
    解析花費輸入字串。
    格式：專案名稱 類別 地點 人名:品項:金額 人名:品項:金額 ...
    範例：日本旅行 午餐 大阪餐廳 培正:拉麵:850 Ginny:壽司:1200
    簡單格式：日本旅行 交通 1200
    """
    parts = raw.strip().split()
    if len(parts) < 2:
        return None

    project_name = parts[0]
    category = parts[1] if len(parts) > 1 else ""

    items = []
    location = ""
    simple_amount = None

    for part in parts[2:]:
        if ":" in part:
            sub = part.split(":")
            if len(sub) == 3:
                try:
                    amount = float(sub[2])
                    items.append({"person": sub[0], "item": sub[1], "amount": amount})
                except ValueError:
                    pass
            elif len(sub) == 2:
                try:
                    amount = float(sub[1])
                    items.append({"person": sub[0], "item": "", "amount": amount})
                except ValueError:
                    pass
        else:
            try:
                simple_amount = float(part)
            except ValueError:
                location = part

    total = sum(i["amount"] for i in items) if items else (simple_amount or 0)
    if total == 0:
        return None

    return {
        "project": project_name,
        "category": category,
        "location": location,
        "items": items,
        "total": total,
        "simple_amount": simple_amount if not items else None,
    }


def add_expense(parsed: dict) -> tuple[dict, str]:
    """新增花費記錄，回傳 (expense_data, doc_id)。"""
    project_name = parsed["project"]

    project = get_project(project_name)
    if not project:
        raise ValueError(f"找不到專案「{project_name}」，請先用「+專案 名稱」建立")

    now = datetime.now(TAIPEI_TZ)
    expense = {
        "project": project_name,
        "category": parsed.get("category", ""),
        "location": parsed.get("location", ""),
        "items": parsed.get("items", []),
        "simple_amount": parsed.get("simple_amount"),
        "total": parsed["total"],
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "created_at": now.strftime("%Y-%m-%d %H:%M"),
        "photo_noted": False,
        "gps_location": None,
    }
    new_total = project.get("total", 0) + parsed["total"]

    if USE_FIRESTORE:
        try:
            db = _get_db()
            _, doc_ref = db.collection("finance_expenses").add(expense)
            db.collection("finance_projects").document(project_name).update({"total": new_total})
            return expense, doc_ref.id
        except Exception as e:
            print(f"[Finance] Firestore add_expense 失敗，fallback 本機：{e}")

    data = _local_load()
    doc_id = uuid.uuid4().hex
    data["expenses"][doc_id] = expense
    if project_name in data["projects"]:
        data["projects"][project_name]["total"] = new_total
    else:
        # 專案本來存在 Firestore、現在才 fallback 到本機：把它也補進本機檔案，
        # 這樣總額才對得起來。
        data["projects"][project_name] = project | {"total": new_total}
    _local_save(data)
    return expense, doc_id


def get_expenses(project_name: str) -> list[dict]:
    if USE_FIRESTORE:
        try:
            docs = (
                _get_db()
                .collection("finance_expenses")
                .where("project", "==", project_name)
                .order_by("created_at")
                .stream()
            )
            return [doc.to_dict() | {"id": doc.id} for doc in docs]
        except Exception as e:
            print(f"[Finance] Firestore get_expenses 失敗，fallback 本機：{e}")
    expenses = [
        e | {"id": eid}
        for eid, e in _local_load()["expenses"].items()
        if e.get("project") == project_name
    ]
    expenses.sort(key=lambda e: e.get("created_at", ""))
    return expenses


def get_latest_expense(project_name: str) -> tuple[dict, str] | None:
    """取得專案最新一筆花費，回傳 (expense_data, doc_id)。"""
    if USE_FIRESTORE:
        try:
            docs = list(
                _get_db()
                .collection("finance_expenses")
                .where("project", "==", project_name)
                .order_by("created_at", direction="DESCENDING")
                .limit(1)
                .stream()
            )
            if not docs:
                return None
            return docs[0].to_dict(), docs[0].id
        except Exception as e:
            print(f"[Finance] Firestore get_latest_expense 失敗，fallback 本機：{e}")
    expenses = get_expenses(project_name)
    if not expenses:
        return None
    latest = expenses[-1]
    return latest, latest["id"]


def get_last_expense_any(chat_id: str) -> tuple[dict, str] | None:
    """取得任意專案最新一筆花費（用於位置附加）。"""
    if USE_FIRESTORE:
        try:
            docs = list(
                _get_db()
                .collection("finance_expenses")
                .order_by("created_at", direction="DESCENDING")
                .limit(1)
                .stream()
            )
            if not docs:
                return None
            return docs[0].to_dict(), docs[0].id
        except Exception as e:
            print(f"[Finance] Firestore get_last_expense_any 失敗，fallback 本機：{e}")
    expenses = [e | {"id": eid} for eid, e in _local_load()["expenses"].items()]
    if not expenses:
        return None
    expenses.sort(key=lambda e: e.get("created_at", ""))
    latest = expenses[-1]
    return latest, latest["id"]


def attach_location(doc_id: str, title: str, lat: float, lon: float, address: str) -> None:
    """將 GPS 位置附加到指定花費記錄。"""
    gps = {"title": title, "lat": lat, "lon": lon, "address": address}
    if USE_FIRESTORE:
        try:
            _get_db().collection("finance_expenses").document(doc_id).update({"gps_location": gps})
            return
        except Exception as e:
            print(f"[Finance] Firestore attach_location 失敗，fallback 本機：{e}")
    data = _local_load()
    if doc_id in data["expenses"]:
        data["expenses"][doc_id]["gps_location"] = gps
        _local_save(data)


def mark_photo_noted(doc_id: str) -> None:
    """標記該筆花費已提醒存圖。"""
    if USE_FIRESTORE:
        try:
            _get_db().collection("finance_expenses").document(doc_id).update({"photo_noted": True})
            return
        except Exception as e:
            print(f"[Finance] Firestore mark_photo_noted 失敗，fallback 本機：{e}")
    data = _local_load()
    if doc_id in data["expenses"]:
        data["expenses"][doc_id]["photo_noted"] = True
        _local_save(data)


# ── 統計 ──────────────────────────────────────────────────────────────────────

def calc_person_summary(expenses: list[dict]) -> dict[str, float]:
    summary: dict[str, float] = {}
    for exp in expenses:
        for item in exp.get("items", []):
            person = item.get("person", "")
            amount = item.get("amount", 0)
            if person:
                summary[person] = summary.get(person, 0) + amount
    return summary


def calc_aa(expenses: list[dict]) -> dict[str, float]:
    total = sum(exp.get("total", 0) for exp in expenses)
    persons = set()
    for exp in expenses:
        for item in exp.get("items", []):
            if item.get("person"):
                persons.add(item["person"])
    if not persons:
        return {}
    per_person = total / len(persons)
    return {p: per_person for p in persons}


# ── 格式化輸出 ────────────────────────────────────────────────────────────────

def format_expense_confirmation(expense: dict, project_name: str) -> str:
    lines = [
        "✅ 已記錄花費！",
        "",
        f"📂 {expense['project']}",
    ]

    category = expense.get("category", "")
    location = expense.get("location", "")
    loc_str = f"｜📍{location}" if location else ""
    lines.append(f"🏷️ {category}{loc_str}")
    lines.append(f"🕐 {expense['date']} {expense['time']}")
    lines.append("")

    items = expense.get("items", [])
    if items:
        for item in items:
            name = item.get("person", "")
            food = item.get("item", "")
            amount = item.get("amount", 0)
            lines.append(f"・{name}：{food + ' ' if food else ''}${amount:,.0f}")

        total = expense["total"]
        person_count = len(items)
        aa = total / person_count if person_count > 0 else 0
        lines.append("")
        lines.append(f"💰 合計：${total:,.0f}")
        lines.append(f"👥 每人 AA：${aa:,.0f}")
    else:
        lines.append(f"💰 金額：${expense.get('simple_amount', 0):,.0f}")

    lines.append("")
    lines.append("─────────────")
    lines.append("📸 有拍照嗎？傳圖片給我，我會提醒你存到相簿！")
    lines.append("📍 想記錄地點？傳送你的位置給我！")

    return "\n".join(lines)


def format_photo_reminder(expense: dict, project_name: str) -> str:
    date = expense.get("date", "")
    category = expense.get("category", "")
    location = expense.get("location", "")
    loc_str = f" @ {location}" if location else ""

    return (
        f"📸 圖片說明已記錄！\n\n"
        f"📂 專案：{project_name}\n"
        f"🏷️ {date} {category}{loc_str}\n\n"
        f"📌 請手動存到 LINE 相簿：\n"
        f"👉 相簿名稱：{project_name}\n"
        f"👉 建議路徑：{project_name} > {date}"
    )


def format_location_attached(expense: dict, gps: dict) -> str:
    category = expense.get("category", "")
    date = expense.get("date", "")
    title = gps.get("title", "")
    address = gps.get("address", "")

    return (
        f"📍 位置已記錄！\n\n"
        f"🏷️ {date} {category}\n"
        f"📌 {title}\n"
        f"🗺️ {address}\n\n"
        f"已附加到最近這筆花費記錄 ✅"
    )


def format_project_list(projects: list[dict]) -> str:
    if not projects:
        return "📂 目前沒有記帳專案\n\n用「+專案 名稱 說明」建立第一個專案！\n例：+專案 日本旅行 6/30-7/4"

    lines = [f"📂 記帳專案清單（{len(projects)} 個）\n"]
    for i, p in enumerate(projects, 1):
        total = p.get("total", 0)
        desc = f"　{p['description']}" if p.get("description") else ""
        lines.append(f"{i}. 【{p['name']}】{desc}\n   💰 累計：${total:,.0f}")

    return "\n".join(lines)


def format_project_detail(project: dict, expenses: list[dict]) -> str:
    name = project.get("name", "")
    total = project.get("total", 0)
    desc = project.get("description", "")

    lines = [f"💰 【{name}】記帳明細"]
    if desc:
        lines.append(f"📝 {desc}")
    lines.append("")

    if not expenses:
        lines.append("還沒有花費記錄！")
        lines.append(f"\n用「+花費 {name} 類別 地點 人名:品項:金額」新增")
        return "\n".join(lines)

    by_date: dict = {}
    for exp in expenses:
        date = exp.get("date", "")
        by_date.setdefault(date, []).append(exp)

    for date, exps in by_date.items():
        day_total = sum(e.get("total", 0) for e in exps)
        lines.append(f"📅 {date}（小計 ${day_total:,.0f}）")
        for exp in exps:
            category = exp.get("category", "")
            location = exp.get("location", "")
            time = exp.get("time", "")
            gps = exp.get("gps_location")

            # 地點顯示（手動輸入或GPS）
            if gps:
                loc_str = f" 📍{gps.get('title', '')}"
            elif location:
                loc_str = f" 📍{location}"
            else:
                loc_str = ""

            photo_str = " 📸" if exp.get("photo_noted") else ""
            lines.append(f"  【{time} {category}{loc_str}{photo_str}】")

            items = exp.get("items", [])
            if items:
                for item in items:
                    person = item.get("person", "")
                    food = item.get("item", "")
                    amount = item.get("amount", 0)
                    food_str = f"{food} " if food else ""
                    lines.append(f"    ・{person}：{food_str}${amount:,.0f}")
                exp_total = exp.get("total", 0)
                person_count = len(items)
                aa = exp_total / person_count if person_count > 0 else 0
                lines.append(f"    合計 ${exp_total:,.0f}｜每人 AA ${aa:,.0f}")
            else:
                amount = exp.get("simple_amount", exp.get("total", 0))
                lines.append(f"    ${amount:,.0f}")
            lines.append("")

    # 個人統計
    person_summary = calc_person_summary(expenses)
    if person_summary:
        lines.append("─────────────")
        lines.append("👤 個人花費統計")
        for person, amount in sorted(person_summary.items()):
            lines.append(f"  ・{person}：${amount:,.0f}")
        lines.append("")

        aa_result = calc_aa(expenses)
        if aa_result:
            lines.append("👥 AA 制應付金額")
            for person, amount in sorted(aa_result.items()):
                paid = person_summary.get(person, 0)
                diff = paid - amount
                if diff > 0:
                    status = f"（多付 ${diff:,.0f}，應收回）"
                elif diff < 0:
                    status = f"（少付 ${abs(diff):,.0f}，應補繳）"
                else:
                    status = "（剛好）"
                lines.append(f"  ・{person}：${amount:,.0f} {status}")
            lines.append("")

    lines.append("─────────────")
    lines.append(f"💳 總花費：${total:,.0f}")

    return "\n".join(lines)


# ── 收據掃描 ──────────────────────────────────────────────────────────────────

def parse_receipt_image(image_bytes: bytes, mime_type: str) -> dict:
    """
    用 Gemini Vision 辨識收據圖片。
    回傳：{
        "store": 店名,
        "items": [{"name": 品項, "price": 金額}, ...],
        "total": 總金額,
        "currency": 幣別（TWD/JPY）
    }
    """
    try:
        import json as _json
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

        prompt = """請分析這張收據圖片，回傳 JSON 格式（不要加任何說明或```符號）：
{
  "store": "店名或地點",
  "currency": "幣別（TWD或JPY或其他）",
  "items": [
    {"name": "品項名稱", "price": 金額數字},
    ...
  ],
  "total": 總金額數字
}

規則：
- 金額只填數字，不要加貨幣符號
- 如果看不清楚店名，填 "不明"
- 如果無法辨識為收據，回傳 {"error": "非收據圖片"}
- 幣別：台幣填 TWD，日幣填 JPY"""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                types.Part.from_text(text=prompt),
            ],
        )

        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return _json.loads(text.strip())

    except Exception as e:
        print(f"[Finance] 收據辨識失敗：{e}")
        return {"error": str(e)}


def format_receipt_result(receipt: dict, project_name: str) -> str:
    """格式化收據辨識結果。"""
    if "error" in receipt:
        return f"⚠️ 無法辨識收據：{receipt['error']}"

    store = receipt.get("store", "不明")
    currency = receipt.get("currency", "TWD")
    items = receipt.get("items", [])
    total = receipt.get("total", 0)

    currency_symbol = "¥" if currency == "JPY" else "$"

    lines = [
        f"🧾 收據辨識完成！",
        f"",
        f"🏪 {store}",
        f"",
    ]

    for item in items:
        name = item.get("name", "")
        price = item.get("price", 0)
        lines.append(f"・{name}　{currency_symbol}{price:,.0f}")

    lines.append(f"")
    lines.append(f"💰 合計：{currency_symbol}{total:,.0f}")
    lines.append(f"")
    lines.append(f"✅ 已自動記入專案：【{project_name}】")
    lines.append(f"")
    lines.append(f"📌 請手動存到 LINE 相簿：")
    lines.append(f"👉 相簿名稱：{project_name}")

    return "\n".join(lines)


def add_receipt_expense(receipt: dict, project_name: str) -> dict:
    """將收據辨識結果記入專案。"""
    store = receipt.get("store", "收據")
    total = receipt.get("total", 0)
    currency = receipt.get("currency", "TWD")
    items = receipt.get("items", [])

    # 轉換成花費格式
    parsed = {
        "project": project_name,
        "category": "收據",
        "location": store,
        "items": [],
        "total": total,
        "simple_amount": total,
        "currency": currency,
    }

    expense, doc_id = add_expense(parsed)
    return expense
