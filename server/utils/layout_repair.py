"""Repair verified local layout defects and validate the final saved PDF before publishing."""

import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pymupdf as pdf
from PIL import Image, ImageFilter
from pypdf.errors import PdfReadError
from pypdf.generic import ArrayObject, ContentStream, DecodedStreamObject
from utils.compound_math import _rules
from utils.layout_audit import (
    RULE_KINDS,
    Issue,
    PageInk,
    fixed_regions,
    inspect_page,
    registered,
)
from utils.vector_figures import Figure, _fragment, clip_out


def _pdf_stream(data):
    obj = DecodedStreamObject()
    obj.set_data(data)
    return ContentStream(obj, None, forced_encoding="bytes")


def _replace_contents(page, data):
    doc = page.parent
    ref = doc.get_new_xref()
    doc.update_object(ref, "<<>>")
    doc.update_stream(ref, data, compress=True)
    page.set_contents(ref)


def _patch(page, original, index, ink, issues):
    # Edit individual text shows and path paints. Broad text redactions remove
    # Chinese on adjacent lines when CJK font boxes extend past the visible ink.
    panels = []
    for box in sorted(
        (v.box for v in issues if v.kind not in RULE_KINDS), key=lambda b: (b.y0, b.x0)
    ):
        if (
            panels
            and abs(panels[-1].x1 - box.x0) < 0.05
            and abs(panels[-1].y0 - box.y0) < 0.05
            and abs(panels[-1].y1 - box.y1) < 0.05
        ):
            panels[-1] = panels[-1] | box
        else:
            panels.append(box)
    protected = [
        (r, x, y, face)
        for r, x, y, _, face in ink.boxes
        if any(b.y0 - 0.15 <= y <= b.y1 + 0.15 and r.intersects(b) for b in panels)
    ]
    font_names = {
        name: (f"Type3 ({ref} 0 R)" if kind == "Type3" else base.split("+")[-1])
        for ref, _, kind, base, name, *_ in page.get_fonts()
    }
    points = {(face, round(x, 1), round(y, 1)) for _, x, y, face in protected}
    content = _pdf_stream(page.read_contents())
    ops = content.operations
    rules = _rules(page, ops) if any(v.erase_rule for v in issues) else ()
    add_lines = []
    pending = []
    changed = False
    for v in issues:
        if v.kind not in RULE_KINDS:
            continue
        if v.erase_rule:
            matches = [
                r for r in rules if max(abs(a - b) for a, b in zip(r.box, v.box)) < 0.20
            ]
            if len(matches) != 1:
                pending.append(v)
                continue
            # Consume the original path. Keep pen, transform and neighbouring glyphs.
            ops[matches[0].operations[-1]] = ([], b"n")
            changed = True
        if v.destination is not None:
            add_lines.append(v.destination)
    matrix = pdf.Matrix(1, 1)
    stack = []
    face = None
    line = None
    for i, (args, op) in enumerate(ops) if panels else ():
        if op == b"q":
            stack.append((matrix, face))
        elif op == b"Q" and stack:
            matrix, face = stack.pop()
        elif op == b"cm":
            matrix = pdf.Matrix(*map(float, args)) * matrix
        elif op == b"Tf":
            face = font_names.get(str(args[0]).lstrip("/"))
        elif op == b"BT":
            line = pdf.Matrix(1, 1)
        elif op == b"Tm":
            line = pdf.Matrix(*map(float, args))
        elif op in (b"Td", b"TD") and line is not None:
            line = pdf.Matrix(1, 0, 0, 1, *map(float, args)) * line
        elif op in (b"T*", b"'", b'"', b"ET"):
            line = None
        elif op in (b"TJ", b"Tj") and line is not None:
            origin = pdf.Point(0, 0) * line * matrix * page.transformation_matrix
            # Native output emits independent positioned text shows. Never alter
            # the advance when the following glyph relies on the current cursor.
            if (
                (face, round(origin.x, 1), round(origin.y, 1)) in points
                and i + 1 < len(ops)
                and ops[i + 1][1] in (b"ET", b"Tm", b"Td", b"TD")
            ):
                ops[i] = ([ArrayObject()], b"TJ")
                changed = True
            line = None
    if changed:
        content.operations = ops
        _replace_contents(page, content.get_data())
    if panels:
        clip_out(page, panels)
        for box in panels:
            with _fragment(original, Figure(index, box)) as fragment:
                page.show_pdf_page(box, fragment, 0, keep_proportion=False)
    for r in add_lines:
        cy = (r.y0 + r.y1) / 2
        page.draw_line((r.x0, cy), (r.x1, cy), width=r.height, color=(0, 0, 0))
    return pending, protected, panels


def _value(doc, ref, key):
    kind, value = doc.xref_get_key(ref, key)
    return doc.xref_object(int(value.split()[0])) if kind == "xref" else value


@dataclass
class PageSlot:
    ref: int
    content: bytes
    ordinal: int
    clip: pdf.Rect
    wrapper: tuple | None = None


def _page_slot(doc, page_index, source_page, layout, first):
    """Resolve the translated page/side, keeping the ordinary page coordinate system."""
    page = doc[page_index * 2 + int(not first) if layout == "TB" else page_index]
    if layout != "LR":
        if page.rect != source_page.rect or page.rotation or page.cropbox != page.rect:
            return None
        return PageSlot(page.xref, page.read_contents(), page.number, page.rect)
    forms = page.get_xobjects()
    x = 0 if first else source_page.rect.width
    desired = source_page.rect + (x, 0, x, 0)
    wrappers = [
        (ref, name)
        for ref, name, parent, b in forms
        if not parent
        and max(
            abs(a - c)
            for a, c in zip(pdf.Rect(b) * page.transformation_matrix, desired)
        )
        < 0.1
    ]
    if len(wrappers) != 1:
        return None
    wrapper, outer_name = wrappers[0]
    children = [(ref, name) for ref, name, parent, _ in forms if parent == wrapper]
    if len(children) != 1:
        return None
    ref, inner_name = children[0]
    bbox = doc.xref_get_key(ref, "BBox")[1].strip("[]").split()
    if (
        len(bbox) != 4
        or max(abs(a - b) for a, b in zip(map(float, bbox), source_page.rect)) > 0.05
    ):
        return None
    return PageSlot(
        ref,
        doc.xref_stream(ref),
        page.number,
        desired,
        (wrapper, outer_name, inner_name),
    )


def _resources_copy(doc, ref):
    resources = doc.get_new_xref()
    doc.update_object(resources, _value(doc, ref, "Resources"))
    for key in ("XObject", "ExtGState"):
        if doc.xref_get_key(resources, key)[0] == "xref":
            doc.xref_set_key(resources, key, _value(doc, resources, key))
    return resources


def _scratch_page(doc, slot, rect):
    resources = _resources_copy(doc, slot.ref)
    page = doc.new_page(width=rect.width, height=rect.height)
    try:
        doc.xref_set_key(page.xref, "Resources", f"{resources} 0 R")
        _replace_contents(page, slot.content)
    except Exception:
        doc.delete_page(-1)
        raise
    return page


def _commit_page(doc, slot, page):
    if slot.wrapper is None:
        for key in ("Contents", "Resources"):
            doc.xref_set_key(slot.ref, key, doc.xref_get_key(page.xref, key)[1])
        return
    # 输出可能对两个半页或多个物理页的 Form 去重。只改这一页的译文引用。
    wrapper, outer_name, inner_name = slot.wrapper
    inner, outer = doc.get_new_xref(), doc.get_new_xref()
    for old, new in ((slot.ref, inner), (wrapper, outer)):
        doc.update_object(new, "<<>>")
        doc.xref_copy(old, new)
    doc.update_stream(inner, page.read_contents(), compress=True)
    doc.xref_set_key(inner, "Resources", doc.xref_get_key(page.xref, "Resources")[1])
    for owner, name, value in (
        (outer, inner_name, inner),
        (doc[slot.ordinal].xref, outer_name, outer),
    ):
        resources = _resources_copy(doc, owner)
        doc.xref_set_key(resources, "XObject/" + name, f"{value} 0 R")
        doc.xref_set_key(owner, "Resources", f"{resources} 0 R")


def _same_surroundings(before, after, areas):
    a, b = before.pixels, after.pixels
    lost = (a < 105) & (
        np.asarray(Image.fromarray(b).filter(ImageFilter.MinFilter(3))) > 200
    )
    added = (b < 105) & (
        np.asarray(Image.fromarray(a).filter(ImageFilter.MinFilter(3))) > 200
    )
    difference = lost | added
    for box in areas:
        x, y, x1, y1 = (
            (box + (-0.8, -0.8, 0.8, 0.8)) * pdf.Matrix(before.zoom, before.zoom)
        ).irect
        difference[max(y, 0) : y1, max(x, 0) : x1] = False

    # Preserve even neighbouring characters whose font boxes touched a repair.
    def cjk(ink):
        return [
            (g.char, round(g.x, 1), round(g.y, 1))
            for g in ink.glyphs
            if "\u3400" <= g.char <= "\u9fff"
        ]

    return difference.sum() <= 2 and cjk(before) == cjk(after)


def _run_page(doc, original, index, src, slot, repair):
    clip = slot.clip
    work = _scratch_page(doc, slot, src.page.rect)
    try:
        matrix = pdf.Matrix(1, 0, 0, 1, -clip.x0, -clip.y0) * pdf.Matrix(
            src.zoom, src.zoom
        )

        def render_final():
            return np.asarray(
                doc[slot.ordinal]
                .get_pixmap(
                    matrix=matrix, clip=clip, colorspace=pdf.csRGB, annots=False
                )
                .pil_image()
                .convert("L")
            )

        before = PageInk(work, render_final)
        issues = inspect_page(src, before)
        if not issues or not repair:
            return issues, 0, issues
        selected = [v for v in issues if v.repairable]
        if not selected:
            return issues, 0, issues
        # A single output text show can straddle two mathematical cells. Restore
        # both before removing the show, including a previously intact neighbour.
        cells = [
            m
            for m in fixed_regions(src)
            if m.kind == "表格数学单元格" and registered(before, m)
        ]
        selected_cells = [v.box for v in selected if v.kind == "表格数学单元格"]
        for box in selected_cells:
            for mark in cells:
                other = mark.area
                if (
                    other in selected_cells
                    or abs(other.y0 - box.y0) > 0.1
                    or abs(other.y1 - box.y1) > 0.1
                ):
                    continue
                if any(
                    box.y0 <= y <= box.y1
                    and rr.intersects(box)
                    and rr.intersects(other)
                    for rr, _, y, _, _ in before.boxes
                ):
                    selected.append(Issue(mark.kind, other))
                    selected_cells.append(other)
        # Rasterise the real pre-edit page while its original contents are still active.
        _ = before.pixels
        pending, removed, panels = _patch(work, original, index, before, selected)
        work = doc.reload_page(work)
        after = PageInk(work)
        remaining = inspect_page(src, after)
        outside = {
            (face, round(x, 1), round(y, 1))
            for _, x, y, face in removed
            if not any(
                b.x0 - 0.7 <= x <= b.x1 + 0.7 and b.y0 - 1 <= y <= b.y1 + 1
                for b in panels
            )
        }
        if outside and any(
            (face, round(x, 1), round(y, 1)) in outside
            for _, x, y, _, face in after.boxes
        ):
            return issues, 0, issues

        def key(v):
            return (v.kind, tuple(v.box))

        old_keys = {key(v) for v in issues}
        new_keys = {key(v) for v in remaining}
        areas = (
            [v.box for v in selected]
            + [v.destination for v in selected if v.destination is not None]
            + [r for r, _, _, _ in removed]
        )
        if new_keys - old_keys or not _same_surroundings(before, after, areas):
            return issues, 0, issues
        unresolved = new_keys | {key(v) for v in pending}
        fixed = len(old_keys - unresolved)
        if fixed:
            _commit_page(doc, slot, work)
        else:
            return issues, 0, issues
        # An uneditable residual must be reported even if a coincident new mark is visible.
        return remaining + [v for v in pending if key(v) not in new_keys], fixed, issues
    finally:
        doc.delete_page(-1)


def check_output(
    source,
    output,
    layout="mono",
    translated_first=True,
    skip_last=0,
    repair=True,
    progress=None,
):
    """Audit native mono/LR/alternating output. Return actual fixes and unresolved source page numbers."""
    changed = 0
    warnings = {}
    changed_pages = {}
    path = Path(output)
    with ExitStack() as cleanup:
        with pdf.open(source) as original, pdf.open(output) as doc:
            count = len(doc) // 2 if layout == "TB" else len(doc)
            permitted = {len(original), max(0, len(original) - skip_last)}
            if count not in permitted or (layout == "TB" and len(doc) % 2):
                return 0, {
                    n + 1: ["分页无法与译本对应"]
                    for n in range(max(0, len(original) - skip_last))
                }
            total = min(count, max(0, len(original) - skip_last))
            for i in range(total):
                if progress and (i % 5 == 0 or i == total - 1):
                    progress(i + 1, total)
                if original[i].rotation or original[i].cropbox != original[i].rect:
                    warnings[i + 1] = ["原页旋转或裁切坐标无法可靠对应"]
                    continue
                slot = _page_slot(doc, i, original[i], layout, translated_first)
                if slot is None:
                    warnings[i + 1] = ["无法核对该页的译文坐标"]
                    continue
                try:
                    src = PageInk(original[i])
                    pending, fixed, initial = _run_page(
                        doc, original, i, src, slot, repair
                    )
                except (pdf.FileDataError, PdfReadError) as exc:
                    warnings[i + 1] = ["此页绘制内容无法核对"]
                    print(f"排版检查：原文第 {i + 1} 页的绘制内容无法解析：{exc}")
                    continue
                changed += fixed
                if fixed:
                    changed_pages[i] = initial, pending
                if pending:
                    warnings[i + 1] = sorted({v.kind for v in pending})
                    if repair:
                        reasons = sorted(
                            {v.detail or "还原后未能验证" for v in pending}
                        )
                        print(
                            f"排版待核对：原文第 {i + 1} 页（{'、'.join(warnings[i + 1])}），{'；'.join(reasons)}"
                        )
            if changed:
                folder = cleanup.enter_context(
                    TemporaryDirectory(prefix=".layout-", dir=path.parent)
                )
                staged = Path(folder) / path.name
                doc.save(staged, garbage=4, deflate=True, use_objstms=1)
        if changed:
            # Reopen the exact bytes about to be published; on spreads inspect
            # the real translated half, including paint outside its page Form.
            verified = False
            with pdf.open(source) as original, pdf.open(staged) as check:
                for i, (_, pending) in changed_pages.items():
                    slot = _page_slot(check, i, original[i], layout, translated_first)
                    if slot is None:
                        break
                    remaining, _, _ = _run_page(
                        check, original, i, PageInk(original[i]), slot, False
                    )
                    known = {(v.kind, tuple(v.box)) for v in pending}
                    if any((v.kind, tuple(v.box)) not in known for v in remaining):
                        break
                else:
                    verified = True
            if verified:
                os.replace(staged, path)
                return changed, warnings
            for i, (initial, _) in changed_pages.items():
                warnings[i + 1] = sorted(
                    {v.kind for v in initial} | {"排版修复回读未通过"}
                )
            return 0, warnings
    return changed, warnings
