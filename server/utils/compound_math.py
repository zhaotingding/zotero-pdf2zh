"""Move the original glyphs and rules of a stacked TeX expression as one PDF glyph."""

import hashlib
import re
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pymupdf as pdf
from PIL import ImageFilter
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
)
from utils.inline_accents import restore_glyph_metrics
from utils.inline_math import _cmap, apply_paint_edits

# Operators/symbols and upright companion faces are separate in TeX's PDF fonts.
_MATH = re.compile(
    r"(?:CM(?:MI|SY|EX|BSY|BX|R|TI)\d+|MS[AB]M\d+|"
    r"LM(?:Math|Roman)|(?:STIX.*|Cambria|XITS|Asana)Math|"
    r".*Math(?:Italic|Symbol|Extension))",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-zA-Z]{2,}")
_FUNCTIONS = {
    "log",
    "ln",
    "lim",
    "min",
    "max",
    "sin",
    "cos",
    "tan",
    "exp",
    "sup",
    "inf",
    "arg",
    "det",
}
_LARGE = re.compile(r"(?:CMEX|MathExtension)", re.IGNORECASE)
_SPECIAL = set("∑∏∫∬∮√˜ˆ¯ˉ")
_N = NameObject


def is_math_face(name):
    return bool(_MATH.match(name.split("+")[-1]))


def is_math_text(family, value):
    family = family.split("+")[-1]
    roman = re.match(r"(?:CM(?:R|BX|TI)\d|LMRoman)", family, re.IGNORECASE)
    return is_math_face(family) and (
        not roman or all(w.lower() in _FUNCTIONS for w in _WORD.findall(value))
    )


def _stream(writer, data):
    value = DecodedStreamObject()
    value.set_data(data)
    return writer._add_object(value)


def _numbers(values):
    return ArrayObject(FloatObject(x) for x in values)


@dataclass
class Paint:
    index: int
    font: str
    size: float
    xscale: float
    yscale: float
    data: bytes
    glyphs: list
    advance: float

    @property
    def box(self):
        result = pdf.Rect(self.glyphs[0][3])
        for glyph in self.glyphs[1:]:
            result |= pdf.Rect(glyph[3])
        return result

    @property
    def text(self):
        return "".join(chr(c[0]) for c in self.glyphs)


@dataclass
class Rule:
    operations: tuple
    box: pdf.Rect

    @property
    def index(self):
        return self.operations[0]


def _trace(page):
    traces = defaultdict(list)
    for span in page.get_texttrace():
        family = span["font"].split("+")[-1]
        plain = (
            span["dir"] == (1, 0)
            and span["type"] == 0
            and span["opacity"] == 1
            and span["color"] in ((0.0,), (0.0, 0.0, 0.0), 0.0)
        )
        traces[family].extend(
            (char, plain and 0 <= char[0] <= 0x10FFFF) for char in span["chars"]
        )
    return traces


def _paints(page, ops, fonts, known_math):
    # Match simple-font bytes to the actual glyphs in paint order. A different count
    # (e.g. a nested Form reusing the face) disqualifies the face on this page.
    traces = _trace(page)
    faces = {}
    for name, resource in fonts.items():
        font = resource.get_object()
        if isinstance(font, DictionaryObject):
            faces[name] = str(font.get("/BaseFont", "")).split("+")[-1].lstrip("/")
    offsets = Counter()
    text_shows = []
    ctm = tm = pdf.Matrix(1, 1)
    font, size = None, 0
    params = {b"Tc": 0.0, b"Tw": 0.0, b"Tz": 100.0, b"Ts": 0.0, b"Tr": 0.0}
    defaults = params.copy()
    stack = []
    for i, (args, op) in enumerate(ops):
        if op == b"q":
            stack.append((ctm, font, size, params.copy()))
        elif op == b"Q" and stack:
            ctm, font, size, params = stack.pop()
        elif op == b"cm":
            ctm = pdf.Matrix(*map(float, args)) * ctm
        elif op == b"BT":
            tm = pdf.Matrix(1, 1)
        elif op == b"Tm":
            tm = pdf.Matrix(*map(float, args))
        elif op == b"Tf":
            font, size = args[0], float(args[1])
        elif op in params:
            params[op] = float(args[0])
        elif op in (b"Tj", b"TJ", b"'", b'"'):
            if op == b'"':
                params[b"Tw"], params[b"Tc"] = map(float, args[:2])
            parts = args[0] if op == b"TJ" else args[-1:]
            data = b"".join(v for v in parts if isinstance(v, bytes))
            family = faces.get(font)
            start = offsets[family]
            offsets[family] += len(data)
            glyphs = traces.get(family, ())[start : start + len(data)]
            mx = tm * ctm
            paint = None
            if (
                data
                and len(glyphs) == len(data)
                and all(ok for _, ok in glyphs)
                and params == defaults
                and size > 0
                and mx.a > 0
                and mx.d > 0
                and abs(mx.b) + abs(mx.c) < 1e-6
                and op in (b"Tj", b"TJ")
                and fonts[font].get("/Subtype") in ("/Type1", "/TrueType")
            ):
                chars = [g for g, ok in glyphs]
                value = "".join(chr(g[0]) for g in chars)
                math = is_math_text(family, value)
                if not known_math:
                    # Some publishers use ordinary embedded faces for every
                    # operand. Text alone is insufficient evidence on those pages.
                    math = (
                        bool(value.strip())
                        and all(w.lower() in _FUNCTIONS for w in _WORD.findall(value))
                        and "\ufffd" not in value
                    )
                if math or value.strip() in {"=", ":=", "≤", "≥", "+", "−"}:
                    face = fonts[font]
                    widths = face.get("/Widths")
                    if widths is not None:
                        # pypdf leaves indirect arrays unresolved when accessed via get().
                        widths = widths.get_object()
                    first = int(face.get("/FirstChar", 0))
                    if isinstance(widths, ArrayObject) and all(
                        0 <= b - first < len(widths) for b in data
                    ):
                        delta = sum(float(widths[b - first]) for b in data) - sum(
                            float(v) for v in parts if not isinstance(v, bytes)
                        )
                        paint = Paint(i, font, size, mx.a, mx.d, data, chars, delta)
            text_shows.append((family, paint))
        elif op in (b"Do", b"BI", b"INLINE IMAGE"):
            text_shows.append((None, None))
    return [
        paint if offsets[face] == len(traces.get(face, ())) else None
        for face, paint in text_shows
    ], faces


def _rules(page, ops):
    # Use MuPDF's effective stroke width/color, and only remove the matched path
    # operators. Longer table borders and multipart paths stay in the source.
    drawings = {}
    for draw in page.get_drawings():
        if len(draw["items"]) != 1 or draw["dashes"] not in (None, "[] 0"):
            continue
        r = draw["rect"]
        if draw["type"] == "s" and draw["items"][0][0] == "l" and r.height < 0.005:
            if (
                not 0 < draw["width"] < 1.6
                or any(draw["lineCap"])
                or draw["stroke_opacity"] != 1
                or draw["color"] != (0.0, 0.0, 0.0)
            ):
                continue
            ink = r + (0, -draw["width"] / 2, 0, draw["width"] / 2)
        elif (
            draw["type"] == "f"
            and draw["items"][0][0] == "re"
            and 0 < r.height < 1.6
            and draw["fill_opacity"] == 1
            and draw["fill"] == (0.0, 0.0, 0.0)
        ):
            ink = r
        else:
            continue
        if r.width >= 1:
            drawings[tuple(round(v, 2) for v in r)] = ink
    rules = []
    ctm, stack, path = pdf.Matrix(1, 1), [], []
    for i, (args, op) in enumerate(ops):
        if op == b"q":
            stack.append(ctm)
        elif op == b"Q" and stack:
            ctm = stack.pop()
        elif op == b"cm":
            ctm = pdf.Matrix(*map(float, args)) * ctm
        elif op in (b"m", b"re", b"l", b"c", b"v", b"y", b"h", b"W", b"W*"):
            path.append((i, op, args, ctm))
        elif op in (b"S", b"s", b"n", b"f", b"f*", b"F", b"B", b"B*", b"b", b"b*"):
            commands = [item[1] for item in path]
            if op in (b"S", b"s") and commands in ([b"m", b"l"], [b"m", b"l", b"h"]):
                a, b = [pdf.Point(*item[2]) * item[3] for item in path[:2]]
                rect = pdf.Rect(a, b).normalize()
            elif op in (b"f", b"f*", b"F") and commands == [b"re"]:
                x, y, width, height = map(float, path[0][2])
                rect = pdf.Rect(x, y, x + width, y + height).normalize() * path[0][3]
            else:
                path.clear()
                continue
            rect = rect * page.transformation_matrix
            ink = drawings.get(tuple(round(v, 2) for v in rect))
            if ink is not None:
                rules.append(Rule((*[item[0] for item in path], i), ink))
            path.clear()
    return rules


def _groups(paints):
    group, box = [], pdf.Rect()
    for paint in [*paints, None]:
        if paint:
            r = paint.box
            em = max(
                paint.size * paint.yscale,
                group[0].size * group[0].yscale if group else 0,
            )
        if group and (
            paint is None
            or r.x0 > box.x1 + 1.65 * em
            or r.x1 < box.x0 - 0.8 * em
            or r.y0 > box.y1 + 0.5 * em
            or r.y1 < box.y0 - 0.5 * em
            or (
                r.x1 < group[-1].box.x0 - 2 * em
                and abs(paint.glyphs[0][2][1] - group[-1].glyphs[-1][2][1]) > 1.35 * em
            )
        ):
            yield group, box
            group, box = [], pdf.Rect()
        if paint:
            group.append(paint)
            box |= r


def _has_numerator_and_denominator(rule, group, em):
    box = rule.box
    ys = [
        y
        for paint in group
        for _, _, (x, y), _ in paint.glyphs
        if box.x0 - 0.2 * em <= x < box.x1
    ]
    return any(box.y0 - em < y < box.y0 for y in ys) and any(
        box.y1 < y < box.y1 + 1.6 * em for y in ys
    )


def _compound_font(writer, fonts, paints, rules, box, baseline, em, originals):
    # All original font outlines, glyph positions and the actual thickness of
    # every rule share a single coordinate system inside the Type 3 glyph.
    scale = 1000 / em
    width = box.width * scale
    bounds = (
        -em * scale * 0.12,
        (baseline - box.y1) * scale - 220,
        width + 160,
        (baseline - box.y0) * scale + 220,
    )
    coords = " ".join(f"{f:.6f}" for f in bounds)
    # Even-odd fill of two identical rectangles paints no ink. It makes the
    # native parser measure the full advance, including nested text at the
    # left of the first radical or rule, when reflowing surrounding prose.
    extent = f"0 0 {width:.6f} 1 re"
    metrics = f"{width:.6f} 0 {coords} d1"
    parts = [f"q {scale:.9f} 0 0 {scale:.9f} 0 0 cm", "BT"]
    used = DictionaryObject()
    for paint in paints:
        used[paint.font] = fonts.raw_get(paint.font)
        parts.append(f"{paint.font} {paint.size:.7f} Tf")
        for b, glyph in zip(paint.data, paint.glyphs, strict=True):
            x, y = glyph[2]
            parts.append(
                f"{paint.xscale:.7f} 0 0 {paint.yscale:.7f} {x - box.x0:.7f} {baseline - y:.7f} Tm <{b:02x}> Tj"
            )
    parts.append("ET")
    for rule in rules:
        r = rule.box
        parts.append(
            f"{r.x0 - box.x0:.7f} {baseline - r.y1:.7f} {r.width:.7f} {r.height:.7f} re f"
        )
    parts.append("Q")
    # The empty measurement path is only for the parser. Some viewers
    # antialias its edges as a hairline even with even-odd cancellation.
    actual = _stream(writer, "\n".join([metrics, *parts]).encode("ascii"))
    # Flat inline metrics leave a gap between consecutive printed lines. A
    # tall fraction otherwise causes BabelDOC to merge both lines' formulas,
    # even across intervening prose. Only the temporary input uses this face.
    bottom, top = bounds[1], bounds[3]
    sy = 620 / (top - bottom)
    proxy = [
        metrics,
        f"q 1 0 0 {sy:.9f} 0 {-bottom * sy:.8f} cm",
        f"{extent} {extent} f*",
        *parts,
        "Q",
    ]
    glyph = _stream(writer, "\n".join(proxy).encode("ascii"))
    logical = "".join(p.text for p in paints)
    fingerprint = hashlib.sha256(logical.encode()).hexdigest()[:20]
    original = _cmap({b"\x01": logical}, "CompoundUnicode" + fingerprint)
    marked = _cmap({b"\x01": "\ue000"}, "PDF2zhInlineCompound" + fingerprint)
    face = _N("/CMMICompound")
    alias = _N(f"/PDF2zhCompound{glyph.idnum}")
    while alias in fonts:
        alias = _N(str(alias) + "X")
    fonts[alias] = writer._add_object(
        DictionaryObject(
            {
                _N("/Type"): _N("/Font"),
                _N("/Subtype"): _N("/Type3"),
                _N("/BaseFont"): face,
                # The native paragraph detector derives Type 3 point size from the
                # global ascent minus descent. Use one em while classifying the text,
                # then restore the true paint bounds (also declared by d1) on export.
                _N("/FontBBox"): _numbers((bounds[0], -200, bounds[2], 800)),
                _N("/PDF2zhPaintBounds"): _numbers(bounds),
                _N("/PDF2zhSourcePaint"): actual,
                _N("/FontMatrix"): _numbers((0.001, 0, 0, 0.001, 0, 0)),
                _N("/CharProcs"): DictionaryObject({_N("/formula"): glyph}),
                _N("/Encoding"): DictionaryObject(
                    {
                        _N("/Type"): _N("/Encoding"),
                        _N("/Differences"): ArrayObject(
                            [NumberObject(1), _N("/formula")]
                        ),
                    }
                ),
                _N("/FirstChar"): NumberObject(1),
                _N("/LastChar"): NumberObject(1),
                _N("/Widths"): _numbers([width]),
                _N("/Resources"): DictionaryObject({_N("/Font"): used}),
                _N("/ToUnicode"): _stream(writer, marked),
            }
        )
    )
    originals[(alias[1:], face[1:])] = marked, original
    return alias, width


def _rewrite(writer, page, visual, originals):
    if "/Resources" not in page or "/Contents" not in page or visual.rotation:
        return False
    resources = DictionaryObject(page["/Resources"])
    if "/Font" not in resources:
        return False
    fonts = DictionaryObject(resources["/Font"])
    content = ContentStream(page["/Contents"], writer, forced_encoding="bytes")
    ops = content.operations
    rules = _rules(visual, ops)
    known_math = any(is_math_face(face[3]) for face in visual.get_fonts())
    if not (known_math or rules):
        return False
    paints, faces = _paints(visual, ops, fonts, known_math)
    edits = {}
    for group, box in _groups(paints):
        first = group[0]
        em = max(p.size * p.yscale for p in group)
        strokes = [
            r
            for r in rules
            if r.index not in edits
            and box.x0 - 0.25 * em <= r.box.x0
            and r.box.x1 <= box.x1 + 0.25 * em
            and box.y0 - 0.18 * em <= (r.box.y0 + r.box.y1) / 2 <= box.y1 + 0.18 * em
            and first.index - 12 <= r.index <= group[-1].index + 12
        ]
        special = any(
            _LARGE.match(faces[p.font]) or not _SPECIAL.isdisjoint(p.text)
            for p in group
        )
        if not known_math:
            has_root = strokes and any("√" in p.text for p in group)
            if not has_root and not any(
                _has_numerator_and_denominator(r, group, em) for r in strokes
            ):
                continue
        # Indexes and relational signs can otherwise be detached altogether at
        # the next line or silently left at their original English location.
        indexed = len({round(p.size * p.yscale, 1) for p in group}) > 1
        relation = any(not set("=:≤≥≈∈").isdisjoint(p.text) for p in group)
        if not (strokes or special or indexed or relation) or len(group) < 2:
            continue
        for stroke in strokes:
            box |= stroke.box
        # A dominant full-size baseline places fractions and leading root glyphs
        # on the neighbouring text line even when the numerator was painted first.
        baselines = Counter(
            round(g[2][1], 2)
            for p in group
            if p.size * p.yscale >= 0.95 * em
            for g in p.glyphs
            if chr(g[0]) not in _SPECIAL and not _LARGE.match(faces[p.font])
        )
        baseline = (
            baselines.most_common(1)[0][0]
            if baselines
            else (box.y0 + box.y1) / 2 + em * 0.25
        )
        alias, width = _compound_font(
            writer, fonts, group, strokes, box, baseline, em, originals
        )
        for p in group:
            edits[p.index] = (1, [([_numbers([-p.advance])], b"TJ")])
        for r in strokes:
            for index in r.operations:
                edits[index] = (1, [])
        x, y = first.glyphs[0][2]
        fs = em / first.yscale
        lead = (x - box.x0) * 1000 / em
        tail = width - lead
        edits[first.index][1][:0] = [
            ([alias, FloatObject(fs)], b"Tf"),
            ([FloatObject(100 * first.yscale / first.xscale)], b"Tz"),
            ([FloatObject((y - baseline) / first.yscale)], b"Ts"),
            (
                [
                    ArrayObject(
                        [
                            FloatObject(lead),
                            ByteStringObject(b"\x01"),
                            FloatObject(tail),
                        ]
                    )
                ],
                b"TJ",
            ),
            ([NumberObject(0)], b"Ts"),
            ([NumberObject(100)], b"Tz"),
            ([first.font, FloatObject(first.size)], b"Tf"),
        ]
    if edits:
        page[_N("/Contents")] = _stream(writer, apply_paint_edits(content, edits))
        resources[_N("/Font")] = fonts
        page[_N("/Resources")] = resources
    return bool(edits)


def restore_compound(doc, ref):
    # 双语拼版可能共用字体。若引擎只保留了临时形状，不能将其当成最终公式交付。
    kind, bounds = doc.xref_get_key(ref, "PDF2zhPaintBounds")
    if kind != "array":
        kind, value = doc.xref_get_key(ref, "ToUnicode")
        if kind == "xref" and b"/CMapName /PDF2zhInlineCompound" in doc.xref_stream(
            int(value.split()[0])
        ):
            raise ValueError("输出缺少复合公式的原始边界")
        return False
    kind, source = doc.xref_get_key(ref, "PDF2zhSourcePaint")
    if kind != "xref":
        raise ValueError("输出缺少复合公式的原始字形")
    doc.xref_set_key(ref, "CharProcs/formula", source)
    doc.xref_set_key(ref, "FontBBox", bounds)
    doc.xref_set_key(ref, "PDF2zhPaintBounds", "null")
    doc.xref_set_key(ref, "PDF2zhSourcePaint", "null")
    restore_glyph_metrics(doc, ref, "formula")
    return True


def restore_dual_source(source, outputs, config):
    """Use the actual publisher page on the original side of a bilingual PDF."""
    for path in outputs:
        if not str(path).lower().endswith(".dual.pdf"):
            continue
        with pdf.open(source) as original, pdf.open(path) as doc:
            before = config.trans_first or config.enhance_compatibility
            alternating = config.dual_mode == "TB"
            count = len(doc) // 2 if alternating else len(doc)
            if count > len(original) or (alternating and len(doc) % 2):
                raise ValueError("双语译本与原稿页数不匹配")
            for i in range(count):
                page = doc[2 * i + int(before) if alternating else i]
                if not any(ft[3] == "CMMICompound" for ft in page.get_fonts(full=True)):
                    continue
                rect = page.rect
                if alternating:
                    # Retain the page object, destinations and annotation links.
                    ref = doc.get_new_xref()
                    doc.update_object(ref, "<<>>")
                    doc.update_stream(ref, b"")
                    page.set_contents(ref)
                    page.show_pdf_page(rect, original, i)
                else:
                    # BabelDOC allocates each side its own page width and centers
                    # the shorter page vertically when their heights differ.
                    size = original[i].rect
                    x = rect.width - size.width if before else 0
                    y = (rect.height - size.height) / 2
                    region = pdf.Rect(x, y, x + size.width, y + size.height)
                    wrappers = [
                        ref
                        for ref, _, parent, box in page.get_xobjects()
                        if not parent
                        and max(
                            abs(a - b)
                            for a, b in zip(
                                pdf.Rect(box) * page.transformation_matrix, region
                            )
                        )
                        < 0.05
                    ]
                    if len(wrappers) != 1:
                        raise ValueError(f"不能确认双语译本第 {i + 1} 页的原文位置")
                    # Swap the whole original Form: copying the page over the
                    # old one would leave the temporary text selectable underneath.
                    scratch = doc.new_page(width=rect.width, height=rect.height)
                    scratch.show_pdf_page(region, original, i)
                    wrapper = next(x[0] for x in scratch.get_xobjects() if x[2] == 0)
                    doc.xref_copy(wrapper, wrappers[0])
                    doc.delete_page(-1)
            doc.saveIncr()


def _same_ink(source, restored):
    # Compare the actual paint that each reader will display after extraction.
    # Subpixel font hinting may change, while displaced glyphs or rules may not.
    def gray(page):
        return page.get_pixmap(
            matrix=pdf.Matrix(2, 2), colorspace=pdf.csGRAY, annots=False
        ).pil_image()

    left, right = gray(source), gray(restored)
    if left.size != right.size:
        return False
    for a, b in ((left, right), (right, left)):
        displaced = (np.asarray(a) < 110) & (
            np.asarray(b.filter(ImageFilter.MinFilter(3))) > 200
        )
        if displaced.sum() > 2:
            return False
    return True


def _write(source, path, doc, indexes):
    originals, changed = {}, []
    with (
        PdfReader(source) as reader,
        closing(PdfWriter(reader, incremental=True)) as writer,
    ):
        for i in indexes:
            if _rewrite(writer, writer.pages[i], doc[i], originals):
                changed.append(i)
        if changed:
            writer.write(path)
    return originals, changed


@contextmanager
def prepare_compound_math(source, skip_last=0):
    with TemporaryDirectory(prefix="pdf2zh-compound-") as temp:
        path = Path(temp) / Path(source).name
        with pdf.open(source) as doc:
            try:
                maps, changed = _write(
                    source, path, doc, range(max(0, len(doc) - skip_last))
                )
            except PdfReadError as exc:
                # 原流程可以用 MuPDF 打开部分无法用 pypdf 拆解的内容流。
                print(f"复合公式预处理沿用原稿：{exc}")
                maps, changed = {}, []
            if changed:
                accepted = []
                # Restore only in this in-memory copy, then keep each page whose
                # original glyphs, lines and surrounding text survive the round trip.
                with pdf.open(path) as check:
                    seen = set()
                    for i in changed:
                        for ref, _, kind, name, *_ in check[i].get_fonts():
                            if (
                                kind == "Type3"
                                and name == "CMMICompound"
                                and ref not in seen
                            ):
                                restore_compound(check, ref)
                                seen.add(ref)
                        if _same_ink(doc[i], check[i]):
                            accepted.append(i)
                if len(accepted) != len(changed):
                    print(
                        "复合公式预处理沿用原稿的页码：",
                        [i + 1 for i in changed if i not in accepted],
                    )
                    maps, _ = _write(source, path, doc, accepted)
        yield (str(path), maps) if maps else (source, {})
