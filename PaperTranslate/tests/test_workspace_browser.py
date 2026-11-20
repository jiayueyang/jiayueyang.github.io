"""Browser acceptance for the workspace, using deterministic API fixtures and the real web assets."""
import mimetypes
import os
from pathlib import Path
import shutil
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1] / "web"
ORIGIN = "http://fixture.test"
PAPER = {
    "id": "book1", "title": "Surface gravity", "authors": "A. Author", "identifier": "arXiv:test",
    "status": "ready", "method": "latex", "warnings": [], "progress": {"done": 30, "total": 30},
    "sections": [{"id": "sec1", "title": "First law", "paragraphs": [
        {"id": f"p{i}", "kind": "prose",
         "sentences": [{"id": f"s{i}", "en": f"Discussion of horizon {i}. " + ("{{eq:e1}}" if i == 10 else ""),
                        "zh": f"关于视界的讨论，第 {i} 段。" + ("{{eq:e1}}" if i == 10 else "")}]} for i in range(30)
    ]}], "equations": {"e1": {"latex": "T=1/(8\\pi M)", "paragraph_id": "p10", "display": False}},
}


@unittest.skipUnless(sync_playwright, "Playwright is optional")
class WorkspaceAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            path = os.getenv("CHROMIUM") or shutil.which("chromium") or cls.pw.chromium.executable_path
            if not Path(path).exists():
                raise unittest.SkipTest("Chromium is unavailable")
            cls.browser = cls.pw.chromium.launch(executable_path=path)
        except Exception as exc:
            cls.pw.stop()
            if "socket() failed: Operation not permitted" in str(exc):
                raise unittest.SkipTest("This runner blocks Chromium process sockets") from exc
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.submitted = None
        self.offline = False
        self.api_requests = []

        def respond(route):
            path = route.request.url.removeprefix(ORIGIN)
            if path.startswith("/PaperTranslate/web/"):
                path = path.removeprefix("/PaperTranslate/web")
            if path.startswith("/api/"):
                self.api_requests.append(path)
            if path.startswith("/api/") and self.offline:
                route.fulfill(status=404,body="Not found")
            elif path == "/api/config":
                route.fulfill(json={"ai_available": True})
            elif path == "/api/papers" and route.request.method == "POST":
                self.submitted = route.request.post_data_buffer
                route.fulfill(json={"id": "book1", "status": "queued"})
            elif path == "/api/papers":
                route.fulfill(json=[PAPER])
            elif path in ("/api/papers/book1", "/api/sample", "/sample.json"):
                route.fulfill(json=dict(PAPER, id="sample" if path != "/api/papers/book1" else "book1"))
            elif path.endswith("/export/chinese") or path == "/example/chinese.pdf":
                route.fulfill(content_type="application/pdf", body=b"%PDF-1.4\n% test fixture")
            else:
                file = ROOT / (path.lstrip("/") or "index.html")
                if file.is_file() and file.resolve().is_relative_to(ROOT):
                    route.fulfill(content_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream",
                                  body=file.read_bytes())
                else:
                    route.fulfill(status=404,body="Not found")

        self.page.route(ORIGIN+"/**",respond)

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def test_import_reader_search_bookmark_download(self):
        page=self.page
        page.goto(ORIGIN)
        page.locator("#source").fill("https://arxiv.org/abs/2401.12345")
        page.locator("#import-form [data-action=settings]").click()
        page.locator("#style-choice").select_option("literal")
        page.locator("#glossary").fill("surface gravity = 表面引力")
        page.locator("[data-action=save-settings]").click()
        page.locator("#submit-import").click()
        page.wait_for_selector(".reader-pane")
        self.assertIn(b"literal",self.submitted)
        self.assertIn("表面引力".encode(),self.submitted)
        self.assertEqual(page.locator(".reader-pane").count(),2)
        self.assertEqual(page.locator("math").count(),2)
        page.locator("#paper-search").fill("10")
        self.assertGreaterEqual(page.locator(".passage.match").count(),2)
        page.locator("[data-action=bookmark]").click()
        self.assertEqual(page.locator(".passage.bookmarked").count(),2)
        page.locator("[data-action=view][data-value=zh]").click()
        self.assertEqual(page.locator(".reader-pane").count(),1)
        page.reload()
        page.wait_for_selector(".reader-pane")
        self.assertEqual(page.locator(".reader-pane").count(),1)
        self.assertEqual(page.locator(".passage.bookmarked").count(),1)
        page.locator("[data-action=exports]").click()
        with page.expect_download() as result:
            page.locator("[data-action=download][data-kind=chinese]").click()
        self.assertTrue(result.value.suggested_filename.endswith("chinese.pdf"))

    def test_static_setup_and_sample(self):
        self.offline=True
        self.page.set_viewport_size({"width":390,"height":844})
        self.page.goto(ORIGIN+"/PaperTranslate/web/")
        self.page.locator("#submit-import").click()
        self.assertTrue(self.page.get_by_role("link",name="在 Codespaces 中打开").is_visible())
        self.page.locator("[data-action=dismiss]").click()
        self.page.locator(".sample-open").click()
        self.page.wait_for_selector(".reader-pane")
        self.assertEqual(self.page.locator(".reader-pane").count(),2)
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"),390)
        self.page.locator("[data-action=locale]").click()
        self.assertEqual(self.page.locator("html").get_attribute("lang"),"en")
        self.assertEqual(self.api_requests,[])
        self.assertEqual(self.page.get_by_role("link",name="View original PDF").get_attribute("href"),
                         "./example/original.pdf")


if __name__ == "__main__":
    unittest.main()
