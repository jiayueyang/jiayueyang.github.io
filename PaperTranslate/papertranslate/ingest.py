"""Bounded retrieval and extraction of public research papers (no TeX execution)."""
from __future__ import annotations

import gzip
import http.client
import io
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
import re
import socket
import ssl
import tarfile
import time
from urllib.parse import quote, unquote, urljoin, urlsplit
from xml.etree import ElementTree

import pymupdf

MAX_FILE = 32 * 1024 * 1024
MAX_SOURCE = 96 * 1024 * 1024
MAX_PAGES = 450
MAX_CHARS = 1_500_000
MAX_ASSETS = 8000
MAX_ASSET_BYTES = 128 * 1024 * 1024
ARXIV = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.\-]+/\d{7})(?:v[1-9]\d*)?", re.I)
DOI = re.compile(r"10\.\d{4,9}/[^\s?#]+", re.I)
PDF_WARNING = "PDF 的阅读顺序、公式和图表采用版面识别。公式截图保留原貌，复杂排版请对照原 PDF。"


class IngestError(ValueError):
    """An actionable error suitable for display to the paper owner."""


def _public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if not ip.is_global or ip.is_multicast or ip.is_reserved:
        return False
    if ip.version == 4:
        return str(ip) != "168.63.129.16"
    if ip.ipv4_mapped or ip.sixtofour or ip.teredo:
        return False
    return ip not in ipaddress.ip_network("64:ff9b::/96")


def fetch(url: str, limit: int = MAX_FILE, accept: str = "*/*") -> bytes:
    """Check every redirect, then connect to the validated address with TLS SNI."""
    deadline = time.monotonic() + 65
    for hop in range(6):
        if len(url) > 4096 or any(ord(c) < 32 or c.isspace() for c in url):
            raise IngestError("无效的论文地址。")
        try:
            parts = urlsplit(url)
            scheme, host = parts.scheme.lower(), parts.hostname
            port = parts.port or (443 if scheme == "https" else 80)
            if (scheme not in {"http", "https"} or not host or parts.username is not None
                    or parts.password is not None or port != (443 if scheme == "https" else 80)
                    or "%" in host or "\\" in parts.netloc):
                raise IngestError("请输入公开的 HTTP 或 HTTPS 论文链接。")
            host = host.encode("idna").decode("ascii")
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            if not records or any(not _public_ip(row[4][0]) for row in records):
                raise IngestError("该地址不是允许访问的公开论文地址。")
            timeout = min(15, deadline - time.monotonic())
            if timeout <= 0:
                raise IngestError("下载超时，请稍后重试或上传 PDF。")
            conn = (http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
                    if scheme == "https" else http.client.HTTPConnection(host, port, timeout=timeout))

            def connect_checked(_address, timeout, source_address=None):
                # Use the validated socket address directly; no second hostname lookup.
                last_error = None
                for family, socktype, proto, _, sockaddr in records:
                    sock = socket.socket(family, socktype, proto)
                    try:
                        sock.settimeout(min(timeout, max(0.1, deadline - time.monotonic())))
                        sock.connect(sockaddr)
                        return sock
                    except OSError as exc:
                        last_error = exc
                        sock.close()
                raise OSError("Unable to connect") from last_error

            conn._create_connection = connect_checked
            try:
                path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
                if parts.query:
                    path += "?" + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
                conn.request("GET", path, headers={"User-Agent": "PaperTranslate/1.0", "Accept": accept,
                                                   "Accept-Encoding": "identity"})
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise IngestError("论文地址重定向失败。")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise IngestError(f"论文服务返回 HTTP {response.status}，请上传可访问的 PDF。")
                size = response.getheader("Content-Length")
                if size and int(size) > limit:
                    raise IngestError("文件超过大小限制。")
                data = bytearray()
                while True:
                    if time.monotonic() > deadline:
                        raise IngestError("下载超时。")
                    chunk = response.read(min(65536, limit + 1 - len(data)))
                    if not chunk:
                        return bytes(data)
                    data.extend(chunk)
                    if len(data) > limit:
                        raise IngestError("文件超过大小限制。")
            finally:
                conn.close()
        except IngestError:
            raise
        except (ValueError, OSError, http.client.HTTPException, UnicodeError) as exc:
            raise IngestError("无法读取公开论文链接，请检查地址或上传 PDF。") from exc
    raise IngestError("论文链接重定向次数过多。")


def identify(source: str) -> tuple[str, str]:
    value = source.strip()
    stripped = re.sub(r"^arxiv:\s*", "", value, flags=re.I)
    if ARXIV.fullmatch(stripped):
        return "arxiv", stripped
    stripped = re.sub(r"^doi:\s*", "", value, flags=re.I)
    if DOI.fullmatch(stripped):
        return "doi", stripped
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() not in ("http", "https") or not host or parsed.username is not None:
            raise ValueError()
        path = unquote(parsed.path)
        if host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            ident = re.sub(r"^/(?:abs|pdf|html|src|e-print)/", "", path).removesuffix(".pdf").strip("/")
            if not ARXIV.fullmatch(ident):
                raise IngestError("无法识别 arXiv 编号。")
            return "arxiv", ident
        if host in {"doi.org", "dx.doi.org"}:
            if not DOI.fullmatch(path.lstrip("/")):
                raise IngestError("无法识别 DOI。")
            return "doi", path.lstrip("/")
        return "url", value
    except IngestError:
        raise
    except ValueError as exc:
        raise IngestError("请输入 arXiv、DOI 或公开 PDF 链接。") from exc


def resolve_doi(doi: str) -> tuple[bytes, dict]:
    encoded = quote(doi, safe="/")
    meta = {}
    links = []
    try:
        record = json.loads(fetch("https://api.crossref.org/works/" + encoded, 2_000_000))["message"]
        meta = {"title": next(iter(record.get("title", [])), ""),
                "authors": ", ".join(" ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name", "")
                                     for a in record.get("author", [])),
                "publication": next(iter(record.get("container-title", [])), "")}
        links += [link["URL"] for link in record.get("link", [])
                  if link.get("content-type") in {"application/pdf", "unspecified"} and link.get("URL")]
    except (IngestError, KeyError, TypeError, ValueError):
        pass
    # OpenAlex indexes OA copies at institutional repositories as well as publishers.
    try:
        record = json.loads(fetch("https://api.openalex.org/works/https://doi.org/" + encoded, 2_000_000))
        for location in record.get("locations", []):
            if location.get("is_oa") and location.get("pdf_url"):
                links.insert(0, location["pdf_url"])
    except (IngestError, KeyError, TypeError, ValueError):
        pass
    links.append("https://doi.org/" + encoded)
    for url in dict.fromkeys(links[:8] + [links[-1]]):
        try:
            data = fetch(url, accept="application/pdf")
            if data[:1024].lstrip().startswith(b"%PDF-"):
                return data, meta
        except IngestError:
            continue
    raise IngestError("未找到可以公开下载的 DOI 全文。请上传你有权使用的 PDF。")


def arxiv_meta(ident: str) -> dict:
    try:
        root = ElementTree.fromstring(fetch("https://export.arxiv.org/api/query?id_list=" + quote(ident), 500_000))
        entry = root.find("{http://www.w3.org/2005/Atom}entry")
        if entry is None:
            return {}
        ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
        if entry.findtext("a:title", "", ns) == "Error":
            return {}
        def element(name):
            return " ".join(entry.findtext(name, "", ns).split())
        return {"title": element("a:title"), "abstract": element("a:summary"), "publication": element("x:journal_ref"),
                "version": element("a:id").rsplit("/", 1)[-1].split("v")[-1] if re.search(r"v\d+$", element("a:id")) else "",
                "authors": ", ".join(a.text or "" for a in entry.findall("a:author/a:name", ns))}
    except (IngestError, ValueError, ElementTree.ParseError):
        return {}


def safe_sources(data: bytes, root: Path) -> None:
    """Copy bounded regular files; never unpack symlinks, devices or traversal paths."""
    if data.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                data = gz.read(MAX_SOURCE + 1)
        except (OSError, EOFError) as exc:
            raise IngestError("LaTeX 源文件压缩格式无效。") from exc
    if len(data) > MAX_SOURCE:
        raise IngestError("LaTeX 源文件超过解压限制。")
    root.mkdir(exist_ok=True, parents=True)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except tarfile.TarError:
        if b"\\documentclass" not in data or len(data) > 8_000_000:
            raise IngestError("找不到可读的 LaTeX 源文件。")
        (root / "main.tex").write_bytes(data)
        return
    total = 0
    allowed = {".tex", ".ltx", ".aux", ".bbl", ".bib", ".sty", ".cls", ".bst", ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".ps", ".txt"}
    seen = set()
    with archive:
        for number, member in enumerate(archive):
            if number >= 2500:
                raise IngestError("LaTeX 源文件数量超过限制。")
            path = PurePosixPath(member.name)
            if member.isdir():
                continue
            if (not member.isfile() or path.is_absolute() or ".." in path.parts or "\\" in member.name
                    or len(member.name) > 300 or any(ord(c) < 32 for c in member.name)):
                raise IngestError("LaTeX 归档含有不受支持的文件路径或类型。")
            if path.suffix.lower() not in allowed:
                continue
            total += member.size
            if member.size > MAX_FILE or total > MAX_SOURCE or path in seen:
                raise IngestError("LaTeX 归档内容超出限制或存在重名文件。")
            seen.add(path)
            target = root.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                raise IngestError("LaTeX 归档成员无法读取。")
            with stream:
                target.write_bytes(stream.read(member.size))


def sentences(text: str) -> list[str]:
    """Conservative sentence boundaries; keep common abbreviations and references."""
    text = re.sub(r"\s+", " ", text).strip()
    out, start = [], 0
    for match in re.finditer(r"(?<=[.!?。！？])\s+(?=[A-Z0-9“‘(\"\\{])|(?<=[。！？])(?=[\u3400-\u9fff])", text):
        prefix = text[start:match.start()].lower()
        if re.search(r"\b(?:eqs?|figs?|refs?|secs?|e\.g|i\.e|et al|dr|prof|no|vs|[a-z])\.$", prefix):
            continue
        out.append(text[start:match.start()])
        start = match.end()
    if text[start:].strip():
        out.append(text[start:].strip())
    return out


class Document:
    def __init__(self, folder: Path, method: str, meta: dict):
        self.data = {"title": "", "authors": "", "identifier": "", "sections": [], "equations": {}, "warnings": [],
                     "method": method, **meta}
        self.folder = folder
        self.assets = folder / "assets"
        self.assets.mkdir(parents=True, exist_ok=True)
        self.count = self.char_count = self.asset_count = self.asset_bytes = 0
        self.section("文首")

    def warn(self, message):
        if message not in self.data["warnings"]:
            self.data["warnings"].append(message)

    def section(self, title):
        target = {"id": f"sec{len(self.data['sections']) + 1:04d}", "title": title, "paragraphs": []}
        self.data["sections"].append(target)
        return target

    def equation(self, latex, raw, display, **extra):
        key = f"e{len(self.data['equations']) + 1:05d}"
        self.data["equations"][key] = {"id": key, "latex": latex, "raw": raw, "display": display, **extra}
        return "{{eq:" + key + "}}"

    def add(self, text, kind="prose", **extra):
        text = text.strip()
        if not text and not extra.get("image"):
            return
        self.char_count += len(text)
        self.count += 1
        if self.count > 35000 or self.char_count > MAX_CHARS:
            raise IngestError("论文长度超过当前导入限制，请拆分后上传。")
        pid = f"p{self.count:05d}"
        for eq in re.findall(r"\{\{eq:(e\d+)\}\}", text):
            if eq in self.data["equations"]:
                self.data["equations"][eq]["paragraph_id"] = pid
        paragraph = {"id": pid, "kind": kind, "sentences": [
            {"id": f"{pid}s{i:03d}", "en": line, "zh": ""} for i, line in enumerate(sentences(text), 1)], **extra}
        self.data["sections"][-1]["paragraphs"].append(paragraph)
        return paragraph

    def image(self, page, rect, name):
        self.asset_count += 1
        if self.asset_count > MAX_ASSETS:
            raise IngestError("论文图片数量超过导入限制。")
        rect = pymupdf.Rect(rect) & page.rect
        if rect.is_empty:
            return None
        zoom = min(1.8, 1600 / max(rect.width, rect.height))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect, alpha=False, annots=False)
        data = pix.tobytes("png")
        self.asset_bytes += len(data)
        if self.asset_bytes > MAX_ASSET_BYTES:
            raise IngestError("论文图片超过存储限制，请拆分后上传。")
        path = self.assets / f"{name}-{self.asset_count}.png"
        path.write_bytes(data)
        return "assets/" + path.name


def _split_column_blocks(blocks, width):
    """PDF producers sometimes group a left and a right line into a single block."""
    output = []
    mid = width / 2
    for block in blocks:
        lines = block.get("lines", [])
        left = [line for line in lines if line["bbox"][2] <= mid + 1]
        right = [line for line in lines if line["bbox"][0] >= mid - 1]
        if left and right and len(left) + len(right) == len(lines):
            for part in (left, right):
                rect = pymupdf.Rect(part[0]["bbox"])
                for line in part[1:]:
                    rect |= pymupdf.Rect(line["bbox"])
                output.append({**block, "lines": part, "bbox": list(rect)})
        else:
            output.append(block)
    return output


def _ordered(blocks, width):
    """Read each side of a gutter before crossing it. Wide blocks separate bands."""
    mid = width / 2
    wide, columns = [], [[], []]
    for block in blocks:
        x0, _, x1, _ = block["bbox"]
        if x0 < mid - .035 * width and x1 > mid + .035 * width:
            wide.append(block)
        else:
            columns[(x0+x1)/2 > mid].append(block)
    result = []
    key = lambda b: (b["bbox"][1], b["bbox"][0])
    for col in columns:
        col.sort(key=key)
    for header in sorted(wide, key=key):
        for i in range(2):
            split = next((n for n,b in enumerate(columns[i]) if b["bbox"][1] >= header["bbox"][1] - 1), len(columns[i]))
            result.extend(columns[i][:split])
            columns[i] = columns[i][split:]
        result.append(header)
    return result + columns[0] + columns[1]


def _math_line(line):
    text = "".join(s["text"] for s in line["spans"]).strip()
    fonts = " ".join(s["font"] for s in line["spans"])
    symbolic = re.search(r"[=∫∂∑∏√±×≤≥≃≈≡∞∝→⊗∇]", text)
    words = re.findall(r"[A-Za-z]{3,}", text)
    return bool((symbolic and len(words) <= 3) or
                (re.search(r"CMMI|CMSY|CMEX|Math|Symbol|MSAM|MSBM", fonts, re.I) and len(words) <= 2))


_MATH_FONT = re.compile(r"CMMI|CMSY|CMEX|Math|Symbol|MSAM|MSBM", re.I)
_INLINE = re.compile(r"(?<![A-Za-z])(?:[A-Za-zα-ωΑ-Ω]{1,2}(?:[_^][\w+-]+)?|\d+(?:\.\d+)?)"
                     r"\s*(?:=|≤|≥|≈|≡|∝|≃)\s*"
                     r"(?:[A-Za-zα-ωΑ-Ω]{1,3}(?![A-Za-z])|[+−-]?\d+(?:\.\d+)?)"
                     r"(?:[²³⁴⁵⁶⁷⁸⁹₀-₉]|[_^][\w+-]+|[*/+−-]\d+)*")


def _inline_line(doc, page, line):
    """Crop math runs directly from source glyph positions inside a prose line."""
    chars, flags = [], []
    for span in line["spans"]:
        glyphs = span.get("chars", [])
        chars.extend(glyphs)
        flags.extend([bool(_MATH_FONT.search(span["font"]) or span["flags"] & 1)] * len(glyphs))
    text = "".join(g["c"] for g in chars)
    if len(text) != len(chars):
        return text
    for match in _INLINE.finditer(text):
        flags[match.start():match.end()] = [True] * len(match[0])
    if not any(flags):
        return text
    result, i = [], 0
    while i < len(chars):
        if not flags[i]:
            result.append(text[i])
            i += 1
            continue
        j = i+1
        while j < len(chars) and flags[j]:
            j += 1
        if not text[i:j].strip():
            result.append(text[i:j])
        else:
            rect = pymupdf.Rect(chars[i]["bbox"])
            for glyph in chars[i+1:j]:
                rect |= pymupdf.Rect(glyph["bbox"])
            image = doc.image(page, rect, "inline")
            result.append(doc.equation("", "", False, image=image, extracted_text=text[i:j], page=page.number+1, bbox=list(rect)))
        i = j
    return "".join(result)


def from_pdf(path: Path, folder: Path, meta: dict) -> dict:
    doc = Document(folder, "pdf", meta)
    doc.warn(PDF_WARNING)
    try:
        pdf = pymupdf.open(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise IngestError("PDF 文件损坏或无法打开。") from exc
    with pdf:
        if not pdf.is_pdf or pdf.needs_pass or not 1 <= len(pdf) <= MAX_PAGES:
            raise IngestError("请上传未加密的 PDF，且页数不超过 450 页。")
        pdf_meta = pdf.metadata or {}
        doc.data["title"] = meta.get("title") or pdf_meta.get("title") or ""
        doc.data["authors"] = meta.get("authors") or pdf_meta.get("author") or ""
        for page in pdf:
            if max(page.rect.width, page.rect.height) > 10000:
                raise IngestError("页面尺寸超过处理限制。")
            page_no = page.number + 1
            # Image metadata is sufficient: render a bounded clip instead of extracting a potentially huge bitmap.
            blocks = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]
            for block in blocks:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        span["text"] = "".join(c["c"] for c in span["chars"])
            blocks = _split_column_blocks(blocks, page.rect.width)
            blocks += [{"type": 1, "bbox": image["bbox"]} for image in page.get_image_info()]
            try:
                # Vector diagrams and ruled tables can otherwise disappear even in digitally produced PDFs.
                for group in page.cluster_drawings():
                    if group.width >= 70 and group.height >= 32 and group.get_area() < .86 * page.rect.get_area():
                        blocks.append({"type": 2, "bbox": tuple(group)})
            except (RuntimeError, ValueError):
                doc.warn("部分矢量图形无法单独提取，请查看原 PDF。")
            text_size = sum(len(span["text"].strip()) for b in blocks for line in b.get("lines", []) for span in line["spans"])
            scan = any(b["type"] == 1 and (pymupdf.Rect(b["bbox"]) & page.rect).get_area() > .7 * page.rect.get_area() for b in blocks)
            if not text_size or (scan and text_size < 40):
                doc.warn(f"第 {page_no} 页缺少文本层，已保存页面原图；该页需要 OCR 或 LaTeX 源文件才能翻译。")
                doc.data.setdefault("untranslated_pages", []).append(page_no)
                image = doc.image(page, page.rect, "page")
                doc.add("", "page_image", page=page_no, image=image)
                continue
            for block in _ordered(blocks, page.rect.width):
                box = block["bbox"]
                if block["type"] != 0:
                    image = doc.image(page, box, "figure")
                    if image:
                        doc.add("", "figure", image=image, page=page_no, bbox=list(box))
                    continue
                lines, line_types = block["lines"], []
                for line in lines:
                    line_types.append(_math_line(line))
                pos = 0
                while pos < len(lines):
                    end = pos + 1
                    while end < len(lines) and line_types[end] == line_types[pos]:
                        end += 1
                    group = lines[pos:end]
                    rect = pymupdf.Rect(group[0]["bbox"])
                    for line in group[1:]:
                        rect |= pymupdf.Rect(line["bbox"])
                    text = " ".join("".join(s["text"] for s in line["spans"]).strip() for line in group).strip()
                    location = {"page": page_no, "bbox": list(rect)}
                    if text and line_types[pos]:
                        image = doc.image(page, rect + (-1, -1, 1, 1), "equation")
                        token = doc.equation("", "", True, image=image, extracted_text=text, **location)
                        doc.add(token, "equation", **location)
                    elif text:
                        text = " ".join(_inline_line(doc, page, line).strip() for line in group).strip()
                        is_title = not doc.data["title"] and page.number == 0 and len(text) > 5
                        is_heading = len(text) < 160 and re.match(
                            r"^(?:abstract|references|acknowledg[e]?ments|appendix\b.*|(?:[IVX]+\.|\d+(?:\.\d+)*\.?)\s+[A-Z].{1,110})$", text, re.I)
                        kind = "prose"
                        if is_title:
                            doc.data["title"] = text
                        if is_heading:
                            doc.section(text)
                            kind = "heading"
                        elif is_title or text.casefold() == doc.data["title"].casefold():
                            kind = "title"
                        elif doc.data["sections"][-1]["title"].casefold() == "references":
                            kind = "reference"
                        elif re.match(r"^(?:fig(?:ure)?|table)\.?\s+[\dIVX]+", text, re.I):
                            kind = "caption"
                        doc.add(text, kind, **location)
                    pos = end
        if not doc.data["title"]:
            doc.data["title"] = "PDF 文献"
    if not any(p["sentences"] for s in doc.data["sections"] for p in s["paragraphs"]):
        raise IngestError("这是一份扫描 PDF，未找到可翻译的文本。请先 OCR 或提供 arXiv 链接。")
    return doc.data


def _group(text, start, left="{", right="}"):
    if start >= len(text) or text[start] != left:
        raise IngestError("LaTeX 源码中的分组不完整。")
    depth, pos = 1, start + 1
    while pos < len(text) and depth:
        c = text[pos]
        if c == "\\":
            pos += 2
            continue
        depth += (c == left) - (c == right)
        pos += 1
    if depth:
        raise IngestError("LaTeX 源码中的分组未闭合。")
    return text[start+1:pos-1], pos


def _args(text, start, count=1):
    pos = start
    while pos < len(text) and text[pos].isspace():
        pos += 1
    while pos < len(text) and text[pos] == "[":
        _, pos = _group(text, pos, "[", "]")
        while pos < len(text) and text[pos].isspace():
            pos += 1
    args = []
    for _ in range(count):
        if pos >= len(text) or text[pos] != "{":
            break
        begin = pos
        value, pos = _group(text, pos)
        args.append((value, begin + 1, pos - 1))
        while pos < len(text) and text[pos].isspace() and len(args) < count:
            pos += 1
    return args, pos if args else start


def _read_tex(path):
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin1")


def _uncomment(tex):
    # Even numbers of preceding slashes leave the percent sign unescaped in TeX.
    return re.sub(r"(?<!\\)((?:\\\\)*)%[^\n]*",
                  lambda m: m[1] + " " * (len(m[0]) - len(m[1])), tex)


def _tree_path(root, base, name, suffixes=("",)):
    if not name or Path(name).is_absolute() or "\\" in name or "\x00" in name:
        return None
    for directory in [base, root]:
        for suffix in suffixes:
            path = (directory / (name + suffix)).resolve()
            if path.is_relative_to(root.resolve()) and path.is_file():
                return path
    return None


def _expanded(root, main):
    budget = [0, 0]
    def expand(path, ancestors):
        if path in ancestors or len(ancestors) >= 30:
            raise IngestError("LaTeX 文件存在循环或嵌套过深。")
        original = _read_tex(path)
        body = _uncomment(original)
        budget[0] += len(body)
        budget[1] += 1
        if budget[0] > 5_000_000 or budget[1] > 600:
            raise IngestError("LaTeX 正文展开后超过限制。")
        def substitute(match):
            cmd, braced, bare, imported, directory, file = match.groups()
            names = braced or bare or (directory.rstrip("/") + "/" + file)
            if cmd == "bibliography":
                target = main.with_suffix(".bbl")
                if not target.exists():
                    choices = list(root.rglob("*.bbl"))
                    if len(choices) != 1:
                        return match[0]
                    target = choices[0]
            else:
                target = _tree_path(root, path.parent, names.strip(), ("", ".tex", ".ltx"))
                if target is None:
                    raise IngestError("LaTeX 正文使用了无法解析的输入文件。")
            return "\n" + expand(target, ancestors | {path}) + "\n"
        parts, cursor = [], 0
        include = (r"\\(input|include|bibliography)\b\s*(?:\{([^{}]+)\}|([a-zA-Z0-9_./-]+))"
                   r"|\\(import|subimport)\s*\{([^{}]+)\}\s*\{([^{}]+)\}")
        for match in re.finditer(include, body):
            parts.extend((original[cursor:match.start()], substitute(match)))
            cursor = match.end()
        parts.append(original[cursor:])
        return "".join(parts)
    return expand(main, set())


MATH_ENVS = {"equation", "align", "alignat", "flalign", "gather", "multline", "eqnarray", "displaymath", "math",
             "IEEEeqnarray", "dmath", "breqn"}
STRUCT = {"section", "subsection", "subsubsection", "chapter", "paragraph", "subparagraph"}
REF_COMMAND = re.compile(r"(?:[Cc]ite\w*|[Cc]ref|[Rr]ef|[Ee]qref|[Aa]utoref|pageref|label|tag|url|hyperref)$")
MATH_START = re.compile(r"(?<!\\)(\$\$?|\\\[|\\\(|\\begin\{([\w*]+)\})")
COMMAND = re.compile(r"\\([a-zA-Z@]+\*?|.)")
TEXT_CMD = {"emph", "textbf", "textit", "textrm", "textsf", "texttt", "textsl", "textsc", "mbox", "underline",
            "MakeUppercase", "MakeLowercase", "textnormal", "ensuretext", "protect"}
DECORATIVE = {"maketitle", "tableofcontents", "clearpage", "newpage", "pagebreak", "noindent", "centering",
              "raggedright", "small", "footnotesize", "large", "Large", "bigskip", "medskip", "smallskip",
              "hfill", "vfill", "allowbreak", "appendix", "item", "and", "par", "protect", "leavevmode"}


def _math_at(tex, pos, end):
    match = MATH_START.match(tex, pos)
    if not match:
        return None
    opening, env = match.groups()
    if env and env.rstrip("*") not in MATH_ENVS:
        return None
    close = "\\end{" + env + "}" if env else {"$": "$", "$$": "$$", "\\[": "\\]", "\\(": "\\)"}[opening]
    if close.startswith("$"):
        found = re.compile(r"(?<!\\)" + re.escape(close)).search(tex, match.end(), end)
        closing = found.start() if found else -1
    else:
        closing = tex.find(close, match.end(), end)
    if closing < 0:
        raise IngestError("LaTeX 数学环境未闭合。")
    return tex[match.end():closing], closing + len(close), opening not in ("$", "\\(", "\\begin{math}"), env


_TEX_BREAK = re.compile(r"\n[ \t\r]*\n+")
_TEX_TEXT = re.compile(r"[^\\${}&\n]+|.", re.S)


class TexReader:
    def __init__(self, tex, raw_tex, root, main, doc):
        self.tex, self.raw_tex = tex, raw_tex
        self.root, self.main, self.doc = root, main, doc
        self.text = []
        self.start = None
        self.last = None
        self.reference = False
        self.abstract = False
        self.ack = False
        self.missing_assets = False
        self.table_count = 0
        preamble = tex[:max(0, tex.find(r"\begin{document}"))]
        self.aliases = {}
        self.math_macros = {}
        aliases = []
        # Support literal short macros common in physics papers without executing TeX.
        definition = re.compile(r"\\(?:def|(?:new|renew|provide)command)\s*\{?\\([a-zA-Z]+)\}?\s*(?:\[0\])?\s*(?=\{)")
        for command in definition.finditer(preamble):
            body, _ = _group(preamble, command.end())
            env = re.fullmatch(r"\\(begin|end)\s*\{([a-zA-Z]+\*?)\}", body.strip())
            if env and env[2].rstrip("*") in MATH_ENVS:
                aliases.append((command[1], env[1], env[2]))
            elif len(body) <= 150 and len(self.math_macros) < 256 and not re.search(r"[#%]|\\(?:def|input|include|begin|end|let)\b", body):
                self.math_macros[command[1]] = body
        for name, where, env in aliases:
            if where == "begin":
                closes = [r"\\" + re.escape(alias) + r"(?![a-zA-Z])" for alias, kind, typ in aliases
                          if kind == "end" and typ == env]
                closes.append(re.escape(r"\end{" + env + "}"))
                self.aliases[name] = env, re.compile("|".join(closes))

    def flush(self, kind=None, extra=None):
        if self.text:
            text = " ".join("".join(self.text).split())
            paragraph = self.doc.add(text, kind or ("reference" if self.reference else "acknowledgment" if self.ack else "abstract" if self.abstract else "prose"),
                                     tex_span=[self.start, self.last], **(extra or {}))
        else:
            paragraph = None
        self.text, self.start, self.last = [], None, None
        return paragraph

    def append(self, text, pos, last):
        if not self.text:
            self.start = pos
        self.last = last
        self.text.append(text)

    def formula(self, pos, end):
        item = _math_at(self.tex, pos, end)
        if item:
            latex, nxt, display, env = item
            opening = MATH_START.match(self.tex, pos).end()
        else:
            command = COMMAND.match(self.tex, pos)
            if not command or command[1] not in self.aliases:
                return None
            env, close = self.aliases[command[1]]
            found = close.search(self.tex, command.end(), end)
            if not found:
                raise IngestError("自定义数学环境未闭合。")
            opening = command.end()
            latex, nxt, display = self.tex[opening:found.start()], found.end(), env != "math"
        label = re.search(r"\\label\s*\{([^{}]+)\}", latex)
        render = re.sub(r"\\label\s*\{[^{}]+\}", "", latex).strip()
        number = re.search(r"\\tag\*?\s*\{([^{}]+)\}", render)
        simple_env = (env or "").rstrip("*")
        if simple_env == "alignat":
            render = r"\begin{alignedat}" + render + r"\end{alignedat}"
        elif simple_env in {"align", "flalign", "eqnarray"}:
            render = r"\begin{aligned}" + render + r"\end{aligned}"
        elif simple_env in {"gather", "multline"}:
            render = r"\begin{gathered}" + render + r"\end{gathered}"
        for _ in range(5):
            if len(render) > 12000:
                break
            expanded = re.sub(r"\\([a-zA-Z]+)", lambda m: self.math_macros.get(m[1], m[0]), render)
            if expanded == render or len(expanded) > 20000:
                break
            render = expanded
        raw_latex = self.raw_tex[opening:opening+len(latex)]
        token = self.doc.equation(raw_latex, self.raw_tex[pos:nxt], display, environment=env or "",
                                  render_latex=render, number=number[1] if number else "",
                                  label=label[1] if label else "")
        return token, nxt, display

    def figure(self, value):
        target = _tree_path(self.root, self.main.parent, value, ("", ".pdf", ".png", ".jpg", ".jpeg"))
        if not target:
            name = Path(value).name
            choices = [p for p in self.root.rglob("*") if p.is_file() and
                       (p.name == name or p.stem == name) and p.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg"}]
            target = choices[0] if len(choices) == 1 else None
        if not target:
            self.missing_assets = True
            return
        try:
            with pymupdf.open(target) as graphic:
                if graphic.page_count:
                    image = self.doc.image(graphic[0], graphic[0].rect, "figure")
                    self.doc.add("", "figure", image=image)
        except (ValueError, RuntimeError):
            self.missing_assets = True

    def run(self, start, end, force_kind=None, depth=0):
        if depth > 40:
            raise IngestError("LaTeX 嵌套过深。")
        tex = self.tex
        pos = start
        while pos < end:
            c = tex[pos]
            if c in "$\\":
                fm = self.formula(pos, end)
                if fm:
                    token, nxt, display = fm
                    if display:
                        self.flush(force_kind)
                    self.append(token, pos, nxt)
                    if display:
                        self.flush("equation")
                    pos = nxt
                    continue
            if c == "\\":
                match = COMMAND.match(tex, pos)
                if not match:
                    pos += 1
                    continue
                cmd = match[1].rstrip("*")
                args, after = _args(tex, match.end())
                val, lo, hi = args[0] if args else ("", 0, 0)
                if cmd == "label" and args:
                    self.flush(force_kind)
                    current = self.doc.data["sections"][-1]["paragraphs"]
                    if current and current[-1]["kind"] in {"heading","caption","figure","table","equation","title"}:
                        self.doc.data.setdefault("anchors", {})[val] = current[-1]["id"]
                elif cmd in STRUCT and args:
                    self.flush(force_kind)
                    self.doc.section(" ".join(val.split()))
                    self.ack = bool(re.search(r"acknowledg", val, re.I))
                    self.reference = val.lower().strip() in {"references", "bibliography"}
                    self.run(lo, hi, "heading", depth+1)
                elif cmd in {"title", "abstract", "caption", "footnote", "thanks"} and args:
                    self.flush(force_kind)
                    if cmd == "abstract":
                        self.doc.section("Abstract")
                    if cmd == "title" and not self.doc.data["title"]:
                        self.doc.data["title"] = " ".join(val.split())
                    self.run(lo, hi, {"thanks": "footnote"}.get(cmd, cmd), depth+1)
                elif cmd in {"begin", "end"} and args:
                    self.flush(force_kind)
                    if val == "abstract":
                        self.abstract = cmd == "begin"
                        if self.abstract:
                            self.doc.section("Abstract")
                    if val.lower() in {"acknowledgments", "acknowledgements", "acknowledgment"}:
                        self.ack = cmd == "begin"
                        if self.ack:
                            self.doc.section("Acknowledgments")
                    if val == "thebibliography":
                        self.reference = cmd == "begin"
                        if self.reference:
                            self.doc.section("References")
                            opt, nex = _args(tex, after)
                            if opt:
                                after = nex
                    if cmd == "begin" and val in {"verbatim","verbatim*","lstlisting","minted","comment"}:
                        end_tag = r"\end{" + val + "}"
                        stop = tex.find(end_tag, after, end)
                        if stop < 0:
                            raise IngestError("LaTeX 代码块或注释块未闭合。")
                        if val != "comment":
                            code_start = after
                            if val == "minted":
                                _, code_start = _args(tex, after)
                            self.doc.add(self.raw_tex[code_start:stop], "code")
                        after = stop + len(end_tag)
                    if cmd == "begin" and val in {"tabular", "tabularx", "tabular*", "longtable", "array"}:
                        close = tex.find("\\end{" + val + "}", after, end)
                        if close < 0:
                            raise IngestError("LaTeX 表格未闭合。")
                        fmt, body = _args(tex, after, 2 if val in {"tabularx", "tabular*"} else 1)
                        raw = tex[pos:close+len(val)+6]
                        paragraphs = self.doc.data["sections"][-1]["paragraphs"]
                        begin_index = len(paragraphs)
                        self.run(body, close, "table", depth+1)
                        cells = paragraphs[begin_index:]
                        self.table_count += 1
                        row = col = 0
                        previous = body
                        for cell in cells:
                            span = cell.get("tex_span")
                            if not span:
                                continue
                            for sep in re.findall(r"\\\\(?:\[[^\]]*\])?|(?<!\\)&", tex[previous:span[0]]):
                                if sep.startswith("\\"):
                                    row += 1
                                    col = 0
                                else:
                                    col += 1
                            cell.update(table_id=f"t{self.table_count:04d}", table_row=row, table_col=col)
                            previous = span[1]
                        if cells:
                            cells[-1]["table_latex"] = raw
                        after = close+len(val)+6
                elif cmd == "includegraphics" and args:
                    self.flush(force_kind)
                    self.figure(val)
                elif cmd in {"bibliography", "addbibresource"} and args:
                    self.flush(force_kind)
                    self.doc.section("References")
                    self.bibliography(val)
                elif cmd in {"bibitem"} and args:
                    self.flush(force_kind)
                    self.reference = True
                    self.doc.add(val, "reference", citation_key=val)
                elif cmd == "bibinfo" and args:
                    pair, nxt = _args(tex, match.end(), 2)
                    if len(pair) == 2:
                        self.flush(force_kind)
                        self.run(pair[1][1], pair[1][2], "reference_title" if val == "title" else "reference", depth+1)
                        after = nxt
                elif cmd in {"label", "bibliographystyle", "pagestyle", "thispagestyle", "vspace", "hspace", "setlength",
                             "title", "author", "affiliation", "email", "date", "keywords", "pacs"}:
                    self.flush(force_kind)
                elif REF_COMMAND.fullmatch(cmd) and args:
                    self.append(tex[pos:after], pos, after)
                elif cmd in TEXT_CMD and args:
                    self.flush(force_kind)
                    self.run(lo, hi, force_kind, depth+1)
                elif cmd in {"href", "textcolor"} and args:
                    pair, nxt = _args(tex, match.end(), 2)
                    if len(pair) == 2:
                        self.flush(force_kind)
                        self.run(pair[1][1], pair[1][2], force_kind, depth+1)
                        after = nxt
                elif cmd == "endinput":
                    self.flush(force_kind)
                    break
                elif cmd in DECORATIVE:
                    self.flush(force_kind)
                    after = match.end()
                elif len(cmd) == 1 and cmd in "&%$#_{}":
                    self.append(cmd, pos, match.end())
                    after = match.end()
                elif cmd in {"\\", ",", ";", "!", " ", "quad", "qquad"}:
                    self.flush(force_kind)
                    after = match.end()
                elif cmd in {"newcommand", "renewcommand", "providecommand", "DeclareMathOperator",
                             "def", "newenvironment", "setcounter", "addtocounter"}:
                    # Arbitrary macro definitions in document bodies must survive reconstruction.
                    self.flush(force_kind)
                    after = tex.find("\n", pos, end)
                    after = end if after < 0 else after
                    self.doc.warn("正文中包含自定义 TeX 指令，其定义保留在源文件。")
                else:
                    self.flush(force_kind)
                    if args:
                        self.run(lo, hi, force_kind, depth+1)
                    else:
                        self.doc.add(tex[pos:match.end()], "reference" if self.reference else "protected")
                    self.doc.warn("论文使用了自定义 LaTeX 命令。阅读器显示可解析的正文，原始命令保留在源文件。")
                pos = max(after, match.end())
                continue
            if c in "{}&":
                self.flush(force_kind)
                pos += 1
                continue
            if c == "\n":
                br = _TEX_BREAK.match(tex, pos, end)
                if br:
                    self.flush(force_kind)
                    pos = br.end()
                    continue
            chunk = _TEX_TEXT.match(tex, pos, end)
            nxt = chunk.end()
            self.append(chunk[0].replace("~", " "), pos, nxt)
            pos = nxt
        self.flush(force_kind)

    def bibliography(self, value):
        for name in value.split(","):
            path = _tree_path(self.root, self.main.parent, name.strip(), ("", ".bib", ".bbl"))
            if not path:
                self.doc.warn("部分参考文献数据库未包含在源文件中，请查阅原 PDF 的参考文献。")
                self.doc.data["source_incomplete"] = True
                continue
            text = _read_tex(path)
            for item in re.finditer(r"@([a-z]+)\s*\{\s*([^,{}]+),", text, re.I):
                if item[1].lower() in {"preamble", "comment", "string"}:
                    continue
                before = text.find("{", item.start(), item.end())
                content, _ = _group(text, before)
                title = re.search(r"\btitle\s*=\s*([\{\"])", content, re.I)
                if title and title[1] == "{":
                    title_text, title_end = _group(content, title.end()-1)
                elif title:
                    title_end = content.find('"', title.end()) + 1
                    title_text = content[title.end():title_end-1]
                else:
                    title_text, title_end = "", 0
                metadata = content if not title_text else (content[:title.start()] + content[title_end:])
                self.doc.add(metadata.strip(), "reference", citation_key=item[2].strip())
                if title_text:
                    self.doc.add(title_text, "reference_title", citation_key=item[2].strip(),
                                 bib_path=path.relative_to(self.root).as_posix(),
                                 bib_span=[before+1+title.end(), before+1+title_end-1])


def from_latex(root: Path, folder: Path, meta: dict) -> dict:
    candidates = sorted(list(root.rglob("*.tex")) + list(root.rglob("*.ltx")),
                        key=lambda p: (p.name.lower() not in {"main.tex", "paper.tex", "ms.tex"}, len(p.parts), p.name))
    main = next((p for p in candidates if re.search(r"\\document(?:class|style)\b", _uncomment(_read_tex(p)))
                 and "\\begin{document}" in _uncomment(_read_tex(p))), None)
    if main is None:
        raise IngestError("缺少 LaTeX 主文件。")
    original_tex = _expanded(root, main)
    tex = _uncomment(original_tex)
    start = tex.find("\\begin{document}")
    end = tex.find("\\end{document}", start+16)
    if start < 0 or end < 0:
        raise IngestError("LaTeX 正文不完整。")
    if re.search(r"\{\{eq:[\w.:-]+\}\}", tex):
        raise IngestError("文稿原文含有与公式标识冲突的字符。")
    doc = Document(folder, "latex", meta)
    doc.data["tex_main"] = main.relative_to(root).as_posix()
    doc.data["version_source"] = "arXiv"
    reader = TexReader(tex, original_tex, root, main, doc)
    # REVTeX often defines title and abstract after begin{document}; JHEP sets both before it.
    for match in re.finditer(r"\\(title|abstract|author)\*?\s*(?=\[|\{)", tex[:start]):
        cmd = match[1]
        args, _ = _args(tex, match.end())
        if not args:
            continue
        val, lo, hi = args[0]
        if cmd == "author":
            if not meta.get("authors"):
                doc.data["authors"] = ", ".join(filter(None, (doc.data["authors"], " ".join(val.split()))))
            continue
        if cmd == "abstract":
            doc.section("Abstract")
        elif not doc.data["title"]:
            doc.data["title"] = " ".join(val.split())
        reader.run(lo, hi, cmd)
    reader.run(start + len("\\begin{document}"), end)
    if not doc.data["authors"]:
        by = []
        for m in re.finditer(r"\\author\*?\s*(?=\[|\{)", tex[start:end]):
            args, _ = _args(tex, start + m.end())
            if args:
                by.append(" ".join(args[0][0].split()))
        doc.data["authors"] = ", ".join(by)
    labels = {}
    for aux in root.rglob("*.aux"):
        labels.update(re.findall(r"\\newlabel\{([^{}]+)\}\{\{([a-zA-Z0-9.()–-]+)\}", _read_tex(aux)))
    anchors = doc.data.setdefault("anchors", {})
    for eq in doc.data["equations"].values():
        for label in re.findall(r"\\label\s*\{([^{}]+)\}", eq["latex"]):
            anchors[label] = eq["paragraph_id"]
        if not eq.get("number") and eq.get("label") in labels:
            eq["number"] = labels[eq["label"]]
    ps = [p for sec in doc.data["sections"] for p in sec["paragraphs"]]
    text = "\n".join(s["en"] for p in ps for s in p["sentences"])
    found_equations = re.findall(r"\{\{eq:(e\d+)\}\}", text)
    if sorted(found_equations) != sorted(doc.data["equations"]):
        raise IngestError("源文稿的公式映射不一致。")
    known_citations = {p["citation_key"] for p in ps if p.get("citation_key")}
    known_refs = set(labels) | set(anchors)
    missing_refs, missing_cites = set(), set()
    for cmd, values in re.findall(r"\\([Cc]ite\w*|[Cc]ref|[Rr]ef|[Ee]qref|[Aa]utoref|pageref)\*?(?:\[[^\]]*\])*\{([^{}]+)\}", text):
        is_cite = cmd.lower().startswith("cite")
        missing = missing_cites if is_cite else missing_refs
        known = known_citations if is_cite else known_refs
        missing.update(key.strip() for key in values.split(",") if key.strip() not in known)
    doc.data["validation"] = {"equations":len(found_equations), "unresolved_refs":sorted(missing_refs)[:100],
                              "unresolved_cites":sorted(missing_cites)[:100]}
    if missing_refs or missing_cites:
        doc.warn(f"源稿中有 {len(missing_refs)} 个交叉引用和 {len(missing_cites)} 个文献键无法定位，已保留原标记。")
    if reader.missing_assets:
        doc.warn("源稿中有当前阅读器无法显示的插图，已保存在原 PDF 或源文件中。")
        doc.data["missing_figures"] = True
    if not any(p["kind"] in ("prose", "abstract", "caption", "acknowledgment") and p["sentences"]
               for s in doc.data["sections"] for p in s["paragraphs"]):
        raise IngestError("LaTeX 源文件中未识别出可翻译的正文。")
    doc.data["sections"] = [s for s in doc.data["sections"] if s["paragraphs"]]
    if not doc.data["title"]:
        doc.data["title"] = meta.get("identifier") or main.stem
    (folder / "expanded-main.tex").write_text(original_tex, encoding="utf-8")
    return doc.data


def _tex_escape(text):
    replacements = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&", "#": r"\#",
                    "%": r"\%", "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(replacements.get(c, c) for c in text)


_TEX_REF = r"\\(?:[Cc]ite\w*|[Cc]ref|[Rr]ef|[Ee]qref|[Aa]utoref|pageref|label|tag|url|hyperref)\*?(?:\[[^\[\]]*\])*\{[^{}]*\}"
_RESTORE = re.compile(r"\{\{eq:(e\d+)\}\}|" + _TEX_REF)


def _tex_sentence(sentence, doc):
    original, translated = sentence["en"], sentence.get("zh", "")
    if not translated.strip():
        raise IngestError("译文尚未完整，不能导出 LaTeX。")
    original_tokens = sorted(m[0] for m in _RESTORE.finditer(original))
    if sorted(m[0] for m in _RESTORE.finditer(translated)) != original_tokens:
        raise IngestError("公式或引用标记不完整，不能导出 LaTeX。")
    parts, pos = [], 0
    for item in _RESTORE.finditer(translated):
        parts.append(_tex_escape(translated[pos:item.start()]))
        parts.append(doc["equations"][item[1]]["raw"] if item[1] else item[0])
        pos = item.end()
    return "".join(parts) + _tex_escape(translated[pos:])


def translated_source(doc: dict, folder: Path) -> tuple[str, str]:
    """Return expanded translated main source and preserve surrounding TeX commands verbatim."""
    if doc.get("method") != "latex":
        raise IngestError("当前论文没有可导出的 LaTeX 源文件。")
    source = (folder / "expanded-main.tex").read_text(encoding="utf-8")
    edits = []
    for sec in doc["sections"]:
        for p in sec["paragraphs"]:
            span = p.get("tex_span")
            if not span or not p["sentences"]:
                continue
            if all(s.get("zh") == s["en"] for s in p["sentences"]):
                continue
            lo, hi = span
            leading = re.match(r"\s*", source[lo:hi])[0]
            trailing = re.search(r"\s*$", source[lo:hi])[0]
            value = " ".join(_tex_sentence(s, doc) for s in p["sentences"])
            edits.append((lo, hi, leading + value + trailing))
    pieces = []
    last = 0
    for lo, hi, value in sorted(edits):
        if lo < last or hi > len(source) or lo >= hi:
            raise IngestError("源文稿段落映射不一致，无法导出。")
        pieces.extend((source[last:lo], value))
        last = hi
    pieces.append(source[last:])
    return doc["tex_main"], "".join(pieces)


def _ingest_document(source: str | None, uploaded: Path | None, folder: Path) -> dict:
    """Ingest exactly one input into a new, caller-owned private storage directory."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if bool(source) == bool(uploaded):
        raise IngestError("请选择一种方式导入论文。")
    meta = {}
    kind, identifier = "upload", ""
    if uploaded is not None:
        uploaded = Path(uploaded)
        if not uploaded.is_file() or uploaded.stat().st_size > MAX_FILE:
            raise IngestError("请上传不超过 32 MiB 的 PDF 文件。")
        data = uploaded.read_bytes()
    else:
        kind, identifier = identify(source)
        if kind == "arxiv":
            meta = arxiv_meta(identifier)
            # Pin unversioned imports to the API version so PDF and TeX correspond.
            if not re.search(r"v\d+$", identifier) and str(meta.get("version", "")).isdigit():
                identifier += "v" + meta["version"]
            version = re.search(r"v(\d+)$", identifier)
            if version:
                meta["version"] = version[1]
            data = fetch("https://arxiv.org/pdf/" + quote(identifier, safe="/"), accept="application/pdf")
        elif kind == "doi":
            data, meta = resolve_doi(identifier)
        else:
            data = fetch(identifier, accept="application/pdf")
    if not data[:1024].lstrip().startswith(b"%PDF-"):
        raise IngestError("链接返回的文件不是可读取的 PDF。请上传原 PDF。")
    pdf = folder / "original.pdf"
    pdf.write_bytes(data)
    try:
        with pymupdf.open(pdf) as original:
            if not original.is_pdf or original.needs_pass or not 1 <= len(original) <= MAX_PAGES:
                raise IngestError("请提供未加密且页数不超过 450 页的 PDF。")
            meta["pages"] = len(original)
    except (RuntimeError, OSError) as exc:
        raise IngestError("PDF 文件无效。") from exc
    meta["identifier"] = identifier
    meta["source_url"] = source or ""
    if kind == "arxiv":
        try:
            root = folder / "source"
            safe_sources(fetch("https://arxiv.org/src/" + quote(identifier, safe="/")), root)
            document = from_latex(root, folder, meta)
            if document.get("source_incomplete") or document.get("missing_figures"):
                raise IngestError("源稿缺少可显示的图或参考文献。")
            return document
        except (IngestError, RuntimeError, OSError, tarfile.TarError, UnicodeError) as exc:
            reason = str(exc) if isinstance(exc, IngestError) else "源文件无法解析。"
            doc = from_pdf(pdf, folder, meta)
            doc["warnings"].insert(0, "使用 PDF 版面解析，LaTeX 源文件不可用。" + reason)
            return doc
    return from_pdf(pdf, folder, meta)


def ingest(source: str | None, uploaded: Path | None, folder: Path, *, cancel_check=None) -> dict:
    """Parse in a disposable child process, then promote only a successful import."""
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    request = {"source": source, "uploaded": str(Path(uploaded).resolve()) if uploaded else None}
    env = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "SYSTEMROOT")
           if name in os.environ}
    with tempfile.TemporaryDirectory(dir=folder, prefix=".import-") as temp:
        stage = Path(temp)
        (stage / "request.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        with subprocess.Popen([sys.executable, "-m", "papertranslate.ingest", str(stage)],
                              cwd=Path(__file__).resolve().parents[1], env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
            try:
                deadline = time.monotonic() + 540
                while child.poll() is None:
                    if cancel_check:
                        cancel_check()
                    if time.monotonic() >= deadline:
                        raise IngestError("导入用时超过限制，请拆分论文或上传 PDF。")
                    try:
                        child.wait(timeout=.75)
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
        file = stage / "result.json"
        if child.returncode != 0 or not file.is_file() or file.stat().st_size > 48 * 1024 * 1024:
            raise IngestError("文档解析超出资源限制或文件损坏，请检查 PDF 或拆分后重试。")
        result = json.loads(file.read_text(encoding="utf-8"))
        if "error" in result:
            raise IngestError(result["error"])
        if cancel_check:
            cancel_check()
        # The queue publishes the document to readers only after these file operations finish.
        for name in ("original.pdf", "assets", "source", "expanded-main.tex"):
            target = folder / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            if (stage / name).exists():
                (stage / name).replace(target)
        return result["paper"]


def _child(stage: Path):
    if sys.platform.startswith("linux"):
        import resource
        for category, limit in ((resource.RLIMIT_AS, 1536 * 1024 * 1024),
                                (resource.RLIMIT_CPU, 120), (resource.RLIMIT_FSIZE, 144 * 1024 * 1024)):
            _, hard = resource.getrlimit(category)
            capped = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
            resource.setrlimit(category, (capped, capped))
    try:
        request = json.loads((stage / "request.json").read_text(encoding="utf-8"))
        paper = _ingest_document(request["source"], Path(request["uploaded"]) if request["uploaded"] else None, stage)
        result = {"paper":paper}
    except IngestError as exc:
        result = {"error":str(exc)[:350]}
    except (ValueError, RuntimeError, TypeError, OSError, OverflowError, MemoryError, tarfile.TarError):
        result = {"error":"文档无法安全解析。请检查文件或上传原始 PDF。"}
    (stage / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__" and len(sys.argv) == 2:
    _child(Path(sys.argv[1]).resolve())
