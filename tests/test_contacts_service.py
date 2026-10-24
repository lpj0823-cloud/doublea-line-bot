"""關係人經驗名錄（contacts_service）：python -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ContactsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = self.tmp.name
        for k in ("PROGRESS_SHEET", "CONTACTS_SHEET"):
            os.environ.pop(k, None)
        import contacts_service
        self.cs = contacts_service

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_add_fullwidth_colon(self):
        self.assertEqual(self.cs.parse_command("名錄 王○○：估驗前一週要先寄估驗表"),
                         ("add", "王○○", "估驗前一週要先寄估驗表"))

    def test_parse_add_halfwidth_colon(self):
        self.assertEqual(self.cs.parse_command("名錄王○○ :  只准平日施工 "), ("add", "王○○", "只准平日施工"))

    def test_parse_find(self):
        self.assertEqual(self.cs.parse_command("名錄 估驗"), ("find", "估驗", ""))
        self.assertEqual(self.cs.parse_command("查名錄 王○○"), ("find", "王○○", ""))

    def test_parse_help_and_empty_note(self):
        self.assertEqual(self.cs.parse_command("名錄"), ("help", "", ""))
        self.assertEqual(self.cs.parse_command("名錄 王○○："), ("help", "", ""))
        self.assertIsNone(self.cs.parse_command("名錄要整理一下嗎大家覺得呢這句很長不是指令而是聊天內容喔真的很長"))
        self.assertIsNone(self.cs.parse_command("今天開會"))

    def test_add_and_find_by_name_unit_tag(self):
        self.cs.add_note("王○○", "估驗前一週要先寄估驗表 #估驗 #文件", "淨雯", "2026-10-25")
        self.cs.add_note("李○○", "只准平日施工", "Justin", "2026-10-24")
        r = self.cs.find("王")
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["note"], "估驗前一週要先寄估驗表")
        self.assertEqual(r[0]["tags"], "估驗、文件")
        self.assertEqual(r[0]["author"], "淨雯")
        self.assertEqual([x["name"] for x in self.cs.find("估驗")], ["王○○"])
        self.cs._save_sheet_rows([["陳○○", "○○水電行", "工班", "工班與廠商", "", "防水做兩道", "防水", "Justin", "2026-10-20", "現任"]])
        self.assertEqual([x["name"] for x in self.cs.find("水電")], ["陳○○"])

    def test_format_limits_and_marks_former(self):
        rows = [{"name": "王○○", "unit": "", "role": "", "note": f"第{i}則", "tags": "", "author": "J",
                 "date": f"2026-10-{i:02d}", "state": "前任" if i == 12 else "現任"} for i in range(1, 13)]
        rows.sort(key=lambda x: x["date"], reverse=True)
        text = self.cs.format_results("王○○", rows, "https://example.com/sheet")
        self.assertIn("第12則", text)
        self.assertNotIn("第2則", text)
        self.assertIn("還有 2 則", text)
        self.assertIn("（前任）", text)
        self.assertIn("https://example.com/sheet", text)
        self.assertIn("名錄 王○○：", self.cs.format_results("王○○", []))

    def test_sync_disabled_returns_none(self):
        self.assertFalse(self.cs.sheet_enabled())
        self.assertIsNone(self.cs.sync())

    def test_enabled_requires_allowed_group(self):
        self.assertFalse(self.cs.enabled(False, ""))
        self.assertTrue(self.cs.enabled(True, "birthday,finance"))
        self.assertFalse(self.cs.enabled(True, "finance, Contacts"))


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class _NotFound(Exception):
    status_code = 404


class _FakeSheets:
    """假的 Sheets 服務：記住每份試算表的格子；gone 裡的 id 一律回 404。"""
    def __init__(self):
        self.books, self.gone, self.n = {}, set(), 0

    def spreadsheets(self):
        return self

    def create(self, body, fields):
        self.n += 1
        sid = f"S{self.n}"
        self.books[sid] = {"名錄": [], "怎麼寫": []}
        return _Req(lambda: {"spreadsheetId": sid, "spreadsheetUrl": f"https://sheet/{sid}"})

    def values(self):
        return self

    def _tab(self, sid, rng):
        if sid in self.gone:
            raise _NotFound("not found")
        return self.books[sid][rng.split("'")[1]]

    def update(self, spreadsheetId, range, valueInputOption, body):
        def run():
            tab = self._tab(spreadsheetId, range)
            tab[:] = body["values"]
        return _Req(run)

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        return _Req(lambda: self._tab(spreadsheetId, range).extend(body["values"]))

    def get(self, spreadsheetId, range):
        return _Req(lambda: {"values": self._tab(spreadsheetId, range)[1:]})


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = self.tmp.name
        os.environ.pop("CONTACTS_SHEET", None)
        os.environ["PROGRESS_SHEET"] = "on"
        import contacts_service
        import progress_service
        self.cs, self.ps = contacts_service, progress_service
        self.fake = _FakeSheets()
        self._orig = progress_service._service
        progress_service._service = lambda: self.fake

    def tearDown(self):
        self.ps._service = self._orig
        os.environ.pop("PROGRESS_SHEET", None)
        self.tmp.cleanup()

    def test_sync_creates_sheet_and_appends_only_new(self):
        self.cs.add_note("王○○", "估驗前一週要先寄估驗表", "淨雯", "2026-10-25")
        self.cs.add_note("李○○", "只准平日施工", "Justin", "2026-10-24")
        self.assertIsNone(self.cs.sync())
        rows = self.fake.books["S1"]["名錄"]
        self.assertEqual(rows[0][0], "姓名")
        self.assertEqual(len(rows), 3)
        self.assertEqual(self.cs.sheet_url(), "https://sheet/S1")
        self.fake.books["S1"]["名錄"].append(["陳○○", "○○水電行", "工班", "", "", "防水做兩道", "防水", "J", "2026-10-20", "現任"])
        self.cs.add_note("王○○", "會勘只看表上的項目", "淨雯", "2026-10-26")
        self.assertIsNone(self.cs.sync())
        self.assertEqual(len(self.fake.books["S1"]["名錄"]), 5)
        self.assertEqual([r["note"] for r in self.cs.find("王○○")], ["會勘只看表上的項目", "估驗前一週要先寄估驗表"])
        self.assertEqual([r["name"] for r in self.cs.find("水電")], ["陳○○"])

    def test_sync_error_keeps_local_notes(self):
        def boom():
            raise RuntimeError("network down")
        self.ps._service = boom
        self.cs.add_note("王○○", "估驗前一週要先寄估驗表", "淨雯", "2026-10-25")
        err = self.cs.sync()
        self.assertTrue(err)
        self.assertEqual(len(self.cs.find("王○○")), 1)
        self.assertIn("沒同步", self.cs.added_text(self.cs.find("王○○")[0] | {"tags": ""}, err))

    def test_sync_recreates_deleted_sheet_and_resends(self):
        self.cs.add_note("王○○", "估驗前一週要先寄估驗表", "淨雯", "2026-10-25")
        self.assertIsNone(self.cs.sync())
        self.fake.gone.add("S1")
        self.cs.add_note("李○○", "只准平日施工", "Justin", "2026-10-24")
        self.assertIsNone(self.cs.sync())
        self.assertEqual(self.cs.sheet_url(), "https://sheet/S2")
        self.assertEqual(len(self.fake.books["S2"]["名錄"]), 3)


if __name__ == "__main__":
    unittest.main()
