import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf as pdf
from utils.inline_fractions import finalize_inline_fonts, prepare_inline_fractions
from utils.inline_math import _codes, math_ranges


class InlineMathTests(unittest.TestCase):
    def test_native_parser_preserves_standard_font_advances_after_marking(self):
        # The PDF viewer can render a standard font correctly even when the translation
        # engine has collapsed all of its protected character advances to zero.
        try:
            from babeldoc.rust_parser_service import parse_pdf_to_il
        except ImportError:
            self.skipTest("BabelDOC parser is in a separate translation environment")
        with TemporaryDirectory() as tmp:
            path = Path(tmp, "core-widths.pdf")
            with pdf.open() as doc:
                page = doc.new_page()
                for i, name in enumerate(("helv", "tiro", "cobo")):
                    page.insert_text(
                        (40, 60 + 25 * i),
                        "The result is F(a) = x = 0.25 today.",
                        fontname=name,
                    )
                doc.save(path)
            before = parse_pdf_to_il(path).page[0].pdf_character
            with prepare_inline_fractions(str(path)) as (prepared, maps):
                self.assertTrue(maps)
                after = parse_pdf_to_il(prepared).page[0].pdf_character
                self.assertGreater(sum(c.char_unicode == "\ue000" for c in after), 15)
                for a, b in zip(before, after, strict=True):
                    self.assertEqual(a.pdf_character_id, b.pdf_character_id)
                    self.assertAlmostEqual(a.advance, b.advance, places=3)
                    self.assertAlmostEqual(a.box.x, b.box.x, places=3)
                finalize_inline_fonts([prepared], maps)
                with pdf.open(path) as source, pdf.open(prepared) as output:
                    self.assertEqual(source[0].get_text(), output[0].get_text())
                    self.assertEqual(
                        source[0].get_pixmap().digest, output[0].get_pixmap().digest
                    )

    def test_recognizes_decorated_and_radical_operands_without_changing_literals(self):
        for expression in (
            "Enc(u) = ˆy",
            "√(a+b) = y",
            "z = √(a+b)",
            "3.25 <= q ≈ f(i)",
        ):
            with self.subTest(expression=expression):
                text = (
                    "We obtain " + expression + " in this case; mode=fast is a setting."
                )
                ranges = [
                    text[start:end]
                    for kind, start, end in math_ranges(text)
                    if kind == "relation"
                ]
                self.assertEqual(ranges, [expression])

    def test_relations_join_balanced_operands_arithmetic_and_chains(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp, "relations.pdf")
            cases = ("F(k) + 0.25 = q = (u - v)", "(a+b) <= G(c)", "w = -2.75")
            with pdf.open() as doc:
                page = doc.new_page()
                for i, expr in enumerate(cases):
                    page.insert_text(
                        (40, 60 + i * 24),
                        "Compare " + expr + " in the paper.",
                        fontsize=10,
                    )
                page.insert_text(
                    (40, 150), "The setting mode=fast and the label (A) remain text."
                )
                doc.save(path)
            with prepare_inline_fractions(str(path)) as (prepared, maps):
                self.assertTrue(maps)
                with pdf.open(prepared) as marked, pdf.open(path) as source:
                    lines = marked[0].get_text().splitlines()
                    for line in lines[: len(cases)]:
                        math = line.removeprefix("Compare ").removesuffix(
                            " in the paper."
                        )
                        self.assertEqual(set(math.replace(" ", "")), {"\ue000"}, line)
                    self.assertIn("mode=fast", lines[-1])
                    self.assertIn("(A)", lines[-1])
                    self.assertEqual(
                        source[0].get_pixmap().digest, marked[0].get_pixmap().digest
                    )
                finalize_inline_fonts([prepared], maps)
                with pdf.open(prepared) as restored, pdf.open(path) as original:
                    self.assertEqual(restored[0].get_text(), original[0].get_text())

    def test_equation_keeps_original_glyphs_with_scripts_in_either_order(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp, "equations.pdf")
            with pdf.open() as doc:
                page = doc.new_page()
                page.insert_font(fontname="Math", fontbuffer=pdf.Font("tiro").buffer)
                for y, upper_first in [(60, True), (90, False)]:
                    page.insert_text((22, y), "Given", fontsize=10)
                    page.insert_text((57, y), "v", fontsize=10, fontname="tiit")
                    scripts = [("′", -6.2, 5.5), ("j", 2.7, 7.3)]
                    if not upper_first:
                        scripts.reverse()
                    for char, shift, size in scripts:
                        page.insert_text(
                            (62, y + shift), char, fontsize=size, fontname="Math"
                        )
                    page.insert_text(
                        (68, y),
                        "=" if upper_first else "≤",
                        fontsize=10,
                        fontname="Math",
                    )
                    page.insert_text((77, y), "F", fontsize=10)
                    page.insert_text((83.5, y + 2), "k", fontsize=7)
                    page.insert_text((88, y), "(u)", fontsize=10)
                    page.insert_text((109, y), "in this case.", fontsize=10)
                    page.insert_text((160, y), "q", fontname="tiit", fontsize=10)
                    for char, shift, size in scripts:
                        page.insert_text(
                            (165.6, y + shift), char, fontsize=size, fontname="Math"
                        )
                    page.insert_text((172, y), "ends here.", fontsize=10)
                page.insert_text((30, 125), "The literal setting mode=fast is text.")
                doc.save(path)
            with prepare_inline_fractions(str(path)) as (prepared, maps):
                with pdf.open(prepared) as marked, pdf.open(path) as original:

                    def glyphs(page):
                        return [
                            (c[0], c[2])
                            for s in page.get_texttrace()
                            for c in s["chars"]
                        ]

                    for (before, pos), (after, _) in zip(
                        glyphs(original[0]), glyphs(marked[0]), strict=True
                    ):
                        if chr(before) == " ":
                            continue
                        is_math = (56 <= pos[0] < 107 or 160 <= pos[0] < 170) and pos[
                            1
                        ] < 100
                        self.assertEqual(
                            after, 0xE000 if is_math else before, (chr(before), pos)
                        )
                    self.assertEqual(
                        marked[0].get_pixmap().digest, original[0].get_pixmap().digest
                    )
                finalize_inline_fonts([prepared], maps)
                with pdf.open(prepared) as restored, pdf.open(path) as original:
                    self.assertEqual(glyphs(restored[0]), glyphs(original[0]))

    def test_variable_with_multiletter_subscript_or_exponent_remains_one_math_run(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp, "variables.pdf")
            with pdf.open() as doc:
                page = doc.new_page()
                for y, base, script, shift in [
                    (65, "u", "j2", 1.7),
                    (105, "k", "3", -3.1),
                ]:
                    page.insert_text((40, y), "The estimate is", fontsize=10)
                    page.insert_text((118, y), base, fontsize=10, fontname="tiit")
                    x = 118 + pdf.get_text_length(base, fontname="tiit", fontsize=10)
                    page.insert_text(
                        (x + 0.2, y + shift), script, fontsize=7, fontname="cour"
                    )
                    page.insert_text((150, y), "for this sample.", fontsize=10)
                page.insert_text((40, 155), "A normal word", fontsize=10)
                x = 40 + pdf.get_text_length("A normal word", fontsize=10)
                page.insert_text((x, 152), "1", fontsize=7)
                page.insert_text(
                    (40, 180), "Different sizes with a shared baseline:", fontsize=10
                )
                page.insert_text((240, 180), "v", fontsize=10)
                page.insert_text((245, 180), "s", fontsize=7)
                doc.save(path)
            with prepare_inline_fractions(str(path)) as (prepared, maps):
                self.assertTrue(maps)
                with pdf.open(prepared) as marked, pdf.open(path) as original:
                    self.assertEqual(
                        marked[0].get_pixmap().digest, original[0].get_pixmap().digest
                    )
                    masked = [
                        c
                        for s in marked[0].get_texttrace()
                        for c in s["chars"]
                        if c[0] == 0xE000
                    ]
                    self.assertEqual(len(masked), 5)
                    self.assertTrue(all(c[2][1] < 120 for c in masked))
                finalize_inline_fonts([prepared], maps)
                with pdf.open(prepared) as restored, pdf.open(path) as original:
                    self.assertEqual(restored[0].get_text(), original[0].get_text())

    def test_named_glyphs_without_unicode_map_follow_the_rendered_character(self):
        encoding = {47: "/", 65: "/y.alt", 66: "/afii10069.ital", 67: "/NoSuchGlyph"}
        self.assertEqual(
            _codes(b"/ABC", encoding, {}),
            [(b"/", "/"), (b"A", "y"), (b"B", "д"), (b"C", "/NoSuchGlyph")],
        )
        self.assertEqual(_codes(b"A", encoding, {"/y.alt": "Y"}), [(b"A", "Y")])

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
