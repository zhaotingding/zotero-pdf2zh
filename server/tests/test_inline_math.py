import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf as pdf
from utils.inline_fractions import finalize_inline_fonts, prepare_inline_fractions


class InlineMathTests(unittest.TestCase):
    def test_formulas_and_prose_can_share_one_text_show_and_one_font(self):
        for composite in (False, True):
            with self.subTest(composite=composite), TemporaryDirectory() as tmp:
                path = Path(tmp) / "source.pdf"
                output = Path(tmp) / "dual.pdf"
                with pdf.open() as doc:
                    page = doc.new_page()
                    page.insert_font(
                        fontname="Body",
                        fontbuffer=pdf.Font("helv").buffer,
                        set_simple=not composite,
                    )
                    page.insert_text(
                        (40, 60),
                        "Compare H(x), Q(kernel(t), y) and G(.) with prose (see text).",
                        fontname="Body",
                    )
                    page.insert_text(
                        (40, 95),
                        "The standalone caret x ^ y is ordinary text.",
                        fontname="Body",
                    )
                    doc.save(path)
                with prepare_inline_fractions(str(path)) as (prepared, maps):
                    self.assertTrue(maps)
                    with (
                        pdf.open(prepared) as work,
                        pdf.open(path) as source,
                        pdf.open() as dual,
                    ):
                        self.assertEqual(
                            source[0].get_pixmap().digest, work[0].get_pixmap().digest
                        )
                        marked = work[0].get_text()
                        self.assertIn("prose (see text)", marked)
                        self.assertIn("caret x ^ y", marked)
                        for call in ("H(x)", "Q(kernel(t), y)", "kernel(t)", "G(.)"):
                            self.assertNotIn(call, marked)
                        page = dual.new_page(
                            width=source[0].rect.width * 2, height=source[0].rect.height
                        )
                        page.show_pdf_page(source[0].rect, source, 0)
                        page.show_pdf_page(
                            source[0].rect
                            + (source[0].rect.width, 0, source[0].rect.width, 0),
                            work,
                            0,
                        )
                        dual.save(output)
                        expected = source[0].get_text()
                    finalize_inline_fonts([output], maps)
                    with pdf.open(output) as restored:
                        self.assertEqual(restored[0].get_text(), expected * 2)

    def test_same_baseline_hat_in_either_paint_order_and_unrelated_prose(self):
        for mark_first in (False, True):
            with self.subTest(mark_first=mark_first), TemporaryDirectory() as tmp:
                path = Path(tmp) / "source.pdf"
                with pdf.open() as doc:
                    page = doc.new_page()
                    page.insert_text((40, 65), "The estimate is", fontsize=10)
                    letters = [(114, "^"), (112, "x")]
                    for x, text in letters if mark_first else reversed(letters):
                        page.insert_text((x, 65), text, fontsize=10)
                    page.insert_text((125, 65), "and a separate x ^ y.", fontsize=10)
                    doc.save(path)
                with prepare_inline_fractions(str(path)) as (prepared, maps):
                    self.assertTrue(maps)
                    with pdf.open(prepared) as result:
                        self.assertEqual(result[0].get_text().count("\ue000"), 2)
                        self.assertIn("a separate x ^ y", result[0].get_text())

    def test_unrelated_parentheses_and_display_math_remain_native(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.pdf"
            with pdf.open() as doc:
                page = doc.new_page()
                page.insert_text(
                    (40, 65), "The comparison (see results) is in Section (A)."
                )
                page.insert_text((140, 130), "H(x)")
                doc.save(path)
            with prepare_inline_fractions(str(path)) as (prepared, maps):
                self.assertEqual(prepared, str(path))
                self.assertFalse(maps)

    def test_form_only_page_keeps_its_original_glyphs_and_text(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested.pdf"
            with pdf.open() as inner, pdf.open() as outer:
                inside = inner.new_page()
                inside.insert_text((40, 65), "The heatmap is H(x) on this page.")
                page = outer.new_page()
                page.show_pdf_page(page.rect, inner, 0)
                outer.save(path)
            with prepare_inline_fractions(str(path)) as (prepared, maps):
                self.assertTrue(maps)
                with pdf.open(path) as original, pdf.open(prepared) as marked:
                    self.assertEqual(
                        marked[0].get_pixmap().digest, original[0].get_pixmap().digest
                    )
                    self.assertIn("\ue000" * 4, marked[0].get_text())
                finalize_inline_fonts([prepared], maps)
                with pdf.open(prepared) as restored:
                    self.assertIn("H(x)", restored[0].get_text())
