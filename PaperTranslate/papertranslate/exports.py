"""Generate complete, validated downloads without executing a paper on the host."""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile

import pymupdf as fitz
from PIL import Image


TOKEN = re.compile(r"\{\{eq:([\w.-]+)\}\}")
KINDS = {"chinese": "pdf", "bilingual": "pdf", "markdown": "md", "latex": "zip"}
_EXPORT_VERSION = "4"
_MATH_LOCK = threading.Lock()


def _validate_translation(doc):
    if doc.get("untranslated_pages") or doc.get("status", "ready") != "ready":
        raise ValueError("全文尚未通过翻译完整性检查，仅可先下载原文。")
    count = 0
    equations = doc.get("equations", {})
    for section in doc.get("sections", []):
        for para in section.get("paragraphs", []):
            for sentence in para.get("sentences", []):
                en = sentence.get("en", "")
                if not en.strip():
                    continue
                count += 1
                zh = sentence.get("zh") or ""
                if not zh.strip():
                    raise ValueError("译文尚未完成，请完成翻译后再导出。")
                ids = TOKEN.findall(en)
                if Counter(ids) != Counter(TOKEN.findall(zh)) or any(i not in equations for i in ids):
                    raise ValueError(f"句子 {sentence.get('id')} 的公式未通过完整性检查。")
    if not count:
        raise ValueError("当前文献没有可导出的译文。")


def _asset(folder: Path, relative: str) -> Path:
    base = (folder / "assets").resolve()
    path = (folder / relative).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise ValueError("文献图片缺失或文件路径无效。")
    return path


def _png(folder, relative):
    path = _asset(folder, relative)
    with Image.open(path) as img:
        if img.width * img.height > 40_000_000:
            raise ValueError("原图过大，请下载原文查看。")
        img.thumbnail((2400, 2600))
        result = io.BytesIO()
        img.convert("RGB").save(result, format="PNG")
        return result.getvalue(), img.size


def _formula_image(latex):
    # MathText runs a fixed mathematical grammar, never system TeX or a shell.
    if not latex or len(latex) > 4000 or latex.count("{") > 180:
        return None
    from matplotlib.font_manager import FontProperties
    from matplotlib.mathtext import MathTextParser, math_to_image

    clean = re.sub(r"\\label\s*\{[^{}]*\}", "", latex).strip()
    clean = re.sub(r"^\\begin\{(?:equation|displaymath)\*?\}|\\end\{(?:equation|displaymath)\*?\}$", "", clean).strip()
    aligned = re.fullmatch(r'\\begin\{(aligned|gathered)\}(.*?)\\end\{\1\}', clean, re.S)
    if aligned:
        rows = re.split(r'\\\\(?:\[[\d.\-]+(?:pt|em|ex)\])?', aligned[2])
        if len(rows) > 40 or not all(row.strip() for row in rows):
            return None
        cell_rows = []
        for row in rows:
            cells = re.split(r'(?<!\\)&', row)
            if len(cells) > 2:
                return None
            images = []
            for part in cells:
                part = re.sub(r'\\(?:notag|nonumber)\b', '', part).strip()
                if not part:
                    images.append(None)
                    continue
                # Nested alignment depends on its own column layout; retain the source.
                if r'\begin{' in part:
                    return None
                cell = _formula_image(part)
                if cell is None:
                    return None
                with Image.open(io.BytesIO(cell[0])) as im:
                    images.append(im.convert('RGBA'))
            cell_rows.append(images)
        columns = max(len(row) for row in cell_rows)
        widths = [max([img.width for row in cell_rows if len(row) > i and (img := row[i]) is not None] or [1]) for i in range(columns)]
        heights = [max([img.height for img in row if img is not None] or [1]) for row in cell_rows]
        w, h = sum(widths)+5, sum(heights)+5*(len(rows)-1)
        if w > 4500 or h > 2000:
            return None
        canvas = Image.new('RGB', (w,h), 'white')
        y = 0
        for row, height in zip(cell_rows, heights):
            for i, img in enumerate(row):
                if img is not None:
                    x = (widths[0]-img.width if columns == 2 else (w-img.width)//2) if i == 0 else widths[0]+5
                    canvas.paste(img, (x, y+(height-img.height)//2), img)
            y += height+5
        data = io.BytesIO()
        canvas.save(data, 'PNG')
        return data.getvalue(), canvas.size
    expression = "$" + clean + "$"
    output = io.BytesIO()
    try:
        with _MATH_LOCK:
            # Limit dimensions before rasterizing exceptionally long equations.
            font = FontProperties(size=12)
            layout = MathTextParser("path").parse(expression, dpi=160, prop=font)
            if layout.width > 4500 or layout.height > 1600:
                return None
            math_to_image(expression, output, prop=font, dpi=160, format="png", color="#152331")
        with Image.open(io.BytesIO(output.getvalue())) as img:
            return output.getvalue(), img.size
    except (ValueError, RuntimeError, RecursionError, OverflowError, SyntaxError):
        return None


_AUX_LABEL = re.compile(r"\\newlabel\s*\{([^{}]{1,220})\}\s*\{\{([^{}\s]{1,38})\}\{")
_SAFE_NUMBER = re.compile(r"[A-Za-z\d.\-]{1,32}\Z")
_REFERENCE = re.compile(r"\\(eqref|ref|autoref|cite[a-zA-Z]*)\*?\{([^{}]+)\}")


def _reference_maps(doc, folder):
    numbers = {}
    anchors = {}
    cite_anchors = {}
    source = folder / 'source'
    if source.is_dir():
        for aux in source.rglob('*.aux'):
            if aux.is_file() and not aux.is_symlink() and aux.stat().st_size < 4_000_000:
                for label, number in _AUX_LABEL.findall(aux.read_text(encoding='utf-8', errors='replace')):
                    if _SAFE_NUMBER.fullmatch(number):
                        numbers[label] = number
    for eq in doc.get('equations', {}).values():
        if eq.get('label') and eq.get('paragraph_id'):
            anchors[eq['label']] = eq['paragraph_id']
        explicit = re.search(r'\\tag\*?\{([^{}]+)\}', eq.get('raw') or '')
        if eq.get('label') and explicit and _SAFE_NUMBER.fullmatch(explicit[1]):
            numbers[eq['label']] = explicit[1]
    for section in doc['sections']:
        for para in section['paragraphs']:
            if para.get('citation_key'):
                cite_anchors.setdefault(para['citation_key'], para['id'])
    return numbers, anchors, cite_anchors


def _expanded_tex(folder):
    path = folder / 'expanded-main.tex'
    return path.read_text(encoding='utf-8') if path.is_file() else ''


def _items_with_tables(paragraphs, source):
    """Recover row/cell boundaries from the exact gaps between translated source spans."""
    pending = []
    for para in paragraphs:
        if para.get('kind') == 'table' and para.get('tex_span') and source:
            pending.append(para)
            if not para.get('table_latex'):
                continue
            rows = [[[]]]
            end = pending[0]['tex_span'][0]
            for part in pending:
                start, new_end = part['tex_span']
                for separator in re.findall(r'\\\\(?:\[[^\]]*\])?|(?<!\\)&', source[end:start]):
                    if separator.startswith('\\'):
                        rows.append([[]])
                    else:
                        rows[-1].append([])
                rows[-1][-1].append(part)
                end = new_end
            yield {'kind': 'grid', 'rows': rows}
            pending.clear()
        else:
            yield from pending
            pending.clear()
            yield para
    yield from pending


def _page_layout(doc, folder, bilingual):
    archive = fitz.Archive()
    file_count = 0
    images = {}
    needs_original = bool(doc.get("missing_figures"))
    numbers, anchors, cite_anchors = _reference_maps(doc, folder)
    warnings = list(doc.get("warnings", []))
    equations = doc.get("equations", {})

    def image_tag(key, picture, is_formula=False, display=False):
        nonlocal file_count
        if key not in images:
            data, (width, height) = picture()
            file_count += 1
            filename = f"asset{file_count}.png"
            archive.add(data, filename)
            images[key] = (filename, width, height)
        filename, w, h = images[key]
        if is_formula:
            scale = min(0.60, 625 / max(w, 1), 280 / max(h, 1))
        else:
            scale = min(1.0, 610 / max(w, 1), 650 / max(h, 1))
        return f'<img src="{filename}" width="{max(1,round(w*scale))}" height="{max(1,round(h*scale))}"/>'

    def math_tag(match):
        nonlocal needs_original
        key = match.group(1)
        eq = equations[key]
        source = eq.get("render_latex") or eq.get("latex") or eq.get("raw") or ""
        picture = eq.get("image")
        number = eq.get("number") or numbers.get(eq.get("label"))
        if number is None and eq.get("label"):
            number = eq["label"]
            note = "部分公式仅保留原文标签，具体编号请查阅原论文。"
            if note not in warnings:
                warnings.append(note)
        suffix = " (" + html.escape(str(number)) + ")" if eq.get("display") and number is not None else ""
        if picture:
            return image_tag(("eq", key), lambda: _png(folder, picture), False) + ("" if eq.get("page") else suffix)
        if ("eq", key) in images:
            return image_tag(("eq", key), lambda: None, True) + suffix
        rendered = _formula_image(source)
        if rendered:
            return image_tag(("eq", key), lambda: rendered, True) + suffix
        needs_original = True
        note = f"公式 {key} 保留了原始 LaTeX，请与原论文核对排版。"
        if note not in warnings:
            warnings.append(note)
        if not source:
            raise ValueError(f"公式 {key} 缺少原始 LaTeX 和图像。")
        return '<code>' + html.escape(eq.get("raw") or source) + '</code>' + suffix

    def references(text):
        parts = []
        offset = 0
        for match in _REFERENCE.finditer(text):
            command, keys = match.groups()
            parts.append(html.escape(text[offset:match.start()]))
            offset = match.end()
            links = []
            for key in keys.split(','):
                key = key.strip()
                dest = (cite_anchors if command.startswith('cite') else anchors).get(key)
                label = html.escape(key if command.startswith('cite') else numbers.get(key, key))
                links.append(f'<a href="#pt-{html.escape(dest, quote=True)}">{label}</a>' if dest else label)
            prefix, suffix = ('[', ']') if command.startswith('cite') else ('(', ')') if command == 'eqref' else ('', '')
            parts.append(prefix + ', '.join(links) + suffix)
        parts.append(html.escape(text[offset:]))
        return ''.join(parts)

    def rich(text):
        parts = []
        previous = 0
        for match in TOKEN.finditer(text):
            parts += [references(text[previous:match.start()]), math_tag(match)]
            previous = match.end()
        return "".join(parts) + references(text[previous:])

    rows = []
    source = _expanded_tex(folder)
    all_paras = [p for s in doc["sections"] for p in s["paragraphs"]]
    if not any(p.get("kind") == "title" for p in all_paras):
        rows.append("<h1>" + html.escape(doc.get("title", "")) + "</h1>")
    subtitle = "中英对照" if bilingual else "中文译文"
    authors = html.escape(str(doc.get("authors") or ""))
    rows.append(f'<p class="meta">PaperTranslate · {subtitle}　{authors}</p>')
    for section in doc["sections"]:
        paras = section["paragraphs"]
        if section.get("title") and not any(p.get("kind") in ("heading", "title") for p in paras[:2]):
            heading = {"Abstract": "摘要", "Acknowledgments": "致谢", "References": "参考文献"}.get(section["title"], section["title"])
            rows.append("<h2>" + html.escape(heading) + "</h2>")
        for p in _items_with_tables(paras, source):
            kind = p.get("kind", "text")
            if kind == 'grid':
                cells = ['<table>']
                for row in p['rows']:
                    cells.append('<tr>')
                    for cell in row:
                        sentences = [s for para in cell for s in para['sentences']]
                        en = ' '.join(s['en'] for s in sentences)
                        zh = ''.join(s['zh'] for s in sentences)
                        content = rich(zh)
                        if bilingual and en != zh:
                            content = rich(en) + '<br/>' + content
                        cells.append('<td>' + content + '</td>')
                    cells.append('</tr>')
                rows.append(''.join(cells) + '</table>')
                continue
            if p.get("image"):
                rows.append('<div>' + image_tag(("img", p["image"]), lambda: _png(folder, p["image"])) + '</div>')
            sentences = p.get("sentences", [])
            if not sentences:
                continue
            if kind == 'title':
                tag = 'h1'
            elif kind == 'heading':
                tag = 'h2'
            else:
                tag = 'p'
            en = " ".join(s.get("en", "") for s in sentences)
            zh = "".join((s.get("zh") or "") for s in sentences)
            if bilingual and en.strip() != zh.strip():
                rows.append('<p class="english" lang="en">' + rich(en) + '</p>')
            anchor = html.escape(p["id"], quote=True)
            rows.append(f'<{tag} id="pt-{anchor}">' + rich(zh) + f'</{tag}>')
    if doc.get("method") == "latex":
        warnings.insert(0, "此 PDF 采用阅读版式重新排版。原始模板和排版资源保存在 LaTeX 下载包中。")
    append_original = needs_original and (folder / "original.pdf").is_file()
    if append_original:
        warnings.append("为便于核对未能重新排版的公式或插图，以下附上原论文页面。")
    if warnings:
        rows.append("<h2>导出说明</h2>")
        rows.extend("<p>" + html.escape(str(w)) + "</p>" for w in warnings)
    css = """body{font-family:sans-serif;font-size:10.5pt;line-height:1.60;color:#172635}
        h1{font-size:20pt;margin:8pt 0}h2{font-size:14pt;margin:14pt 0 6pt}
        p{margin:6pt 0}.meta{color:#48675c;font-size:9pt}.english{color:#435363;font-size:9.8pt}
        table{border-collapse:collapse;margin:8pt 0}td{border:.5pt solid #b0bfb8;padding:4pt} img{vertical-align:middle}code{font-size:9pt;color:#5d4129}"""
    story = fitz.Story('<html lang="zh-Hans"><body>' + "\n".join(rows) + '</body></html>', user_css=css, archive=archive)
    return story, append_original


def _write_pdf(doc, folder, bilingual, output):
    story, append_original = _page_layout(doc, folder, bilingual)
    page = fitz.paper_rect("a4")
    content = page + (49, 48, -49, -48)

    def rectangle(page_index, previous):
        if page_index > 2500:
            raise ValueError("生成的文献超过 2500 页排版上限。")
        return page, content, None

    with story.write_with_links(rectangle) as pdf:
        pdf.set_metadata({"title": doc.get("title", ""), "producer": "PaperTranslate"})
        for index, item in enumerate(pdf):
            item.insert_text((page.width - 65, page.height - 25), str(index + 1), fontsize=8, color=(.3,.4,.4))
        pdf.subset_fonts()
        if append_original:
            with fitz.open(folder / "original.pdf") as original:
                pdf.insert_pdf(original, links=False, annots=False, widgets=False)
        pdf.save(output, garbage=4, deflate=True)


def _validated_pdf(path):
    if not path.is_file():
        raise ValueError("此论文没有可用的 PDF 文件。")
    with fitz.open(path) as pdf:
        if not pdf.is_pdf or pdf.needs_pass or not 0 < len(pdf) <= 2501:
            raise ValueError("PDF 文件未通过验证。")
        for page in pdf:
            if page.rect.is_empty:
                raise ValueError("PDF 文件的页尺寸无效。")


def _markdown(doc, folder):
    equations = doc.get("equations", {})
    def image_data(path):
        data, _ = _png(folder, path)
        return '![](data:image/png;base64,' + base64.b64encode(data).decode('ascii') + ')'
    def formula(match):
        eq = equations[match.group(1)]
        if eq.get("raw"):
            return eq["raw"]
        if eq.get("latex"):
            delimiter = "$$" if eq.get("display") else "$"
            return delimiter + eq["latex"] + delimiter
        if eq.get("image"):
            return image_data(eq["image"])
        raise ValueError("有公式缺少可导出的原始内容。")
    blocks = ["# " + doc["title"], str(doc.get("authors") or "")]
    source = _expanded_tex(folder)
    for section in doc["sections"]:
        if section.get("title") and not any(p.get("kind") in {"title", "heading"} for p in section["paragraphs"][:2]):
            blocks.append("## " + section["title"])
        for p in _items_with_tables(section["paragraphs"], source):
            if p.get('kind') == 'grid':
                grid = []
                for row in p['rows']:
                    grid.append([TOKEN.sub(formula, ''.join(s['zh'] for para in cell for s in para['sentences']))
                                 .replace('|',r'\|').replace('\n', '<br>') for cell in row])
                width = max(len(row) for row in grid)
                lines = ['| ' + ' | '.join(row + [''] * (width-len(row))) + ' |' for row in grid]
                lines.insert(1, '| ' + ' | '.join(['---'] * width) + ' |')
                blocks.append('\n'.join(lines))
                continue
            if p.get("image"):
                blocks.append(image_data(p["image"]))
            text = "".join(s.get("zh") or "" for s in p.get("sentences", []))
            if text:
                prefix = {"title": "# ", "heading": "## "}.get(p.get("kind"), "")
                blocks.append(prefix + TOKEN.sub(formula, text))
    if doc.get("warnings"):
        blocks.append("## 导出说明")
        blocks.extend(str(w) for w in doc["warnings"])
    return "\n\n".join(filter(None, blocks)) + "\n"


def _translated_tree(doc, folder, target):
    if doc.get("method") != "latex":
        raise ValueError("只有取得原始 LaTeX 源码的论文才能下载译文源码。")
    from .ingest import translated_source

    main, translated = translated_source(doc, folder)
    relative = Path(main)
    if relative.is_absolute() or '..' in relative.parts or relative.suffix.lower() not in {'.tex', '.ltx'}:
        raise ValueError("无效的 LaTeX 主文件。")
    source = (folder / 'source').resolve()
    total = 0
    if not source.is_dir():
        raise ValueError("原始 LaTeX 文件夹缺失。")
    for item in source.rglob('*'):
        if item.is_symlink() or not item.is_file():
            continue
        path = item.relative_to(source)
        if path == relative or any(s.startswith('.') for s in path.parts):
            continue
        total += item.stat().st_size
        if total > 220 * 1024 * 1024:
            raise ValueError("原始 LaTeX 资源超过大小上限。")
        dest = target / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, dest)
    from .ingest import _tex_sentence
    bibliography_edits = {}
    for section in doc['sections']:
        for p in section['paragraphs']:
            if p.get('kind') != 'reference_title' or not p.get('bib_path') or not p.get('bib_span'):
                continue
            if not any(s['zh'] != s['en'] for s in p['sentences']):
                continue
            bib = (target / p['bib_path']).resolve()
            if not bib.is_relative_to(target) or bib.suffix.lower() != '.bib' or not bib.is_file():
                raise ValueError('参考文献源文件无效。')
            lo, hi = p['bib_span']
            new_title = ' '.join(_tex_sentence(s, doc) for s in p['sentences'])
            bibliography_edits.setdefault(bib, []).append((lo, hi, new_title))
    for bib, edits in bibliography_edits.items():
        data = bib.read_bytes()
        try:
            contents = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            contents = data.decode('latin-1')
        last = len(contents)
        for lo, hi, value in sorted(edits, reverse=True):
            if not 0 <= lo < hi <= last:
                raise ValueError('参考文献标题的原文位置无法验证。')
            contents = contents[:lo] + value + contents[hi:]
            last = lo
        bib.write_text(contents, encoding='utf-8')
    # Injection affects the download/build copy only. Original math stays byte-for-byte intact.
    marker = re.search(r'\\begin\s*\{document\}', translated)
    if marker is None:
        raise ValueError("译文源码缺少 document 环境。")
    support = '\n\\usepackage{xeCJK}\n\\setCJKmainfont{Noto Serif CJK SC}\n'
    translated = translated[:marker.start()] + support + translated[marker.start():]
    dest = target / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(translated, encoding='utf-8')
    return relative


def _source_pdf(doc, folder, root, output):
    """Optional original-template compilation. Fail closed to the reflow renderer."""
    bwrap = shutil.which('bwrap')
    prlimit = shutil.which('prlimit')
    if not bwrap or not prlimit or not Path('/usr/bin/xelatex').is_file():
        return False
    tree, build = root / 'tree', root / 'build'
    tree.mkdir()
    build.mkdir()
    try:
        main = _translated_tree(doc, folder, tree)
    except (OSError, ValueError):
        return False
    # Work from a temporary, unprivileged copy. Bind only system typesetting data.
    # No service working directory, credentials, home, host /tmp or network is visible.
    cmd = [prlimit, '--cpu=30', '--as=1610612736', '--fsize=100663296', '--nofile=128', '--',
           bwrap, '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL', '--clearenv']
    for directory in ['/usr', '/lib', '/lib64', '/etc/fonts', '/etc/texmf', '/var/lib/texmf', '/var/cache/fontconfig']:
        if Path(directory).exists():
            cmd.extend(['--ro-bind', directory, directory])
    cmd.extend(['--dev', '/dev', '--tmpfs', '/tmp', '--ro-bind', str(tree), '/paper',
                '--bind', str(build), '/out', '--setenv', 'PATH', '/usr/bin', '--setenv', 'HOME', '/tmp',
                '--setenv', 'TEXMFVAR', '/tmp/texmf-var', '--setenv', 'TEXMFCONFIG', '/tmp/texmf-config',
                '--setenv', 'openin_any', 'p', '--setenv', 'openout_any', 'p',
                '--setenv', 'SOURCE_DATE_EPOCH', '946684800', '--setenv', 'XDG_CACHE_HOME', '/tmp',
                '--chdir', str(Path('/paper') / main.parent), '--'])
    engine = ['/usr/bin/xelatex', '-no-shell-escape', '-halt-on-error', '-interaction=batchmode',
              '-output-directory=/out', '-jobname=translated', './' + main.name]
    def run(program):
        return subprocess.run(cmd + program, timeout=38, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False).returncode == 0
    try:
        if not run(engine):
            return False
        if not (build/'translated.bbl').exists() and list(tree.rglob('*.bib')):
            # Original-template bibliographies may ship precompiled .bbl resources. If
            # regeneration is required and cannot be completed we use the reading PDF.
            if (build/'translated.aux').is_file() and '\\bibdata' in (build/'translated.aux').read_text(errors='replace'):
                if not run(['/usr/bin/bibtex', '/out/translated']):
                    return False
        if not run(engine) or not run(engine):
            return False
        result = build / 'translated.pdf'
        if result.is_symlink() or not result.is_file():
            return False
        _validated_pdf(result)
        log = (build/'translated.log').read_text(errors='replace')
        if 'Missing character:' in log or re.search(r'(undefined references|Citation .+? undefined|Reference .+? undefined)', log):
            return False
        with fitz.open(result) as pdf:
            actual = re.sub(r'\s+', '', ''.join(page.get_text() for page in pdf))
            for section in doc['sections']:
                samples = [re.search(r'[\u3400-\u9fff]{2,10}', s.get('zh') or '') for p in section['paragraphs'] for s in p.get('sentences', [])]
                if any(sample and sample.group() not in actual for sample in samples):
                    return False
        shutil.copyfile(result, output)
        return True
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired):
        return False


def export(doc: dict, kind: str, folder: Path) -> Path:
    """Produce a version-keyed artifact atomically. Reject partial translations."""
    folder = Path(folder).resolve()
    if kind == 'original':
        original = folder / 'original.pdf'
        _validated_pdf(original)
        return original
    if kind not in KINDS:
        raise ValueError('不支持的导出格式。')
    _validate_translation(doc)
    key = hashlib.sha256(json.dumps([_EXPORT_VERSION, kind, doc], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
    outdir = folder / 'exports'
    outdir.mkdir(exist_ok=True)
    result = outdir / f'{kind}-{key}.{KINDS[kind]}'
    if result.is_file():
        return result
    with tempfile.TemporaryDirectory(dir=outdir) as name:
        work = Path(name)
        temp = work / f'output.{KINDS[kind]}'
        if kind == 'latex':
            tree = work / 'tree'
            tree.mkdir()
            _translated_tree(doc, folder, tree)
            with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                for item in sorted(tree.rglob('*')):
                    if item.is_file():
                        z.write(item, item.relative_to(tree))
            with zipfile.ZipFile(temp) as z:
                if z.testzip() is not None:
                    raise ValueError('源码归档未通过验证。')
        elif kind == 'markdown':
            temp.write_text(_markdown(doc, folder), encoding='utf-8')
        else:
            compiled = kind == 'chinese' and doc.get('method') == 'latex' and _source_pdf(doc, folder, work, temp)
            if not compiled:
                _write_pdf(doc, folder, kind == 'bilingual', temp)
            _validated_pdf(temp)
        os.replace(temp, result)
    return result
