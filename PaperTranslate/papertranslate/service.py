"""Durable single-user library and jobs. SQLite stores state; paper assets stay on disk."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import ai

LOG = logging.getLogger(__name__)
DEFAULT_GLOSSARY = {
    "entanglement entropy": "纠缠熵", "Hawking radiation": "霍金辐射",
    "event horizon": "事件视界", "cosmological horizon": "宇宙学视界",
    "quantum field theory": "量子场论", "general relativity": "广义相对论",
    "renormalization group": "重整化群", "critical exponent": "临界指数",
}
_MIGRATION = """
CREATE TABLE IF NOT EXISTS papers(
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 source TEXT, uploaded INTEGER NOT NULL DEFAULT 0, options TEXT NOT NULL,
 status TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0, error TEXT,
 title TEXT NOT NULL DEFAULT '', authors TEXT NOT NULL DEFAULT '',
 identifier TEXT NOT NULL DEFAULT '', method TEXT NOT NULL DEFAULT '',
 total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0, document TEXT
);
CREATE TABLE IF NOT EXISTS translations(
 paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 sentence_id TEXT NOT NULL, cache_key TEXT NOT NULL, zh TEXT NOT NULL,
 PRIMARY KEY(paper_id,sentence_id)
);
CREATE INDEX IF NOT EXISTS translations_cache ON translations(cache_key);
CREATE TABLE IF NOT EXISTS explanations(
 id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audio_jobs(
 id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 options TEXT NOT NULL, status TEXT NOT NULL, error TEXT, generation INTEGER NOT NULL DEFAULT 0,
 done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(paper_id, options)
);
CREATE INDEX IF NOT EXISTS papers_queue ON papers(status,updated_at);
CREATE INDEX IF NOT EXISTS audio_queue ON audio_jobs(status,updated_at);
PRAGMA user_version = 1;
"""

def _json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")

def _walk(doc):
    for section in doc.get("sections", []):
        for paragraph in section["paragraphs"]:
            for sentence in paragraph["sentences"]:
                yield section, paragraph, sentence

def _document_key(doc, options):
    """Identify unchanged scientific content before looking up a prior translation."""
    digest = hashlib.sha256()
    digest.update(_json([doc["title"], doc.get("authors"), doc.get("identifier"),
                         doc.get("method"), doc.get("equations"), options,
                         os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"]).encode())
    for sec, paragraph, sentence in _walk(doc):
        digest.update(_json([sec["id"], sec["title"], paragraph["id"], paragraph.get("kind"),
                             sentence["id"], sentence["en"]]).encode())
    return digest.hexdigest()

def _status(row):
    return {**{k: row[k] for k in ("id", "status", "error", "created_at", "updated_at")},
            "progress": {"done": row["done"], "total": row["total"]}}

def _summary(row):
    return {**_status(row), **{k: row[k] for k in ("title", "authors", "identifier", "method", "audio_status")}}

def _row(conn, sql, args):
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise KeyError("记录不存在")
    return row

_FIELDS = """
 p.id, p.status, p.created_at, p.updated_at, p.error, p.title, p.authors, p.identifier,
 p.method, p.total, p.done,
 (SELECT a.status FROM audio_jobs a WHERE a.paper_id=p.id ORDER BY (a.status='ready') DESC,a.updated_at DESC LIMIT 1) audio_status
"""

class _Interrupted(Exception):
    pass

class Service:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv("PT_DATA_DIR") or "data").resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.docs = self.root / "papers"
        self.docs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / "library.sqlite3"
        self._quit = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._sample_lock = threading.Lock()
        with self._conn() as con:
            con.execute("PRAGMA journal_mode=WAL")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("文献库格式比当前服务的版本更新。")
            if version == 0:
                con.executescript("BEGIN IMMEDIATE;\n" + _MIGRATION + "\nCOMMIT;")
            if version < 2:
                con.executescript("""BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS reading_states(
                        paper_id TEXT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE, state TEXT NOT NULL);
                    PRAGMA user_version=2;
                    COMMIT;""")

    @contextmanager
    def _conn(self, write=False):
        con = sqlite3.connect(self.db, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys=ON")
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if con.in_transaction:
                con.commit()
        finally:
            con.close()  # rolls back any interrupted transaction

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._quit.clear()
        self._thread = threading.Thread(target=self._run, name="papertranslate-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._quit.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _path(self, pid):
        if pid != "sample" and not re.fullmatch(r"[a-f0-9]{32}", pid):
            raise KeyError("论文不存在")
        return self.docs / pid

    def folder(self, pid):
        path = self._path(pid)
        with self._conn() as con:
            _row(con, "SELECT id FROM papers WHERE id=? AND status!='deleting'", (pid,))
        return path

    def list(self):
        with self._conn() as con:
            return [_summary(r) for r in con.execute(f"SELECT {_FIELDS} FROM papers p WHERE status!='deleting' ORDER BY created_at DESC")]

    def _summary(self, pid):
        with self._conn() as con:
            return _summary(_row(con, f"SELECT {_FIELDS} FROM papers p WHERE p.id=? AND status!='deleting'", (pid,)))

    def create(self, source=None, uploaded=None, mode="accurate", glossary=None, translate_refs=False, acknowledgments=True):
        if bool(source) == bool(uploaded):
            raise ValueError("请选择一个论文链接或一个 PDF。")
        if mode not in {"accurate", "natural", "literal"}:
            raise ValueError("未知翻译模式。")
        terms = DEFAULT_GLOSSARY if glossary is None else glossary
        if not isinstance(terms, dict) or len(terms) > 500 or not all(
            isinstance(k, str) and isinstance(v, str) and 0 < len(k) <= 200 and 0 < len(v) <= 200 for k, v in terms.items()
        ):
            raise ValueError("术语表需要有效的词语和译名。")
        if source is not None and (not isinstance(source, str) or len(source) > 4096):
            raise ValueError("链接过长或无效。")
        options = _json({"mode":mode, "glossary":terms, "translate_refs":bool(translate_refs),
                         "acknowledgments":bool(acknowledgments)})
        pid = uuid.uuid4().hex
        dest = self._path(pid)
        dest.mkdir(mode=0o700)
        try:
            if uploaded is not None:
                shutil.copyfile(uploaded, dest / "upload.pdf")
            now = _now()
            with self._conn(True) as con:
                con.execute("INSERT INTO papers(id,created_at,updated_at,options,status,source,uploaded,title) VALUES(?,?,?,?,?,?,?,?)",
                            (pid, now, now, options, "queued", source, int(uploaded is not None), "PDF" if uploaded else str(source)))
            self._wake.set()
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return self._summary(pid)

    def get(self, pid):
        with self._conn() as con:
            con.execute("BEGIN")
            row = _row(con, f"SELECT {_FIELDS},p.document,p.options FROM papers p WHERE p.id=? AND p.status!='deleting'", (pid,))
            doc = json.loads(row["document"]) if row["document"] else {"sections":[], "equations":{}, "warnings":[]}
            doc.update(_summary(row))
            doc["options"] = json.loads(row["options"])
            translations = dict(con.execute("SELECT sentence_id,zh FROM translations WHERE paper_id=?", (pid,)))
        for _, _, s in _walk(doc):
            if s["id"] in translations:
                s["zh"] = translations[s["id"]]
        return doc

    def cancel(self, pid):
        with self._conn(True) as con:
            old = _row(con, "SELECT status FROM papers WHERE id=? AND status!='deleting'", (pid,))["status"]
            if old in {"queued", "processing", "translating"}:
                con.execute("UPDATE papers SET status='cancelled',generation=generation+1,error=NULL,updated_at=? WHERE id=?", (_now(), pid))
        return self._summary(pid)

    def resume(self, pid):
        with self._conn(True) as con:
            old = _row(con, "SELECT status FROM papers WHERE id=? AND status!='deleting'", (pid,))["status"]
            if old in {"failed", "cancelled"}:
                con.execute("UPDATE papers SET status='queued',generation=generation+1,error=NULL,updated_at=? WHERE id=?", (_now(), pid))
        self._wake.set()
        return self._summary(pid)

    def delete(self, pid):
        self._path(pid)
        with self._conn(True) as con:
            _row(con, "SELECT id FROM papers WHERE id=? AND status!='deleting'", (pid,))
            con.execute("UPDATE papers SET status='deleting',generation=generation+1,updated_at=? WHERE id=?", (_now(), pid))
            con.execute("UPDATE audio_jobs SET status='cancelled',generation=generation+1 WHERE paper_id=?", (pid,))
        self._wake.set()
        self._cleanup()

    @contextmanager
    def lease(self, pid, exclusive=False, wait=True):
        """Keep assets alive during response streaming, synthesis, or background work."""
        self._path(pid)
        stripe = hashlib.sha256(pid.encode()).hexdigest()[:2]
        with (self.root / ("paper-lock-" + stripe)).open("a") as lock:
            kind = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(lock, kind if wait else kind | fcntl.LOCK_NB)
            yield

    def _cleanup(self):
        with self._conn() as con:
            ids = [r[0] for r in con.execute("SELECT id FROM papers WHERE status='deleting'")]
        for pid in ids:
            try:
                with self.lease(pid, exclusive=True, wait=False), self._conn(True) as con:
                    deleting = con.execute("SELECT id FROM papers WHERE id=? AND status='deleting'", (pid,)).fetchone()
                    if deleting:
                        path = self._path(pid)
                        if path.exists():
                            shutil.rmtree(path)
                        con.execute("DELETE FROM papers WHERE id=?", (pid,))
            except BlockingIOError:
                # A running API response or job will release the shared lease.
                continue

    def ensure_sample(self):
        with self._sample_lock:
            bundle = Path(__file__).parent/"sample"
            with self._conn(True) as con:
                current = con.execute("SELECT status FROM papers WHERE id='sample'").fetchone()
                if current and current[0] == "deleting":
                    raise ValueError("示例正在移除，请稍后再打开。")
                if not current:
                    doc = json.loads((bundle/"document.json").read_text(encoding="utf-8"))
                    dest = self._path("sample")
                    shutil.copytree(bundle, dest, dirs_exist_ok=True)
                    n = sum(1 for _ in _walk(doc))
                    now = _now()
                    con.execute("""INSERT INTO papers(id,created_at,updated_at,options,status,title,authors,identifier,method,total,done,document)
                                   VALUES('sample',?,?,?,'ready',?,?,?,?,?,?,?)""",
                                (now, now, _json({}), doc["title"], doc.get("authors", ""), doc.get("identifier", ""),
                                 doc.get("method", "latex"), n, n, _json(doc)))
        return self.get("sample")

    def get_reading(self, pid):
        with self._conn() as con:
            row = _row(con, """SELECT r.state FROM papers p LEFT JOIN reading_states r ON p.id=r.paper_id
                                WHERE p.id=? AND p.status!='deleting'""", (pid,))
        return json.loads(row[0]) if row[0] else {}

    def save_reading(self, pid, state):
        value = _json(state)
        if len(value.encode()) > 128 * 1024:
            raise ValueError("阅读记录超出大小限制。")
        with self._conn(True) as con:
            _row(con, "SELECT id FROM papers WHERE id=? AND status!='deleting'", (pid,))
            con.execute("""INSERT INTO reading_states(paper_id,state) VALUES(?,?)
                           ON CONFLICT(paper_id) DO UPDATE SET state=excluded.state""", (pid, value))
        return state

    def save_explanation(self, pid, result):
        eid = uuid.uuid4().hex
        saved = {**result, "id":eid}
        with self._conn(True) as con:
            _row(con, "SELECT id FROM papers WHERE id=? AND status!='deleting'", (pid,))
            con.execute("INSERT INTO explanations VALUES(?,?,?,?)", (eid, pid, _json(saved), _now()))
        return saved

    def get_explanation(self, pid, eid):
        with self._conn() as con:
            row = _row(con, """SELECT e.content FROM explanations e JOIN papers p ON p.id=e.paper_id
                             WHERE e.id=? AND e.paper_id=? AND p.status!='deleting'""", (eid, pid))
        return json.loads(row[0])

    def queue_audio(self, pid, options):
        lang = options.get("language") or "zh"
        mode = options.get("formula_mode") or "brief"
        voice = options.get("voice") or "alloy"
        if lang not in {"en", "zh"} or mode not in {"brief", "full", "skip"} or not re.fullmatch(r"[a-z0-9_-]{1,40}", voice):
            raise ValueError("未知的音频语言、公式模式或音色。")
        opts = {"language":lang, "formula_mode":mode, "voice":voice,
                "section_id":options.get("section_id") or None, "explanation_id":options.get("explanation_id") or None}
        doc = self.get(pid)
        if opts["section_id"] and opts["explanation_id"]:
            raise ValueError("请选择论文章节或讲解。")
        units = self._audio_units(doc, opts)
        now = _now()
        with self._conn(True) as con:
            _row(con, "SELECT id FROM papers WHERE id=? AND status!='deleting'", (pid,))
            key = _json(opts)
            row = con.execute("SELECT id,status FROM audio_jobs WHERE paper_id=? AND options=?", (pid, key)).fetchone()
            if row:
                jid = row["id"]
                if row["status"] in {"cancelled", "failed"} or (row["status"] == "ready" and not self._audio_path(pid, jid).is_file()):
                    con.execute("UPDATE audio_jobs SET status='queued',error=NULL,generation=generation+1,updated_at=? WHERE id=?", (now, jid))
            else:
                jid = uuid.uuid4().hex
                con.execute("INSERT INTO audio_jobs(id,paper_id,options,status,total,created_at,updated_at) VALUES(?,?,?,'queued',?,?,?)",
                            (jid, pid, key, len(units), now, now))
        self._wake.set()
        return self.get_audio(pid, jid)

    def get_audio(self, pid, jid):
        with self._conn() as con:
            row = _row(con, """SELECT a.* FROM audio_jobs a JOIN papers p ON p.id=a.paper_id
                WHERE a.id=? AND a.paper_id=? AND p.status!='deleting'""", (jid, pid))
            return {**_status(row), "paper_id":pid, "options":json.loads(row["options"])}

    def cancel_audio(self, pid, jid):
        with self._conn(True) as con:
            con.execute("""UPDATE audio_jobs SET status='cancelled',generation=generation+1,error=NULL,updated_at=?
                           WHERE id=? AND paper_id=? AND status IN ('processing','queued')""", (_now(), jid, pid))
        return self.get_audio(pid, jid)

    def _audio_path(self, pid, jid):
        if not re.fullmatch(r"[a-f0-9]{32}", jid):
            raise KeyError("音频任务不存在")
        return self._path(pid)/"audio-exports"/(jid+".mp3")

    def audio_file(self, pid, jid):
        if self.get_audio(pid, jid)["status"] != "ready":
            raise ValueError("音频尚未完成。")
        path = self._audio_path(pid, jid)
        if not path.is_file():
            raise ValueError("音频文件缺失，请重新导出。")
        return path

    def _audio_units(self, doc, opts):
        lang = opts["language"]
        if opts["explanation_id"]:
            ex = self.get_explanation(doc["id"], opts["explanation_id"])
            units = ex["sentences"]
        else:
            if not doc["sections"] or (lang == "zh" and doc["status"] != "ready"):
                raise ValueError("请先完成论文处理，再导出完整音频。")
            if opts["section_id"] and not any(s["id"] == opts["section_id"] for s in doc["sections"]):
                raise KeyError("章节不存在")
            units = [s for sec, _, s in _walk(doc) if not opts["section_id"] or sec["id"] == opts["section_id"]]
        if not units or any(s.get("en") and not s.get(lang) for s in units):
            raise ValueError("所选内容尚未提供完整的朗读文本。")
        return units

    def _run(self):
        while not self._quit.is_set():
            try:
                with (self.root/"worker.lock").open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        self._quit.wait(1)
                        continue
                    with self._conn(True) as con:
                        con.execute("UPDATE papers SET status='queued' WHERE status IN ('processing','translating')")
                        con.execute("UPDATE audio_jobs SET status='queued' WHERE status='processing'")
                    while not self._quit.is_set():
                        self._wake.clear()
                        self._cleanup()
                        if not self._work_one():
                            self._wake.wait(1)
            except Exception:
                # Logs deliberately exclude papers, source URLs and upstream responses.
                LOG.error("Library worker interrupted; retrying.")
                self._quit.wait(1)

    def _work_one(self):
        with self._conn(True) as con:
            job = con.execute("""SELECT id, 'papers' AS kind,generation, updated_at,id AS paper_id FROM papers WHERE status='queued'
                UNION ALL SELECT a.id,'audio_jobs',a.generation,a.updated_at,a.paper_id FROM audio_jobs a
                JOIN papers p ON p.id=a.paper_id WHERE a.status='queued' AND p.status!='deleting' ORDER BY updated_at LIMIT 1""").fetchone()
            if not job:
                return False
            table, rid, generation = job["kind"], job["id"], job["generation"]
            con.execute(f"UPDATE {table} SET status='processing',updated_at=? WHERE id=?", (_now(), rid))
        try:
            with self.lease(job["paper_id"]):
                with self._conn() as con:
                    self._check(con, table, rid, generation)
                if table == "papers":
                    self._translate(rid, generation)
                else:
                    self._render_audio(rid, generation)
        except _Interrupted:
            pass
        except Exception as exc:
            # Keep upstream validation messages; never serialize traces or raw response bodies.
            safe = (str(exc)[:500] if isinstance(exc, (ValueError, RuntimeError, KeyError))
                    else f"处理失败（{type(exc).__name__}），可稍后继续。")
            with self._conn(True) as con:
                con.execute(f"""UPDATE {table} SET status='failed',error=?,updated_at=?
                    WHERE id=? AND generation=? AND status IN ('processing','translating')""", (safe, _now(), rid, generation))
        return True

    def _check(self, con, table, rid, generation):
        if self._quit.is_set() or not con.execute(
            f"SELECT 1 FROM {table} WHERE id=? AND generation=? AND status IN ('processing','translating')", (rid, generation)
        ).fetchone():
            raise _Interrupted()

    def _translate(self, pid, generation):
        with self._conn() as con:
            current = _row(con, "SELECT source,uploaded,options,document FROM papers WHERE id=?", (pid,))
        opts = json.loads(current["options"])
        path = self._path(pid)
        if current["document"]:
            doc = self.get(pid)
        else:
            from .ingest import ingest
            def check_cancel():
                with self._conn() as con:
                    self._check(con, "papers", pid, generation)
            doc = ingest(current["source"], path / "upload.pdf" if current["uploaded"] else None,
                         path, cancel_check=check_cancel)
            items = list(_walk(doc))
            if len({s["id"] for _, _, s in items}) != len(items):
                raise ValueError("提取的文档没有可用的独立句子，无法确认翻译完整性。")
            with self._conn(True) as con:
                self._check(con, "papers", pid, generation)
                con.execute("""UPDATE papers SET title=?,authors=?,identifier=?,method=?,document=?,total=?,updated_at=? WHERE id=?""",
                    (doc["title"], doc.get("authors", ""), doc.get("identifier", ""), doc["method"], _json(doc), len(items), _now(), pid))
        with self._conn(True) as con:
            self._check(con, "papers", pid, generation)
            con.execute("UPDATE papers SET status='translating' WHERE id=?", (pid,))
            finished = {r[0] for r in con.execute("SELECT sentence_id FROM translations WHERE paper_id=?", (pid,))}
        document_key = _document_key(doc, opts)
        for section in doc["sections"]:
            paragraphs = section["paragraphs"]
            context = doc["title"] + "\n" + section["title"]
            excerpt = " ".join(sentence["en"] for p in paragraphs[:3] for sentence in p["sentences"])[:1000]
            batch = []
            budget = 0
            for p in paragraphs:
                for s in p["sentences"]:
                    if s["id"] in finished:
                        continue
                    if batch and (budget + len(s["en"]) > 5500 or len(batch) >= 12):
                        self._translate_batch(pid, generation, batch, opts, context, excerpt, document_key)
                        batch, budget = [], 0
                    batch.append((p, s))
                    budget += len(s["en"])
            if batch:
                self._translate_batch(pid, generation, batch, opts, context, excerpt, document_key)
        with self._conn(True) as con:
            self._check(con, "papers", pid, generation)
            row = _row(con, "SELECT done,total FROM papers WHERE id=?", (pid,))
            if doc.get("untranslated_pages"):
                numbers = ", ".join(str(n) for n in doc["untranslated_pages"][:12])
                raise ValueError(f"第 {numbers} 页只有原始图像。请上传带文字的 PDF 或使用 arXiv 源文件。已完成的译文保留在阅读器中。")
            if not row["total"]:
                raise ValueError("没有提取到可供翻译的文字，请上传带文字的 PDF 或使用 arXiv 源文件。")
            if row["done"] != row["total"]:
                raise ValueError("完整性检查未通过，尚有句子需要翻译。")
            con.execute("UPDATE papers SET status='ready',error=NULL,updated_at=? WHERE id=?", (_now(), pid))

    def _translate_batch(self, pid, generation, batch, opts, context, excerpt, document_key):
        resolved, keys, needed = {}, {}, []
        # The cache is scoped to identical documents and options in this single-user library.
        with self._conn() as con:
            self._check(con, "papers", pid, generation)
            for paragraph, s in batch:
                sid, text = s["id"], s["en"]
                kind = paragraph.get("kind", "")
                protected = not re.search(r"[\w\u4e00-\u9fff]", re.sub(r"\{\{(?:eq|ref|cite):[^{}]+\}\}", "", text))
                keep = (protected or kind in {"author", "authors", "metadata", "verbatim", "code", "raw", "image", "protected"}
                    or kind in {"reference", "references", "bibliography"}
                    or kind == "reference_title" and not opts["translate_refs"]
                    or (kind in {"acknowledgment", "acknowledgments", "acknowledgement", "acknowledgements"}
                        or re.search(r"acknowledg[e]?ments?\s*$", context, re.I)) and not opts["acknowledgments"])
                key = hashlib.sha256(_json([document_key, sid, context, kind, text]).encode()).hexdigest()
                keys[sid] = key
                cached = con.execute("""SELECT t.zh FROM translations t JOIN papers p ON p.id=t.paper_id
                    WHERE t.cache_key=? AND p.status!='deleting' LIMIT 1""", (key,)).fetchone()
                if keep or s.get("zh") or cached:
                    resolved[sid] = s.get("zh") or (text if keep else cached[0])
                else:
                    needed.append(s)
        if needed:
            result = ai.translate_batch(needed, opts["glossary"], opts["mode"], context + "\n" + excerpt)
            if set(result) != {s["id"] for s in needed} or any(not isinstance(v, str) or not v.strip() for v in result.values()):
                raise ValueError("翻译结果缺少句子。完成的批次已保存。")
            resolved.update(result)
        with self._conn(True) as con:
            self._check(con, "papers", pid, generation)
            con.executemany("INSERT INTO translations VALUES (?,?,?,?)",
                            [(pid, sid, keys[sid], value) for sid, value in resolved.items()])
            con.execute("UPDATE papers SET done=done+?,updated_at=? WHERE id=?", (len(resolved), _now(), pid))

    def _render_audio(self, jid, generation):
        with self._conn() as con:
            job = _row(con, "SELECT paper_id,options FROM audio_jobs WHERE id=?", (jid,))
        pid = job["paper_id"]
        opts = json.loads(job["options"])
        doc = self.get(pid)
        units = self._audio_units(doc, opts)
        parts = []
        for index, sentence in enumerate(units, 1):
            with self._conn() as con:
                self._check(con, "audio_jobs", jid, generation)
            spoken = ai.speech_text(sentence[opts["language"]], doc, opts["language"], opts["formula_mode"])
            if spoken.strip():
                parts.append(ai.synthesize(spoken, opts["voice"], self._path(pid) / "audio"))
            with self._conn(True) as con:
                self._check(con, "audio_jobs", jid, generation)
                con.execute("UPDATE audio_jobs SET done=?,updated_at=? WHERE id=?", (index, _now(), jid))
        if not parts:
            raise ValueError("所选范围没有可朗读的内容。")
        output = self._audio_path(pid, jid)
        output.parent.mkdir(exist_ok=True)
        ai.merge_audio(parts, output)
        if not output.is_file() or not output.stat().st_size:
            raise ValueError("合成音频为空。")
        with self._conn(True) as con:
            self._check(con, "audio_jobs", jid, generation)
            con.execute("UPDATE audio_jobs SET status='ready',error=NULL,updated_at=? WHERE id=?", (_now(), jid))
