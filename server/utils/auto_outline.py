"""Build embedded bookmarks before returning a translated attachment to Zotero."""

import os
import re
import shutil
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path

import pymupdf as pdf

TEXT_FLAGS = pdf.TEXTFLAGS_DICT & ~pdf.TEXT_PRESERVE_IMAGES
NUMBER = re.compile(
    r"^(\d{1,2}(?:\.\d{1,2})*|[IVXLCDM]+|[A-Z])([.)、]?)\s+([A-Za-z\u3400-\u9fff].*)$"
)
NAMED = re.compile(
    r"^(abstract|introduction|background|related works?|materials and methods|methods|results|discussion|conclusions?|limitations|references|bibliography|acknowledg[e]?ments?|appendix(?: [A-Z])?|摘要|引言|相关工作|研究方法|实验结果|结论|参考文献|致谢)$",
    re.IGNORECASE,
)


def clean(text):
    return " ".join(unicodedata.normalize("NFKC", text).replace("\u00ad", "").split())


def lines(page):
    return [
        line
        for b in page.get_text("dict", flags=TEXT_FLAGS)["blocks"]
        for line in b.get("lines", [])
        if line["dir"][0] > 0.9
    ]


def text(line):
    return clean("".join(s["text"] for s in line["spans"]))


def detect(doc):
    # 先估计正文字号和分栏，再按页及栏内顺序读取，避免双栏论文左右串章。
    pages = [lines(p) for p in doc]
    sizes = Counter()
    for group in pages:
        for line in group:
            for s in line["spans"]:
                sizes[round(s["size"], 1)] += len(s["text"].strip())
    if not sizes:
        return []
    body = sizes.most_common(1)[0][0]
    result = []
    in_refs = False
    parent = ""
    section_level = 1
    seen = set()
    for idx, group in enumerate(pages):
        w = doc[idx].rect.width
        split = (
            sum(w * 0.47 < l["bbox"][0] < w * 0.57 and len(text(l)) > 32 for l in group)
            >= 3
        )
        ordered = sorted(
            group,
            key=lambda l: (
                split and l["bbox"][0] >= w * 0.48,
                round(l["bbox"][1]),
                l["bbox"][0],
            ),
        )
        body_lefts = Counter(
            round(l["bbox"][0] / 4) for l in group if len(text(l)) > 32
        )
        margins = [
            bucket * 4 for bucket, _ in body_lefts.most_common(2 if split else 1)
        ]
        consumed = set()
        for pos, line in enumerate(ordered):
            if pos in consumed:
                continue
            value = text(line)
            if not value:
                continue
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            first = spans[0]
            x, y = line["bbox"][:2]
            size = first["size"]
            styled = bool(first["flags"] & 18)
            head = value
            # IEEE 小节可与正文共用一行；斜体标题还可能延续到下一行。
            if pos + 1 < len(ordered):
                nxt = ordered[pos + 1]
                if (
                    first["flags"] & 2
                    and abs(nxt["bbox"][0] - x) < 14
                    and 0 < nxt["bbox"][1] - y < size * 1.8
                    and nxt["spans"][0]["flags"] & 2
                    and not NUMBER.match(text(nxt))
                    and ":" not in value
                ):
                    head += " " + text(nxt)
            numbered = NUMBER.match(head)
            chinese = re.match(
                r"^第[〇零一二三四五六七八九十百千万0-9]+([章节篇部])\s*\S", value
            )
            if re.match(r"^(Abstract\b|摘要)", value, re.IGNORECASE) and idx == 0:
                title, level = ("摘要" if value.startswith("摘要") else "Abstract"), 1
            elif chinese and len(value) < 70 and not re.search(r"[，。；]", value):
                title, level = value, 2 if chinese[1] == "节" else 1
            elif (
                NAMED.fullmatch(value)
                and value[0] == value[0].upper()
                and size >= body * 0.96
            ):
                title, level = value, 1
            elif in_refs:
                continue
            elif numbered:
                num, suffix, words = numbered.groups()
                if suffix == ")":
                    if not styled or ":" not in head:
                        continue
                    level = 3
                    head = head.split(":", 1)[0]
                elif num.isalpha():
                    if suffix != "." or (
                        len(num) == 1 and num not in "IVX" and not styled
                    ):
                        continue
                    level = 2 if len(num) == 1 and (styled or num not in "IVX") else 1
                else:
                    level = num.count(".") + 1
                    if size < body * 0.9 or (
                        not styled and words[0].islower() and not NAMED.fullmatch(words)
                    ):
                        continue
                    if level == 1 and parent.isdigit() and int(num) != int(parent) + 1:
                        continue
                    if (
                        level == 1
                        and not styled
                        and size <= body
                        and not NAMED.fullmatch(words)
                    ):
                        continue
                    if int(num.split(".")[0]) > 30 or (
                        level > 1 and parent and num.split(".")[0] != parent
                    ):
                        continue
                if (
                    len(head) > 128
                    or re.search(r"[;=%\[\]]", head)
                    or head.endswith((".", ","))
                ):
                    continue
                title = head
                if level == 1:
                    parent = num
            elif (
                first["flags"] & 16
                and size >= body * 0.96
                and parent
                and any(abs(x - margin) < 6 for margin in margins)
            ):
                prefix = ""
                right = line["bbox"][2]
                for j in range(pos, min(pos + 12, len(ordered))):
                    current = ordered[j]
                    if j != pos:
                        same_line = (
                            abs(current["bbox"][1] - y) < 1
                            and 0 <= current["bbox"][0] - right < 1.6 * size
                        )
                        next_line = (
                            abs(current["bbox"][0] - x) < 8
                            and 1 < current["bbox"][1] - y < 1.8 * size
                        )
                        if (
                            not (same_line or next_line)
                            or not current["spans"][0]["flags"] & 16
                        ):
                            break
                    right = current["bbox"][2]
                    for s in current["spans"]:
                        if not s["flags"] & 16:
                            break
                        prefix += s["text"]
                    if prefix.rstrip().endswith((".", "。", ":")):
                        if j != pos:
                            consumed.update(range(pos + 1, j + 1))
                        break
                    if not all(s["flags"] & 16 for s in current["spans"]):
                        break
                    prefix += " "
                if prefix.rstrip().endswith((".", "。", ":")) and len(prefix) <= 115:
                    title, level = clean(prefix).rstrip(".。:"), section_level + 1
                elif (
                    size > body * 1.08
                    and len(value) < 85
                    and all(s["flags"] & 16 for s in spans)
                ):
                    title, level = value, 1
                else:
                    continue
            elif (
                y > 24
                and 3 <= len(value) < 85
                and size >= body * 1.09
                and (idx or result)
                and not value.endswith((".", ":", ",", "。"))
                and not re.search(r"[\d=]{3}|[@]|^(fig|table)\b", value, re.IGNORECASE)
            ):
                title, level = value, 1
            else:
                continue
            if len(title) < 2 or (parent, title.casefold()) in seen:
                continue
            seen.add((parent, title.casefold()))
            if numbered or chinese or level == 1:
                section_level = level
            level = min(level, result[-1][0] + 1 if result else 1)
            result.append((level, title, idx + 1, pdf.Point(x, y)))
            if re.fullmatch(r"references|bibliography|参考文献", title, re.IGNORECASE):
                in_refs = True
    return result


def source_entries(doc):
    original = doc.get_toc(False)
    if not original:
        return detect(doc), False
    entries = []
    for level, title, page, dest in original:
        if 1 <= page <= len(doc):
            point = dest.get("to")
            if not isinstance(point, pdf.Point):
                point = pdf.Point(0, 0)
            entries.append((level, title, page, point))
    return entries, True


def translated_anchor(line_list, point, title, lang):
    # 仅采用同一栏、同一位置的译文标题；对正文行或缺少可靠对应的位置沿用原题。
    candidates = []
    for line in line_list:
        x, y = line["bbox"][:2]
        if abs(x - point.x) > 25 or abs(y - point.y) > 11:
            continue
        value = text(line)
        if not value:
            continue
        num = re.match(r"^(\d+(?:\.\d+)*|[A-Z]+)(?:[.)、]|\s)", title)
        if num and not re.match(re.escape(num[1]) + r"(?:[.)、]|\s)", value):
            continue
        if title.lower() == "abstract" and value.startswith("摘要"):
            value = "摘要"
        if not num:
            value = re.split("[。]", value, 1)[0]
        if ")" in title[:4]:
            value = re.split("[:：]", value, 1)[0]
        if lang.startswith("zh") and not re.search(r"[\u3400-\u9fff]", value):
            continue
        if len(value) > 95 or re.search("[。，；;]", value):
            continue
        candidates.append(
            (abs(y - point.y) + abs(x - point.x) * 0.3, value, pdf.Point(x, y))
        )
    if candidates:
        _, title, point = min(candidates, key=lambda item: item[0])
    return title, point


def destination(source_page, output, kind, group, first, anchor, cropper, config):
    # 将原页位置投影到实际的单语、双语或分栏裁切布局。
    w = source_page.rect.width
    page = group
    x, y = anchor
    if kind in ("mono-cut", "dual-cut", "crop-compare"):
        left, right, _, _ = cropper._get_clips(source_page, config)
        col = int(x >= w / 2)
        clip = (left, right)[col]
        page = group * 2 + col
        if kind == "dual-cut":
            page = page * 2 + first
        x, y = x - clip.x0, y - clip.y0
        if kind == "crop-compare":
            rect = output[page].rect
            scale = min(rect.width / 2 / clip.width, rect.height / clip.height)
            x = (first + 0.5) * rect.width / 2 + (x - clip.width / 2) * scale
            y = rect.height / 2 + (y - clip.height / 2) * scale
    elif kind == "TB":
        page = group * 2 + first
    elif kind == "LR":
        x += first * w
    return page, pdf.Point(x, y)


def attach_outlines(source, paths, config, server, engine):
    with pdf.open(source) as original:
        entries, inherited = source_entries(original)
        if not entries:
            print("⚠️ 大纲：原文没有可识别的章节，已保留译本。")
            return
        for name in dict.fromkeys(paths):
            path = Path(name)
            kind = server.get_filetype(name)
            if kind == "dual":
                kind = server.get_dual_mode(name, "TB")
            elif kind == "compare":
                kind = "LR"
            first = (
                int(not (config.trans_first or config.enhance_compatibility))
                if engine == "pdf2zh_next"
                else 1
            )
            per_page = {"TB": 2, "dual-cut": 4, "mono-cut": 2, "crop-compare": 2}.get(
                kind, 1
            )
            with pdf.open(path) as output:
                n, rem = divmod(len(output), per_page)
                expected = len(original)
                if config.only_include_translated_page:
                    expected -= config.skip_last_pages
                if rem or n not in {len(original), expected}:
                    print(f"⚠️ 大纲：无法核对 {path.name} 的分页，保留其已有书签。")
                    continue
                existing = output.get_toc(False)
                if (
                    inherited
                    and kind == "mono"
                    and n == len(original)
                    and existing == original.get_toc(False)
                ):
                    continue
                toc = []
                last_page = -1
                for level, title, p, anchor in entries:
                    if p > n:
                        continue
                    page, point = destination(
                        original[p - 1],
                        output,
                        kind,
                        p - 1,
                        first,
                        anchor,
                        server.cropper,
                        config,
                    )
                    if not inherited:
                        if page != last_page:
                            page_lines = lines(output[page])
                            last_page = page
                        title, point = translated_anchor(
                            page_lines, point, title, config.targetLang
                        )
                    rect = output[page].rect
                    margin = 0 if inherited else 4
                    point = pdf.Point(
                        max(0, min(point.x - margin, rect.width - 1)),
                        max(0, min(point.y - margin, rect.height - 1)),
                    )
                    level = min(level, toc[-1][0] + 1 if toc else 1)
                    toc.append(
                        [
                            level,
                            title,
                            page + 1,
                            {
                                "kind": pdf.LINK_GOTO,
                                "page": page,
                                "to": point,
                                "zoom": 0,
                            },
                        ]
                    )
            if not toc:
                continue
            # 返回插件前原子替换。大纲写入失败时保留已完成的译本。
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".outline.tmp")
            os.close(fd)
            try:
                shutil.copyfile(path, tmp)
                with pdf.open(tmp) as out:
                    out.set_toc(toc, collapse=1)
                    out.saveIncr()
                with pdf.open(tmp) as check:
                    rows = check.get_toc(False)
                    if len(rows) != len(toc) or any(
                        a[:3] != b[:3] or abs(a[3]["to"] - b[3]["to"]) > 0.1
                        for a, b in zip(rows, toc)
                    ):
                        raise ValueError("写入的大纲坐标未通过回读")
                os.replace(tmp, path)
                print(
                    f"📑 {path.name}：{'沿用' if inherited else '生成'} {len(toc)} 条章节书签"
                )
            finally:
                Path(tmp).unlink(missing_ok=True)
