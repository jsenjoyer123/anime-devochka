"""E2E-тесты фронта: настоящий сервер и браузер, вместо модели — заготовленный NDJSON.

Запуск:  uv run --with playwright python -m unittest discover -s tests/e2e
Браузер: скачанный `playwright install chromium`, а если его нет — системный (AIKO_CHROMIUM=/путь).
Нужен интернет: three.js и three-vrm грузятся с CDN.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:
    sync_playwright = None

import http_api  # noqa: E402
import usage  # noqa: E402
import workspace  # noqa: E402

SYSTEM_CHROMIUM = os.environ.get("AIKO_CHROMIUM") or shutil.which("chromium") or shutil.which("google-chrome")


@unittest.skipIf(sync_playwright is None, "нет playwright: uv run --with playwright …")
class UiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        root = cls.tmp / "proj"
        root.mkdir()
        (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
        cls._paths = (workspace.NOTES_DIR, workspace.SENPAI_PATH, workspace.RECENT_PATH, workspace.USAGE_PATH)
        workspace.NOTES_DIR = cls.tmp / "notes"
        workspace.SENPAI_PATH = workspace.NOTES_DIR / "_senpai.md"
        workspace.RECENT_PATH = workspace.NOTES_DIR / "_projects.json"
        workspace.USAGE_PATH = workspace.NOTES_DIR / "_usage.json"
        http_api.API_KEY, http_api.MODEL = "key", "test-model"
        http_api.AGENTS.clear()
        cls.agent = http_api.open_project(root)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), http_api.Handler)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except PlaywrightError:  # свой браузер Playwright не скачан — берём системный
            cls.browser = cls.pw.chromium.launch(executable_path=SYSTEM_CHROMIUM)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        workspace.NOTES_DIR, workspace.SENPAI_PATH, workspace.RECENT_PATH, workspace.USAGE_PATH = cls._paths
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.page = self.browser.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.confirms = []  # что страница отправила в /api/confirm
        self.page.route("**/api/confirm", self._confirm)
        self.page.route("**/api/tts", lambda route: route.fulfill(status=501, json={"error": "нет"}))

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [])

    def _confirm(self, route):
        self.confirms.append(json.loads(route.request.post_data))
        route.fulfill(json={"ok": True})

    def chat_replies(self, *events):
        body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
        self.page.route("**/api/chat", lambda route: route.fulfill(
            body=body, headers={"Content-Type": "application/x-ndjson"}))

    def open(self):
        self.page.goto(self.url)
        self.page.wait_for_selector("#meta:has-text('test-model')")

    def say(self, text):
        self.page.fill("#input", text)
        self.page.press("#input", "Enter")

    def test_page_loads(self):
        self.open()
        self.assertIn("proj", self.page.text_content("#meta"))
        self.assertTrue(self.page.is_visible("#learn"))
        self.page.wait_for_selector("#loading", state="hidden", timeout=30_000)  # VRM загрузилась

    def test_run_confirm_allow_always(self):
        self.chat_replies({"type": "confirm", "id": "r1", "kind": "run", "command": "make test", "reason": "проверить"},
                          {"type": "reply", "text": "[happy] запускаю"})
        self.open()
        self.say("прогони тесты")
        self.page.click("text=Разрешать всегда")
        self.page.wait_for_selector("text=разрешено навсегда")
        self.assertEqual(self.confirms, [{"id": "r1", "approve": True, "always": True}])

    def test_commit_message_is_editable(self):
        self.chat_replies({"type": "confirm", "id": "c1", "kind": "commit", "message": "fix", "files": ["app.py"],
                           "add_all": False}, {"type": "reply", "text": "жду"})
        self.open()
        self.say("закоммить")
        self.assertFalse(self.page.is_visible("text=Разрешать всегда"))
        self.page.fill(".commit-msg", "fix: поправила привет")
        self.page.click("text=Закоммитить")
        self.page.wait_for_selector("text=коммитим")
        self.assertEqual(self.confirms[0]["message"], "fix: поправила привет")

    def test_diff_on_screen(self):
        self.chat_replies({"type": "diff", "title": "app.py · правки",
                           "diff": "diff --git a/app.py b/app.py\n@@ -1 +1 @@\n-print('hi')\n+print('привет')"},
                          {"type": "reply", "text": "вот"})
        self.open()
        self.say("покажи правки")
        self.page.wait_for_selector("#screen pre.diff")
        self.assertEqual(self.page.text_content("#screen-title"), "app.py · правки")
        classes = self.page.eval_on_selector_all("#screen pre.diff span", "els => els.map(e => e.className)")
        self.assertEqual(classes, ["file", "hunk", "del", "add"])

    def test_spent_counter_and_expensive_turn(self):
        self.chat_replies({"type": "usage", "cost": 0.04, "today": 0.31},
                          {"type": "usage", "cost": 0.03, "today": 0.34},
                          {"type": "reply", "text": "готово"})
        self.open()
        self.assertFalse(self.page.is_visible("#spent"))  # сегодня ещё ничего не потрачено
        usage.add({"cost": 0.34})  # сервер знает тот же итог, что пришёл в потоке
        self.say("изучи всё")
        self.page.wait_for_selector("text=этот ответ стоил ~7¢")
        self.assertEqual(self.page.text_content("#spent"), "💸 0,34 $")

    def test_allowed_commands_in_menu(self):
        self.agent.ws.set_allowed("make lint", True)
        self.open()
        self.page.click("#projects")
        self.page.click("text=✕ make lint")
        self.page.wait_for_selector("#project-menu .allowed", state="detached")
        self.assertEqual(self.agent.ws.allowed_commands(), [])


if __name__ == "__main__":
    unittest.main()
