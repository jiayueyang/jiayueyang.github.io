import tempfile
import unittest
from pathlib import Path
import zipfile

import pymupdf as fitz
from PIL import Image

from papertranslate.exports import export


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.folder = Path(self.work.name)
        (self.folder/'assets').mkdir()
        Image.new('RGB', (50,30), 'navy').save(self.folder/'assets'/'figure.png')
        self.sentence = {'id':'s1', 'en':'Gravity obeys {{eq:e1}}.', 'zh':'引力满足方程 {{eq:e1}}。'}
        self.doc = {'id':'example', 'title':'Gravity', 'authors':'A. Researcher', 'method':'pdf',
                    'sections':[{'id':'sec1','title':'Abstract', 'paragraphs':[
                        {'id':'p1', 'kind':'text', 'sentences':[self.sentence]},
                        {'id':'p2', 'kind':'figure', 'image':'assets/figure.png','sentences':[]}]}],
                    'equations':{'e1':{'latex':r'R_{\mu\nu}=0','raw':r'\(R_{\mu\nu}=0\)', 'paragraph_id':'p1'}}}

    def test_bilingual_pdf_has_both_languages_math_and_figure(self):
        path = export(self.doc, 'bilingual', self.folder)
        with fitz.open(path) as pdf:
            page = pdf[0]
            self.assertIn('引力满足方程', page.get_text())
            self.assertIn('Gravity obeys', page.get_text())
            self.assertGreaterEqual(len(page.get_images()), 2)
            self.assertNotIn(r'R_{', page.get_text())
            # The font contains visible CJK glyphs, in addition to a searchable text map.
            boxes = page.search_for('引力')
            self.assertTrue(boxes)
            pixmap = page.get_pixmap(clip=boxes[0])
            self.assertGreater(len(set(pixmap.samples)), 5)
        self.assertEqual(export(self.doc, 'bilingual', self.folder), path)
        with fitz.open(export(self.doc, 'chinese', self.folder)) as pdf:
            self.assertNotIn('Gravity obeys', pdf[0].get_text())

    def test_markdown_preserves_original_formula_and_figure(self):
        content = export(self.doc, 'markdown', self.folder).read_text()
        self.assertIn(self.doc['equations']['e1']['raw'], content)
        self.assertIn('data:image/png;base64,', content)

    def test_block_incomplete_or_changed_math(self):
        for zh in ('', '张量为零。', '{{eq:e2}}', '{{eq:e1}}{{eq:e1}}'):
            with self.subTest(zh=zh):
                self.sentence['zh'] = zh
                with self.assertRaises(ValueError):
                    export(self.doc, 'chinese', self.folder)

    def test_missing_image_fails(self):
        (self.folder/'assets'/'figure.png').unlink()
        with self.assertRaises(ValueError):
            export(self.doc, 'bilingual', self.folder)

    def test_image_equation_without_tex(self):
        self.doc['equations']['e1']={'latex':'', 'image':'assets/figure.png', 'page':1}
        with fitz.open(export(self.doc, 'chinese', self.folder)) as pdf:
            self.assertTrue(pdf[0].get_images())

    def test_original_requires_valid_pdf(self):
        with self.assertRaises(ValueError):
            export(self.doc, 'original', self.folder)
        with fitz.open() as original:
            original.new_page().insert_text((50,50),'The original document')
            original.save(self.folder/'original.pdf')
        self.assertEqual(export(self.doc, 'original', self.folder), self.folder/'original.pdf')

    def test_source_archive_includes_assets_and_unicode(self):
        from papertranslate.ingest import from_latex

        source = self.folder/'source'
        source.mkdir()
        (source/'references.bib').write_text('@article{x}')
        (source/'main.tex').write_text(r'''\documentclass{article}
\title{Gravity}
\begin{document}
\maketitle
We study gravity. The tensor obeys \(R_{\mu\nu}=0\).
\end{document}''')
        parsed = from_latex(source, self.folder, {})
        for section in parsed['sections']:
            for para in section['paragraphs']:
                for sentence in para['sentences']:
                    sentence['zh'] = (sentence['en'].replace('We study gravity.', '我们研究引力。')
                                      .replace('The tensor obeys', '张量满足').replace('Gravity', '引力'))
        with zipfile.ZipFile(export(parsed, 'latex', self.folder)) as package:
            self.assertIsNone(package.testzip())
            self.assertIn('references.bib', package.namelist())
            rebuilt = package.read('main.tex').decode()
            self.assertIn('我们研究引力', rebuilt)
            self.assertIn(r'\(R_{\mu\nu}=0\)', rebuilt)
            self.assertIn('xeCJK', rebuilt)
        with fitz.open(export(parsed,'chinese',self.folder)) as paper:
            self.assertIn('我们研究引力', ''.join(page.get_text() for page in paper))

    def test_bibliography_option_replaces_only_title_in_source_archive(self):
        from papertranslate.ingest import from_latex
        source = self.folder/'source'
        source.mkdir()
        (source/'main.tex').write_text(r'\documentclass{article}\begin{document}Here we study gravity.\bibliography{refs}\end{document}')
        original = '@article{ref,author={A. Author}, title={General Relativity}, year={1916}}\n'
        (source/'refs.bib').write_text(original)
        parsed = from_latex(source, self.folder, {})
        for section in parsed['sections']:
            for para in section['paragraphs']:
                for sentence in para['sentences']:
                    sentence['zh'] = '广义相对论' if para['kind']=='reference_title' else sentence['en']
        with zipfile.ZipFile(export(parsed, 'latex', self.folder)) as package:
            self.assertEqual(package.read('refs.bib').decode(), original.replace('General Relativity','广义相对论'))

    def test_tables_keep_cell_coordinates_in_markdown_and_pdf(self):
        from papertranslate.ingest import from_latex

        source = self.folder/'source'
        source.mkdir()
        tex = r'\documentclass{article}' + '\n' + r'\begin{document}' + '\n' + r'''Data are presented here.
\begin{tabular}{ll}
Stage & Value \\
Phase & $1/2$ \\
\end{tabular}
\end{document}'''
        (source/'main.tex').write_text(tex)
        parsed = from_latex(source, self.folder, {})
        translated = {'Stage':'阶段', 'Value':'数值', 'Phase':'相位'}
        for section in parsed['sections']:
            for para in section['paragraphs']:
                for sentence in para['sentences']:
                    sentence['zh'] = translated.get(sentence['en'], sentence['en'])
        md = export(parsed, 'markdown', self.folder).read_text()
        self.assertIn('| 阶段 | 数值 |\n| --- | --- |\n| 相位 | $1/2$ |',md)
        with fitz.open(export(parsed, 'bilingual', self.folder)) as pdf:
            self.assertTrue(pdf[0].search_for('相位'))
            self.assertGreater(pdf[0].search_for('数值')[0].x0, pdf[0].search_for('阶段')[0].x0)

    def test_unreconstructed_equation_includes_original_pdf(self):
        self.doc['equations']['e1']['latex'] = r'\customTensor{T}'
        self.doc['equations']['e1']['raw'] = r'\(\customTensor{T}\)'
        with fitz.open() as original:
            original.new_page().insert_text((45,50),'Original typeset equation')
            original.save(self.folder/'original.pdf')
        with fitz.open(export(self.doc, 'chinese', self.folder)) as pdf:
            self.assertIn('原始 LaTeX', pdf[0].get_text())
            self.assertIn('Original typeset equation', pdf[-1].get_text())

    def test_original_equation_numbers_and_internal_links(self):
        source = self.folder/'source'
        source.mkdir()
        (source/'main.aux').write_text(r'\newlabel{eq:gravity}{{3.7}{12}{Title}{equation.3.7}{}}')
        self.doc['equations']['e1'].update(display=True, label='eq:gravity')
        self.doc['sections'][0]['paragraphs'].append({'id':'p3','kind':'text','sentences':[
            {'id':'s3','en':r'See \eqref{eq:gravity}.', 'zh':r'见式 \eqref{eq:gravity}。'}]})
        with fitz.open(export(self.doc,'chinese',self.folder)) as pdf:
            self.assertIn('3.7', pdf[0].get_text())
            self.assertTrue(pdf[0].get_links())


if __name__=='__main__':
    unittest.main()
