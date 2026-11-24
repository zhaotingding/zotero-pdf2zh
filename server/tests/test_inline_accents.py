"""Overprinted TeX accents with ordinary, display and multi-letter controls."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf as pdf
from utils.inline_fractions import finalize_inline_fonts, prepare_inline_fractions


def accent_fixture(path, accent, base_text="f", dx=0):
    # Use the renderer's bundled faces to exercise actual Type 1 resources and Tj/TJ.
    with pdf.open() as doc:
        page = doc.new_page(width=230, height=220)
        page.insert_text((20, 65), "The vector is", fontsize=10)
        for resource, builtin, tex, text, offset, y in [
            ("Hat", "tiro", "CMR10", accent, dx, -2.5),
            ("Letter", "tibo", "CMBX10", base_text, 0, 0),
        ]:
            ref = page.insert_font(
                fontname=resource, fontbuffer=pdf.Font(builtin).buffer, set_simple=True
            )
            for baseline in (65, 140):
                page.insert_text(
                    (81 + offset, baseline + y), text, fontname=resource, fontsize=10
                )
            doc.xref_set_key(ref, "BaseFont", "/" + tex)
            desc = int(doc.xref_get_key(ref, "FontDescriptor")[1].split()[0])
            doc.xref_set_key(desc, "FontName", "/" + tex)
        # Draw in source reading order (mark then base at each occurrence).
        text, hat1, hat2, base1, base2 = page.get_contents()
        doc.xref_set_key(
            page.xref,
            "Contents",
            "["
            + " ".join(f"{ref} 0 R" for ref in [text, hat1, base1, hat2, base2])
            + "]",
        )
        doc.save(path)


class InlineAccentTests(unittest.TestCase):
    def test_output_cleaner_keeps_width_declaration_first(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "accent.pdf"
            out = path.with_name("mono.pdf")
            accent_fixture(path, "^")
            with prepare_inline_fractions(str(path)) as (prepared, maps):
                with pdf.open(prepared) as doc:
                    font = next(f for f in doc[0].get_fonts() if f[2] == "Type3")
                    ref = int(
                        doc.xref_get_key(font[0], "CharProcs/accent")[1].split()[0]
                    )
                    program = doc.xref_stream(ref)
                    doc.update_stream(ref, b"q " + program + b" Q")
                    doc.save(out)
                finalize_inline_fonts([out], maps)
                with pdf.open(out) as result:
                    operators = result.xref_stream(ref).split()
                    self.assertEqual(operators[6:8], [b"d1", b"q"])
                    self.assertIn("f\u0302", result[0].get_text())
                before = out.stat().st_size
                finalize_inline_fonts([out], maps)
                self.assertEqual(out.stat().st_size, before)

    def test_overprinted_hat_is_a_single_searchable_glyph(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "accent.pdf"
            accent_fixture(path, "^")
            with prepare_inline_fractions(str(path)) as (prepared, maps):
                self.assertNotEqual(prepared, str(path))
                self.assertFalse(maps)
                with pdf.open(prepared) as fixed, pdf.open(path) as original:
                    page = fixed[0]
                    region = pdf.Rect(0, 40, 200, 83)
                    self.assertIn("f\u0302", page.get_textbox(region))
                    self.assertEqual(
                        sum(ft[2] == "Type3" for ft in page.get_fonts()), 1
                    )
                    display = pdf.Rect(60, 110, 120, 160)
                    self.assertEqual(
                        page.get_pixmap(clip=display).digest,
                        original[0].get_pixmap(clip=display).digest,
                    )
                    self.assertEqual(
                        page.get_textbox(display), original[0].get_textbox(display)
                    )

    def test_separate_superscript_and_whole_word_keep_native_paint(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.pdf"
            for word, dx in [("f", 12), ("foo", 0)]:
                with self.subTest(word=word, dx=dx):
                    accent_fixture(path, "^", word, dx)
                    with prepare_inline_fractions(str(path)) as (prepared, _):
                        self.assertEqual(prepared, str(path))
