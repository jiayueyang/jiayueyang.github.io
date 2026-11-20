"""HTTP interface for the single-user PaperTranslate workspace."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import tempfile
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from . import ai, exports, ingest
from .service import Service

WEB = Path(__file__).resolve().parents[1] / "web"
MAX_UPLOAD = ingest.MAX_FILE
MAX_JSON = 256 * 1024
COOKIE = "pt_session"
SESSION_SECONDS = 7 * 86400
Language = Literal["en", "zh"]
FormulaMode = Literal["skip", "brief", "full"]
Identifier = Annotated[str, Field(min_length=1, max_length=160)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Output(BaseModel):
    model_config = ConfigDict(extra="allow")


class Progress(Output):
    done: int
    total: int


class Job(Output):
    id: str
    status: str
    progress: Progress
    error: str | None = None


class PaperSummary(Job):
    title: str
    authors: str
    identifier: str
    method: str
    created_at: str


class Sentence(Output):
    id: str
    en: str
    zh: str = ""


class Paragraph(Output):
    id: str
    kind: str
    sentences: list[Sentence]


class Section(Output):
    id: str
    title: str
    paragraphs: list[Paragraph]


class Paper(PaperSummary):
    sections: list[Section]
    equations: dict[str, dict]
    warnings: list[str]


class Explanation(Output):
    id: str
    text: str
    sentences: list[Sentence]
    references: list[str]


class Login(Input):
    token: str = Field(min_length=1, max_length=4096)


class Selector(Input):
    language: Language = "zh"
    formula_mode: FormulaMode = "brief"


class Preview(Selector):
    text: str = Field(min_length=1, max_length=36000)


class Speech(Selector):
    sentence_id: Identifier
    explanation_id: Identifier | None = None
    voice: str = Field(default="alloy", min_length=1, max_length=32)
    override: str | None = Field(default=None, max_length=36000)


class AudioExport(Selector):
    voice: str = Field(default="alloy", min_length=1, max_length=32)
    section_id: Identifier | None = None
    explanation_id: Identifier | None = None


class Turn(Input):
    role: Literal["assistant", "user"]
    content: str = Field(max_length=9000)


class Tutor(Input):
    equation_id: Identifier | None = None
    depth: Literal["concise", "quick", "detailed", "research", "derivation"] = "detailed"
    question: str = Field(default="", max_length=3000)
    history: list[Turn] = Field(default_factory=list, max_length=8)


def _token():
    return os.getenv("PT_ACCESS_TOKEN", "").strip()


def _session(key, expiry):
    message = ("PaperTranslate/session/" + str(expiry)).encode()
    return f"{expiry}." + hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def _equal(left, right):
    return hmac.compare_digest(left.encode(), right.encode())


def _loopback(name):
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _private_codespace_host(request):
    if os.getenv("PT_TRUST_PRIVATE_CODESPACE") != "1" or os.getenv("CODESPACES") != "true":
        return None
    name = os.getenv("CODESPACE_NAME", "")
    domain = os.getenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "")
    if not name or not domain:
        return None
    allowed = re.escape(name) + r"-\d+\." + re.escape(domain)
    hosts = [(request.url.hostname or "").lower()]
    if request.client and _loopback(request.client.host):
        hosts.append(request.headers.get("x-forwarded-host", "").lower())
    return next((h for h in hosts if re.fullmatch(allowed, h)), None)


def _trusted_local(request):
    host = (request.url.hostname or "").lower()
    peer = request.client.host.lower() if request.client else ""
    if host == "testserver" and peer == "testclient":
        return True
    if _private_codespace_host(request):
        return True
    if _loopback(host) and (peer == "testclient" or _loopback(peer)):
        # TestClient's peer is synthetic, but the Host still has to be local.
        forwarded = request.headers.get("x-forwarded-for", "").split(",")
        real_ip = request.headers.get("x-real-ip", "").strip()
        return (not request.headers.get("forwarded") and not request.headers.get("x-forwarded-host")
                and (not real_ip or _loopback(real_ip))
                and all(not ip.strip() or _loopback(ip.strip()) for ip in forwarded))
    return False


def _authorized(request):
    key = _token()
    if not key:
        return _trusted_local(request)
    scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and _equal(bearer, key):
        return True
    cookie = request.cookies.get(COOKIE, "")
    expiry, _, _ = cookie.partition(".")
    now = int(time.time())
    return bool(re.fullmatch(r"\d{1,12}", expiry) and now <= int(expiry) <= now + SESSION_SECONDS + 10
                and _equal(cookie, _session(key, expiry)))


def _same_site(request):
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True  # Non-browser API clients can use bearer authentication.
    try:
        browser = urlsplit(origin)
        return (browser.scheme in ("http", "https") and not browser.username and not browser.password
                and browser.netloc.lower() in (request.url.netloc.lower(), _private_codespace_host(request))
                and (request.url.scheme != "https" or browser.scheme == "https"))
    except ValueError:
        return False


def _error(status, detail):
    return JSONResponse({"detail": detail}, status_code=status, headers={"Cache-Control": "no-store"})


@contextmanager
def _slot(slots):
    if not slots.acquire(blocking=False):
        raise HTTPException(429, "工作台正在处理其他请求，请稍后重试。", headers={"Retry-After": "2"})
    try:
        yield
    finally:
        slots.release()


def _form_bool(form, name, default):
    value = form.get(name)
    if value is None:
        return default
    if value not in ("true", "false", "1", "0", "on", "off"):
        raise HTTPException(422, "翻译选项的格式无效。")
    return value in ("true", "1", "on")


def _import_form(service, form):
    allowed = {"source", "file", "mode", "glossary", "translate_refs", "acknowledgments"}
    fields = list(form.multi_items())
    if len(fields) != len(set(k for k, _ in fields)) or any(k not in allowed for k, _ in fields):
        raise HTTPException(422, "表单包含重复或未知的字段。")
    upload = form.get("file")
    source = form.get("source")
    if any(not isinstance(v, str) for k, v in fields if k != "file"):
        raise HTTPException(422, "文本字段需要使用文字。")
    source = source.strip() if source else None
    if bool(source) == bool(upload):
        raise HTTPException(422, "请选择论文链接或 PDF 文件。")
    glossary = None
    if "glossary" in form:
        try:
            glossary = json.loads(form["glossary"])
        except (ValueError, TypeError):
            raise HTTPException(422, "术语表需要有效的 JSON 词典。") from None
    options = dict(mode=form.get("mode", "accurate"), glossary=glossary,
                   translate_refs=_form_bool(form, "translate_refs", False),
                   acknowledgments=_form_bool(form, "acknowledgments", True))
    if source:
        ingest.identify(source)
        return service.create(source=source, **options)
    if not isinstance(upload, UploadFile) or not upload.filename:
        raise HTTPException(422, "请选择 PDF 文件。")
    if upload.size is not None and upload.size > MAX_UPLOAD:
        raise HTTPException(413, "PDF 超过 32 MiB。")
    with tempfile.TemporaryDirectory(dir=service.root) as tmp:
        path = Path(tmp) / "input.pdf"
        size = 0
        with path.open("wb") as sink:
            while chunk := upload.file.read(256 * 1024):
                if size == 0 and not chunk[:1024].lstrip().startswith(b"%PDF-"):
                    raise HTTPException(422, "文件没有可识别的 PDF 标头。")
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "PDF 超过 32 MiB。")
                sink.write(chunk)
        if not size:
            raise HTTPException(422, "上传文件为空。")
        return service.create(uploaded=path, **options)


def create_app(data_dir=None, start_worker=True):
    """Create an independent application and a durable workspace on first use."""
    service_lock = threading.Lock()

    def store():
        if getattr(app.state, "service", None) is None:
            with service_lock:
                if getattr(app.state, "service", None) is None:
                    app.state.service = Service(data_dir)
        return app.state.service

    @asynccontextmanager
    async def lifespan(_):
        service = store()
        if start_worker:
            service.start()
        try:
            yield
        finally:
            if start_worker:
                service.stop()

    app = FastAPI(title="PaperTranslate API", version="1.0.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    audio_slots = threading.BoundedSemaphore(3)
    tutor_slots = threading.BoundedSemaphore(2)
    export_slots = threading.BoundedSemaphore(1)
    login_guard = threading.Lock()
    attempts = []
    if data_dir is not None:
        store()

    class AssetLease:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            match = re.fullmatch(r"/api/papers/(sample|[a-f0-9]{32})(/.*)?", scope.get("path", ""))
            if scope["type"] != "http" or not match or (scope["method"] == "DELETE" and not match[2]):
                return await self.app(scope, receive, send)
            lease = store().lease(match[1], wait=False)
            try:
                lease.__enter__()
            except BlockingIOError:
                return await _error(409, "论文正在移除，请稍后再试。")(scope, receive, send)
            try:
                await self.app(scope, receive, send)
            finally:
                lease.__exit__(None, None, None)

    app.add_middleware(AssetLease)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        public = request.url.path in ("/api/config", "/api/login")
        is_api = request.url.path.startswith("/api/")
        if is_api and request.method not in ("HEAD", "GET", "OPTIONS") and not _same_site(request):
            return _error(403, "浏览器请求来源与工作台地址不一致。")
        if is_api and not public and not _authorized(request):
            if not _token():
                return _error(403, "远程访问需要在服务器设置 PT_ACCESS_TOKEN。")
            return _error(401, "请输入工作台访问口令。")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            importing = request.url.path == "/api/papers" and request.method == "POST"
            cap = MAX_UPLOAD + MAX_JSON if importing else MAX_JSON
            declared = request.headers.get("content-length")
            if declared and (not declared.isdigit() or len(declared) > 12 or int(declared) > cap):
                return _error(413, "请求超过大小限制。")
            if not importing:  # Validate chunked JSON before FastAPI deserializes it.
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > cap:
                        return _error(413, "请求超过大小限制。")
                    body.extend(chunk)
                # BaseHTTPMiddleware replays the cached body to the downstream router.
                request._body = bytes(body)
        response = await call_next(request)
        if is_api:
            response.headers["Cache-Control"] = "private, no-store"
        if not is_api:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; "
                "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @app.exception_handler(KeyError)
    async def missing(_, __):
        return _error(404, "论文、段落或任务不存在。")

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        message = str(exc)
        if not re.search(r"[\u3400-\u9fff]", message) or re.search(r"(?:^|[ (])/[\w./-]+", message):
            message = "输入或文档格式无效。"
        return _error(422, message[:400])

    @app.exception_handler(ai.AIError)
    async def bad_ai(_, exc):
        return _error(503, str(exc))

    @app.exception_handler(RequestValidationError)
    async def bad_input(_, __):
        return _error(422, "请求的字段或选项无效。")

    @app.exception_handler(Exception)
    async def internal(_, __):
        return _error(500, "服务未能完成请求。请检查配置后重试。")

    @app.get("/api/config")
    def config(request: Request):
        return {"auth_required": bool(_token()) or not _trusted_local(request),
                "authenticated": _authorized(request), "ai_available": bool(os.getenv("OPENAI_API_KEY", "").strip()),
                "max_upload_bytes": MAX_UPLOAD}

    @app.post("/api/login")
    def login(request: Request, payload: Login, response: Response):
        key = _token()
        if not key:
            if _trusted_local(request):
                return {"authenticated": True}
            raise HTTPException(403, "远程访问需要在服务器设置 PT_ACCESS_TOKEN。")
        with login_guard:
            now = time.monotonic()
            attempts[:] = [stamp for stamp in attempts if now - stamp < 60]
            if len(attempts) >= 15:
                raise HTTPException(429, "尝试次数过多，请稍后重试。")
            if not _equal(key, payload.token):
                attempts.append(now)
                raise HTTPException(401, "访问口令不正确。")
            attempts.clear()
        local_http = _loopback(request.url.hostname or "") or (
            request.url.hostname == "testserver" and _trusted_local(request))
        secure = request.url.scheme == "https" or not local_http or bool(_private_codespace_host(request))
        response.set_cookie(COOKIE, _session(key, int(time.time()) + SESSION_SECONDS), max_age=SESSION_SECONDS,
                            secure=secure, httponly=True, samesite="strict", path="/api")
        return {"authenticated": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE, path="/api")
        return {"authenticated": False}

    @app.get("/api/papers", response_model=list[PaperSummary])
    def papers():
        return store().list()

    form_schema = {
        "type": "object", "properties": {
            "source": {"type": "string", "description": "arXiv, DOI or direct public PDF URL"},
            "file": {"type": "string", "format": "binary"},
            "mode": {"type": "string", "enum": ["accurate", "natural", "literal"]},
            "glossary": {"type": "string", "description": "JSON object mapping terms to translations"},
            "translate_refs": {"type": "boolean"}, "acknowledgments": {"type": "boolean"},
        },
    }

    @app.post("/api/papers", status_code=202, response_model=PaperSummary, openapi_extra={
        "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": form_schema}}}
    })
    async def create_paper(request: Request):
        sharing = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if sharing not in ("multipart/form-data", "application/x-www-form-urlencoded"):
            raise HTTPException(415, "请使用表单提交链接或上传 PDF。")
        limit = MAX_UPLOAD + MAX_JSON if sharing == "multipart/form-data" else MAX_JSON
        async def chunks():
            n = 0
            async for chunk in request.stream():
                n += len(chunk)
                if n > limit:
                    raise HTTPException(413, "上传文件或表单超过大小限制。")
                yield chunk
        if sharing == "multipart/form-data":
            try:
                form = await MultiPartParser(request.headers, chunks(), max_files=1, max_fields=6,
                                             max_part_size=MAX_JSON).parse()
            except MultiPartException:
                raise HTTPException(422, "上传表单无法读取，请重新选择 PDF。") from None
        else:
            from starlette.formparsers import FormParser
            form = await FormParser(request.headers, chunks()).parse()
        try:
            return await run_in_threadpool(_import_form, store(), form)
        finally:
            await form.close()

    @app.get("/api/sample", response_model=Paper)
    def sample():
        return store().ensure_sample()

    @app.get("/api/papers/{pid}", response_model=Paper)
    def paper(pid: str):
        return store().get(pid)

    @app.delete("/api/papers/{pid}", status_code=204)
    def delete(pid: str):
        store().delete(pid)
        return Response(status_code=204)

    @app.post("/api/papers/{pid}/cancel", response_model=PaperSummary)
    def cancel(pid: str):
        return store().cancel(pid)

    @app.post("/api/papers/{pid}/resume", response_model=PaperSummary)
    def resume(pid: str):
        return store().resume(pid)

    @app.get("/api/papers/{pid}/assets/{filename}")
    def asset(pid: str, filename: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,175}\.png", filename, re.I):
            raise HTTPException(404, "文献图片不存在。")
        base = store().folder(pid) / "assets"
        target = base / filename
        if base.is_symlink() or target.is_symlink() or not target.is_file():
            raise HTTPException(404, "文献图片不存在。")
        return FileResponse(target, media_type="image/png")

    @app.get("/api/papers/{pid}/export/{kind}")
    def export_paper(pid: str, kind: Literal["original", "chinese", "bilingual", "markdown", "latex"], inline: bool = False):
        doc = store().get(pid)
        if kind != "original" and (doc["status"] != "ready" or doc.get("untranslated_pages")):
            raise HTTPException(409, "完整译文尚未就绪，可以先下载原文。")
        folder = store().folder(pid)
        if kind == "original" and not (folder / "original.pdf").is_file():
            raise HTTPException(409, "原文尚未导入完成。")
        with _slot(export_slots):
            out = exports.export(doc, kind, folder)
        mime = {".pdf":"application/pdf", ".zip":"application/zip", ".md":"text/markdown"}
        return FileResponse(out, media_type=mime[out.suffix], filename=kind + out.suffix,
                            content_disposition_type="inline" if inline and kind == "original" else "attachment")

    @app.post("/api/papers/{pid}/explain", response_model=Explanation)
    def explain(pid: str, payload: Tutor):
        doc = store().get(pid)
        if not doc["sections"]:
            raise HTTPException(409, "请等待论文原文导入完成。")
        if payload.equation_id and payload.equation_id not in doc["equations"]:
            raise HTTPException(404, "公式不存在。")
        with _slot(tutor_slots):
            result = ai.explain(doc, payload.equation_id, payload.depth, payload.question,
                                [turn.model_dump() for turn in payload.history])
        return store().save_explanation(pid, result)

    @app.get("/api/papers/{pid}/explanations/{eid}", response_model=Explanation)
    def explanation(pid: str, eid: str):
        return store().get_explanation(pid, eid)

    @app.post("/api/papers/{pid}/speech-preview")
    def preview(pid: str, payload: Preview):
        return {"text": ai.speech_text(payload.text, store().get(pid), payload.language, payload.formula_mode)}

    @app.post("/api/papers/{pid}/audio")
    def audio(pid: str, payload: Speech):
        doc = store().get(pid)
        if payload.explanation_id:
            sentences = store().get_explanation(pid, payload.explanation_id)["sentences"]
        else:
            sentences = (s for sec in doc["sections"] for p in sec["paragraphs"] for s in p["sentences"])
        sentence = next((s for s in sentences if s["id"] == payload.sentence_id), None)
        if sentence is None:
            raise HTTPException(404, "朗读句子不存在。")
        original = sentence.get(payload.language) or ""
        if not original and not (payload.override and sentence.get("en") and "{{eq:" in sentence["en"]):
            raise HTTPException(409, "所选语言的句子尚未就绪。")
        if payload.override is not None and "{{eq:" not in (sentence.get("en") or "") + (sentence.get("zh") or ""):
            raise HTTPException(422, "自定义读音仅适用于含有公式的句子。")
        spoken = (payload.override if payload.override is not None
                  else ai.speech_text(original, doc, payload.language, payload.formula_mode))
        if not spoken.strip():
            raise HTTPException(422, "此句在所选读法下没有朗读文本。")
        with _slot(audio_slots):
            out = ai.synthesize(spoken, payload.voice, store().folder(pid) / "audio")
        return FileResponse(out, media_type="audio/mpeg")

    @app.post("/api/papers/{pid}/audio-export", status_code=202, response_model=Job)
    def audio_export(pid: str, options: AudioExport):
        return store().queue_audio(pid, options.model_dump())

    @app.get("/api/papers/{pid}/audio-export/{jid}", response_model=Job)
    def audio_status(pid: str, jid: str):
        return store().get_audio(pid, jid)

    @app.delete("/api/papers/{pid}/audio-export/{jid}", response_model=Job)
    def cancel_export(pid: str, jid: str):
        return store().cancel_audio(pid, jid)

    @app.get("/api/papers/{pid}/audio-export/{jid}/file")
    def download_audio(pid: str, jid: str):
        return FileResponse(store().audio_file(pid, jid), media_type="audio/mpeg", filename="narration.mp3")

    @app.get("/api/papers/{pid}/reading-state")
    def reading_state(pid: str):
        return store().get_reading(pid)

    @app.put("/api/papers/{pid}/reading-state")
    def save_reading(pid: str, value: dict):
        return store().save_reading(pid, value)

    @app.api_route("/api/{unknown:path}", include_in_schema=False, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    def unknown_api(unknown: str):
        raise HTTPException(404, "API 路径不存在。")

    app.mount("/", StaticFiles(directory=WEB, html=True), name="workspace")
    return app


app = create_app()
