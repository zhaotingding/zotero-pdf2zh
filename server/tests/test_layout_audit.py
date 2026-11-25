"""Export checks use real PDF text, path paint order and rendered output."""

import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pymupdf as pdf
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from utils.auto_outline import add_review_bookmarks
from utils.layout_audit import PageInk, inspect_page
from utils.layout_repair import check_output


def fixture(path, damaged=False, ambiguous=False):
    def text(x, y, value, face="M", size=10):
        return f"BT /{face} {size} Tf 1 0 0 1 {x} {y} Tm ({value}) Tj ET\n"

    def rule(x, y, width):
        return f"0 G .55 w {x} {y} m {x + width} {y} l S\n"

    with PdfWriter() as writer:
        fonts = DictionaryObject()
        for key, name in (("/M", "/CMMI10"), ("/T", "/Helvetica")):
            fonts[NameObject(key)] = writer._add_object(
                DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Font"),
                        NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject(name),
                        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                    }
                )
            )

        def page(data):
            obj = writer.add_blank_page(300, 240)
            obj[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): fonts}
            )
            stream = DecodedStreamObject()
            stream.set_data(data.encode("ascii"))
            obj[NameObject("/Contents")] = writer._add_object(stream)

        # Two stacked operands move together; a broken export leaves the line in the prose.
        def operands(x):
            return text(x + 2, 173, "5", size=8) + text(x, 164, "mn", size=8)

        x = 155 if damaged else 65
        page(
            text(19, 170, "Given:", face="T")
            + operands(x)
            + rule(65, 171, 11)
            + (operands(207) if ambiguous else "")
            + text(32, 132, "Body text survives.", face="T")
        )
        bg = "q .7 .84 1 rg 80 121 112 21 re f Q\n"
        equation = text(52 if damaged else 85, 128, "w=q+1+x")
        page(
            text(20, 172, "Algorithm 1. Updates", face="T")
            + text(27, 128, "8:", face="T")
            + (equation + bg if damaged else bg + equation)
            + text(30, 113, "End loop", face="T")
        )
        table = text(99, 197, "Table 2. Comparison", face="T")
        for y in [184, 168, 140, 108]:
            table += rule(60 if y == 140 else 20, y, 220 if y == 140 else 260)
        table += ".5 w 60 108 m 60 184 l S 132 108 m 132 184 l S\n"
        table += text(70, 173, "Method", face="T") + text(
            150, 173, "Convex case", face="T"
        )
        table += text(67, 151, "Method-X", face="T") + text(
            111 if damaged else 156, 151, "x+y-z=abc"
        )
        table += text(69, 121, "Other", face="T") + text(152, 121, "w+x-a=bcd")
        page(table)
        writer.add_outline_item(title="First section", page_number=0)
        writer.write(path)


class LayoutAuditTests(unittest.TestCase):
    def test_overpaint_on_the_real_spread_cannot_pass_via_an_intact_inner_form(self):
        with TemporaryDirectory() as tmp:
            paper = Path(tmp, "paper.pdf")
            source = Path(tmp, "algorithm.pdf")
            target = Path(tmp, "reader.dual.pdf")
            fixture(paper)
            with pdf.open(paper) as all_pages, pdf.open() as single:
                single.insert_pdf(all_pages, from_page=1, to_page=1)
                single.save(source)
            with pdf.open(source) as original, pdf.open() as dual:
                page = dual.new_page(width=600, height=240)
                for left in (0, 300):
                    page.show_pdf_page(pdf.Rect(left, 0, left + 300, 240), original, 0)
                # The late rectangle is in the enclosing spread, not the math Form.
                page.draw_rect(
                    pdf.Rect(80, 98, 192, 119), color=None, fill=(0.7, 0.84, 1)
                )
                dual.save(target)
            before = sha256(target.read_bytes()).digest()
            count, review = check_output(source, target, "LR", True)
            self.assertEqual(count, 0)
            self.assertIn("彩底公式", review[1])
            self.assertEqual(sha256(target.read_bytes()).digest(), before)

    def test_recognizes_and_repairs_paint_and_cell_violations(self):
        with TemporaryDirectory() as tmp:
            src, out = Path(tmp, "source.pdf"), Path(tmp, "result.mono.pdf")
            fixture(src)
            fixture(out, True)
            with pdf.open(src) as original, pdf.open(out) as old:
                for i, expected in enumerate(("分数线", "彩底公式", "表格数学单元格")):
                    self.assertIn(
                        expected,
                        [
                            v.kind
                            for v in inspect_page(PageInk(original[i]), PageInk(old[i]))
                        ],
                    )
            count, warnings = check_output(src, out)
            self.assertEqual(warnings, {})
            self.assertGreaterEqual(count, 3)
            self.assertEqual(check_output(src, out, repair=False), (0, {}))
            with pdf.open(src) as original, pdf.open(out) as result:
                bars = PageInk(result[0])
                self.assertTrue(bars.has_bar(pdf.Rect(155, 68.725, 166, 69.275)))
                self.assertFalse(bars.has_bar(pdf.Rect(65, 68.725, 76, 69.275)))
                self.assertIn("End loop", result[1].get_text())
                self.assertEqual(result.get_toc(), original.get_toc())
                for i, rect in [
                    (1, pdf.Rect(81, 99, 190, 118)),
                    (2, pdf.Rect(134, 73, 278, 98)),
                ]:
                    self.assertEqual(
                        original[i].get_pixmap(clip=rect).digest,
                        result[i].get_pixmap(clip=rect).digest,
                    )

    def test_ambiguous_destination_keeps_existing_art_and_points_to_source_page(self):
        with TemporaryDirectory() as tmp:
            src, out = Path(tmp, "source.pdf"), Path(tmp, "test.mono.pdf")
            fixture(src)
            fixture(out, True, True)
            count, warnings = check_output(src, out)
            self.assertIn("分数线", warnings[1])
            self.assertGreater(count, 0)
            config = SimpleNamespace(
                trans_first=False, enhance_compatibility=False, dual_mode="LR"
            )
            service = SimpleNamespace(get_filetype=lambda _: "mono")
            for _ in range(2):
                add_review_bookmarks([out], {"mono": warnings}, config, service)
            with pdf.open(out) as doc:
                self.assertTrue(
                    PageInk(doc[0]).has_bar(pdf.Rect(65, 68.725, 76, 69.275))
                )
                titles = doc.get_toc()
                self.assertEqual(sum("排版待核对" in v[1] for v in titles), 1)
                self.assertEqual(titles[-1], [2, "原文第 1 页 · 分数线", 1])

    def test_spreads_and_alternating_pages_edit_only_the_translated_side(self):
        with TemporaryDirectory() as tmp:
            src, bad = Path(tmp, "source.pdf"), Path(tmp, "bad.pdf")
            fixture(src)
            fixture(bad, True)
            for mode, first in [
                ("LR", False),
                ("LR", True),
                ("TB", False),
                ("TB", True),
            ]:
                with self.subTest(mode=mode, first=first):
                    out = Path(tmp, f"{mode}-{first}.pdf")
                    with (
                        pdf.open(src) as original,
                        pdf.open(bad) as translated,
                        pdf.open() as result,
                    ):
                        docs = (
                            (translated, original) if first else (original, translated)
                        )
                        for i in range(len(original)):
                            if mode == "LR":
                                page = result.new_page(width=600, height=240)
                                for n, doc in enumerate(docs):
                                    page.show_pdf_page(
                                        pdf.Rect(n * 300, 0, (n + 1) * 300, 240), doc, i
                                    )
                            else:
                                for doc in docs:
                                    result.insert_pdf(doc, from_page=i, to_page=i)
                        result.save(out)
                    count, warnings = check_output(src, out, mode, first)
                    self.assertEqual(warnings, {})
                    self.assertGreaterEqual(count, 3)
                    with pdf.open(src) as original, pdf.open(out) as result:
                        for i in range(3):
                            if mode == "LR":
                                x = int(first) * 300
                                image = result[i].get_pixmap(
                                    clip=pdf.Rect(x, 0, x + 300, 240)
                                )
                            else:
                                image = result[2 * i + int(first)].get_pixmap()
                            self.assertEqual(
                                original[i].get_pixmap().digest, image.digest
                            )
