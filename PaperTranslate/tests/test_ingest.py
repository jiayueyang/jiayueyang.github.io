import gzip
import io
from pathlib import Path
import socket
import tarfile
from unittest.mock import patch

import pymupdf
import unittest
from tempfile import TemporaryDirectory

from papertranslate import ingest as module
from papertranslate.ingest import IngestError, from_latex, identify, ingest, safe_sources, translated_source


def paper_pdf(path):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=700)
        pdf.set_metadata({"title": "Cavity thermodynamics", "author": "A. Researcher"})
        page.insert_text((38, 60), "Cavity thermodynamics")
        page.insert_text((38, 110), "First left paragraph.")
        page.insert_text((330, 110), "First right paragraph.")
        page.insert_text((38, 250), "Second left paragraph.")
        page.insert_text((330, 250), "Second right paragraph.")
        page.insert_text((44, 300), "T = k / (2 pi)")
        pdf.save(path)
    return path


def paras(doc):
    return [p for sec in doc["sections"] for p in sec["paragraphs"]]


def archive(entries):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, raw in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return out.getvalue()


TEX = r"""\documentclass{article}
\title{Cavity thermodynamics}
\author{A. Researcher}
\begin{document}
\maketitle
\begin{abstract}
We study $T_H$ at a wall.
\end{abstract}
\section{Introduction}
\input{sections/result}
\caption{Caption with $r_c$.}
\begin{thebibliography}{1}
\bibitem{hawking} S. Hawking. Example title.
\end{thebibliography}
\end{document}
"""
RESULT = r"""The temperature satisfies Eq.~\eqref{temp}. See \cite{hawking}.

\begin{equation}
T_H=\frac{\kappa}{2\pi}\label{temp}
\end{equation}

We also have $E=mc^2$.\footnote{A useful clarification.}
\section{Acknowledgments}
We thank our colleagues.
"""



class IngestTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)

    def test_pdf_layout_math_and_stable_ids(self):
        tmp_path = self.tmp_path
        file = paper_pdf(tmp_path / "sample.pdf")
        d = ingest(None, file, tmp_path / "one")
        text = [s["en"] for p in paras(d) for s in p["sentences"]]
        assert text.index("Second left paragraph.") < text.index("First right paragraph.")
        eq = next(iter(d["equations"].values()))
        assert not eq["latex"] and (tmp_path / "one" / eq["image"]).is_file()
        assert eq["page"] == 1 and eq["paragraph_id"] and len(eq["bbox"]) == 4
        assert d["authors"] == "A. Researcher"
        assert d == ingest(None, file, tmp_path / "two")


    def test_mixed_scan_document_marks_untranslated_pages(self):
        tmp_path = self.tmp_path
        file = paper_pdf(tmp_path / "sample.pdf")
        with pymupdf.open(file) as pdf:
            page = pdf.new_page()
            page.draw_rect(pymupdf.Rect(60, 60, 300, 200))
            both = tmp_path / "mixed.pdf"
            pdf.save(both)
        doc = ingest(None, both, tmp_path / "store")
        assert doc["untranslated_pages"] == [2]
        assert any(p.get("image") and p["kind"] == "page_image" for p in paras(doc))

    def test_source_roundtrip_and_citation_integrity(self):
        tmp_path = self.tmp_path
        source = tmp_path / "source"
        safe_sources(archive({"main.tex": TEX.encode(), "sections/result.tex": RESULT.encode()}), source)
        doc = from_latex(source, tmp_path, {})
        equations = doc["equations"]
        assert [e["latex"].strip() for e in equations.values()] == [
            "T_H", r"T_H=\frac{\kappa}{2\pi}\label{temp}", "E=mc^2", "r_c"]
        assert all(e.get("paragraph_id") for e in equations.values())
        assert {p["kind"] for p in paras(doc)} >= {"abstract", "caption", "title", "equation", "footnote", "acknowledgment", "reference"}
        assert doc["authors"] == "A. Researcher"
        for p in paras(doc):
            for sentence in p["sentences"]:
                sentence["zh"] = sentence["en"].replace("We study", "我们研究").replace("We also have", "我们还有")
        main, tex = translated_source(doc, tmp_path)
        assert main == "main.tex"
        assert "我们研究 $T_H$" in tex and r"\input{sections/result}" not in tex
        assert r"\label{temp}" in tex and r"\cite{hawking}" in tex and r"\begin{thebibliography}{1}" in tex
        with self.assertRaisesRegex(IngestError, "标记"):
            first = next(s for p in paras(doc) for s in p["sentences"] if "eq:" in s["en"])
            first["zh"] = "错误"
            translated_source(doc, tmp_path)


    def test_arxiv_ingest_pins_version_and_prefers_source(self):
        tmp_path = self.tmp_path
        data = paper_pdf(tmp_path/"paper.pdf").read_bytes()
        feed = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Cavity</title><id>https://arxiv.org/abs/2401.12345v2</id><author><name>A. Researcher</name></author></entry></feed>'
        def response(url, *args, **kwargs):
            if "api/query" in url:
                return feed
            if url.endswith("/pdf/2401.12345v2"):
                return data
            if url.endswith("/src/2401.12345v2"):
                return archive({"main.tex": TEX.encode(), "sections/result.tex": RESULT.encode()})
            raise AssertionError(url)
        with patch.object(module, "fetch", response):
            doc = module._ingest_document("https://arxiv.org/abs/2401.12345", None, tmp_path / "paper")
        assert doc["identifier"] == "2401.12345v2" and doc["method"] == "latex"
        assert (tmp_path / "paper/original.pdf").read_bytes() == data


    def test_doi_metadata_and_fallback(self):
        tmp_path = self.tmp_path
        data = paper_pdf(tmp_path/"sample.pdf").read_bytes()
        def response(url, *args, **kwargs):
            if "api.crossref.org" in url:
                return b'{"message":{"title":["Crossref paper"],"author":[{"given":"Ada","family":"Lovelace"}],"link":[]}}'
            if "api.openalex.org" in url:
                return b'{"locations":[{"is_oa":true,"pdf_url":"https://oa.example/article.pdf"}]}'
            assert url == "https://oa.example/article.pdf"
            return data
        with patch.object(module, "fetch", response):
            doc = module._ingest_document("https://doi.org/10.1234/test", None, tmp_path/"store")
        assert doc["title"] == "Crossref paper" and doc["authors"] == "Ada Lovelace"


    def test_ids_and_invalid_inputs(self):
        tmp_path = self.tmp_path
        for val in ("hep-th/9901001", "arXiv:2401.12345v3", "https://arxiv.org/pdf/2401.12345v3.pdf"):
            assert identify(val)[0] == "arxiv"
        assert identify("10.1234/foo") == ("doi","10.1234/foo")
        with self.assertRaises(IngestError):
            identify("file:///etc/passwd")
        with self.assertRaises(IngestError):
            safe_sources(archive({"../main.tex":b"abc"}), tmp_path/"tree")
        with self.assertRaises(IngestError):
            safe_sources(gzip.compress(b"broken"), tmp_path/"tree")
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",443))]):
            with self.assertRaises(IngestError):
                module.fetch("https://paper.example/p")
        for url in ("file:///x", "https://user@example.org/x", "https://example.org:8088/x"):
            with self.assertRaises(IngestError):
                module.fetch(url)

    def test_bib_title_offsets_comment_and_label_preservation(self):
        root = self.tmp_path/"src"
        bib = '@article{H, author={Hawking, S.}, title={Radiation from black holes}, year={1975}}'
        tex = (r"\documentclass{article}\begin{document}Consider $T% original comment"
               "\n" + r"=1$.\begin{equation}E=M\label{mass}\end{equation}\bibliography{refs}\end{document}")
        safe_sources(archive({"main.tex":tex.encode(), "refs.bib":bib.encode(),
                              "main.aux":br"\newlabel{mass}{{1.5}{4}{Example}{equation.1.5}{}}"}), root)
        doc = from_latex(root, self.tmp_path,{})
        p = next(p for p in paras(doc) if p["kind"]=="reference_title")
        lo, hi = p["bib_span"]
        assert p["bib_path"] == "refs.bib" and bib[lo:hi] == "Radiation from black holes"
        equations = list(doc["equations"].values())
        assert "% original comment" in equations[0]["raw"]
        assert equations[1]["number"] == "1.5" and "label" not in equations[1]["render_latex"]

    def test_inline_pdf_formula_preserves_original_visual(self):
        file = self.tmp_path/"inline.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((35, 60), "The exact temperature is T = 2 in these units.")
            pdf.save(file)
        doc = ingest(None, file, self.tmp_path/"store")
        eq = next(iter(doc["equations"].values()))
        assert eq["extracted_text"] == "T = 2" and not eq["display"]
        assert (self.tmp_path/"store"/eq["image"]).is_file()

    def test_common_equation_shortcuts_tables_and_unbraced_input(self):
        root = self.tmp_path/"src"
        source = r"""\documentclass{article}
\newcommand{\be}{\begin{equation}}
\newcommand{\ee}{\end{equation}}
\def\TH{T_{H}}
\begin{document}
\input part
\begin{tabular}{cc}
First & Second \\
A & B \\
\end{tabular}
\end{document}"""
        part = r"Here is the temperature. \be\TH=1\label{eq:temp}\ee"
        safe_sources(archive({"main.tex":source.encode(), "part.tex":part.encode()}), root)
        doc=from_latex(root, self.tmp_path,{})
        eq=next(iter(doc['equations'].values()))
        assert eq['latex']==r"\TH=1\label{eq:temp}" and eq['render_latex']=="T_{H}=1"
        assert eq['raw']==r"\be\TH=1\label{eq:temp}\ee"
        cells = [p for p in paras(doc) if p.get("table_id")]
        assert [(p["table_row"],p["table_col"]) for p in cells] == [(0,0),(0,1),(1,0),(1,1)]
