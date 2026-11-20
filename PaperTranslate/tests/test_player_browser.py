"""Browser acceptance for real media events. Install playwright and Chromium to run locally.

Responses are deterministic fixtures. HTMLAudioElement, its clock, the reader module,
seeking, pause/resume, automatic sentence changes, caching, and persistence are real.
"""
import io
import json
import os
import shutil
import struct
import unittest
import wave
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
DOC = {
    "id": "test", "title": "Test", "equations": {"e1": {"latex": "E=mc^2", "paragraph_id": "p1"}},
    "sections": [{"id": "sec", "title": "Setup", "paragraphs": [
        {"id": "p1", "sentences": [{"id": "s1", "en": "First {{eq:e1}}.", "zh": "第一句。"}]},
        {"id": "p2", "sentences": [{"id": "s2", "en": "Second.", "zh": "第二句。"}]},
        {"id": "p3", "sentences": [{"id": "s3", "en": "Third.", "zh": "第三句。"}]},
    ]}],
}
EXPLANATION = {"id":"exp1","text":"动能可以展开。","sentences":[{"id":"ex1","zh":"动能可以展开。","kind":"deduced",
                "heading":"推导","references":["p1"]}],"references":["p1"]}


def wav(seconds):
    dest = io.BytesIO()
    with wave.open(dest, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(struct.pack('<h', 0) * int(8000 * seconds))
    return dest.getvalue()


@unittest.skipUnless(sync_playwright, "Playwright is optional")
class PlayerAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            binary = os.getenv("CHROMIUM") or shutil.which("chromium") or cls.pw.chromium.executable_path
            if not Path(binary).exists():
                raise unittest.SkipTest("Install Chromium to run browser acceptance")
            cls.browser = cls.pw.chromium.launch(executable_path=binary, args=["--autoplay-policy=no-user-gesture-required"])
        except Exception as exc:
            cls.pw.stop()
            if 'socket() failed: Operation not permitted' in str(exc):
                raise unittest.SkipTest("This runner does not permit Chromium's process socket") from exc
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.log, self.errors = [], []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        source = (ROOT / "web/player.js").read_text()
        css = (ROOT / "web/player.css").read_text()
        columns = ''.join(
            f'<div data-paragraph="p{i}"><span data-lang="{lang}" data-sentence="s{i}">Sentence {i}</span></div>'
            for lang in ["en", "zh"] for i in [1, 2, 3]
        )
        index = f"""<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/player.css">{columns}
        <div id="player-root"></div><div id="tutor-root"></div><script type="module">
        import * as controls from "/player.js";window.player=controls;
        window.PaperTranslate={{goParagraph:id=>window.visited=id,math:(n,tex)=>n.textContent=tex}};
        window.doc={json.dumps(DOC)}; controls.setDocument(window.doc);
        for(const el of document.querySelectorAll('[data-sentence]'))el.onclick=()=>controls.readFrom(el.dataset.sentence,el.dataset.lang);
        </script>"""
        self.clips = {k:wav(d) for k,d in [('s1',1.8),('s2',2.1),('s3',1.8),('ex1',2)]}

        def respond(route):
            path = route.request.url.split("http://fixture.test")[-1]
            payload = route.request.post_data_json if route.request.post_data else None
            self.log.append((path, payload))
            if path == "/": route.fulfill(content_type="text/html",body=index)
            elif path == "/player.js": route.fulfill(content_type="text/javascript",body=source)
            elif path == "/player.css": route.fulfill(content_type="text/css",body=css)
            elif path.endswith("/audio"): route.fulfill(content_type="audio/wav",body=self.clips[payload['sentence_id']])
            elif path.endswith("/explain"): route.fulfill(json=EXPLANATION)
            elif path.endswith("/speech-preview"): route.fulfill(json={"text":"能量等于质量乘以光速的平方。"})
            elif path.endswith("/audio-export"): route.fulfill(json={"id":"j1","status":"queued"})
            elif path.endswith("/audio-export/j1"): route.fulfill(json={"id":"j1","status":"ready"})
            else: route.fulfill(status=404,body="Unknown fixture")
        self.page.route("http://fixture.test/**",respond)
        self.page.goto("http://fixture.test/")
        self.page.wait_for_selector("#pt-play")

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [])

    def test_real_media_reader(self):
        page = self.page
        page.locator('[data-sentence=s1][data-lang=zh]').click()
        page.wait_for_selector('[data-sentence=s1][data-lang=zh].pt-speaking')
        self.assertEqual(page.locator('[data-paragraph=p1].pt-speaking-paragraph').count(), 2)
        page.locator("#pt-play").click()
        self.assertEqual(page.locator("#pt-play").get_attribute("aria-label"), "播放")
        page.locator("#pt-play").click()
        page.wait_for_selector('[data-sentence=s2].pt-speaking')
        page.locator("#pt-settings-btn").click()
        page.get_by_label("语速",exact=True).select_option("2")
        page.get_by_role('button',name="关闭",exact=True).click()
        page.locator('[data-sentence=s3][data-lang=zh]').click()
        page.wait_for_selector('[data-sentence=s3].pt-speaking')
        page.locator("#pt-timeline").evaluate("(el)=>{el.value=500;el.dispatchEvent(new Event('change'))}")
        page.wait_for_selector('[data-sentence=s1].pt-speaking')
        page.evaluate("player.closeDocument(); player.setDocument(doc)")
        self.assertEqual(page.locator("#pt-counter").inner_text(), "Setup · 句 1 / 3")
        self.assertTrue(any(p.endswith("/audio") and body["sentence_id"]=="s2" for p,body in self.log))
        page.locator("#pt-settings-btn").click()
        page.get_by_role('button',name="生成全文 MP3").click()
        page.get_by_role('link',name="下载 MP3").wait_for()
        self.assertTrue(page.get_by_role('link',name="下载 MP3").get_attribute("href").endswith("/j1/file"))

    def test_equation_tutor_and_narration(self):
        page=self.page
        page.evaluate("player.openEquation('e1')")
        page.get_by_role('button',name="朗读公式").click()
        page.get_by_role('textbox',name="预览或修改公式读音").fill("质量乘以光速的平方。")
        page.get_by_role('button',name="播放公式").click()
        page.wait_for_selector('[data-sentence=s1].pt-speaking')
        self.assertTrue(any(p.endswith("/audio") and body.get("override") == "质量乘以光速的平方。" for p,body in self.log))
        page.get_by_role('button',name="逐步推导").click()
        page.get_by_role('button',name="朗读讲解").click()
        page.wait_for_selector('[data-tutor-sentence=ex1].pt-speaking')
        self.assertTrue(any(p.endswith("/explain") and body["depth"]=="derivation" and body["equation_id"]=="e1" for p,body in self.log))
        page.get_by_role('textbox',name="追问").fill("为什么？")
        page.get_by_role('button',name="提问",exact=True).click()
        page.get_by_role('button',name="Setup · p1").click()
        self.assertEqual(page.evaluate("window.visited"),"p1")
        page.evaluate("player.openTutor()")
        page.get_by_role('button',name="生成讲解").click()
        page.get_by_role('button',name="朗读讲解").click()
        page.wait_for_selector('[data-tutor-sentence=ex1].pt-speaking')
        self.assertTrue(any(p.endswith("/explain") and body["equation_id"] is None for p,body in self.log))
