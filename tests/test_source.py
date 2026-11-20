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

    def test_portfolio_scope_and_download(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright, playwright.chromium.launch() as browser:
            page = browser.new_page(accept_downloads=True)
            page.goto(self.base)
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

