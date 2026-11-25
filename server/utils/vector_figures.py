"""Keep text, symbols and paths in dense diagrams together during PDF reflow."""

import hashlib
import math
import re
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pymupdf as pdf
from pypdf import PdfReader
from utils.compound_math import is_math_face, is_math_text, math_words_only
from utils.page_overlay import figure_boxes

CAPTION = re.compile(
    r"^(Fig(?:ure)?s?\.?|Table|图|表)\s*\d+[a-z]?(?=[.：:、\s—–-])", re.IGNORECASE
)


@dataclass
class Figure:
    page: int
    box: pdf.Rect
    pixels: tuple = ()
    digest: bytes = b""


def _overlap_x(a, b):
    return min(a.x1, b.x1) - max(a.x0, b.x0)


def _touch(a, b, gap):
    dx = max(a.x0 - b.x1, b.x0 - a.x1, 0)
    dy = max(a.y0 - b.y1, b.y0 - a.y1, 0)
    return (dx == 0 and dy <= gap) or (dy == 0 and dx <= gap)


def _groups(boxes, gap, captions):
    dividers = [
        ((a.x1 + b.x0) / 2, max(a.y0, b.y0))
        for a, _ in captions
        for b, _ in captions
        if a.x1 < b.x0 and abs(a.y0 - b.y0) < 20
    ]
    groups = []
    for box in boxes:
        i = 0
        while i < len(groups):
            other = groups[i]
            union = box | other
            column_pair = any(
                min((box.x0 + box.x1) / 2, (other.x0 + other.x1) / 2)
                < x
                < max((box.x0 + box.x1) / 2, (other.x0 + other.x1) / 2)
                and -4 <= y - union.y1 < 80
                for x, y in dividers
            )
            if (
                _touch(box, other, gap)
                and not column_pair
                and not any(union.intersects(b) for b, _ in captions)
            ):
                box = union
                groups.pop(i)
                i = 0
            else:
                i += 1
        groups.append(box)
    return groups


def _complex_path(draw):
    return any(
        it[0] in ("c", "qu")
        or (
            it[0] == "l"
            and abs(it[1].x - it[2].x) > 0.5
            and abs(it[1].y - it[2].y) > 0.5
        )
        for it in draw["items"]
    )


def _lines(page):
    data = page.get_text("dict", flags=pdf.TEXTFLAGS_DICT & ~pdf.TEXT_PRESERVE_IMAGES)
    return [
        (
            pdf.Rect(line["bbox"]),
            re.sub(r"[\x00-\x1f]", " ", "".join(s["text"] for s in line["spans"])).strip(),
        )
        for block in data["blocks"]
        for line in block.get("lines", ())
    ]


def _math_table_rows(page, captions, draws):
    # Repeated, coextensive rules identify rows. Only freeze data consisting
    # of mathematical expressions and terse identifiers. Headings and prose
    # still go through translation, including mixed tables on the same page.
    rules = defaultdict(list)
    for draw in draws:
        rect = draw["rect"]
        if (
            len(draw["items"]) == 1
            and rect.height < 1
            and rect.width > page.rect.width * 0.23
        ):
            rules[round(rect.x0), round(rect.x1)].append((rect.y0 + rect.y1) / 2)
    spans = page.get_texttrace()
    for (left, right), lines in rules.items():
        ys = sorted({round(y, 2) for y in lines})
        if len(ys) < 3:
            continue
        for cap in captions:
            if not left < (cap.x0 + cap.x1) / 2 < right:
                continue
            # Start at the ruled edge next to this caption; do not connect
            # distant tables sharing identical column widths.
            edge = min(
                range(len(ys)),
                key=lambda i: min(abs(ys[i] - cap.y0), abs(ys[i] - cap.y1)),
            )
            if min(abs(ys[edge] - cap.y0), abs(ys[edge] - cap.y1)) > 28:
                continue
            step = -1 if ys[edge] < cap.y0 else 1
            limit = edge
            while (
                0 <= limit + step < len(ys)
                and 3 < abs(ys[limit + step] - ys[limit]) < 55
            ):
                limit += step
            start = None
            nearby = ys[min(edge, limit) : max(edge, limit) + 1]
            for top, bottom in pairwise(nearby):
                math_count = 0
                prose = False
                for span in spans:
                    chars = "".join(
                        chr(c[0]) if 0 <= c[0] <= 0x10FFFF else "\ufffd"
                        for c in span["chars"]
                        if left <= c[2][0] <= right and top < c[2][1] < bottom
                    )
                    if is_math_text(span["font"], chars):
                        math_count += len(chars)
                    elif re.search(r"[a-z]{2,}|[A-Z]{6,}|[\u3400-\u9fff\ufffd]", chars):
                        prose = True
                if math_count >= 10 and not prose:
                    start = top if start is None else start
                elif start is not None:
                    yield pdf.Rect(
                        left - 1, math.floor(start), right + 1, math.ceil(top)
                    )
                    start = None
            if start is not None:
                yield pdf.Rect(
                    left - 1, math.floor(start), right + 1, math.ceil(nearby[-1] + 0.5)
                )


def _display_math_regions(page, draws):
    # 长推导中多层乘积、求和与分式共用数行。以正文为边界保留独立的原版推导，
    # 避免引擎把各行压成一个段落。含说明、表格或插图的区域走各自的处理流程。
    if not any("CMEX" in f[3] or "Math" in f[3] for f in page.get_fonts()):
        return []
    blocks = page.get_text(
        "dict", flags=pdf.TEXTFLAGS_DICT & ~pdf.TEXT_PRESERVE_IMAGES
    )["blocks"]
    spans = [
        s
        for b in blocks
        for line in b.get("lines", ())
        for s in line["spans"]
        if s["text"].strip()
    ]
    barriers = []
    for s in spans:
        if re.search(r"[\u3400-\u9fff]", s["text"]) or (
            re.search(r"[A-Za-z]{2,}", s["text"])
            and not is_math_text(s["font"], s["text"])
            and not math_words_only(s["text"])
        ):
            barriers.append((s["bbox"][1], s["bbox"][3]))
    if not barriers:
        return []
    intervals = []
    for a, b in sorted(barriers):
        if intervals and a <= intervals[-1][1]:
            intervals[-1] = (intervals[-1][0], max(b, intervals[-1][1]))
        else:
            intervals.append((a, b))
    gaps = zip(
        [0, *[b for _, b in intervals]], [*[a for a, _ in intervals], page.rect.height]
    )
    result = []
    for y0, y1 in gaps:
        if y1 - y0 < 40:
            continue
        band = [s for s in spans if y0 < s["origin"][1] < y1]
        # Require large operators on multiple lines, not a lone tall radical.
        large = sorted(
            s["origin"][1]
            for s in band
            if "CMEX" in s["font"] or re.search(r"[∑∏∫]", s["text"])
        )
        if len(large) < 3 or large[-1] - large[0] < 22:
            continue
        if sum(len(s["text"].strip()) for s in band if is_math_face(s["font"])) < 45:
            continue
        rect = pdf.Rect()
        for s in band:
            rect |= pdf.Rect(s["bbox"])
        if rect.height < 40 or rect.width < 40:
            continue
        if any(pdf.Rect(im["bbox"]).intersects(rect) for im in page.get_image_info()):
            continue
        if any(
            ((d["rect"].width < 1 and d["rect"].height > 5) or _complex_path(d))
            and rect.intersects(d["rect"] + (-0.5, -0.5, 0.5, 0.5))
            for d in draws
        ):
            continue
        rect = (
            pdf.Rect(
                math.floor(rect.x0 - 1.5),
                max(math.ceil(y0), math.floor(rect.y0 - 2)),
                math.ceil(rect.x1 + 1.5),
                min(math.floor(y1), math.ceil(rect.y1 + 2)),
            )
            & page.rect
        )
        if not rect.is_empty:
            result.append(rect)
    if not result:
        return []
    # 正文里的行内分式可能高出本行文字框。沿整页真实留白切分，不能将下一句
    # 的分子或上标裁进上一块推导；找不到清楚边界时沿用原翻译流程。
    zoom = min(2.0, math.sqrt(9_000_000 / max(1, page.rect.get_area())))
    pix = page.get_pixmap(
        matrix=pdf.Matrix(zoom, zoom), colorspace=pdf.csGRAY, annots=False
    )
    pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    blank = pixels.min(axis=1) > 242
    runs = np.flatnonzero(np.diff(np.r_[False, blank, False])).reshape(-1, 2)
    seams = [(a / zoom, b / zoom) for a, b in runs if b - a >= 1.5 * zoom]

    def edge(value):
        near = [(a, b) for a, b in seams if a - 12 <= value <= b + 12]
        if not near:
            return None
        a, b = min(near, key=lambda pair: max(pair[0] - value, value - pair[1], 0))
        lo, hi = math.ceil(a + 0.25), math.floor(b - 0.25)
        return (
            min(hi, max(lo, round((max(a, value - 12) + min(b, value + 12)) / 2)))
            if lo <= hi
            else None
        )

    isolated = []
    for box in result:
        top, bottom = edge(box.y0), edge(box.y1)
        if top is not None and bottom is not None and bottom - top >= 40:
            isolated.append(pdf.Rect(box.x0, top, box.x1, bottom))
    return isolated


def find_figures(path, skip_last=0):
    found = []
    with pdf.open(path) as doc, PdfReader(path) as structure:
        for index in range(max(0, len(doc) - skip_last)):
            page = doc[index]
            if (
                page.rotation
                or page.rect != page.cropbox
                or any(a.type[1] == "Redact" for a in page.annots() or ())
            ):
                continue
            blocks = _lines(page)
            captions = [
                (box, m[1].lower())
                for box, text in blocks
                if (m := CAPTION.match(text))
            ]
            draws = page.get_drawings()
            found.extend(Figure(index, box) for box in _display_math_regions(page, draws))
            if not captions:
                continue
            table_captions = [
                box for box, label in captions if label in ("table", "表")
            ]
            if table_captions:
                regions = _math_table_rows(page, table_captions, draws)
                found.extend(
                    Figure(index, box)
                    for box in {tuple(r): r for r in regions}.values()
                )
            if len(table_captions) == len(captions):
                continue
            images = [pdf.Rect(im["bbox"]) for im in page.get_image_info()]
            if (
                len(draws) < 8 or not any(_complex_path(draw) for draw in draws)
            ) and len(images) < 2:
                continue
            centers = [(d["rect"].tl + d["rect"].br) / 2 for d in draws]
            words = page.get_text("words")
            # 原稿有 Form 时用出版文件给出的边界；扁平化矢量图按相邻路径聚类。
            boxes = (
                figure_boxes(structure.pages[index], page)
                if page.get_xobjects()
                else []
            )
            if not boxes:
                boxes = page.cluster_drawings(
                    drawings=draws, x_tolerance=5, y_tolerance=5
                )
            tiles = [
                b
                for b in boxes
                if b.width >= 32
                and b.height >= 12
                and b.get_area() < 0.9 * page.rect.get_area()
                and (
                    sum(b.contains(c) for c in centers) >= 4
                    or sum(b.contains(im.tl) for im in images) >= 2
                )
                and sum(b.contains(pdf.Point(w[0], w[1])) for w in words) >= 2
            ]
            if not tiles:
                continue
            barriers = [b for b, _ in captions]
            for cluster in _groups(tiles, min(28, page.rect.width * 0.06), captions):
                candidates = []
                for caption, label in captions:
                    if (
                        _overlap_x(caption, cluster)
                        < min(caption.width, cluster.width) / 2
                    ):
                        continue
                    gap = max(caption.y0 - cluster.y1, cluster.y0 - caption.y1)
                    extra_labels = [
                        b
                        for b, t in blocks
                        if b not in barriers
                        and cluster.y1 - 2 <= b.y0 < b.y1 <= caption.y0
                        and len(t.split()) <= 12
                        and _overlap_x(b, cluster) > 0.8 * b.width
                    ]
                    if -1 <= gap <= 26 or (
                        26 < gap <= 50
                        and extra_labels
                        and caption.y0 - max(b.y1 for b in extra_labels) < 20
                    ):
                        candidates.append((gap, label, caption))
                if not candidates:
                    continue
                if (
                    sum(1 for _, label, _ in candidates if label not in ("table", "表"))
                    > 1
                ):
                    # 分别配有图题的并排图不能合成一个跨栏矩形。
                    continue
                _, label, caption = min(candidates, key=lambda c: c[0])
                if label in ("table", "表"):
                    continue
                # 标签有时由页面单独绘制（如面板名和图例），纳入相邻短行，但保留图题和正文。
                bounds = pdf.Rect(cluster)
                lower = (
                    min(caption.y0 - 1, cluster.y1 + 42)
                    if caption.y0 > cluster.y1
                    else cluster.y1 + 8
                )
                for box, text in blocks:
                    if (
                        box not in barriers
                        and len(text.split()) <= 12
                        and _overlap_x(box, cluster) > 0.8 * box.width
                        and box.y0 >= cluster.y0 - 13
                        and box.y1 <= lower
                        and box.height <= 18
                    ):
                        bounds |= box
                box = (bounds + (-2, -4, 2, 1)) & page.rect
                if caption.y0 >= cluster.y1 - 1:
                    box.y1 = min(box.y1, caption.y0 - 1)
                else:
                    box.y0 = max(box.y0, caption.y1 + 1)
                # 单栏图的留白可能碰到相邻栏图题；限缩留白，不裁掉作者图块。
                for other in barriers:
                    if other != caption and box.intersects(other):
                        if cluster.x1 <= other.x0:
                            box.x1 = min(box.x1, math.floor(other.x0))
                        elif cluster.x0 >= other.x1:
                            box.x0 = max(box.x0, math.ceil(other.x1))
                if not box.is_empty and not any(box.intersects(b) for b in barriers):
                    box = pdf.Rect(box.irect)
                    for previous in reversed(found):
                        if previous.page != index:
                            break
                        overlap = previous.box & box
                        if (
                            not overlap.is_empty
                            and overlap.width <= 4
                            and overlap.height > overlap.width
                        ):
                            # 两幅并列图的留白只画一次，以免导出重复的文字层。
                            left, right = sorted(
                                (previous.box, box), key=lambda region: region.x0
                            )
                            left.x1 = right.x0 = round((overlap.x0 + overlap.x1) / 2)
                    found.append(Figure(index, box))
    return found


def _erase(page, regions):
    for rect in regions:
        page.add_redact_annot(rect, fill=False, cross_out=False)
    # 删除旧文字以免重复搜索。对原有图形做反向裁切，跨越图边界的线条在图外仍照原样绘制。
    page.apply_redactions(
        images=pdf.PDF_REDACT_IMAGE_NONE, graphics=pdf.PDF_REDACT_LINE_ART_NONE
    )
    clip_out(page, regions)


def clip_out(page, regions):
    """Exclude regions from the old drawing without deleting neighbouring text."""
    page.wrap_contents()
    matrix = ~page.transformation_matrix

    def rectangle(rect):
        r = rect * matrix
        return f"{r.x0:.8f} {r.y0:.8f} {r.width:.8f} {r.height:.8f} re"

    doc = page.parent
    content = ["q"]
    for box in regions:
        content.extend([rectangle(page.rect), rectangle(box), "W* n"])
    refs = []
    for stream in ("\n".join(content) + "\n", "\nQ\n"):
        ref = doc.get_new_xref()
        doc.update_object(ref, "<<>>")
        doc.update_stream(ref, stream.encode("ascii"))
        refs.append(ref)
    all_refs = [refs[0], *page.get_contents(), refs[1]]
    doc.xref_set_key(
        page.xref, "Contents", "[" + " ".join(f"{ref} 0 R" for ref in all_refs) + "]"
    )


@contextmanager
def prepare_vector_figures(source, skip_last=0):
    figures = find_figures(source, skip_last)
    if not figures:
        yield source, figures
        return
    with TemporaryDirectory(prefix="pdf2zh-figures-") as work:
        prepared = Path(work) / Path(source).name
        with pdf.open(source) as original, pdf.open(source) as doc:
            for index in sorted({fig.page for fig in figures}):
                page = doc[index]
                group = [fig for fig in figures if fig.page == index]
                _erase(page, [fig.box for fig in group])
                for fig in group:
                    # 临时位图让排版引擎为整图预留空间。指纹仅用于找回此任务插入的对象。
                    pix = original[index].get_pixmap(
                        clip=fig.box, alpha=False, annots=False
                    )
                    stamp = hashlib.sha256(f"{index}/{fig.box}".encode()).digest()
                    for x, val in enumerate(stamp[: min(24, pix.width)]):
                        r, g, b = pix.pixel(x, 0)
                        pix.set_pixel(x, 0, (r, g, (b & 254) | (val & 1)))
                    fig.pixels = (pix.width, pix.height)
                    fig.digest = pix.digest
                    page.insert_image(fig.box, pixmap=pix)
            doc.save(prepared, garbage=1, deflate=True)
        yield str(prepared), figures


def _fragment(original, fig):
    part = pdf.open()
    try:
        part.insert_pdf(
            original, from_page=fig.page, to_page=fig.page, links=False, annots=False
        )
        page = part[0]
        page.clip_to_rect(fig.box)
        page.set_cropbox(fig.box)
        return part
    except Exception:
        part.close()
        raise


def restore_vector_figures(source, outputs, figures):
    if not figures:
        return
    lookup = {(fig.pixels, fig.digest): fig for fig in figures}
    sizes = {fig.pixels for fig in figures}
    with pdf.open(source) as original:
        for path in outputs:
            with pdf.open(path) as doc:
                restored = set()
                refs = {
                    im[0]: im[2:4]
                    for page in doc
                    for im in page.get_images(full=True)
                    if im[2:4] in sizes
                }
                for ref, size in refs.items():
                    fig = lookup.get((size, pdf.Pixmap(doc, ref).digest))
                    if fig is None:
                        continue
                    # 图像 Do 使用单位正方形。用同一坐标空间的矢量 Form 替换，其它侧、裁切和画序不变。
                    scratch = doc.new_page(width=1, height=1)
                    with _fragment(original, fig) as fragment:
                        scratch.show_pdf_page(
                            scratch.rect, fragment, 0, keep_proportion=False
                        )
                    wrapper = next(x[0] for x in scratch.get_xobjects() if not x[2])
                    # show_pdf_page 写入的五位小数会在数百点宽的图上放大，重新精确归一化原图 BBox。
                    values = doc.xref_get_key(wrapper, "BBox")[1].strip("[]").split()
                    bounds = pdf.Rect(*map(float, values))
                    matrix = (
                        1 / bounds.width,
                        0,
                        0,
                        1 / bounds.height,
                        -bounds.x0 / bounds.width,
                        -bounds.y0 / bounds.height,
                    )
                    doc.xref_set_key(
                        wrapper,
                        "Matrix",
                        "[" + " ".join(f"{v:.12f}" for v in matrix) + "]",
                    )
                    doc.xref_copy(wrapper, ref)
                    doc.delete_page(-1)
                    restored.add(fig.digest)
                if restored != {fig.digest for fig in figures}:
                    raise ValueError("输出缺少密集结构图的定位对象，未能验证矢量图还原")
                doc.saveIncr()


def translated_box(figure, captions):
    box = pdf.Rect(figure.box)
    for cap in captions:
        if _overlap_x(cap, box) <= 0:
            continue
        if box.y0 < cap.y0 and box.y1 - 12 <= cap.y0 <= box.y1 + 1:
            box.y1 = min(box.y1, cap.y0 - 0.25)
        if cap.y1 < box.y1 and box.y0 - 1 <= cap.y1 <= box.y0 + 12:
            box.y0 = max(box.y0, cap.y1 + 0.25)
    return box


def repair_existing_mono(source, target, figures):
    """Repair a staged existing translation with the same physical pages as the source."""
    with pdf.open(source) as original, pdf.open(target) as doc:
        if len(doc) != len(original):
            raise ValueError("原文和单语译本页数不一致")
        for index in sorted({f.page for f in figures}):
            page = doc[index]
            if (
                page.rect != original[index].rect
                or page.rotation
                or any(a.type[1] == "Redact" for a in page.annots() or ())
            ):
                raise ValueError(f"第 {index + 1} 页无法按原坐标安全还原")
            captions = [box for box, text in _lines(page) if CAPTION.match(text)]
            regions = [
                translated_box(fig, captions) for fig in figures if fig.page == index
            ]
            _erase(page, regions)
            for box in regions:
                page.show_pdf_page(
                    box, original, index, clip=box, keep_proportion=False
                )
        if figures:
            doc.saveIncr()
