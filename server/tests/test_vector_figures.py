import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pymupdf as pdf
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from utils.vector_figures import (
    find_figures,
    prepare_vector_figures,
    restore_vector_figures,
)


def paper(path, caption):
    with pdf.open() as art, pdf.open() as doc:
        canvas = art.new_page(width=144, height=72)
        for i in range(9):
            x = 2 + i * 15
            canvas.draw_polyline(
                [(x, 45), (x + 10, 45), (x + 6, 23), (x, 45)], color=(0, 0, 0)
            )
        canvas.insert_text((3, 60), "Source gate   Destination node", fontsize=8)
        page = doc.new_page(width=240, height=200)
        page.insert_text((20, 25), "This paragraph should be translated.", fontsize=10)
        page.show_pdf_page(pdf.Rect(42, 50, 186, 122), art, 0)
        page.insert_text((34, 137), caption, fontsize=9)
        page.insert_text((21, 165), "Following paragraph.", fontsize=10)
        doc.save(path)


class VectorFiguresTests(unittest.TestCase):
    def test_stacked_display_derivation_keeps_vector_ink_and_surrounding_prose(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp, "derivation.pdf")
            with PdfWriter() as writer:
                page = writer.add_blank_page(width=300, height=280)
                faces = DictionaryObject()
                for key, name in (
                    ("/R", "/Helvetica"),
                    ("/M", "/CMMI10"),
                    ("/Big", "/CMEX10"),
                ):
                    faces[NameObject(key)] = writer._add_object(
                        DictionaryObject(
                            {
                                NameObject("/Type"): NameObject("/Font"),
                                NameObject("/Subtype"): NameObject("/Type1"),
                                NameObject("/BaseFont"): NameObject(name),
                                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                            }
                        )
                    )
                page[NameObject("/Resources")] = DictionaryObject(
                    {NameObject("/Font"): faces}
                )
                ops = ["BT /R 10 Tf 20 240 Td (By the first bound, we obtain) Tj ET"]
                for y in (215, 188, 160, 132):
                    ops.extend(
                        [
                            f"BT /Big 14 Tf 35 {y} Td (P) Tj ET",
                            f"BT /M 10 Tf 49 {y} Td (f+x-y+e+z-a+b+c=0) Tj ET",
                        ]
                    )
                ops.append("BT /R 10 Tf 20 95 Td (The next argument follows.) Tj ET")
                stream = DecodedStreamObject()
                stream.set_data("\n".join(ops).encode())
                page[NameObject("/Contents")] = writer._add_object(stream)
                writer.write(source)
            with prepare_vector_figures(str(source)) as (prepared, regions):
                self.assertEqual(len(regions), 1)
                restored = Path(tmp, "result.pdf")
                shutil.copyfile(prepared, restored)
                with pdf.open(prepared) as work:
                    self.assertIn("By the first bound", work[0].get_text())
                    self.assertIn("The next argument", work[0].get_text())
                    self.assertEqual(len(work[0].get_image_info()), 1)
                restore_vector_figures(source, [restored], regions)
                with pdf.open(restored) as result, pdf.open(source) as original:
                    self.assertEqual(
                        result[0].get_text(sort=True), original[0].get_text(sort=True)
                    )
                    pixels = [
                        np.asarray(
                            p[0]
                            .get_pixmap(matrix=pdf.Matrix(3, 3), colorspace=pdf.csGRAY)
                            .pil_image()
                        ).astype(np.int16)
                        for p in (original, result)
                    ]
                    # Nested vector transforms can shift antialias samples by a few grey levels.
                    self.assertLessEqual(np.abs(pixels[0] - pixels[1]).max(), 6)

    def test_only_math_rows_are_protected_between_table_rules(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp, "ruled-table.pdf")
            with PdfWriter() as writer:
                page = writer.add_blank_page(width=240, height=220)
                faces = DictionaryObject()
                for name, family in (("/Math", "/CMMI10"), ("/Body", "/Helvetica")):
                    faces[NameObject(name)] = writer._add_object(
                        DictionaryObject(
                            {
                                NameObject("/Type"): NameObject("/Font"),
                                NameObject("/Subtype"): NameObject("/Type1"),
                                NameObject("/BaseFont"): NameObject(family),
                            }
                        )
                    )
                page[NameObject("/Resources")] = DictionaryObject(
                    {NameObject("/Font"): faces}
                )
                data = DecodedStreamObject()
                data.set_data(b"""
                    0.5 w 20 180 m 220 180 l S 20 160 m 220 160 l S
                    20 140 m 220 140 l S 20 120 m 220 120 l S
                    BT /Body 9 Tf 28 166 Td (Approximation method) Tj ET
                    BT /Body 9 Tf 28 146 Td (ABC) Tj /Math 10 Tf 58 0 Td (x+y+z-a+b+c) Tj ET
                    BT /Body 9 Tf 28 126 Td (Descriptive text) Tj /Math 10 Tf 82 0 Td (x+y+z-a+b+c) Tj ET
                    BT /Body 9 Tf 40 104 Td (Table 2. Sensitivity results.) Tj ET
                """)
                page[NameObject("/Contents")] = writer._add_object(data)
                writer.write(path)
            figures = find_figures(path)
            self.assertEqual(len(figures), 1)
            box = figures[0].box
            self.assertTrue(box.contains(pdf.Point(110, 74)))
            for y in (54, 94, 116):
                self.assertFalse(box.contains(pdf.Point(110, y)))

    def test_diagram_excluded_from_text_then_restored_as_selectable_vectors_in_both_layouts(
        self,
    ):
        with TemporaryDirectory() as tmp:
            source = Path(tmp, "test.pdf")
            paper(source, "Fig. 1. Architecture of the system.")
            with prepare_vector_figures(str(source)) as (prepared, figures):
                self.assertEqual(len(figures), 1)
                mono = Path(tmp, "mono.pdf")
                dual = Path(tmp, "dual.pdf")
                shutil.copyfile(prepared, mono)
                with (
                    pdf.open(prepared) as work,
                    pdf.open(source) as original,
                    pdf.open() as out,
                ):
                    txt = work[0].get_text()
                    self.assertNotIn("Source gate", txt)
                    for phrase in ("This paragraph", "Fig. 1", "Following paragraph"):
                        self.assertIn(phrase, txt)
                    page = out.new_page(width=480, height=200)
                    page.show_pdf_page(original[0].rect, original, 0)
                    page.show_pdf_page(pdf.Rect(240, 0, 480, 200), work, 0)
                    out.save(dual)
                restore_vector_figures(source, [mono, dual], figures)
                for path, offset in [(mono, 0), (dual, 240)]:
                    with pdf.open(path) as result, pdf.open(source) as original:
                        expected = original[0].search_for("Destination node")[0]
                        actual = result[0].search_for(
                            "Destination node",
                            clip=pdf.Rect(offset, 0, offset + 240, 200),
                        )[0]
                        self.assertAlmostEqual(
                            actual.x0 - offset, expected.x0, places=3
                        )
                        self.assertAlmostEqual(actual.y0, expected.y0, places=3)
                        self.assertFalse(result[0].get_image_info())
                        self.assertGreaterEqual(len(result[0].get_drawings()), 9)

    def test_table_caption_and_skipped_pages_do_not_protect_body_or_table(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp, "table.pdf")
            paper(path, "Table 1. Units and values.")
            self.assertFalse(find_figures(path))
            paper(path, "Figure 1: Architecture.")
            self.assertFalse(find_figures(path, 1))
