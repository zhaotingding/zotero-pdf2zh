"""Keep artwork but uncover translated text hidden by a publisher's page facsimile."""

import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymupdf as pdf
from PIL import ImageFilter
from pypdf import PdfReader
from scipy import ndimage


@dataclass
class Overlay:
    size: tuple
    pixels: tuple
    regions: list
    source_text: str


def paint_pattern(name):
    number = rb"([-+]?(?:\d+(?:\.\d*)?|\.\d+))"
    return re.compile(
        rb"(?<!\S)q\s+"
        + rb"\s+".join([number] * 6)
        + rb"\s+cm\s+/"
        + re.escape(name.encode("ascii"))
        + rb"\s+Do\s+Q(?!\S)"
    )


def full_size(match, size):
    w, h = size
    return all(
        abs(float(value) - expected) < 0.02
        for value, expected in zip(match.groups(), (w, 0, 0, h, 0, 0))
    )


def figure_boxes(page, source):
    # 出版 PDF 常在 Form XObject 中保留原图坐标，即使该对象仅剩图中文字。
    boxes = []
    objects = page["/Resources"].get("/XObject", {})
    matrix = pdf.Matrix(1, 1)
    stack = []
    for args, op in page.get_contents().operations:
        if op == b"q":
            stack.append(matrix)
        elif op == b"Q" and stack:
            matrix = stack.pop()
        elif op == b"cm":
            matrix = pdf.Matrix(*map(float, args)) * matrix
        elif op == b"Do":
            obj = objects[args[0]].get_object()
            if obj["/Subtype"] == "/Form":
                transform = pdf.Matrix(
                    *map(float, obj.get("/Matrix", [1, 0, 0, 1, 0, 0]))
                )
                rect = (
                    pdf.Rect(*map(float, obj["/BBox"]))
                    * transform
                    * matrix
                    * source.transformation_matrix
                )
                rect = (rect + (-1, -1, 1, 1)) & source.rect
                if not rect.is_empty:
                    boxes.append(rect)
    return boxes


def art_regions(page, ref, stream, boxes):
    # 对照原稿有/无整页图片的渲染，找出图片独有的图形（包括没有 Form 的彩图）。
    def gray():
        return page.get_pixmap(colorspace=pdf.csGRAY, annots=False).pil_image()

    full = gray()
    page.parent.update_stream(ref, b"q Q")
    try:
        under = gray()
    finally:
        page.parent.update_stream(ref, stream)
    ink = np.asarray(under) < 165
    same_ink = np.asarray(full.filter(ImageFilter.MinFilter(5))) < 190
    if ink.sum() < 1200 or (ink & same_ink).sum() / ink.sum() < 0.95:
        return None
    residual = (np.asarray(full) < 230) & (
        np.asarray(under.filter(ImageFilter.MinFilter(5))) > 243
    )
    for box in boxes:
        x, y, x1, y1 = box.irect
        residual[y:y1, x:x1] = False
    # 仅将相邻的缺失图素并成区域。扫描页没有可见底层正文，已在上面排除。
    groups, count = ndimage.label(ndimage.maximum_filter(residual, size=11))
    for i, sl in enumerate(ndimage.find_objects(groups, max_label=count), 1):
        yy, xx = np.where(residual[sl] & (groups[sl] == i))
        if len(xx) < 3:
            continue
        x, y = sl[1].start, sl[0].start
        rect = pdf.Rect(
            x + xx.min() - 2, y + yy.min() - 2, x + xx.max() + 3, y + yy.max() + 3
        )
        boxes.append(rect & page.rect)
    keep = np.zeros(ink.shape, dtype=bool)
    for box in boxes:
        x, y, x1, y1 = box.irect
        keep[y:y1, x:x1] = True
    if (ink & ~keep).sum() < 1200 or keep.mean() > 0.85:
        return None
    return boxes


def find_overlays(source):
    found = {}
    structure = None
    with pdf.open(source) as doc:
        for page in doc:
            # 匹配单独覆盖整个页面的绘图指令，避免把普通插图和扫描件当作遮挡。
            size = (page.rect.width, page.rect.height)
            if page.rotation or page.cropbox != pdf.Rect(0, 0, *size):
                continue
            images = {
                im[7]: im
                for im in page.get_images(full=True)
                if not im[-1] and not im[1]
            }
            if not images:
                continue
            for ref in page.get_contents():
                # 独立的一次绘图通常不到 100 字节，跳过巨大的正文流。
                if int(doc.xref_get_key(ref, "Length")[1]) > 512:
                    continue
                raw = doc.xref_stream(ref)
                if len(raw) > 300 or b" Do" not in raw:
                    continue
                for name, im in images.items():
                    if not name.isascii():
                        continue
                    m = paint_pattern(name).fullmatch(raw.strip())
                    if not m or not full_size(m, size):
                        continue
                    txt = page.get_text().strip()
                    if len(txt) < 200:
                        continue
                    if structure is None:
                        structure = PdfReader(source)
                    boxes = figure_boxes(structure.pages[page.number], page)
                    boxes = art_regions(page, ref, raw, boxes)
                    if boxes is not None:
                        # 译本可能重新压缩相同的位图，按解码后的像素匹配。
                        digest = pdf.Pixmap(doc, im[0]).digest
                        found[digest] = Overlay(size, im[2:4], boxes, txt)
    return found


def covers_translated_text(page, clip, source_text):
    source_text = "".join(source_text.split())
    if "".join(page.get_text(clip=clip).split()) == source_text:
        return False
    # bboxlog 与 texttrace 的序号共用同一绘制顺序；只计这一侧整页图下的可见文字。
    paints = [
        i
        for i, (kind, box) in enumerate(page.get_bboxlog())
        if kind == "fill-image" and max(abs(a - b) for a, b in zip(box, clip)) < 1
    ]
    if len(paints) != 1:
        return False
    text = "".join(
        chr(char[0])
        for span in page.get_texttrace()
        if span["seqno"] < paints[0]
        and span["type"] in (0, 1)
        and span["opacity"] > 0
        and pdf.Rect(span["bbox"]).intersects(clip)
        for char in span["chars"]
        if 0 <= char[0] <= 0x10FFFF
    )
    translated = "".join(text.split())
    return (
        len(translated) >= 120
        and sum(
            translated[j : j + 8] not in source_text
            for j in range(0, len(translated) - 7, 8)
        )
        >= 8
    )


def restore_output(path, overlays):
    changed = set()
    updates = {}
    path = Path(path)
    stamp = path.stat()
    with pdf.open(path) as doc:
        for page in doc:
            owners = {
                xref: (parent, pdf.Rect(bbox))
                for xref, _, parent, bbox in page.get_xobjects()
            }
            for xref, _, width, height, _, _, _, name, _, owner in page.get_images(
                full=True
            ):
                if not name.isascii() or not any(
                    o.pixels == (width, height) for o in overlays.values()
                ):
                    continue
                for ref in [owner] if owner else page.get_contents():
                    stream = doc.xref_stream(ref)
                    if b"/" + name.encode("ascii") not in stream:
                        continue
                    matches = list(paint_pattern(name).finditer(stream))
                    if len(matches) != 1:
                        continue
                    m = matches[0]
                    # 独立页面内容流可以仅绘制图片；共享 Form 仍需包含译文绘图指令。
                    if b"BT" not in stream and (owner or stream.strip() != m[0]):
                        continue
                    dims = (float(m[1]), float(m[4]))
                    if not any(full_size(m, o.size) for o in overlays.values()):
                        continue
                    overlay = overlays.get(pdf.Pixmap(doc, xref).digest)
                    if not overlay or not full_size(m, overlay.size):
                        continue
                    # 双语版含两份相同位图；通过所属 Form 的位置排除英文原文一侧。
                    clip = page.rect
                    top = owner
                    while top in owners:
                        parent, bbox = owners[top]
                        if not parent:
                            clip = bbox * page.transformation_matrix
                            break
                        top = parent
                    if abs(clip.width - dims[0]) > 1 or abs(clip.height - dims[1]) > 1:
                        continue
                    if not covers_translated_text(page, clip, overlay.source_text):
                        continue
                    # 写入 PDF 原生裁剪路径，图片像素、底层翻译文本和批注坐标保持一致。
                    # 每个区域单独作矩形裁剪，避免复杂裁剪蒙版改变细线和图中文字的采样。
                    fragments = []
                    for b in overlay.regions:
                        rect = f"{b.x0:.4f} {dims[1] - b.y1:.4f} {b.width:.4f} {b.height:.4f}"
                        fragments.append(b"q " + rect.encode() + b" re W n " + m[0][1:])
                    replacement = b"\n".join(fragments) or b"q Q"
                    target = (None if owner else page.number, ref)
                    updates[target] = (
                        stream[: m.start()] + replacement + stream[m.end() :]
                    )
                    changed.add(page.number + 1)
        if not changed:
            return []
        page_count, toc = len(doc), doc.get_toc(False)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".overlay.tmp")
    os.close(fd)
    try:
        # 增量写入和原子替换，保留所有原始页对象和书签。
        shutil.copyfile(path, tmp)
        with pdf.open(tmp) as copy:
            for (page_number, ref), stream in updates.items():
                if page_number is None:
                    copy.update_stream(ref, stream)
                    continue
                # 不同页可复用同一个绘图流。把修正后的流只挂到目标页。
                page = copy[page_number]
                new_ref = copy.get_new_xref()
                copy.update_object(new_ref, "<<>>")
                copy.update_stream(new_ref, stream)
                refs = [new_ref if x == ref else x for x in page.get_contents()]
                copy.xref_set_key(
                    page.xref,
                    "Contents",
                    "[" + " ".join(f"{x} 0 R" for x in refs) + "]",
                )
            copy.saveIncr()
        with pdf.open(tmp) as check:
            if len(check) != page_count or check.get_toc(False) != toc:
                raise ValueError("修复后的页数或书签不一致")
        for attempt in range(5):
            now = path.stat()
            if (stamp.st_size, stamp.st_mtime_ns) != (now.st_size, now.st_mtime_ns):
                raise OSError("文件已被其他任务更新")
            try:
                os.replace(tmp, path)
                break
            except PermissionError as exc:
                if getattr(exc, "winerror", None) not in (5, 32) or attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
    finally:
        Path(tmp).unlink(missing_ok=True)
    return sorted(changed)


def repair_translation_outputs(source, outputs):
    try:
        overlays = find_overlays(source)
    except Exception as exc:  # noqa: BLE001 - optional repair must not discard a completed translation
        print(f"⚠️ 整页图片检查跳过：{exc}")
        return
    if overlays:
        for path in outputs:
            try:
                pages = restore_output(path, overlays)
                if pages:
                    print(f"已修复整页图片遮挡：{Path(path).name}，页码 {pages}")
            except Exception as exc:  # noqa: BLE001 - still return other outputs of this job
                print(f"⚠️ {Path(path).name} 整页图片处理未完成：{exc}")
