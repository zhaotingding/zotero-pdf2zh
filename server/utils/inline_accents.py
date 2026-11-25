"""Keep an overprinted TeX accent and its base letter together when text reflows."""

import re

import numpy as np
import pymupdf as pdf
from PIL import ImageFilter
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
)

ACCENTS = {"ˆ": "\u0302", "^": "\u0302", "˜": "\u0303", "¯": "\u0304", "˙": "\u0307"}

# 某些 PDF 清理器会在 Type 3 字形前包一层 q/Q；字宽声明必须是第一个操作。
_METRICS_AFTER_SAVE = re.compile(rb"^\s*(q\s+)((?:[-+.\d]+\s+){6}d1)\b")


def restore_glyph_metrics(doc, font_ref, procedure="accent"):
    kind, ref = doc.xref_get_key(font_ref, "CharProcs/" + procedure)
    if kind != "xref":
        return False
    glyph = int(ref.split()[0])
    stream = doc.xref_stream(glyph)
    fixed, count = _METRICS_AFTER_SAVE.subn(rb"\2 \1", stream, count=1)
    if count:
        doc.update_stream(glyph, fixed)
    return bool(count)


def accent_font(writer, fonts, pair, runs):
    """Pack the two original glyph paints into one selectable, uncoloured Type 3 glyph."""
    mark, letter = pair
    (_, mark_font, _, mark_code), (_, base_font, _, base_code) = runs
    base = fonts[base_font]
    if any(
        fonts[n].get("/Subtype") != "/Type1" or "/FontDescriptor" not in fonts[n]
        for n in [mark_font, base_font]
    ):
        return None
    widths = base.get("/Widths")
    index = base_code - int(base.get("/FirstChar", 0))
    if widths is None or not 0 <= index < len(widths):
        return None
    advance = float(widths[index])
    dx = (mark.origin[0] - letter.origin[0]) * 1000 / letter.size
    dy = (letter.origin[1] - mark.origin[1]) * 1000 / letter.size
    bounds = pdf.Rect(base["/FontDescriptor"]["/FontBBox"])
    bounds |= pdf.Rect(fonts[mark_font]["/FontDescriptor"]["/FontBBox"]) + (
        dx,
        dy,
        dx,
        dy,
    )
    coords = " ".join(f"{v:.5f}" for v in bounds)

    def stream(data):
        obj = DecodedStreamObject()
        obj.set_data(data.encode("ascii"))
        return writer._add_object(obj)

    # d1 继承正文颜色。字宽沿用基字符；帽号和字母仍用论文中的原字体绘制。
    glyph = stream(
        f"{advance} 0 {coords} d1\nBT /Mark 1000 Tf 1 0 0 1 {dx:.6f} {dy:.6f} Tm <{mark_code:02x}> Tj ET\n"
        f"BT /Base 1000 Tf 1 0 0 1 0 0 Tm <{base_code:02x}> Tj ET"
    )
    unicode = (letter.text + ACCENTS[mark.text]).encode("utf-16-be").hex()
    cmap = stream(
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /InlineAccent def /CMapType 2 def\n1 begincodespacerange <01> <01> endcodespacerange\n"
        f"1 beginbfchar <01> <{unicode}> endbfchar\nendcmap CMapName currentdict /CMap defineresource pop end end"
    )

    def dictionary(**fields):
        return DictionaryObject(
            {NameObject("/" + key): value for key, value in fields.items()}
        )

    font = dictionary(
        Type=NameObject("/Font"),
        Subtype=NameObject("/Type3"),
        BaseFont=NameObject("/CMMIInlineAccent"),
        FontBBox=ArrayObject(FloatObject(value) for value in bounds),
        FontMatrix=ArrayObject(
            FloatObject(value) for value in (0.001, 0, 0, 0.001, 0, 0)
        ),
        CharProcs=dictionary(accent=glyph),
        Encoding=dictionary(
            Type=NameObject("/Encoding"),
            Differences=ArrayObject([NumberObject(1), NameObject("/accent")]),
        ),
        FirstChar=NumberObject(1),
        LastChar=NumberObject(1),
        Widths=ArrayObject([FloatObject(advance)]),
        Resources=dictionary(
            Font=dictionary(
                Mark=fonts.raw_get(mark_font), Base=fonts.raw_get(base_font)
            )
        ),
        ToUnicode=cmap,
    )
    return writer._add_object(font)


def protect_accents(writer, fonts, ops, shows, accents, edits):
    def single_glyph(index):
        operands, op = ops[index]
        parts = operands if op == b"Tj" else operands[0]
        return len(parts) == 1 and isinstance(parts[0], bytes) and len(parts[0]) == 1

    for pair in accents:
        if not all(shows.get(g.anchor) for g in pair):
            continue
        runs = [shows[g.anchor] for g in pair]
        (mark_i, _, _, _), (base_i, font, size, _) = runs
        # 限定为两个单字形绘制操作，避免吞掉相邻文字或改变后续字距。
        if mark_i >= base_i or any(
            not single_glyph(i) or i in edits for i in (mark_i, base_i)
        ):
            continue
        combined = accent_font(writer, fonts, pair, runs)
        if combined is None:
            continue
        alias = NameObject(f"/PDF2zhAccent{base_i}")
        while alias in fonts:
            alias = NameObject(str(alias) + "X")
        fonts[alias] = combined
        edits[mark_i] = (1, [])
        edits[base_i] = (
            1,
            [
                ([alias, size], b"Tf"),
                ([ByteStringObject(b"\x01")], b"Tj"),
                ([font, size], b"Tf"),
            ],
        )


def same_appearance(before, after, accents):
    """Allow only local font-hinting differences when nesting original glyphs."""
    first = before.get_pixmap(annots=False)
    second = after.get_pixmap(annots=False)
    if first.digest == second.digest:
        return True
    if not accents or first.irect != second.irect:
        return False
    different = (np.asarray(first.pil_image()) != np.asarray(second.pil_image())).any(
        axis=2
    )
    for mark, letter in accents:
        region = ((mark.box | letter.box) + (-1, -1, 1, 1)) & before.rect
        a, b = [
            p.get_pixmap(matrix=pdf.Matrix(4, 4), clip=region, annots=False).pil_image()
            for p in (before, after)
        ]
        aa, bb = [np.asarray(im).astype(np.int16) for im in (a, b)]
        if np.abs(aa - bb).sum() > 0.10 * (255 - aa).sum():
            return False
        for lhs, rhs in [(a, b), (b, a)]:
            if (
                (np.asarray(lhs.convert("L")) < 130)
                & (np.asarray(rhs.convert("L").filter(ImageFilter.MinFilter(3))) > 190)
            ).any():
                return False
        x, y, x1, y1 = region.irect
        different[y:y1, x:x1] = False
    return not different.any()
