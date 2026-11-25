"""Round-trip native layout inputs, including a fraction printed in ordinary type."""

import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pymupdf as pdf
from utils.compound_math import _same_ink, prepare_compound_math, restore_dual_source
from utils.inline_fractions import finalize_inline_fonts


def fraction_page(doc):
    page = doc.new_page(width=235, height=130)
    page.insert_font(
        fontname="Body", fontbuffer=pdf.Font("tiro").buffer, set_simple=True
    )
    page.insert_text((12, 49), "Given the ratio", fontname="Body", fontsize=10)
    page.insert_text((86, 44), "n", fontname="Body", fontsize=8)
    page.draw_line((85, 46), (92, 46), width=0.45)
    page.insert_text((85.5, 53), "m", fontname="Body", fontsize=8)
    page.insert_text((100, 49), "we obtain the result.", fontname="Body", fontsize=10)
    page.draw_line((12, 71), (155, 71), width=0.5)


class CompoundTests(unittest.TestCase):
    def test_dual_keeps_the_authored_page_and_the_correct_translation_in_either_order(
        self,
    ):
        with TemporaryDirectory() as directory:
            source = Path(directory, "source.pdf")
            with pdf.open() as doc:
                fraction_page(doc)
                doc.save(source)
            with prepare_compound_math(str(source)) as (prepared, maps):
                self.assertTrue(maps)
                with pdf.open(prepared) as marked, pdf.open() as translation:
                    marked[0].insert_text((20, 98), "Temporary source mark")
                    page = translation.new_page(width=255, height=138)
                    page.insert_text((15, 65), "Translated paragraph")
                    for mode in ("LR", "TB"):
                        for first in (False, True):
                            with self.subTest(mode=mode, translated_first=first):
                                target = Path(directory, f"{mode}-{first}.dual.pdf")
                                pair = (
                                    (translation, marked)
                                    if first
                                    else (marked, translation)
                                )
                                with pdf.open() as out:
                                    if mode == "LR":
                                        page = out.new_page(width=490, height=138)
                                        left = 0
                                        for doc in pair:
                                            right = left + doc[0].rect.width
                                            page.show_pdf_page(
                                                pdf.Rect(left, 0, right, 138), doc, 0
                                            )
                                            left = right
                                    else:
                                        for doc in pair:
                                            out.insert_pdf(doc)
                                    out.set_toc([[1, "Section", 1]])
                                    out.save(target)
                                options = SimpleNamespace(
                                    dual_mode=mode,
                                    trans_first=first,
                                    enhance_compatibility=False,
                                )
                                restore_dual_source(source, [target], options)
                                with (
                                    pdf.open(source) as original,
                                    pdf.open(target) as result,
                                ):
                                    self.assertEqual(
                                        result.get_toc(), [[1, "Section", 1]]
                                    )
                                    self.assertEqual(
                                        sum(
                                            p.get_text().count("Translated paragraph")
                                            for p in result
                                        ),
                                        1,
                                    )
                                    self.assertFalse(
                                        any(
                                            "Temporary source mark" in p.get_text()
                                            for p in result
                                        )
                                    )
                                    if mode == "LR":
                                        left = 255 if first else 0
                                        region = pdf.Rect(left, 4, left + 235, 134)
                                        image = result[0].get_pixmap(clip=region)
                                    else:
                                        image = result[int(first)].get_pixmap()
                                    self.assertEqual(
                                        image.digest, original[0].get_pixmap().digest
                                    )

    def test_fraction_is_one_measured_unit_and_original_ink_returns_on_both_sides(self):
        try:
            from babeldoc.rust_parser_service import parse_pdf_to_il
        except ImportError:
            self.skipTest("BabelDOC parser is in a separate translation environment")
        with TemporaryDirectory() as directory:
            source = Path(directory, "paper.pdf")
            with pdf.open() as doc:
                fraction_page(doc)
                fraction_page(doc)
                doc.save(source)
            with prepare_compound_math(str(source), skip_last=1) as (prepared, maps):
                self.assertEqual(len(maps), 1)
                page = parse_pdf_to_il(prepared, pages="1").page[0]
                marks = [g for g in page.pdf_character if g.char_unicode == "\ue000"]
                self.assertEqual(len(marks), 1)
                glyph = marks[0]
                self.assertAlmostEqual(glyph.pdf_style.font_size, 8, places=2)
                self.assertAlmostEqual(glyph.visual_bbox.box.x, glyph.box.x, places=2)
                self.assertAlmostEqual(
                    glyph.visual_bbox.box.x2 - glyph.box.x, glyph.advance, places=2
                )
                self.assertLess(glyph.visual_bbox.box.y2 - glyph.visual_bbox.box.y, 6)
                mono, dual = [
                    Path(directory, name + ".pdf") for name in ("mono", "dual")
                ]
                shutil.copyfile(prepared, mono)
                with pdf.open(prepared) as work, pdf.open() as out:
                    page = out.new_page(width=470, height=130)
                    page.show_pdf_page(pdf.Rect(0, 0, 235, 130), work, 1)
                    page.show_pdf_page(pdf.Rect(235, 0, 470, 130), work, 0)
                    out.save(dual)
                finalize_inline_fonts([mono, dual], maps)
                with (
                    pdf.open(source) as original,
                    pdf.open(mono) as restored,
                    pdf.open(dual) as spread,
                ):
                    self.assertTrue(_same_ink(original[0], restored[0]))
                    self.assertEqual(
                        original[1].get_pixmap().digest, restored[1].get_pixmap().digest
                    )
                    self.assertIn("we obtain the result", restored[0].get_text())
                    self.assertNotIn("\ue000", restored[0].get_text())
                    # The copy inside the dual is reached through nested Forms.
                    right = spread[0].get_pixmap(clip=pdf.Rect(235, 0, 470, 130))
                    self.assertEqual(right.digest, restored[0].get_pixmap().digest)
