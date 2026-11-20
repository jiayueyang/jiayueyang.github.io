import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from papertranslate.service import Service

def document(rows):
    paragraphs = []
    for i, (kind, text) in enumerate(rows):
        paragraphs.append({"id":f"p{i}", "kind":kind,
                           "sentences":[{"id":f"s{i}", "en":text, "zh":""}]})
    return {"title":"Gravity", "authors":"A. Researcher", "identifier":"", "method":"latex",
            "warnings":[], "equations":{},
            "sections":[{"id":"u1", "title":"Introduction", "paragraphs":paragraphs}]}

def words(result):
    return [s["zh"] for section in result["sections"] for p in section["paragraphs"] for s in p["sentences"]]

def translate(batch, *args):
    return {s["id"]:"译文 " + s["en"] for s in batch}

class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service(Path(self.tmp.name)/"store")
        self.addCleanup(self.service.stop)

    def create(self):
        return self.service.create(source="https://arxiv.org/abs/2401.12345")["id"]

    def test_failure_resumes_at_unfinished_batch(self):
        source = document([("prose", f"Result {i}.") for i in range(19)])
        pid = self.create()
        def interrupt(batch, *args):
            if batch[0]["id"] == "s12":
                raise RuntimeError("Test interruption")
            return translate(batch)
        with patch("papertranslate.ingest.ingest", return_value=source, create=True) as ingest, \
             patch("papertranslate.ai.translate_batch", side_effect=interrupt):
            self.service._work_one()
            self.assertEqual(ingest.call_count, 1)
        self.assertEqual(self.service.get(pid)["status"], "failed")
        self.assertEqual(self.service.get(pid)["progress"], {"done":12, "total":19})
        service2 = Service(self.service.root)
        service2.resume(pid)
        with patch("papertranslate.ai.translate_batch", side_effect=translate) as ai:
            service2._work_one()
            ids = [s["id"] for s in ai.call_args.args[0]]
            self.assertEqual(ids, [f"s{i}" for i in range(12, 19)])
        self.assertEqual(service2.get(pid)["status"], "ready")
        self.assertTrue(all(words(service2.get(pid))))

    def test_cancelled_result_cannot_overwrite_resumed_job(self):
        source=document([("prose", "The state is pure.")])
        pid=self.create()
        def cancel_and_resume(batch, *args):
            self.service.cancel(pid)
            self.service.resume(pid)
            return translate(batch)
        with patch("papertranslate.ingest.ingest", return_value=source, create=True), \
             patch("papertranslate.ai.translate_batch", side_effect=cancel_and_resume):
            self.service._work_one()
        self.assertEqual(self.service.get(pid)["status"], "queued")
        self.assertEqual(self.service.get(pid)["progress"]["done"], 0)
        with patch("papertranslate.ai.translate_batch", side_effect=translate):
            self.service._work_one()
        self.assertEqual(self.service.get(pid)["status"], "ready")

    def test_translation_policy_and_scan_completeness(self):
        source=document([("prose", "Area increases."), ("equation", "{{eq:e1}}"),
                         ("reference", "A. Name, Journal 3, p. 5."),
                         ("reference_title", "Physical limits."), ("acknowledgment", "Thanks to Ada.")])
        source["untranslated_pages"]=[5]
        pid=self.service.create(source="test", translate_refs=True, acknowledgments=False)["id"]
        with patch("papertranslate.ingest.ingest", return_value=source, create=True), \
            patch("papertranslate.ai.translate_batch", side_effect=translate):
            self.service._work_one()
        result=self.service.get(pid)
        self.assertEqual(result["status"], "failed")
        self.assertIn("5", result["error"])
        self.assertEqual(words(result), ["译文 Area increases.", "{{eq:e1}}", "A. Name, Journal 3, p. 5.",
                                        "译文 Physical limits.", "Thanks to Ada."])
        with self.assertRaises(ValueError):
            self.service.queue_audio(pid, {"language":"zh"})

    def test_progress_lives_across_worker_restart_and_only_one_process_claims_job(self):
        source=document([("prose", "One.")])
        pid=self.create()
        service2=Service(self.service.root)
        self.addCleanup(service2.stop)
        with self.service._conn(True) as db:
            db.execute("UPDATE papers SET status='translating' WHERE id=?", (pid,))
        with patch("papertranslate.ingest.ingest", return_value=source, create=True) as obtain, \
             patch("papertranslate.ai.translate_batch", side_effect=translate) as api:
            self.service.start()
            service2.start()
            for _ in range(100):
                if self.service.get(pid)["status"] == "ready":
                    break
                time.sleep(.03)
            self.assertEqual(self.service.get(pid)["status"], "ready")
            self.assertEqual(api.call_count, 1)
            self.assertEqual(obtain.call_count, 1)

    def test_audio_export_cache_cancellation_explanation_and_delete(self):
        source=document([("prose", "One."), ("prose", "Two.")])
        pid=self.create()
        with patch("papertranslate.ingest.ingest", return_value=source, create=True), \
             patch("papertranslate.ai.translate_batch", side_effect=translate):
            self.service._work_one()
        a=self.service.queue_audio(pid, {"language":"zh"})
        self.service.cancel_audio(pid, a["id"])
        self.assertEqual(self.service.get_audio(pid, a["id"])["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.service.audio_file(pid, a["id"])
        self.assertEqual(self.service.queue_audio(pid, {"language":"zh"})["id"], a["id"])
        def synth(text, voice, folder):
            folder.mkdir(exist_ok=True)
            path=folder / ("one.mp3" if "One" in text else "two.mp3")
            path.write_bytes(text.encode())
            return path
        def merge(paths, out):
            out.write_bytes(b" ".join(p.read_bytes() for p in paths))
            return out
        with patch("papertranslate.ai.speech_text", side_effect=lambda s, *args: s), \
             patch("papertranslate.ai.synthesize", side_effect=synth), \
             patch("papertranslate.ai.merge_audio", side_effect=merge):
            self.service._work_one()
        self.assertEqual(self.service.get_audio(pid, a["id"])["progress"], {"done":2, "total":2})
        self.assertIn("Two", self.service.audio_file(pid, a["id"]).read_text())
        self.assertEqual(self.service.queue_audio(pid, {"language":"zh"})["status"], "ready")
        ex=self.service.save_explanation(pid, {"text":"示例", "sentences":[{"id":"a", "zh":"示例", "en":""}]})
        self.assertEqual(self.service.get_explanation(pid, ex["id"])["text"], "示例")
        other=self.create()
        with self.assertRaises(KeyError):
            self.service.get_explanation(other, ex["id"])
        self.assertEqual(self.service.queue_audio(pid, {"explanation_id":ex["id"]})["progress"]["total"], 1)
        folder=self.service.folder(pid)
        self.service.delete(pid)
        self.assertFalse(folder.exists())
        self.assertEqual(len(self.service.list()), 1)
        with self.assertRaises(KeyError):
            self.service.get_audio(pid, a["id"])
        with self.assertRaises(KeyError):
            self.service.folder("../")

    def test_duplicate_import_reuses_only_identical_version_and_options(self):
        original = document([("prose", "One.")])
        changed = document([("prose", "Two.")])
        with patch("papertranslate.ingest.ingest", side_effect=[original, original, changed], create=True), \
             patch("papertranslate.ai.translate_batch", side_effect=translate) as translator:
            for _ in range(3):
                pid = self.create()
                self.service._work_one()
                self.assertEqual(self.service.get(pid)["status"], "ready")
            self.assertEqual(translator.call_count, 2)

    def test_upload_copy_survives_caller_cleanup(self):
        src=Path(self.tmp.name)/"input.pdf"
        src.write_bytes(b"%PDF-bytes")
        pid=self.service.create(uploaded=src)["id"]
        src.unlink()
        self.assertEqual((self.service.folder(pid)/"upload.pdf").read_bytes(), b"%PDF-bytes")

if __name__ == "__main__":
    unittest.main()
