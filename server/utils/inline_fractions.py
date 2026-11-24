"""Keep TeX subscript commas and the fraction rule in the same inline formula."""

import re
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

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

MATH = re.compile(r"(?:CMMI|CMSY|CMEX|CMR)\d+\Z")
ITALIC = re.compile(r"CMMI\d+\Z")
# BabelDOC splits a formula on literal ASCII commas. Only the temporary alias
# uses SMALL COMMA; its embedded glyph, advance, and encoding stay identical.
COMMA = re.compile(rb"(<([0-9A-Fa-f]{2})>\s*)<002[cC]>")
BFCHAR = re.compile(rb"(?s)\bbeginbfchar\b(.*?)\bendbfchar\b")


def inline_rules(page):
    """Locate short fraction bars with math above, subscript commas below and nearby prose."""
    if page.rotation or not any(
        ITALIC.fullmatch(f[3].split("+")[-1]) for f in page.get_fonts()
    ):
        return []
    math_chars, prose = [], []
    for span in page.get_texttrace():
        if span["type"] not in (0, 1) or span["opacity"] <= 0 or span["dir"] != (1, 0):
            continue
        name = span["font"].split("+")[-1]
        if MATH.fullmatch(name):
            math_chars.extend((c[0], name, *c[2]) for c in span["chars"])
        elif re.search(
            r"[A-Za-z]{2,}", "".join(chr(c[0]) for c in span["chars"] if c[0] >= 0)
        ):
            prose.extend(
                (pdf.Rect(c[3]), span["size"])
                for c in span["chars"]
                if c[0] >= 0 and chr(c[0]).isalpha()
            )
    commas = [
        (font, x, y)
        for c, font, x, y in math_chars
        if c == 44 and ITALIC.fullmatch(font)
    ]
    if not commas:
        return []
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
        below = [
            (font, x, y)
            for font, x, y in commas
            if bar.x0 <= x < bar.x1 and 0 < y - bar.y0 < 11
        ]
        if not below or not any(
            bar.x0 <= x < bar.x1 and 0 < bar.y0 - y < 12 for _, _, x, y in math_chars
        ):
            continue
        if any(
            box.y0 <= bar.y0 <= box.y1
            and (0 <= bar.x0 - box.x1 < 4 * size or 0 <= box.x0 - bar.x1 < 4 * size)
            for box, size in prose
        ):
            rules.append((bar, draw["width"], draw["color"], below))
    return rules


def matching_operators(ops, fonts, matrix, rules):
    # 只在显式文字定位之后匹配字形原点，不推测连续排字或字距调整后的坐标。
    state = (pdf.Matrix(1, 1), None, None)
    stack = []
    line = None
    at_start = False
    bars, shows = {}, {}
    for i, (args, op) in enumerate(ops):
        ctm, font, size = state
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
                    for _, _, _, commas in rules:
                        for anchor in commas:
                            if (
                                anchor[0] == family
                                and abs(point - pdf.Point(*anchor[1:])) < 0.15
                            ):
                                shows[anchor] = (i, font, size, parts[0][0])
            at_start = False
        elif op == b"m" and i >= 2 and i + 3 < len(ops):
            if (
                [o for _, o in ops[i - 2 : i]] != [b"q", b"cm"]
                or [o for _, o in ops[i + 1 : i + 4]] != [b"l", b"S", b"Q"]
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
                            ([FloatObject(v) for v in color], b"rg"),
                            ([FloatObject(v) for v in rect], b"re"),
                            ([], b"f"),
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


def rewrite_page(page, writer, matrix, rules, originals):
    content = ContentStream(page["/Contents"], writer, forced_encoding="bytes")
    ops = content.operations
    resources = DictionaryObject(page["/Resources"].get_object())
    fonts = DictionaryObject(resources["/Font"].get_object())
    bars, shows = matching_operators(ops, fonts, matrix, rules)
    edits, aliases = {}, {}
    for j, (_, _, _, commas) in enumerate(rules):
        if j not in bars or not all(c in shows for c in commas):
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
    if not edits:
        return False
    for i in sorted(edits, reverse=True):
        n, replacements = edits[i]
        ops[i : i + n] = replacements
    content.operations = ops
    data = DecodedStreamObject()
    data.set_data(content.get_data())
    resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = writer._add_object(data)
    return True


def _prepare(path, skip_last, stack):
    original = Path(path)
    maps = {}
    with pdf.open(original) as doc:
        plans = []
        for i in range(max(0, len(doc) - skip_last)):
            page = doc[i]
            rules = inline_rules(page)
            if rules:
                plans.append(
                    (
                        i,
                        page.transformation_matrix,
                        rules,
                        page.get_pixmap(annots=False).digest,
                    )
                )
    if not plans:
        return path, maps
    prepared = (
        Path(stack.enter_context(TemporaryDirectory(prefix="pdf2zh-inline-")))
        / original.name
    )
    with (
        PdfReader(original) as reader,
        closing(PdfWriter(reader, incremental=True)) as writer,
    ):
        for i, matrix, rules, _ in plans:
            rewrite_page(writer.pages[i], writer, matrix, rules, maps)
        if not maps:
            return path, {}
        writer.write(prepared)
    with pdf.open(prepared) as check:
        if any(
            check[i].get_pixmap(annots=False).digest != digest
            for i, _, _, digest in plans
        ):
            print("行内公式预处理改变了原页外观，沿用原稿")
            return path, {}
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


def restore_commas(paths, originals):
    """Restore exact ToUnicode tables in mono and dual outputs, including nested Forms."""
    if not originals:
        return
    # 拼版或资源去重可能重命名字体。用本次私有映射的内容核对身份。
    by_content = dict(originals.values())
    for path in paths:
        with pdf.open(path) as doc:
            changed = False
            seen = set()
            for page in doc:
                for font in page.get_fonts(full=True):
                    if not ITALIC.fullmatch(font[3].split("+")[-1]):
                        continue
                    kind, value = doc.xref_get_key(font[0], "ToUnicode")
                    if kind != "xref":
                        continue
                    ref = int(value.split()[0])
                    if ref in seen:
                        continue
                    seen.add(ref)
                    actual = doc.xref_stream(ref)
                    before = by_content.get(actual)
                    if before is not None:
                        doc.update_stream(ref, before)
                        changed = True
                    elif (
                        font[4],
                        font[3],
                    ) in originals and b"<FE50>" in actual.upper():
                        raise ValueError(
                            "输出的数学字体字符映射发生变化，无法确认下标逗号"
                        )
            if changed:
                doc.saveIncr()
