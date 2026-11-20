"""End-to-end HTTP journey with real parsing, database, PDF exports and encoded audio.

Only external arXiv and OpenAI responses are replaced. The separate PDF-upload
test also exercises the resource-limited parser child process used in production.
"""
from collections import Counter
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pymupdf

from papertranslate import api, ingest


EQUATION = r"T=\frac{\kappa}{2\pi}"
TEX = r"""\documentclass{article}
\title{Horizons}
\author{A. Researcher}
\begin{document}
\maketitle
\begin{abstract}We study horizons.\end{abstract}
\section{Temperature}
The horizon has a temperature.
\begin{equation}T=\frac{\kappa}{2\pi}\label{temp}\end{equation}
We use Eq.~\eqref{temp}.
\end{document}
"""
FEED = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>https://arxiv.org/abs/2401.12345v2</id><title>Horizons</title>
    <author><name>A. Researcher</name></author><summary>We study horizons.</summary>
    </entry></feed>"""


def _paper_pdf():
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        pdf.set_metadata({"title":"Horizons","author":"A. Researcher"})
        page.insert_text((60,65),"Horizons",fontsize=19)
        page.insert_text((60,125),"We study horizons.")
        page.insert_text((60,175),"The horizon has a temperature.")
        page.insert_text((100,225),"T = k / (2 pi)")
        return pdf.tobytes()


def _mp3():
    return subprocess.check_output(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi",
        "-i","sine=frequency=440:duration=0.16","-ar","24000","-ac","1","-f","mp3","pipe:1"])


class RemoteFixtures:
    def __init__(self, pdf, audio):
        self.pdf, self.audio = pdf, audio
        self.calls = Counter()

    def arxiv(self, url, *args, **opts):
        self.calls[url] += 1
        if "export.arxiv.org/api/query" in url:
            return FEED
        if url == "https://arxiv.org/pdf/2401.12345v2":
            return self.pdf
        if url == "https://arxiv.org/src/2401.12345v2":
            return gzip.compress(TEX.encode())
        raise AssertionError(f"Unexpected external URL: {url}")

    def openai(self, request, **options):
        url = request.full_url
        self.calls[url] += 1
        body = json.loads(request.data)
        if url == "https://api.openai.com/v1/audio/speech":
            if not body["input"].strip():
                raise AssertionError("Empty audio input")
            return io.BytesIO(self.audio)
        if url != "https://api.openai.com/v1/responses":
            raise AssertionError(f"Unexpected external URL: {url}")
        payload = json.loads(body["input"])
        kind = body["text"]["format"]["name"]
        if kind == "paper_translation":
            dictionary={
                "Horizons":"视界", "We study horizons.":"我们研究视界。", "Temperature":"温度",
                "The horizon has a temperature.":"视界具有温度。", "Abstract":"摘要",
            }
            rows = []
            for row in payload["sentences"]:
                sent = row["en"].strip()
                if sent.startswith("We use Eq."):
                    translated = "我们使用公式" + "".join(re.findall(r"⟪PT\d+⟫",sent)) + "。"
                else:
                    translated = dictionary.get(sent)
                if translated is None:
                    raise AssertionError("Unrecognized source sentence in test provider: " + sent)
                rows.append({"id":row["id"],"zh":translated})
            result = {"sentences":rows}
        elif kind == "paper_tutorial":
            source = next(row for row in payload["source_excerpts"] if EQUATION in row["text"])
            result = {"sections":[{"heading":"视界温度","sentences":[{"text":"温度与表面引力成正比。",
                        "kind":"source","references":[source["id"]]}]}]}
        else:
            raise AssertionError(kind)
        message = {"status":"completed","output":[{"type":"message","content":[
                     {"type":"output_text","text":json.dumps(result,ensure_ascii=False)}]}]}
        return io.BytesIO(json.dumps(message,ensure_ascii=False).encode())


class JourneyTests(unittest.TestCase):
    def success(self, response):
        self.assertLess(response.status_code,300,response.text[:500] if response.status_code>=300 else "")
        return response

    def ready(self, client, url):
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            result = self.success(client.get(url)).json()
            self.assertNotIn(result["status"],("cancelled","failed"),result.get("error"))
            if result["status"] == "ready":
                self.assertGreater(result["progress"]["total"],0)
                self.assertEqual(result["progress"]["total"],result["progress"]["done"])
                return result
            time.sleep(.04)
        self.fail("Job did not complete within the test timeout")

    @unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg required")
    def test_arxiv_translation_reading_and_listening(self):
        original = _paper_pdf()
        remotes = RemoteFixtures(original,_mp3())
        # Network fixtures are in this process, so invoke the exact parsing body
        # used by the child; the separate upload journey retains process isolation.
        def in_process(source, uploaded, folder, **opts):
            return ingest._ingest_document(source,uploaded,folder)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{"OPENAI_API_KEY":"fixture",
             "OPENAI_TTS_MODEL":"gpt-4o-mini-tts","PT_ACCESS_TOKEN":""}), \
             patch.object(ingest,"ingest",side_effect=in_process), patch.object(ingest,"fetch",side_effect=remotes.arxiv), \
             patch("urllib.request.urlopen",side_effect=remotes.openai):
            app = api.create_app(data_dir=tmp,start_worker=True)
            with TestClient(app,base_url="http://localhost") as client:
                imported = self.success(client.post("/api/papers",data={"source":"https://arxiv.org/abs/2401.12345",
                                                                         "mode":"accurate"})).json()
                root = "/api/papers/"+imported["id"]
                doc = self.ready(client,root)
                self.assertEqual(doc["method"],"latex")
                self.assertEqual(doc["identifier"],"2401.12345v2")
                self.assertEqual(remotes.calls["https://arxiv.org/src/2401.12345v2"],1)
                paragraphs = [p for sec in doc["sections"] for p in sec["paragraphs"]]
                sents = [s for p in paragraphs for s in p["sentences"]]
                self.assertTrue(all(s["zh"] for s in sents))
                current = next(s for s in sents if s["zh"]=="视界具有温度。")
                pid = next(p["id"] for p in paragraphs if current in p["sentences"])
                eq_id, eq = next(iter(doc["equations"].items()))
                self.assertIn(EQUATION,eq["latex"])
                self.assertEqual(self.success(client.get(root+"/export/original")).content,original)
                for kind in ["chinese","bilingual"]:
                    data = self.success(client.get(root+"/export/"+kind)).content
                    with pymupdf.open(stream=data,filetype="pdf") as reader:
                        words = "".join(p.get_text() for p in reader).replace(" ","")
                    self.assertIn("视界",words)
                    if kind=="bilingual":
                        self.assertIn("horizon",words.lower())
                with ZipFile(io.BytesIO(self.success(client.get(root+"/export/latex")).content)) as archive:
                    tex = archive.read("main.tex").decode()
                    self.assertIn(EQUATION,tex)
                    self.assertIn("视界具有温度",tex)
                voice={"language":"zh","formula_mode":"full","voice":"alloy"}
                mp3 = self.success(client.post(root+"/audio",json={**voice,"sentence_id":current["id"]}))
                self.assertIn("audio/",mp3.headers["content-type"])
                self.assertEqual(mp3.content,remotes.audio)
                repeat = self.success(client.post(root+"/audio",json={**voice,"sentence_id":current["id"]}))
                self.assertEqual(mp3.content,repeat.content)
                self.assertEqual(remotes.calls["https://api.openai.com/v1/audio/speech"],1)
                for target, depth in [(eq_id,"derivation"),(None,"detailed")]:
                    ex=self.success(client.post(root+"/explain",json={"equation_id":target, "depth":depth,
                                            "history":[],"question":""})).json()
                    self.assertIn(eq["paragraph_id"],ex["references"])
                    self.assertTrue(ex["id"])
                    segment=ex["sentences"][0]
                    self.assertIn("表面引力",segment["zh"])
                    self.success(client.post(root+"/audio",json={**voice,"sentence_id":segment["id"],"explanation_id":ex["id"]}))
                job = self.success(client.post(root+"/audio-export",json=voice)).json()
                media_url=root+"/audio-export/"+job["id"]
                self.ready(client,media_url)
                whole=self.success(client.get(media_url+"/file"))
                self.assertGreater(len(whole.content),len(remotes.audio))
                state={"mode":"bilingual","paragraph_id":pid,"bookmarks":[{"paragraph_id":pid,"sentence_id":current["id"]}]}
                self.success(client.put(root+"/reading-state",json=state))
            with TestClient(api.create_app(data_dir=tmp,start_worker=False),base_url="http://localhost") as reopened:
                state2=self.success(reopened.get(root+"/reading-state")).json()
                self.assertEqual(state2["paragraph_id"],pid)
                self.assertEqual(state2["bookmarks"],state["bookmarks"])
                self.assertEqual(self.success(reopened.get(root)).json()["status"],"ready")

    def test_pdf_upload_runs_isolated_parser_and_serves_original_equation(self):
        original=_paper_pdf()
        remotes=RemoteFixtures(original,b"")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{"OPENAI_API_KEY":"fixture","PT_ACCESS_TOKEN":""}), \
             patch("urllib.request.urlopen",side_effect=remotes.openai), \
             TestClient(api.create_app(data_dir=tmp,start_worker=True),base_url="http://localhost") as client:
            item=self.success(client.post("/api/papers",files={"file":("horizons.pdf",original,"application/pdf")})).json()
            root="/api/papers/"+item["id"]
            doc=self.ready(client,root)
            self.assertEqual(doc["method"],"pdf")
            eq=next(e for e in doc["equations"].values() if e.get("image"))
            raw=self.success(client.get(root+"/"+eq["image"]))
            self.assertTrue(raw.content.startswith(b"\x89PNG"))
            self.assertEqual(self.success(client.get(root+"/export/original")).content,original)
