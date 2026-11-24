"""Offline structural tests for identified inline fractions; no external fonts or API."""

import shutil
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf as pdf
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from utils.inline_fractions import prepare_inline_fractions, restore_commas


def write_fixture(
    path, with_prose=True, with_commas=True, math_face="/CMMI5", absolute_bar=False
):
    writer = PdfWriter()
    cmap = DecodedStreamObject()
    cmap.set_data(b"""begincmap
1 begincodespacerange <00> <ff> endcodespacerange
4 beginbfchar
<2C> <002C>
<61> <0061>
<70> <0070>
<74> <0074>
endbfchar
endcmap""")

    def font(name):
        return DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject(name),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )

    math = font(math_face)
    math[NameObject("/ToUnicode")] = writer._add_object(cmap)
    fonts = DictionaryObject(
        {
            NameObject("/M"): writer._add_object(math),
            NameObject("/T"): writer._add_object(font("/Helvetica")),
        }
    )
    for _ in range(2):
        page = writer.add_blank_page(width=200, height=300)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
        stream = DecodedStreamObject()
        # Two subscript commas in the denominator. Another comma in the same
        # math font, and ordinary punctuation in the prose, are controls.
        text = b"BT /T 10 Tf 1 0 0 1 45 198 Tm (where, ) Tj ET\n" if with_prose else b""
        stream.set_data(
            text
            + b"""
BT /M 7 Tf 1 0 0 1 99 203 Tm (t) Tj ET
0.4 w q 1 0 0 1 80 200 cm 0 0 m 45 0 l S Q
BT /M 7 Tf 1 0 0 1 81 195 Tm (a) Tj /M 5 Tf 1 0 0 1 85 193 Tm (,p) Tj
1 0 0 1 108 193 Tm [(,p)] TJ ET
BT /M 5 Tf 1 0 0 1 45 140 Tm (,p) Tj ET
0.4 w q 1 0 0 1 20 110 cm 0 0 m 100 0 l S Q
"""
        )
        if not with_commas:
            stream.set_data(stream.get_data().replace(b"(,p)", b"(ap)", 2))
        if absolute_bar:
            stream.set_data(
                stream.get_data().replace(
                    b"q 1 0 0 1 80 200 cm 0 0 m 45 0 l S Q", b"80 200 m 125 200 l S"
                )
            )
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_outline_item(title="Section", page_number=0)
    writer.write(path)
    writer.close()


class InlineFractionTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "paper.pdf"
        write_fixture(self.source)

    def test_mark_only_math_commas_keep_glyphs_and_restore_nested_dual(self):
        before = sha256(self.source.read_bytes()).digest()
        with pdf.open(self.source) as doc:
            text = doc[0].get_text()
            pixels = doc[0].get_pixmap().digest
            bookmarks = doc.get_toc()
        with prepare_inline_fractions(str(self.source), skip_last=1) as (
            prepared,
            maps,
        ):
            self.assertTrue(maps)
            self.assertEqual(Path(prepared).name, self.source.name)
            mono = self.root / "mono.pdf"
            shutil.copyfile(prepared, mono)
            dual = self.root / "dual.pdf"
            with pdf.open(mono) as doc, pdf.open() as out:
                self.assertEqual(doc[0].get_pixmap().digest, pixels)
                self.assertEqual(doc[0].get_text().count("﹐"), 2)
                self.assertEqual(doc[0].get_text().replace("﹐", ","), text)
                self.assertEqual(doc[1].get_text(), text)
                self.assertEqual([d["type"] for d in doc[0].get_drawings()], ["f", "s"])
                self.assertEqual([d["type"] for d in doc[1].get_drawings()], ["s", "s"])
                page = out.new_page(width=400, height=300)
                page.show_pdf_page(pdf.Rect(0, 0, 200, 300), doc, 1)
                page.show_pdf_page(pdf.Rect(200, 0, 400, 300), doc, 0)
                out.save(dual)
            restore_commas([mono, dual], maps)
            with pdf.open(mono) as doc, pdf.open(dual) as out:
                self.assertEqual(doc[0].get_text(), text)
                self.assertEqual(out[0].get_text().count(","), 2 * text.count(","))
                self.assertNotIn("﹐", out[0].get_text())
                self.assertEqual(doc.get_toc(), bookmarks)
        self.assertFalse(Path(prepared).exists())
        self.assertEqual(sha256(self.source.read_bytes()).digest(), before)

    def test_display_only_fractions_and_skipped_pages_are_untouched(self):
        for prose, skip in [(False, 0), (True, 2)]:
            with self.subTest(prose=prose, skip=skip):
                write_fixture(self.source, prose)
                with prepare_inline_fractions(str(self.source), skip) as (
                    prepared,
                    maps,
                ):
                    self.assertEqual(prepared, str(self.source))
                    self.assertFalse(maps)

    def test_plain_denominator_moves_with_fraction_without_private_comma_maps(self):
        write_fixture(self.source, with_commas=False)
        with prepare_inline_fractions(str(self.source), skip_last=1) as (
            prepared,
            maps,
        ):
            self.assertNotEqual(str(prepared), str(self.source))
            self.assertFalse(maps)
            with pdf.open(prepared) as doc, pdf.open(self.source) as source:
                self.assertEqual([d["type"] for d in doc[0].get_drawings()], ["f", "s"])
                self.assertEqual([d["type"] for d in doc[1].get_drawings()], ["s", "s"])
                self.assertEqual(doc[0].get_text(), source[0].get_text())
                self.assertEqual(
                    doc[0].get_pixmap().digest, source[0].get_pixmap().digest
                )

    def test_other_font_and_absolute_rule_restore_subscript_commas(self):
        write_fixture(self.source, math_face="/Helvetica", absolute_bar=True)
        with prepare_inline_fractions(str(self.source)) as (prepared, maps):
            self.assertTrue(maps)
            with pdf.open(prepared) as work, pdf.open(self.source) as original:
                self.assertEqual(work[0].get_text().count("\ue000"), 2)
                self.assertEqual(
                    work[0].get_pixmap().digest, original[0].get_pixmap().digest
                )
                self.assertEqual(
                    [draw["type"] for draw in work[0].get_drawings()], ["f", "s"]
                )
                text = original[0].get_text()
            restore_commas([prepared], maps)
            with pdf.open(prepared) as work:
                self.assertEqual(work[0].get_text(), text)

    def test_server_restores_text_before_downstream_repairs(self):
        from server import PDFTranslator

        service = PDFTranslator(SimpleNamespace(enable_venv=False))
        config = SimpleNamespace(skip_last_pages=1)
        output = self.root / "output.mono.pdf"

        def engine(prepared, settings, task):
            self.assertNotEqual(str(prepared), str(self.source))
            with pdf.open(prepared) as doc:
                self.assertEqual(doc[0].get_text().count("﹐"), 2)
            shutil.copyfile(prepared, output)
            return [str(output)]

        def check_output(original, paths):
            self.assertEqual(original, str(self.source))
            with pdf.open(paths[0]) as result, pdf.open(original) as source:
                self.assertEqual(result[0].get_text(), source[0].get_text())

        with (
            patch.object(service, "_run_pdf2zh_next", side_effect=engine) as run,
            patch(
                "server.repair_translation_outputs", side_effect=check_output
            ) as downstream,
        ):
            self.assertEqual(
                service.translate_pdf_next(str(self.source), config, "job"),
                [str(output)],
            )
            run.assert_called_once()
            downstream.assert_called_once()
            self.assertFalse(Path(run.call_args.args[0]).exists())

    def test_visual_mismatch_reverts_only_affected_page(self):
        with (
            patch("utils.inline_fractions.same_appearance", side_effect=[False, True]),
            prepare_inline_fractions(str(self.source)) as (prepared, maps),
            pdf.open(prepared) as doc,
        ):
            self.assertTrue(maps)
            self.assertEqual([s["type"] for s in doc[0].get_drawings()], ["s", "s"])
            self.assertEqual([s["type"] for s in doc[1].get_drawings()], ["f", "s"])
            self.assertNotIn("﹐", doc[0].get_text())
            self.assertIn("﹐", doc[1].get_text())
