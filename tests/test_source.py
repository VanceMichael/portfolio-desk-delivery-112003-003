from pathlib import Path
from threading import Thread
import json
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from portfolio_portal import create_source


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.server = create_source()
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def source_response(self, path):
        try:
            with urlopen(self.base + path, timeout=2) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    def test_input_documents(self):
        for name in ['dispatch', 'mailroom', 'pages']:
            with self.subTest(document=name):
                value = Path("data", name + ".json").read_text(encoding="utf-8")
                self.assertTrue(json.loads(value))

    def advance(self, page, turn):
        page.get_by_role("link", name="场馆时钟").click()
        page.get_by_label("公告轮次").fill(turn)
        page.get_by_role("button", name="推进公告").click()

    def test_portfolio_scope_and_download(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright, playwright.chromium.launch() as browser:
            page = browser.new_page(accept_downloads=True)
            page.goto(self.base)
            self.advance(page, "2")
            page.get_by_role("link", name="席位选择").click()
            page.get_by_label("企业席位").select_option("E2")
            page.get_by_label("面谈场次").select_option("PM2")
            page.get_by_label("作品用途").select_option("named")
            page.get_by_label("授权公告轮次").select_option("2")
            page.get_by_role("button", name="查看授权").click()
            self.assertEqual(page.locator("dd").first.inner_text(), "P15")
            with page.expect_popup() as event:
                page.get_by_role("link", name="打开领取页").click()
            stale = event.value
            self.assertEqual(stale.locator("dd").first.inner_text(), "P12")
            stale.close()
            page.get_by_role("link", name="刷新授权与领取入口").click()
            with page.expect_popup() as event:
                page.get_by_role("link", name="打开领取页").click()
            pickup = event.value
            self.assertEqual(pickup.locator("dd").first.inner_text(), "P15")
            with pickup.expect_download() as event:
                pickup.get_by_role("link", name="W17N").click()
            self.assertIn("J17", event.value.path().read_text(encoding="utf-8"))

    def counter(self, page, action, fields, files=()):
        page.get_by_role("link", name="场馆中转台").click()
        page.get_by_label("手续").select_option(action)
        for field, value in fields.items():
            page.get_by_label(field, exact=True).fill(value)
        if files:
            page.get_by_label("作品文件").set_input_files(list(files))
        page.get_by_role("button", name="交中转台").click()

    def lookup(self, page, delivery, ticket=""):
        page.get_by_role("link", name="场馆中转台").click()
        page.get_by_label("查询投递号").fill(delivery)
        if ticket:
            page.get_by_label("查询办理号").fill(ticket)
        page.get_by_role("button", name="查询", exact=True).click()

    def test_hold_and_seat_purge_have_independent_receipts(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright, playwright.chromium.launch() as browser:
            page = browser.new_page()
            page.set_default_timeout(5000)
            page.goto(self.base)
            self.advance(page, "2")
            self.counter(page, "cancel", {"办理号":"C1", "投递号":"TX2", "需处理的成员":"W17Appendix.txt"})
            self.counter(page, "purge", {"办理号":"C2", "投递号":"TX2", "需处理的成员":"W17N.txt"})
            rows = page.get_by_role("table", name="投递成员")
            self.assertIn("中转已撤", rows.get_by_role("row").filter(has_text="W17Appendix.txt").inner_text())
            self.assertIn("待席位处理", rows.get_by_role("row").filter(has_text="W17N.txt").inner_text())
            self.advance(page, "3")
            self.lookup(page, "TX2")
            self.assertIn("席位已清除", rows.get_by_role("row").filter(has_text="W17N.txt").inner_text())
            self.lookup(page, "TX1")
            self.assertIn("席位已收", rows.inner_text())

    def test_new_submission_exists_when_ack_is_temporarily_absent(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright, playwright.chromium.launch() as browser:
            page = browser.new_page()
            page.set_default_timeout(5000)
            page.goto(self.base)
            self.advance(page, "3")
            uploads = [Path("originals/works", name + ".txt").resolve() for name in ("W17N", "W17Appendix")]
            self.counter(page, "dispatch", {"办理号":"S77", "投递号":"NEW77", "交付单":"D102", "授权编号":"P15"}, uploads)
            self.assertTrue(page.get_by_role("heading", name="作品领取").is_visible())
            self.lookup(page, "NEW77", "S77")
            self.assertTrue(page.get_by_text("办理回条未到").is_visible())
            self.assertIn("中转留存", page.get_by_role("table", name="投递成员").inner_text())
            self.advance(page, "4")
            self.lookup(page, "NEW77", "S77")
            self.assertTrue(page.get_by_text("S77 已进入中转", exact=False).is_visible())
