"""Access controls and service failure paths at the HTTP boundary."""
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from papertranslate.api import create_app


class ApiBoundaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = create_app(data_dir=folder.name,start_worker=False)

    def test_local_single_user_and_remote_access_default(self):
        with patch.dict(os.environ,{"PT_ACCESS_TOKEN":"","PT_TRUST_PRIVATE_CODESPACE":""}):
            for base in ("http://localhost","http://testserver"):
                with self.subTest(base=base), TestClient(self.app,base_url=base) as local:
                    self.assertEqual(local.get("/api/papers").status_code,200)
                    self.assertTrue(local.get("/api/config").json()["authenticated"])
            with TestClient(self.app,base_url="https://reader.example") as remote:
                self.assertFalse(remote.get("/api/config").json()["authenticated"])
                self.assertEqual(remote.get("/api/papers").status_code,403)
                self.assertEqual(remote.post("/api/login",json={"token":"fixture"}).status_code,403)

    def test_token_login_logout_and_cross_site_mutation(self):
        with patch.dict(os.environ,{"PT_ACCESS_TOKEN":"local-test-access-token"}), \
             TestClient(self.app,base_url="https://reader.example") as client:
            self.assertEqual(client.get("/api/papers").status_code,401)
            self.assertEqual(client.post("/api/login",json={"token":"incorrect"}).status_code,401)
            login=client.post("/api/login",json={"token":"local-test-access-token"})
            self.assertEqual(login.status_code,200)
            cookie=login.headers["set-cookie"].lower()
            for expected in ("httponly","samesite=strict","secure"):
                self.assertIn(expected,cookie)
            self.assertNotIn("local-test-access-token",cookie)
            self.assertEqual(client.get("/api/papers").status_code,200)
            self.assertEqual(client.post("/api/logout",headers={"Origin":"https://other.example"}).status_code,403)
            self.assertEqual(client.post("/api/logout",headers={"Sec-Fetch-Site":"cross-site"}).status_code,403)
            self.assertTrue(client.get("/api/config").json()["authenticated"])
            self.assertEqual(client.post("/api/logout",headers={"Origin":"https://reader.example"}).status_code,200)
            self.assertEqual(client.get("/api/papers").status_code,401)
            self.assertEqual(client.get("/api/papers",headers={"Authorization":"Bearer local-test-access-token"}).status_code,200)

    def test_unconfigured_ai_and_bad_identifiers(self):
        with patch.dict(os.environ,{"PT_ACCESS_TOKEN":"","OPENAI_API_KEY":""}), \
             patch("urllib.request.urlopen",side_effect=AssertionError("network used without a key")), \
             TestClient(self.app) as client:
            self.assertFalse(client.get("/api/config").json()["ai_available"])
            doc=client.get("/api/sample").json()
            prefix="/api/papers/"+doc["id"]
            eqid=next(iter(doc["equations"]))
            sid=next(s["id"] for sec in doc["sections"] for p in sec["paragraphs"] for s in p["sentences"] if s.get("zh"))
            for endpoint, value in [("explain",{"equation_id":eqid}),("audio",{"sentence_id":sid})]:
                result=client.post(prefix+"/"+endpoint,json=value)
                self.assertEqual(result.status_code,503,result.text)
                self.assertIn("OPENAI_API_KEY",result.json()["detail"])
            self.assertEqual(client.post(prefix+"/explain",json={"equation_id":"missing"}).status_code,404)
            self.assertEqual(client.post(prefix+"/audio",json={"sentence_id":"missing"}).status_code,404)
            self.assertEqual(client.get(prefix+"/assets/missing.png").status_code,404)
            self.assertEqual(client.post("/api/papers",files={"file":("paper.pdf",b"text","application/pdf")}).status_code,422)
