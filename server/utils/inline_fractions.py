"""Preserve inline TeX fractions and accented variables during PDF translation."""

import re
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple

import pymupdf as pdf
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import (
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
)
from utils.inline_accents import (
    ACCENTS,
    protect_accents,
    restore_glyph_metrics,
    same_appearance,
)
from utils.inline_math import (
    apply_paint_edits,
    inline_slots,
    protect_form_runs,
    protect_inline_runs,
)

MATH = re.compile(r"(?:(?:CMMI|CMSY|CMEX|CMR|CMBX)\d+|MTMI)\Z")
ITALIC = re.compile(r"CMMI\d+\Z")


class Glyph(NamedTuple):
    text: str
    font: str
    origin: tuple
    size: float
    box: pdf.Rect
    color: tuple

    @property
    def anchor(self):
        return (self.font, *self.origin)


# BabelDOC splits a formula on literal ASCII commas. Only the temporary alias
# uses SMALL COMMA; its embedded glyph, advance, and encoding stay identical.
COMMA = re.compile(rb"(<([0-9A-Fa-f]{2})>\s*)<002[cC]>")
BFCHAR = re.compile(rb"(?s)\bbeginbfchar\b(.*?)\bendbfchar\b")


def inline_geometry(page):
    """Find printed fractions and superimposed TeX accents next to running prose."""
    if page.rotation:
        return [], []
    math_chars, prose, pairs = [], [], []
    previous = None
    for span in page.get_texttrace():
        if span["type"] not in (0, 1) or span["opacity"] <= 0 or span["dir"] != (1, 0):
            previous = None
            continue
        name = span["font"].split("+")[-1]
        if MATH.fullmatch(name):
            for c in span["chars"]:
                if not 0 <= c[0] <= 0x10FFFF:
                    previous = None
                    continue
                glyph = Glyph(
                    chr(c[0]), name, c[2], span["size"], pdf.Rect(c[3]), span["color"]
                )
                if (
                    previous
                    and previous.text in ACCENTS
                    and glyph.text.isalpha()
                    and previous.color == glyph.color
                    and abs(previous.size - glyph.size) < 0.02
                    and previous.size > 0
                    and 0.08 * glyph.size
                    < glyph.origin[1] - previous.origin[1]
                    < 0.9 * glyph.size
                    and min(glyph.box.x1, previous.box.x1)
                    > max(glyph.box.x0, previous.box.x0)
                ):
                    pairs.append((previous, glyph))
                math_chars.append(glyph)
                previous = glyph
            continue
        previous = None
        text = "".join(chr(c[0]) for c in span["chars"] if 0 <= c[0] <= 0x10FFFF)
        if not re.search(r"[A-Za-z]{3,}", text):
            # 来自混排字体的小号分子、分母仍可由上下位置与分数线识别。
            math_chars.extend(
                Glyph(
                    chr(c[0]), name, c[2], span["size"], pdf.Rect(c[3]), span["color"]
                )
                for c in span["chars"]
                if 0 <= c[0] <= 0x10FFFF and not chr(c[0]).isspace()
            )
        if re.search(r"[A-Za-z]{2,}", text):
            prose.extend(
                (pdf.Rect(c[3]), span["size"])
                for c in span["chars"]
                if c[0] >= 0 and chr(c[0]).isalpha()
            )

    def nearby_sizes(left, right, y):
        return [
            size
            for box, size in prose
            if box.y0 <= y <= box.y1
            and (0 <= left - box.x1 < 4 * size or 0 <= box.x0 - right < 4 * size)
        ]

    accents = [
        (mark, letter)
        for mark, letter in pairs
        if nearby_sizes(
            min(mark.box.x0, letter.box.x0),
            max(mark.box.x1, letter.box.x1),
            letter.origin[1],
        )
    ]
    rules = []
    for draw in page.get_drawings():
        bar = draw["rect"]
        if (
            draw["type"] != "s"
            or len(draw["items"]) != 1
            or draw["items"][0][0] != "l"
            or bar.height > 0.01
            or not 3 < bar.width <= 72
            or not 0 < draw["width"] <= 1.5
            or draw["stroke_opacity"] != 1
            or any(draw["lineCap"])
            or draw["dashes"] != "[] 0"
            or not draw["color"]
            or len(draw["color"]) != 3
        ):
            continue
        sizes = nearby_sizes(bar.x0, bar.x1, bar.y0)
        if not sizes:
            continue
        chars = [g for g in math_chars if bar.x0 - 0.1 <= g.origin[0] < bar.x1]
        comma_anchors = [
            g.anchor
            for g in chars
            if g.text == ","
            and ITALIC.fullmatch(g.font)
            and 0 < g.origin[1] - bar.y0 < 11
        ]
        if comma_anchors:
            is_fraction = any(0 < bar.y0 - g.origin[1] < 12 for g in chars)
        else:
            # 没有下标逗号作线索时，要求短横线上下都有接近横线的小字号数学字形。
            script_size = 0.95 * max(sizes)
            scripts = [g for g in chars if g.size <= script_size]
            is_fraction = any(
                0 < g.origin[1] - bar.y0 < 1.7 * g.size for g in scripts
            ) and any(0 < bar.y0 - g.origin[1] < g.size for g in scripts)
        if is_fraction:
            rules.append((bar, draw["width"], draw["color"], comma_anchors))
    return rules, accents


def matching_operators(ops, fonts, matrix, rules, accents):
    # 只在显式文字定位之后匹配字形原点，不推测连续排字或字距调整后的坐标。
    state = (pdf.Matrix(1, 1), None, None)
    stack = []
    line = None
    at_start = False
    bars, shows = {}, {}
    open_path = False
    anchors = {c for _, _, _, commas in rules for c in commas}
    anchors.update(g.anchor for pair in accents for g in pair)
    for i, (args, op) in enumerate(ops):
        ctm, font, size = state
        fresh_path = not open_path
        if op in (b"m", b"l", b"h", b"re", b"c", b"v", b"y"):
            open_path = True
        elif op in (b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"n"):
            open_path = False
        if op == b"q":
            stack.append(state)
        elif op == b"Q" and stack:
            state = stack.pop()
        elif op == b"cm":
            state = (pdf.Matrix(*map(float, args)) * ctm, font, size)
        elif op == b"Tf":
            state = (ctm, *args)
        elif op == b"BT":
            line = pdf.Matrix(1, 1)
            at_start = True
        elif op == b"Tm":
            line = pdf.Matrix(*map(float, args))
            at_start = True
        elif op in (b"Td", b"TD") and line is not None:
            line = pdf.Matrix(1, 0, 0, 1, *map(float, args)) * line
            at_start = True
        elif op in (b"T*", b"'", b'"', b"ET"):
            line = None
            at_start = False
        elif op in (b"Tj", b"TJ"):
            if at_start and line is not None and font in fonts:
                point = pdf.Point(0, 0) * line * ctm * matrix
                parts = args if op == b"Tj" else args[0]
                if parts and isinstance(parts[0], bytes) and parts[0]:
                    family = (
                        str(fonts[font].get_object().get("/BaseFont", ""))
                        .lstrip("/")
                        .split("+")[-1]
                    )
                    for anchor in anchors:
                        if (
                            anchor[0] == family
                            and abs(point - pdf.Point(*anchor[1:])) < 0.15
                        ):
                            shows[anchor] = (
                                None
                                if anchor in shows
                                else (i, font, size, parts[0][0])
                            )
            at_start = False
        elif op == b"m" and fresh_path and i + 2 < len(ops):
            if (
                [o for _, o in ops[i + 1 : i + 3]] != [b"l", b"S"]
                or ctm.a <= 0
                or ctm.d <= 0
                or ctm.b
                or ctm.c
            ):
                continue
            a, b = pdf.Point(*args), pdf.Point(*ops[i + 1][0])
            if a.y != b.y:
                continue
            visual = pdf.Rect(a * ctm * matrix, b * ctm * matrix).normalize()
            for j, (bar, width, color, _) in enumerate(rules):
                if max(abs(v - w) for v, w in zip(bar, visual)) < 0.03:
                    h = width / ctm.d
                    rect = (min(a.x, b.x), a.y - h / 2, abs(b.x - a.x), h)
                    bars[j] = (
                        i,
                        [
                            ([], b"q"),
                            ([FloatObject(v) for v in color], b"rg"),
                            ([FloatObject(v) for v in rect], b"re"),
                            ([], b"f"),
                            ([], b"Q"),
                        ],
                    )
    return bars, shows


def protected_cmap(font, code):
    if font.get("/Subtype") != "/Type1" or "/ToUnicode" not in font:
        return None
    cmap = font["/ToUnicode"].get_data()
    if b"<FE50>" in cmap.upper():
        return None

    def protect(block):
        return COMMA.sub(
            lambda m: m[1] + b"<FE50>" if int(m[2], 16) == code else m[0], block[0]
        )

    marked = BFCHAR.sub(protect, cmap)
    return (
        (cmap, b"% PDF2zh protected inline punctuation\n" + marked)
        if marked != cmap
        else None
    )


def rewrite_page(page, writer, matrix, rules, accents, slots, originals):
    content = ContentStream(page["/Contents"], writer, forced_encoding="bytes")
    ops = content.operations
    resources = DictionaryObject(page["/Resources"].get_object())
    fonts = (
        DictionaryObject(resources["/Font"])
        if "/Font" in resources
        else DictionaryObject()
    )
    bars, shows = matching_operators(ops, fonts, matrix, rules, accents)
    edits, aliases = {}, {}
    for j, (_, _, _, commas) in enumerate(rules):
        if j not in bars or not all(shows.get(c) for c in commas):
            continue
        runs = [shows[c] for c in commas]
        mappings = {
            (font, code): protected_cmap(fonts[font], code) for _, font, _, code in runs
        }
        if not all(mappings.values()):
            continue
        # 分数线与全部下标逗号都能定位才一起修改。正文和别处的公式继续引用原字体。
        for i, font, size, code in runs:
            key = (font, code)
            if key not in aliases:
                cmap, marked = mappings[key]
                name = NameObject(f"/PDF2zhInlineMath{len(originals)}")
                while name in fonts:
                    name = NameObject(str(name) + "X")
                mapping = DecodedStreamObject()
                mapping.set_data(marked)
                clone = DictionaryObject(fonts[font])
                clone[NameObject("/ToUnicode")] = writer._add_object(mapping)
                fonts[name] = writer._add_object(clone)
                aliases[key] = name
                originals[(str(name)[1:], str(clone["/BaseFont"])[1:])] = (marked, cmap)
            edits[i] = (
                1,
                [([aliases[key], size], b"Tf"), ops[i], ([font, size], b"Tf")],
            )
        i, replacement = bars[j]
        edits[i] = (3, replacement)
    protect_accents(writer, fonts, ops, shows, accents, edits)
    protect_inline_runs(writer, fonts, ops, slots, edits, originals)
    nested = protect_form_runs(writer, resources, ops, slots, originals)
    if not edits and not nested:
        return False
    if edits:
        data = DecodedStreamObject()
        data.set_data(apply_paint_edits(content, edits))
        page[NameObject("/Contents")] = writer._add_object(data)
        resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources
    return True


def _write_pages(original, prepared, plans):
    maps = {}
    changed = {}
    with (
        PdfReader(original) as reader,
        closing(PdfWriter(reader, incremental=True)) as writer,
    ):
        for i, matrix, rules, accents, slots in plans:
            if rewrite_page(
                writer.pages[i], writer, matrix, rules, accents, slots, maps
            ):
                changed[i] = accents
        if changed:
            writer.write(prepared)
    return changed, maps


def _prepare(path, skip_last, stack):
    original = Path(path)
    with pdf.open(original) as doc:
        plans = []
        for i in range(max(0, len(doc) - skip_last)):
            page = doc[i]
            rules, accents = inline_geometry(page)
            slots = inline_slots(page, [bar for bar, _, _, _ in rules])
            if rules or accents or slots:
                plans.append((i, page.transformation_matrix, rules, accents, slots))
    if not plans:
        return path, {}
    prepared = (
        Path(stack.enter_context(TemporaryDirectory(prefix="pdf2zh-inline-")))
        / original.name
    )
    changed, maps = _write_pages(original, prepared, plans)
    if not changed:
        return path, {}
    with pdf.open(prepared) as check, pdf.open(original) as source:
        rejected = {
            i
            for i, accents in changed.items()
            if not same_appearance(source[i], check[i], accents)
        }
    if rejected:
        print("行内公式预处理沿用原稿的页码：", sorted(i + 1 for i in rejected))
        accepted = [
            plan for plan in plans if plan[0] in changed and plan[0] not in rejected
        ]
        if not accepted:
            return path, {}
        _, maps = _write_pages(original, prepared, accepted)
    return str(prepared), maps


@contextmanager
def prepare_inline_fractions(path, skip_last=0):
    with ExitStack() as stack:
        try:
            prepared = _prepare(path, skip_last, stack)
        except (
            PdfReadError,
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            print(f"跳过不支持的行内公式预处理：{exc}")
            prepared = path, {}
        yield prepared


def finalize_inline_fonts(paths, originals):
    """Restore Unicode maps and native Type 3 metrics, including nested dual-page Forms."""
    # 拼版或资源去重可能重命名字体。用本次私有映射的内容核对身份。
    by_content = dict(originals.values())
    for path in paths:
        with pdf.open(path) as doc:
            changed = False
            seen = set()
            fonts_seen = set()
            for page in doc:
                for font in page.get_fonts(full=True):
                    if font[0] in fonts_seen:
                        continue
                    fonts_seen.add(font[0])
                    if font[2:4] == ("Type3", "CMMIInlineAccent"):
                        if restore_glyph_metrics(doc, font[0]):
                            changed = True
                        continue
                    if not originals:
                        continue
                    kind, value = doc.xref_get_key(font[0], "ToUnicode")
                    if kind != "xref":
                        continue
                    ref = int(value.split()[0])
                    if ref in seen:
                        continue
                    actual = doc.xref_stream(ref)
                    before = by_content.get(actual)
                    if before is not None and b"/CMapName /PDF2zhInlineCore" in actual:
                        # 一个 CMap 可能被多个资源共用。逐个恢复原标准字体（含编码），保留各自的绘制坐标。
                        kind, source = doc.xref_get_key(font[0], "PDF2zhCoreSource")
                        if kind != "xref":
                            raise ValueError("无法确认受保护的标准字体来源")
                        doc.xref_copy(int(source.split()[0]), font[0])
                        changed = True
                        continue
                    seen.add(ref)
                    if before is not None:
                        doc.update_stream(ref, before)
                        changed = True
                    elif b"/CMapName /PDF2zhInline" in actual or (
                        (font[4], font[3]) in originals and b"<FE50>" in actual.upper()
                    ):
                        raise ValueError(
                            "输出的数学字体字符映射发生变化，无法还原行内公式文字"
                        )
            if changed:
                doc.saveIncr()


def restore_commas(paths, originals):
    if originals:
        finalize_inline_fonts(paths, originals)
