import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from papertranslate import ai


EQUATION = r"G_{\mu\nu}+\Lambda g_{\mu\nu}=8\pi G T_{\mu\nu}"
DOCUMENT = {
    "title":"Field equations",
    "sections":[{"id":"s1","title":"Gravity","paragraphs":[
        {"id":"p1","sentences":[{"id":"a","en":"We assume a smooth spacetime."}]},
        {"id":"p2","sentences":[{"id":"b","en":"This gives {{eq:e1}}."}]},
    ]}],
    "equations":{"e1":{"latex":EQUATION,"paragraph_id":"p2"}},
}


class TranslationTests(unittest.TestCase):
    def test_validate_math_citation_and_retry(self):
        rows = [{"id":"p1s1","en":r"The entropy obeys $S=A/4G$ in {{eq:e1}}; see \cite{BH} and [1–4]."}]
        def generate(name, schema, instructions, payload):
            masked = payload["sentences"][0]["en"]
            self.assertNotIn("$",masked)
            self.assertNotIn("cite",masked)
            if "validation" not in payload:
                return {"sentences":[{"id":"p1s1","zh":"一个没有公式的句子。"}]}
            markers = ai._MARKER.findall(masked)
            return {"sentences":[{"id":"p1s1","zh":"熵公式 "+" ".join(markers)}]}
        with patch.object(ai,"_generate",side_effect=generate) as call:
            value = ai.translate_batch(rows,{"entropy":"熵"},"accurate","")["p1s1"]
        self.assertEqual(call.call_count,2)
        for part in ["$S=A/4G$","{{eq:e1}}",r"\cite{BH}","[1–4]"]:
            self.assertIn(part,value)

    def test_missing_sentence_or_duplicate_rejected(self):
        for data in [[], [{"id":"s","zh":"文本"},{"id":"s","zh":"文本"}]]:
            with self.subTest(data=data), patch.object(ai,"_generate",return_value={"sentences":data}):
                with self.assertRaises(ai.AIError):
                    ai.translate_batch([{"id":"s","en":"A sentence."}],{}, "accurate","")

    def test_math_only_no_api(self):
        rows=[{"id":"a","en":r"\[x=2\]"},{"id":"b","en":"{{eq:e0}} [3, 4]."}]
        with patch.object(ai,"_generate") as mock:
            self.assertEqual(ai.translate_batch(rows,{}),{r["id"]:r["en"] for r in rows})
            mock.assert_not_called()

    def test_no_key_message(self):
        with patch.dict(os.environ,{"OPENAI_API_KEY":""}):
            with self.assertRaisesRegex(ai.AIError,"OPENAI_API_KEY"):
                ai.translate_batch([{"id":"a","en":"One sentence."}],{})

    def test_response_rest_shape(self):
        body={"status":"completed","output":[{"type":"message","content":[
            {"type":"output_text","text":json.dumps({"ok":True})}]}]}
        with patch.object(ai,"_request",return_value=json.dumps(body).encode()) as post:
            self.assertTrue(ai._generate("valid", ai._object({"ok":{"type":"boolean"}}),"Task",{})["ok"])
            endpoint, payload = post.call_args.args
            self.assertEqual(endpoint,"responses")
            self.assertEqual(payload["text"]["format"]["type"],"json_schema")
            self.assertIs(payload["store"],False)


class ExplanationTests(unittest.TestCase):
    @staticmethod
    def answer(ref):
        return {"sections":[{"heading":"引力","sentences":[
            {"text":"光滑性是文中假设。","kind":"source","references":[ref]}]}]}

    def test_grounded_equation(self):
        with patch.object(ai,"_generate",return_value=self.answer("p1")) as mock:
            found=ai.explain(DOCUMENT,"e1","derivation","假设是什么？",[])
            payload=mock.call_args.args[-1]
            self.assertEqual(payload["equation"],EQUATION)
            self.assertIn(EQUATION,payload["source_excerpts"][1]["text"])
            self.assertEqual(found["references"],["p1"])
            self.assertEqual(found["sentences"][0]["kind"],"source")
            self.assertEqual(found["sentences"][0]["paragraph_id"],"p1")
            self.assertIn("原文",found["text"])

    def test_unavailable_reference_and_image_only_equation(self):
        with patch.object(ai,"_generate",return_value=self.answer("invented")):
            with self.assertRaises(ai.AIError):
                ai.explain(DOCUMENT,None,"research","",[])
        doc=dict(DOCUMENT,equations={"a":{"latex":"","image":"eq.png"}})
        with self.assertRaisesRegex(ai.AIError,"原图"):
            ai.explain(doc,"a","derivation","",[])


class NarrationTests(unittest.TestCase):
    def test_tensor_indices_and_modes(self):
        full=ai.speech_text("得到 {{eq:e1}}。",DOCUMENT,"zh","full")
        self.assertIn("下标 缪 纽",full)
        self.assertIn("大写拉姆达",full)
        self.assertEqual(ai.speech_text("Next {{eq:e1}}",DOCUMENT,"en","skip"),"Next")
        self.assertLess(len(ai.equation_preview(DOCUMENT,"e1","zh","brief")),len(full))
        fraction=ai.speech_text(r"The value is \(\frac{x^2}{y_1}\).",DOCUMENT,"en","full")
        for token in ("numerator x squared","denominator y subscript 1","end fraction"):
            self.assertIn(token,fraction)

    def test_sentence_cache_and_atomic_export(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("FFmpeg required")
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            # A real short audio fixture exercises MP3 boundary handling.
            fixture=root/"fixture.mp3"
            subprocess.run(["ffmpeg","-nostdin","-loglevel","error","-f","lavfi","-i",
                "sine=frequency=440:duration=0.2","-ar","24000","-ac","1",str(fixture)],check=True)
            with patch.object(ai,"_request",return_value=fixture.read_bytes()) as post:
                one=ai.synthesize("引力。","alloy",root)
                self.assertEqual(one,ai.synthesize("引力。","alloy",root))
                two=ai.synthesize("温度。","alloy",root)
                self.assertEqual(post.call_count,2)
                self.assertEqual(post.call_args.args[0],"audio/speech")
            merged=ai.merge_audio([one,two],root/"合并 音频.mp3")
            info=subprocess.check_output(["ffprobe","-v","error","-of","json","-show_format",str(merged)])
            self.assertGreater(float(json.loads(info)["format"]["duration"]),0.35)
            with self.assertRaises(ai.AIError):
                ai.merge_audio([one, root/"absent.mp3"],root/"incomplete.mp3")
            self.assertFalse((root/"incomplete.mp3").exists())

    def test_bad_audio_does_not_enter_cache(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ai,"_request",return_value=b"bad"):
            with self.assertRaises(ai.AIError):
                ai.synthesize("内容","alloy",tmp)
            self.assertFalse(list(Path(tmp).glob("*.mp3")))


class AlgebraTests(unittest.TestCase):
    def test_conservative_scalar_identity_verification(self):
        self.assertTrue(ai.verified_scalar_identity(r"直接展开 \((x+y)^2=x^2+2*x*y+y^2\)。"))
        for formula in [r"x=x+1",r"x/x=1",r"A_{ab}=A_{ba}",r"f(x)=f(x)"]:
            self.assertFalse(ai.verified_scalar_identity(r"\["+formula+r"\]"))

    def test_digits_and_eqref_preserved(self):
        masked, values = ai._mask("With 1.25 in Eq. (3.4), set 1.5e-5 as default.")
        self.assertEqual(values,["1.25","(3.4)","1.5e-5"])
        self.assertNotIn("3.4",masked)


class RealtimeNarrationTests(unittest.TestCase):
    def events(self, transcript):
        import base64
        from unittest.mock import MagicMock
        ws=MagicMock()
        ws.__enter__.return_value=ws
        ws.recv.side_effect=[json.dumps(event) for event in [
            {"type":"session.created"},
            {"type":"response.output_audio.delta","delta":base64.b64encode(bytes(12000)).decode()},
            {"type":"response.output_audio_transcript.done","transcript":transcript},
            {"type":"response.done","response":{"status":"completed"}},
        ]]
        return ws

    @unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg required")
    def test_realtime_model_routes_to_websocket_and_validates_transcript(self):
        import websockets.sync.client
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{"OPENAI_API_KEY":"offline",
                "OPENAI_TTS_MODEL":"gpt-realtime-2.1-mini"}), patch.object(ai,"_request") as http:
            ws=self.events("黑洞 熵！")
            with patch.object(websockets.sync.client,"connect",return_value=ws) as conn:
                path=ai.synthesize("黑洞熵。","alloy",Path(tmp))
                self.assertIn("wss://api.openai.com/v1/realtime?",conn.call_args.args[0])
                self.assertEqual(json.loads(ws.send.call_args_list[0].args[0])["item"]["content"][0]["text"],"黑洞熵。")
                self.assertEqual(json.loads(ws.send.call_args_list[1].args[0])["response"]["audio"]["output"]["voice"],"alloy")
                self.assertGreater(path.stat().st_size,64)
            with patch.object(websockets.sync.client,"connect",return_value=self.events("温度降低")):
                with self.assertRaisesRegex(ai.AIError,"转写"):
                    ai.synthesize("温度升高","alloy",Path(tmp))
            http.assert_not_called()
            self.assertEqual(len(list(Path(tmp).glob("*.mp3"))),1)
