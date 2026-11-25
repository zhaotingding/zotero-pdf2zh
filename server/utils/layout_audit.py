"""Check the rendered translation against math, grid lines and backgrounds in its source."""

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from itertools import pairwise

import numpy as np
import pymupdf as pdf
from PIL import Image, ImageFilter
from utils.compound_math import is_math_face, is_math_text, math_words_only
from utils.vector_figures import CAPTION, _lines


@dataclass(frozen=True)
class Glyph:
    face: str
    char: str
    x: float
    y: float
    size: float
    right: float = 0.0
    name: str = ""


@dataclass
class Mark:
    area: pdf.Rect
    kind: str
    color: tuple = ()


@dataclass
class Issue:
    kind: str
    box: pdf.Rect
    destination: pdf.Rect | None = None
    erase_rule: bool = False
    repairable: bool = True
    detail: str = ""


def _horizontal(draw):
    """Use visible source paths, independently of the preparer's operator matching."""
    box = draw["rect"]
    if len(draw["items"]) != 1 or box.width < 1 or box.height > 1.5:
        return None
    if (
        draw["type"] == "s"
        and draw["items"][0][0] == "l"
        and box.height < 0.01
        and draw["stroke_opacity"] == 1
        and draw["color"] in ((0.0,), (0.0, 0.0, 0.0))
        and 0 < draw["width"] <= 1.5
    ):
        return box + (0, -draw["width"] / 2, 0, draw["width"] / 2)
    if (
        draw["type"] == "f"
        and draw["items"][0][0] == "re"
        and draw["fill_opacity"] == 1
        and draw["fill"] in ((0.0,), (0.0, 0.0, 0.0))
        and box.height > 0
    ):
        return box
    return None


def _colored(draw):
    fill = draw["fill"]
    return (
        draw["type"] in ("f", "fs")
        and fill
        and len(fill) == 3
        and max(fill) - min(fill) > 0.08
        and draw["fill_opacity"] == 1
        and len(draw["items"]) == 1
        and draw["items"][0][0] == "re"
    )


class PageInk:
    def __init__(self, page, render=None):
        self.page = page
        self.render = render
        # 普通论文按 216 dpi 核对，超大页面限制整页光栅内存。
        self.zoom = min(
            3.0,
            math.sqrt(9_000_000 / max(1, page.rect.get_area())),
            8192 / max(1, page.rect.width, page.rect.height),
        )
        self.draws = page.get_drawings()
        self.boxes = []  # math glyph paint bounds and paint order, including Type 3 runs
        self.glyphs = []
        self.outer_bars = [r for d in self.draws if (r := _horizontal(d)) is not None]
        self.bars = list(self.outer_bars)
        self.by_key = defaultdict(list)
        self.bins = defaultdict(list)
        self.rows = defaultdict(list)
        self._compound = {}
        self._glyph_names = {}
        self._fonts = {}
        font_refs = defaultdict(set)
        for row in page.get_fonts(full=True):
            font_refs[row[3].split("+")[-1]].add(row[0])
        for span in page.get_texttrace():
            if (
                span["type"] not in (0, 1)
                or span["opacity"] <= 0
                or span["size"] <= 0
                or span["dir"] != (1, 0)
            ):
                continue
            face = span["font"].split("+")[-1]
            if face.startswith("Type3 (") and self._expand(span):
                continue
            simple_math = is_math_face(face) or math_words_only(
                "".join(chr(c[0]) for c in span["chars"] if 0 <= c[0] <= 0x10FFFF)
            )
            run = []
            for code, gid, (x, y), box in span["chars"]:
                if not 0 <= code <= 0x10FFFF or chr(code).isspace():
                    continue
                char = chr(code)
                name = ""
                if face.startswith("CMEX") and len(font_refs[face]) == 1 and gid >= 0:
                    if face not in self._fonts:
                        data = page.parent.extract_font(next(iter(font_refs[face])))[-1]
                        self._fonts[face] = pdf.Font(fontbuffer=data) if data else None
                    if self._fonts[face] is not None:
                        if (face, gid) not in self._glyph_names:
                            self._glyph_names[face, gid] = (
                                pdf.mupdf.fz_get_glyph_name2(
                                    self._fonts[face].this, gid
                                )
                                or ""
                            )
                        name = self._glyph_names[face, gid]
                self._add(Glyph(face, char, x, y, span["size"], box[2], name))
                if simple_math and not "\u3400" <= char <= "\u9fff":
                    if run and (
                        abs(y - run[-1][2]) > 0.3
                        or x < run[-1][1] - 0.2 * span["size"]
                        or x > run[-1][0].x1 + 0.8 * span["size"]
                    ):
                        self._record_run(run, span["seqno"], face)
                        run = []
                    run.append((pdf.Rect(box), x, y))
            if run:
                self._record_run(run, span["seqno"], face)

    def _record_run(self, run, seqno, face):
        # 一个 Tj 可以从单元格外开始，在格内结束。按整段的墨迹选中其绘制起点，
        # 同时保留相邻独立 Tj 的起点，防止恢复后在格外残留半个公式。
        bounds = pdf.Rect(run[0][0])
        for box, _, _ in run[1:]:
            bounds |= box
        self.boxes.extend((bounds, x, y, seqno, face) for _, x, y in run)

    def _add(self, g):
        self.glyphs.append(g)
        self.by_key[g.face, g.char].append(g)
        self.bins[g.face, g.char, math.floor(g.x / 4), math.floor(g.y / 4)].append(g)
        self.rows[math.floor(g.y / 12)].append(g)

    def _font_program(self, ref):
        doc = self.page.parent
        if doc.xref_get_key(ref, "BaseFont")[1] != "/CMMICompound":
            return None
        kind, cmap = doc.xref_get_key(ref, "ToUnicode")
        if kind != "xref" or b"/CMapName /CompoundUnicode" not in doc.xref_stream(
            int(cmap.split()[0])
        ):
            return None
        key, val = doc.xref_get_key(ref, "CharProcs/formula")
        if key != "xref":
            return None
        lines = doc.xref_stream(int(val.split()[0])).splitlines()
        # Read the final paint program. Its text origins and filled rules are
        # relative to the single movable glyph, with original embedded fonts.
        unit = None
        glyphs = []
        bars = []
        names = {}
        for line in lines:
            a = line.split()
            if line.startswith(b"q ") and line.endswith(b" cm") and len(a) == 8:
                unit = float(a[1]) / 1000
            elif line.endswith(b" Tf") and len(a) == 3:
                name = a[0][1:].decode("ascii")
                if name not in names:
                    names[name] = (
                        doc.xref_get_key(ref, "Resources/Font/" + name + "/BaseFont")[1]
                        .lstrip("/")
                        .split("+")[-1]
                    )
                face, em = names[name], float(a[1])
            elif len(a) == 9 and a[6] == b"Tm" and a[-1] == b"Tj":
                glyphs.append((face, float(a[4]), -float(a[5]), em * float(a[3])))
            elif len(a) == 6 and a[-2:] == [b"re", b"f"]:
                x, y, w, h = map(float, a[:4])
                bars.append(pdf.Rect(x, -y - h, x + w, -y))
        bounds = doc.xref_get_key(ref, "FontBBox")[1].strip("[]").split()
        return (
            (unit, glyphs, bars, tuple(map(float, bounds)))
            if unit and glyphs and len(bounds) == 4
            else None
        )

    def _expand(self, span):
        match = re.fullmatch(r"Type3 \((\d+) 0 R\)", span["font"])
        if not match:
            return False
        ref = int(match[1])
        if ref not in self._compound:
            self._compound[ref] = self._font_program(ref)
        program = self._compound[ref]
        if not program:
            return False
        unit, glyphs, bars, bounds = program
        chars = span["chars"]
        if len(chars) % len(glyphs):
            return False
        scale = span["size"] * unit
        for start in range(0, len(chars), len(glyphs)):
            char = chars[start]
            x, y = char[2]
            # The single character's nominal bbox omits protruding scripts. Use
            # the restored paint bounds when removing that indivisible show.
            fs = span["size"] / 1000
            inkbox = pdf.Rect(
                x + fs * bounds[0],
                y - fs * bounds[3],
                x + fs * bounds[2],
                y - fs * bounds[1],
            )
            self.boxes.append((inkbox, x, y, span["seqno"], span["font"]))
            for c, (face, dx, dy, em) in zip(
                chars[start : start + len(glyphs)], glyphs, strict=True
            ):
                if 0 <= c[0] <= 0x10FFFF:
                    self._add(
                        Glyph(
                            face, chr(c[0]), x + scale * dx, y + scale * dy, em * scale
                        )
                    )
            for b in bars:
                self.bars.append(
                    pdf.Rect(
                        x + b.x0 * scale,
                        y + b.y0 * scale,
                        x + b.x1 * scale,
                        y + b.y1 * scale,
                    )
                )
        return True

    def band(self, box, padding=0):
        return [
            g
            for k in range(
                math.floor((box.y0 - padding) / 12),
                math.floor((box.y1 + padding) / 12) + 1,
            )
            for g in self.rows.get(k, ())
            if box.x0 - padding <= g.x <= box.x1 + padding
            and box.y0 - padding <= g.y <= box.y1 + padding
        ]

    def near(self, g, x, y, scale, tol):
        ix, iy = math.floor(x / 4), math.floor(y / 4)
        return any(
            abs(p.x - x) <= tol
            and abs(p.y - y) <= tol
            and abs(p.size - g.size * scale) <= max(0.18, 0.045 * p.size)
            for i in range(ix - 1, ix + 2)
            for j in range(iy - 1, iy + 2)
            for p in self.bins.get((g.face, g.char, i, j), ())
        )

    @cached_property
    def pixels(self):
        if self.render is not None:
            return self.render()
        return np.asarray(
            self.page.get_pixmap(
                matrix=pdf.Matrix(self.zoom, self.zoom),
                colorspace=pdf.csRGB,
                annots=False,
            )
            .pil_image()
            .convert("L")
        )

    def has_bar(self, r):
        # A geometrically registered rule must also be visible in the final raster.
        y = (r.y0 + r.y1) / 2
        if not any(
            abs(b.x0 - r.x0) < 0.75
            and abs(b.x1 - r.x1) < 0.75
            and abs((b.y0 + b.y1) / 2 - y) < 0.55
            for b in self.bars
        ):
            return False
        x0, x1 = (
            max(0, math.ceil(self.zoom * r.x0)),
            min(self.pixels.shape[1], math.floor(self.zoom * r.x1)),
        )
        lo, hi = (
            max(0, math.floor(self.zoom * y) - 1),
            min(self.pixels.shape[0], math.ceil(self.zoom * y) + 2),
        )
        a = self.pixels[lo:hi, x0:x1]
        return bool(a.size and np.mean(a.min(axis=0) < 236) > 0.85)


def _math_only(chars):
    if len(chars) < 3:
        return False
    # 避免只凭公式字体名吞掉单元格里的自然语言。按字号、基线和字距断开单词；
    # 普通字体里形如 x+y=z 的内容也可用运算符和孤立操作数识别。
    if not any(g.char in "=+−/∑∏∂∇∈≤≥√" for g in chars) and not (
        len({g.face for g in chars if is_math_face(g.face)}) > 1
        and any(g.char.isdigit() for g in chars)
    ):
        return False
    em = max(g.size for g in chars)
    word = ""
    prev = None
    for g in [*chars, None]:
        if (
            not g
            or not g.char.isalpha()
            or (
                prev
                and (
                    g.face != prev.face
                    or abs(g.y - prev.y) > 0.3
                    or g.x - prev.right > 0.18 * g.size
                    or g.x < prev.x
                )
            )
        ):
            if word and (
                any("\u3400" <= c <= "\u9fff" for c in word)
                or (
                    len(word) > 1
                    and not is_math_text(prev.face, word)
                    and not math_words_only(word)
                    and prev.size > 0.9 * em
                )
            ):
                return False
            word = ""
        if g and g.char.isalpha():
            word += g.char
        prev = g
    return True


def _grid_cells(ink):
    """Follow full and partial rules to find individual cells, including merged rows."""
    captions = [
        b
        for b, s in _lines(ink.page)
        if (m := CAPTION.match(s)) and m[1].lower() in ("table", "表")
    ]
    if not captions:
        return
    horizontals = [r for r in ink.bars if r.width > ink.page.rect.width * 0.19]
    verticals = [
        d["rect"]
        for d in ink.draws
        if len(d["items"]) == 1 and d["rect"].width < 1 and d["rect"].height >= 3
    ]
    for cap in captions:
        edges = [
            r
            for r in horizontals
            if r.x0 < (cap.x0 + cap.x1) / 2 < r.x1
            and min(abs(r.y0 - cap.y1), abs(r.y1 - cap.y0)) < 29
        ]
        if not edges:
            continue
        anchor = min(edges, key=lambda r: min(abs(r.y0 - cap.y1), abs(r.y1 - cap.y0)))
        left, right = anchor.x0, anchor.x1
        near = [
            r
            for r in horizontals
            if r.x0 >= left - 1
            and r.x1 <= right + 1
            and (abs(r.x0 - left) < 1 or abs(r.x1 - right) < 1)
        ]
        direction = 1 if anchor.y0 > cap.y0 else -1
        levels = sorted(
            {round((r.y0 + r.y1) / 2, 2) for r in near}, reverse=direction < 0
        )
        a = round((anchor.y0 + anchor.y1) / 2, 2)
        band = [a]
        for y in levels[levels.index(a) + 1 :]:
            if not 3 < abs(y - band[-1]) < 55:
                break
            band.append(y)
        # A short partial rule inside the next paragraph cannot close a table.
        full = {
            round((r.y0 + r.y1) / 2, 2)
            for r in near
            if abs(r.x0 - left) < 1 and abs(r.x1 - right) < 1
        }
        stop = max((i for i, y in enumerate(band) if y in full), default=0)
        for top, bottom in pairwise(sorted(band[: stop + 1])):
            mid = (top + bottom) / 2
            xs = sorted(
                {
                    round(r.x0, 1)
                    for r in verticals
                    if left + 1 < r.x0 < right - 1 and r.y0 < mid < r.y1
                }
            )
            if not xs:
                continue
            for x0, x1 in pairwise([left, *xs, right]):
                cell = pdf.Rect(x0, top, x1, bottom)
                chars = ink.band(cell)
                if _math_only(chars):
                    yield Mark(cell, "表格数学单元格")


def fixed_regions(ink):
    found = {tuple(m.area): m for m in _grid_cells(ink)}
    for d in ink.draws:
        if not _colored(d):
            continue
        b = d["rect"]
        if b.width < 2.5 * b.height or not 5 < b.height < 45:
            continue
        chars = ink.band(b)
        if not _math_only(chars):
            continue
        found[tuple(b)] = Mark(b, "彩底公式", d["fill"])
    return list(found.values())


def _region_problem(src, dst, mark):
    box = mark.area + (0.55, 0.45, -0.55, -0.45)
    if box.is_empty:
        return False
    # Both crops start at exactly the same device pixel, also at fractional PDF coordinates.
    x, y, x1, y1 = (box * pdf.Matrix(src.zoom, src.zoom)).irect
    a, b = src.pixels[y:y1, x:x1], dst.pixels[y:y1, x:x1]
    if not a.size or a.shape != b.shape:
        return True
    if np.array_equal(a, b):
        return False
    bg = float(np.percentile(a, 85))
    left, right = a < bg * 0.60, b < bg * 0.60
    if mark.kind == "彩底公式":
        if abs(float(np.percentile(b, 85)) - bg) > 15:
            return True
        above = [
            d["seqno"]
            for d in dst.draws
            if _colored(d)
            and max(abs(u - v) for u, v in zip(d["rect"], mark.area)) < 0.65
        ]
        obscured = bool(above) and any(
            n < max(above) and rect.intersects(box) and box.y0 <= yy <= box.y1
            for rect, _, yy, n, _ in dst.boxes
        )
        return bool(
            left.sum() > 12
            and (
                right.sum() < 0.60 * left.sum()
                or (obscured and right.sum() < 0.87 * left.sum())
            )
        )
    # On a fixed cell the author already typeset the entire expression. Allow
    # one pixel of hinting, but never flow formulas into the adjoining label.
    delta = sum(
        np.count_nonzero(
            (one < bg * 0.60)
            & (
                np.asarray(Image.fromarray(other).filter(ImageFilter.MinFilter(3)))
                > bg * 0.85
            )
        )
        for one, other in ((a, b), (b, a))
    )
    return delta > max(6, 0.013 * (left.sum() + right.sum()))


def registered(dst, mark):
    box = mark.area
    # 被挤入数学区域的译文不能作为背景一并盖掉。
    if any(
        "\u3400" <= g.char <= "\u9fff" and box.y0 < g.y - 0.3 * g.size < box.y1
        for g in dst.band(box, padding=2)
        if box.x0 < g.x + 0.4 * g.size < box.x1
    ):
        return False
    if mark.kind == "彩底公式":
        return any(
            _colored(d)
            and max(abs(u - v) for u, v in zip(d["rect"], box)) < 0.75
            and max(abs(u - v) for u, v in zip(mark.color, d["fill"])) < 0.03
            for d in dst.draws
        )
    return all(
        any(
            abs((b.y0 + b.y1) / 2 - y) < 0.65
            and b.x0 <= box.x0 + 1
            and b.x1 >= box.x1 - 1
            for b in dst.bars
        )
        for y in (box.y0, box.y1)
    ) and any(
        d["rect"].width < 1
        and d["rect"].y0 < (box.y0 + box.y1) / 2 < d["rect"].y1
        and min(abs(d["rect"].x0 - x) for x in (box.x0, box.x1)) < 0.75
        for d in dst.draws
    )


def _fraction_features(src, r):
    if not 2 < r.width < 160:
        return None
    cy = (r.y0 + r.y1) / 2
    glyphs = src.band(pdf.Rect(r.x0 - 0.3, cy - 13, r.x1 + 0.1, cy + 16))
    math = [g for g in glyphs if is_math_face(g.face) or g.char.isdigit()]
    top = [g for g in math if 0.20 < cy - g.y < min(8, g.size * 0.85)]
    bottom = [g for g in math if 0.15 < g.y - cy < min(13, g.size * 1.50)]
    if top:
        nearest = min(cy - g.y for g in top)
        top = [g for g in top if cy - g.y < nearest + 0.48 * g.size]
    if not top or not bottom:
        return None
    return top, bottom


def _root_features(src, r):
    # TeX draws the radical hook and its overbar separately. Match their
    # junction rather than borrowing a numerator from the preceding line.
    cy = (r.y0 + r.y1) / 2
    left = src.band(pdf.Rect(r.x0 - 23, cy - 4, r.x0, cy + 5))
    heads = [
        g
        for g in left
        if (g.char == "√" or g.name.startswith("radical"))
        and abs(g.right - r.x0) < 0.75
    ]
    if len(heads) != 1:
        return None
    head = heads[0]
    inside = [
        g
        for g in src.band(
            pdf.Rect(r.x0 - 0.15, cy - 1, r.x1 + 0.1, cy + 2.2 * head.size)
        )
        if is_math_face(g.face) and 0.1 < g.y - cy < 2 * head.size
    ]
    return ([head], inside) if inside else None


def _unchanged_patch(src, dst, box):
    box = (box + (-1.5, -3, 1.5, 5)) & src.page.rect
    x, y, x1, y1 = (box * pdf.Matrix(src.zoom, src.zoom)).irect
    a, b = src.pixels[y:y1, x:x1], dst.pixels[y:y1, x:x1]
    return _same_shape(a, b)


def _same_shape(a, b):
    if a.shape != b.shape or not a.size:
        return False
    if np.max(np.abs(a.astype(np.int16) - b)) < 30:
        return True
    count = ((a < 140) | (b < 140)).sum()
    if count < 8:
        return False
    for one, other in ((a, b), (b, a)):
        if np.count_nonzero(
            (one < 125)
            & (
                np.asarray(Image.fromarray(other).filter(ImageFilter.MinFilter(3)))
                > 190
            )
        ) > max(1, 0.015 * count):
            return False
    return True


def _visual_witness(src, dst, r, claimed, root):
    # A font may be substituted or have no Unicode mapping. Verify the actual
    # numerator/denominator (or radicand) against visible rules at their new scale.
    cy = (r.y0 + r.y1) / 2
    features = root or _fraction_features(src, r)
    em = max(g.size for side in features for g in side)
    region = pdf.Rect(r.x0, cy - (0.18 if root else 1.2) * em, r.x1, cy + 1.4 * em)

    def crop(ink, box):
        x, y, x1, y1 = (box * pdf.Matrix(ink.zoom, ink.zoom)).irect
        return ink.pixels[max(0, y) : max(0, y1), max(0, x) : max(0, x1)]

    sample = crop(src, region)
    if not sample.size or (sample < 140).sum() < 8:
        return None
    for b in sorted(dst.bars, key=lambda b: abs(b.y0 - r.y0) + 0.1 * abs(b.x0 - r.x0)):
        k = b.width / r.width
        if tuple(b) in claimed or not 0.35 <= k <= 1.65 or abs(b.y0 - r.y0) > 96:
            continue
        y = (b.y0 + b.y1) / 2
        target = crop(
            dst,
            pdf.Rect(b.x0, y + (region.y0 - cy) * k, b.x1, y + (region.y1 - cy) * k),
        )
        if (
            target.size
            and _same_shape(
                sample,
                np.asarray(
                    Image.fromarray(target).resize(
                        (sample.shape[1], sample.shape[0]), Image.Resampling.BILINEAR
                    )
                ),
            )
            and dst.has_bar(b)
        ):
            return b
    return None


RULE_KINDS = {"分数线", "根号横线"}


def _placements(dst, r, top, bottom):
    chars = top + bottom
    # Use a relatively uncommon symbol on each side so a missing script cannot
    # prevent the other original characters from locating the fraction.
    seeds = [
        min(row, key=lambda g: len(dst.by_key[g.face, g.char]) or 10**8)
        for row in (top, bottom)
    ]
    candidates = {}
    cy = (r.y0 + r.y1) / 2
    for g in seeds:
        for target in dst.by_key[g.face, g.char]:
            scale = target.size / g.size
            if not 0.3 <= scale <= 2 or abs(target.y - g.y) > 96:
                continue
            x = target.x + (r.x0 - g.x) * scale
            y = target.y + (cy - g.y) * scale
            key = (round(x * 2), round(y * 2), round(scale * 20))
            if key in candidates:
                continue
            tol = max(0.35, 0.075 * target.size)
            hits = [
                sum(
                    dst.near(
                        ch,
                        x + (ch.x - r.x0) * scale,
                        y + (ch.y - cy) * scale,
                        scale,
                        tol,
                    )
                    for ch in row
                )
                for row in (top, bottom)
            ]
            if not min(hits) or sum(hits) < max(2, len(chars) * 0.65):
                continue
            point = pdf.Rect(
                x,
                y - r.height * scale / 2,
                x + r.width * scale,
                y + r.height * scale / 2,
            )
            candidates[key] = (sum(hits), point)
    if not candidates:
        return []
    best = max(hits for hits, _ in candidates.values())
    found = []
    for hits, box in candidates.values():
        if hits == best and not any(
            max(abs(a - b) for a, b in zip(box, v)) < 0.9 for v in found
        ):
            found.append(box)
    return found


def inspect_page(src, dst):
    issues = []
    claimed = set()
    regions = fixed_regions(src)
    for m in regions:
        if _region_problem(src, dst, m):
            issues.append(Issue(m.kind, m.area, repairable=registered(dst, m)))
    for r in src.bars:
        if any(m.area.contains(r) for m in regions):
            continue
        if _unchanged_patch(src, dst, r):
            claimed.add(tuple(r))
            continue
        if any(
            g.name.startswith("braceh") and abs(g.right - r.x0) < 0.75
            for g in src.band(pdf.Rect(r.x0 - 20, r.y0 - 4, r.x0, r.y1 + 4))
        ):
            continue
        # A partial border of a merged cell can be as short as a fraction bar.
        # Both ends meet vertical rules; it has no numerator or denominator.
        if all(
            any(
                abs(d["rect"].x0 - x) < 0.55
                and d["rect"].width < 0.6
                and d["rect"].height > 3
                and d["rect"].y0 - 1 <= r.y0 <= d["rect"].y1 + 1
                for d in src.draws
            )
            for x in (r.x0, r.x1)
        ):
            continue
        root = _root_features(src, r)
        kind = "根号横线" if root else "分数线"
        features = root or _fraction_features(src, r)
        if not features or not src.has_bar(r):
            continue
        top, bottom = features
        if dst.has_bar(r) and all(
            sum(dst.near(g, g.x, g.y, 1, 0.65) for g in side)
            >= max(1, 0.65 * len(side))
            for side in (top, bottom)
        ):
            claimed.add(tuple(r))
            continue
        places = _placements(dst, r, top, bottom)
        aligned = [p for p in places if max(abs(a - b) for a, b in zip(p, r)) < 0.8]
        residual = not aligned and any(
            max(abs(a - b) for a, b in zip(b, r)) < 0.30 for b in dst.outer_bars
        )
        missing = [p for p in places if not dst.has_bar(p)]
        if len(places) == 1 and (missing or residual):
            p = places[0]
            scale = p.width / r.width
            certain = all(
                dst.near(
                    g,
                    p.x0 + (g.x - r.x0) * scale,
                    p.y0 + (g.y - r.y0) * scale,
                    scale,
                    max(0.35, 0.075 * g.size * scale),
                )
                for g in top + bottom
            )
            issues.append(
                Issue(
                    kind,
                    r,
                    missing[0] if missing else None,
                    residual,
                    certain,
                    "" if certain else "只有部分原文字形能对应",
                )
            )
        elif residual or missing or not places:
            witness = None if residual else _visual_witness(src, dst, r, claimed, root)
            if witness is not None:
                claimed.add(tuple(witness))
            else:
                issues.append(
                    Issue(
                        kind,
                        r,
                        repairable=False,
                        detail="目标位置不唯一" if places else "无法对应原文字形",
                    )
                )
        elif places:
            matched = min(
                places, key=lambda q: abs(q.y0 - r.y0) + 0.1 * abs(q.x0 - r.x0)
            )
            for b in dst.bars:
                if max(abs(a - c) for a, c in zip(b, matched)) < 0.75:
                    claimed.add(tuple(b))
    # 两个不同的原分式若都指向同一个缺线位置，字符相同也不足以证明对应关系。
    unique = {}
    for v in issues:
        unique.setdefault((v.kind, tuple(v.box)), v)
    issues = list(unique.values())
    proposals = [v for v in issues if v.destination is not None and v.repairable]
    for i, v in enumerate(proposals):
        for other in proposals[i + 1 :]:
            if max(abs(a - b) for a, b in zip(v.destination, other.destination)) < 0.65:
                v.repairable = other.repairable = False
                v.detail = other.detail = "多个原式对应同一目标"
    return issues
