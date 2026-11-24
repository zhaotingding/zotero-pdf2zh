"""Render small publisher-style PDFs through the translation output repair hook."""

import unittest
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf as pdf
from PIL import ImageDraw
from utils.page_overlay import repair_translation_outputs

WIDTH, HEIGHT = 320, 400
BODY = pdf.Rect(10, 20, 310, 305)
FIGURE = pdf.Rect(26, 322, 150, 357)
ENGLISH = "An original sentence about facial representations."
CHINESE = "这是该论文的中文译文，用于核对图片下面的文字是否正常显示。"


def draw_text(page, text, render_mode=0):
    for row in range(18):
        page.insert_text(
            (18, 38 + row * 15),
            text,
            fontsize=9,
            fontname="china-s" if text == CHINESE else "helv",
            render_mode=render_mode,
        )


def render(page, rect):
    return page.get_pixmap(clip=rect, annots=False).digest


class PageOverlayTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.source = Path(tmp.name) / "source.pdf"
        self.output = Path(tmp.name) / "translated.pdf"
        with pdf.open() as doc:
            page = doc.new_page(width=WIDTH, height=HEIGHT)
            draw_text(page, ENGLISH)
            # 图形只存在于整页位图，用像素校验能发现直接删图的错误。
            raster = page.get_pixmap().pil_image()
            ImageDraw.Draw(raster).rectangle(tuple(FIGURE), fill=(24, 119, 230))
            stream = BytesIO()
            raster.save(stream, format="PNG")
            self.image = stream.getvalue()
            page.insert_image(page.rect, stream=self.image)
            doc.save(self.source, deflate_images=True)
        with pdf.open() as reference:
            page = reference.new_page(width=WIDTH, height=HEIGHT)
            draw_text(page, CHINESE)
            self.expected_text = render(page, BODY)

    def write_translation(
        self, *, split=False, unpack=False, hidden=False, background=False
    ):
        with pdf.open() as doc:
            page = doc.new_page(width=WIDTH, height=HEIGHT)
            if background:
                page.insert_image(page.rect, stream=self.image)
            draw_text(page, CHINESE, render_mode=3 if hidden else 0)
            if not background:
                image_ref = page.insert_image(page.rect, stream=self.image)
                if unpack:
                    # 相同像素分别使用 Flate 压缩和未经压缩的 PDF 图像流。
                    samples = doc.xref_stream(image_ref)
                    doc.update_stream(image_ref, samples, compress=False)
            if not split:
                stream = b"\n".join(doc.xref_stream(ref) for ref in page.get_contents())
                ref = doc.get_new_xref()
                doc.update_object(ref, "<<>>")
                doc.update_stream(ref, stream)
                page.set_contents(ref)
            doc.new_page(width=WIDTH, height=HEIGHT).insert_text((20, 20), "Page 2")
            doc.set_toc([[1, "Results", 1, 42]])
            doc.save(self.output, deflate_images=not unpack)
        if unpack:
            with pdf.open(self.source) as original, pdf.open(self.output) as translated:

                def compressed_digest(doc):
                    ref = doc[0].get_images()[0][0]
                    return sha256(doc.xref_stream_raw(ref)).digest()

                self.assertNotEqual(
                    compressed_digest(original), compressed_digest(translated)
                )

    def repair_and_check(self, offset=0):
        body = BODY + (offset, 0, offset, 0)
        figure = FIGURE + (offset, 0, offset, 0)
        with pdf.open(self.output) as doc:
            self.assertNotEqual(render(doc[0], body), self.expected_text)
            art = render(doc[0], figure)
            untouched = render(doc[1], doc[1].rect)
            toc = doc.get_toc(False)
            text = doc[0].get_text()
        original = sha256(self.source.read_bytes()).digest()
        repair_translation_outputs(self.source, [self.output])
        with pdf.open(self.output) as doc:
            self.assertEqual(render(doc[0], body), self.expected_text)
            self.assertEqual(render(doc[0], figure), art)
            self.assertEqual(render(doc[1], doc[1].rect), untouched)
            self.assertEqual(doc.get_toc(False), toc)
            self.assertEqual(doc[0].get_text(), text)
        self.assertEqual(sha256(self.source.read_bytes()).digest(), original)
        repaired = sha256(self.output.read_bytes()).digest()
        repair_translation_outputs(self.source, [self.output])
        self.assertEqual(sha256(self.output.read_bytes()).digest(), repaired)

    def test_single_language_image_streams(self):
        for split, unpack in [
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        ]:
            with self.subTest(split=split, unpack=unpack):
                self.write_translation(split=split, unpack=unpack)
                self.repair_and_check()

    def test_bilingual_preserves_original_half(self):
        self.write_translation(unpack=True)
        dual = self.output.with_name("dual.pdf")
        with (
            pdf.open(self.source) as source,
            pdf.open(self.output) as translated,
            pdf.open() as doc,
        ):
            page = doc.new_page(width=2 * WIDTH, height=HEIGHT)
            left = source[0].rect
            page.show_pdf_page(left, source, 0)
            page.show_pdf_page(left + (WIDTH, 0, WIDTH, 0), translated, 0)
            doc.insert_pdf(translated, from_page=1, to_page=1)
            doc.set_toc(translated.get_toc(False))
            doc.save(dual)
        self.output = dual
        with pdf.open(dual) as doc:
            original_side = render(doc[0], left)
        self.repair_and_check(offset=WIDTH)
        with pdf.open(dual) as doc:
            self.assertEqual(render(doc[0], left), original_side)

    def test_shared_image_command_only_changes_the_translated_page(self):
        self.write_translation(split=True)
        shared = self.output.with_name("shared.pdf")
        with pdf.open(self.output) as doc:
            # 第二页复用第一页的绘图指令，但只有第一页有可显示的译文。
            doc[1].insert_image(doc[1].rect, xref=doc[0].get_images()[0][0])
            doc[1].set_contents(doc[0].get_contents()[-1])
            doc.save(shared)
        self.output = shared
        self.repair_and_check()

    def test_no_visible_translation_under_image(self):
        for options in [{"hidden": True}, {"background": True}]:
            with self.subTest(options=options):
                self.write_translation(**options)
                before = sha256(self.output.read_bytes()).digest()
                repair_translation_outputs(self.source, [self.output])
                self.assertEqual(sha256(self.output.read_bytes()).digest(), before)

    def test_scanned_original_with_hidden_ocr_keeps_its_image(self):
        scan = self.source.with_name("scan.pdf")
        with pdf.open() as doc:
            page = doc.new_page(width=WIDTH, height=HEIGHT)
            draw_text(page, ENGLISH, render_mode=3)
            page.insert_image(page.rect, stream=self.image)
            doc.save(scan, deflate_images=True)
        self.write_translation()
        before = sha256(self.output.read_bytes()).digest()
        repair_translation_outputs(scan, [self.output])
        self.assertEqual(sha256(self.output.read_bytes()).digest(), before)
