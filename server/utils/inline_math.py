"""Protect inline math across font changes while keeping the PDF's original glyphs."""

import hashlib
import re
import unicodedata
from collections import defaultdict
from itertools import pairwise
from typing import NamedTuple

from pypdf._cmap import get_encoding
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)


class Glyph(NamedTuple):
    char: str
    font: str
    offset: int
    x: float
    y: float
    right: float
    size: float


_FUNCTIONS = r"sin|cos|tan|exp|log|ln|max|min|det|argmax|argmin|softmax|sigmoid"
_LETTER = r"[^\W\d_]"
_CALL = re.compile(
    rf"(?<!\w)(?:{_LETTER}\w+(?=[(\[])|(?:{_FUNCTIONS}|{_LETTER})[ ]?)[(\[]"
)
_WORD = re.compile(rf"{_LETTER}{{2,}}")
_RELATION = re.compile(
    rf"(?<!\w)(?:{_LETTER}|\d+)[ ]*[=≈≠≤≥∈∝→][ ]*(?:{_LETTER}|\d+)(?!\w)"
)
_ARG_WORD = re.compile(rf"(?:{_FUNCTIONS})\Z")
_ACCENTS = frozenset("^ˆ˜~¯ˉ˙¨ˇ˘´`→")


def _mark_line(line, selected):
    # 用真实字间距恢复词界。参考文献序号、正文中的括号和独立展示公式不参与。
    text, places = [], []
    for i, g in enumerate(line):
        if i and g.x - line[i - 1].right > 0.16 * g.size:
            text.append(" ")
            places.append(None)
        text.append(g.char)
        places.append(g)
    text = "".join(text)
    if not re.search(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}", text):
        return

    def mark(start, end):
        for g in places[start:end]:
            if g is not None:
                selected[g.font].update(
                    range(g.offset, g.offset + len(_normal(g.char)))
                )

    for match in _CALL.finditer(text):
        # 保留配对括号、嵌套调用、数学运算符和参数。含普通单词的括号留给翻译。
        stack = []
        for end in range(match.end() - 1, len(text)):
            ch = text[end]
            if ch in "([":
                stack.append(")" if ch == "(" else "]")
            elif ch in ")]":
                if not stack or ch != stack.pop():
                    break
                if not stack:
                    body = text[match.end() : end]
                    if body.strip() and all(
                        _ARG_WORD.fullmatch(word[0])
                        or body[word.end() : word.end() + 1] in ("(", "[")
                        or all(0x1D400 <= ord(c) <= 0x1D7FF for c in word[0])
                        for word in _WORD.finditer(body)
                    ):
                        mark(match.start(), end + 1)
                    break
            elif not (
                ch.isalnum()
                or ch in " .,·…_|!*/+-:^"
                or unicodedata.category(ch) in {"Sm", "Mn", "Sk"}
            ):
                break

    for match in _RELATION.finditer(text):
        mark(*match.span())
    for i, (a, b) in enumerate(pairwise(places)):
        if a is None or b is None:
            continue
        if a.char in _ACCENTS or unicodedata.combining(a.char):
            accent, base = a, b
        elif b.char in _ACCENTS or unicodedata.combining(b.char):
            accent, base = b, a
        else:
            continue
        if not base.char.isalpha():
            continue
        # 间距帽号可以与字母共用基线，也可以后绘制。词内重音和独立的幂运算符保持原样。
        if (
            (i and text[i - 1].isalpha())
            or (i + 2 < len(text) and text[i + 2].isalpha())
            or min(a.right, b.right) <= max(a.x, b.x)
            or not -0.2 * base.size <= base.y - accent.y <= base.size
        ):
            continue
        mark(i, i + 2)


def _normal(value):
    return unicodedata.normalize("NFKC", value)


def _family(name):
    # 同一嵌入字体的 PDF BaseFont 与字形跟踪名可能用空格或连字符分词。
    return re.sub(r"[\s-]", "", name.lstrip("/").split("+")[-1])


def inline_slots(page, fraction_bars=()):
    if page.rotation:
        return {}
    strings, offsets, selected = defaultdict(list), defaultdict(int), defaultdict(set)
    line = []
    for span in page.get_texttrace():
        family = _family(span["font"])
        horizontal = (
            span["type"] in (0, 1) and span["opacity"] > 0 and span["dir"] == (1, 0)
        )
        for code, _, (x, y), box in span["chars"]:
            ch = chr(code) if 0 <= code <= 0x10FFFF else "\ufffd"
            normalized = _normal(ch)
            g = Glyph(ch, family, offsets[family], x, y, box[2], span["size"])
            strings[family].append(normalized)
            offsets[family] += len(normalized)
            if (
                ch == ","
                and horizontal
                and any(
                    bar.x0 <= x < bar.x1 and 0 < y - bar.y0 < 1.7 * g.size
                    for bar in fraction_bars
                )
            ):
                selected[family].add(g.offset)
            if line:
                prev = line[-1]
                em = max(g.size, prev.size)
                if (
                    not horizontal
                    or abs(y - prev.y) > 0.75 * em
                    or x < prev.x - 0.7 * em
                    or x > prev.right + 2.2 * em
                ):
                    _mark_line(line, selected)
                    line = []
            if horizontal:
                line.append(g)
    _mark_line(line, selected)
    return {
        f: ("".join(strings[f]), indices) for f, indices in selected.items() if indices
    }


def _codes(data, encoding, cmap):
    # 简单字体用单字节，Identity CID 字体用两个字节。歧义编码不猜字节边界。
    if isinstance(encoding, dict):
        codes = ((bytes([c]), encoding.get(c, chr(c))) for c in data)
    elif encoding == "utf-16-be" and len(data) % 2 == 0:
        pairs = (data[i : i + 2] for i in range(0, len(data), 2))
        codes = ((raw, chr(int.from_bytes(raw, "big"))) for raw in pairs)
    elif encoding in ("charmap", "latin-1"):
        codes = ((bytes([c]), chr(c)) for c in data)
    else:
        return []
    return [(raw, cmap.get(char, char)) for raw, char in codes]


def _math_paints(ops, fonts, slots, edits):
    offsets, projected, changes = defaultdict(int), defaultdict(list), defaultdict(list)
    encodings = {}
    font, size = None, None
    stack = []
    for index, (args, op) in enumerate(ops):
        if op == b"q":
            stack.append((font, size))
        elif op == b"Q" and stack:
            font, size = stack.pop()
        elif op == b"Tf":
            font, size = args
        elif op in (b"TJ", b"Tj", b"'", b'"') and font in fonts:
            if font not in encodings:
                face = fonts[font]
                family = _family(str(face.get("/BaseFont", "")))
                encodings[font] = (
                    family,
                    get_encoding(face) if family in slots else None,
                )
            family, encoding = encodings[font]
            if not encoding:
                continue
            parts = args[0] if op == b"TJ" else args[-1:]
            chunks = []
            for part in parts:
                if not isinstance(part, bytes):
                    chunks.append((False, part, None))
                    continue
                glyphs = _codes(part, *encoding)
                if part and not glyphs:
                    projected[family].append("\0")
                for raw, ch in glyphs:
                    value = _normal(ch)
                    start = offsets[family]
                    offsets[family] += len(value)
                    projected[family].append(value)
                    chosen = bool(value) and all(
                        i in slots[family][1] for i in range(start, start + len(value))
                    )
                    chunks.append((chosen, raw, ch))
            if index not in edits and any(chosen for chosen, _, _ in chunks):
                changes[family].append((index, font, size, chunks))
    # 用整页同字体的实际字形顺序核对字节位置。表单、隐藏层等造成歧义时不改该字体。
    return [
        paint
        for f, paints in changes.items()
        if "".join(projected[f]) == slots[f][0]
        for paint in paints
    ]


def _cmap(mapping, name):
    sizes = {len(raw) for raw in mapping}
    lines = [
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap",
        f"/CMapName /{name} def /CMapType 2 def",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        f"{len(sizes)} begincodespacerange",
    ]
    lines.extend(f"<{'00' * size}> <{'FF' * size}>" for size in sorted(sizes))
    lines.append("endcodespacerange")
    entries = [
        f"<{raw.hex()}> <{ch.encode('utf-16-be').hex()}>"
        for raw, ch in sorted(mapping.items())
    ]
    for i in range(0, len(entries), 100):
        block = entries[i : i + 100]
        lines.extend([f"{len(block)} beginbfchar", *block, "endbfchar"])
    lines.append("endcmap CMapName currentdict /CMap defineresource pop end end")
    return "\n".join(lines).encode("ascii")


def protect_inline_runs(writer, fonts, ops, slots, edits, originals):
    if not slots:
        return
    paints = _math_paints(ops, fonts, slots, edits)
    codes = defaultdict(dict)
    for _, font, _, chunks in paints:
        codes[font].update((raw, ch) for chosen, raw, ch in chunks if chosen)
    aliases = {}
    for font, chars in codes.items():
        # 引擎把私用区字符作为公式原样排版。只改选中字形的临时字符映射，绘制与字距沿用原字体。
        original = _cmap(chars, "InlineUnicode")
        digest = hashlib.sha256(original).hexdigest()[:20]
        marked = _cmap({raw: "\ue000" for raw in chars}, "PDF2zhInline" + digest)
        alias = NameObject(f"/PDF2zhRun{len(originals)}")
        while alias in fonts:
            alias = NameObject(str(alias) + "X")
        clone = DictionaryObject(fonts[font])
        stream = DecodedStreamObject()
        stream.set_data(marked)
        clone[NameObject("/ToUnicode")] = writer._add_object(stream)
        fonts[alias] = writer._add_object(clone)
        originals[(str(alias)[1:], str(clone.get("/BaseFont", ""))[1:])] = (
            marked,
            original,
        )
        aliases[font] = alias

    for index, font, size, chunks in paints:
        args, op = ops[index]
        replacement = []
        if op == b'"':
            replacement.extend([([args[0]], b"Tw"), ([args[1]], b"Tc")])
        if op in (b"'", b'"'):
            replacement.append(([], b"T*"))
        active = False
        run = ArrayObject()
        for chosen, raw, ch in chunks:
            if ch is not None and chosen != active:
                if run:
                    replacement.append(([run], b"TJ"))
                    run = ArrayObject()
                replacement.append(([aliases[font] if chosen else font, size], b"Tf"))
                active = chosen
            if ch is not None and run and isinstance(run[-1], bytes):
                run[-1] = ByteStringObject(run[-1] + raw)
            else:
                run.append(ByteStringObject(raw) if ch is not None else raw)
        if run:
            replacement.append(([run], b"TJ"))
        if active:
            replacement.append(([font, size], b"Tf"))
        edits[index] = (1, replacement)


def apply_paint_edits(content, edits):
    ops = content.operations
    for index in sorted(edits, reverse=True):
        count, replacement = edits[index]
        ops[index : index + count] = replacement
    content.operations = ops
    return content.get_data()


def protect_form_runs(writer, resources, ops, slots, originals, parents=()):
    if not slots or "/XObject" not in resources:
        return False
    objects = DictionaryObject(resources["/XObject"])
    changed = False
    for name in dict.fromkeys(args[0] for args, op in ops if op == b"Do"):
        form = objects[name]
        if form.get("/Subtype") != "/Form" or id(form) in parents:
            continue
        # 表单可以跨页复用，只为当前页面克隆已确认位置的绘制流与资源。
        local = DictionaryObject(form.get("/Resources", resources).get_object())
        content = ContentStream(form, writer, forced_encoding="bytes")
        nested = protect_form_runs(
            writer, local, content.operations, slots, originals, (*parents, id(form))
        )
        edits = {}
        if "/Font" in local:
            fonts = DictionaryObject(local["/Font"])
            protect_inline_runs(
                writer, fonts, content.operations, slots, edits, originals
            )
            if edits:
                local[NameObject("/Font")] = fonts
        if not edits and not nested:
            continue
        clone = DecodedStreamObject()
        clone.update(
            {
                k: v
                for k, v in form.items()
                if k not in ("/Length", "/Filter", "/DecodeParms")
            }
        )
        clone[NameObject("/Resources")] = local
        clone.set_data(apply_paint_edits(content, edits) if edits else form.get_data())
        objects[name] = writer._add_object(clone)
        changed = True
    if changed:
        resources[NameObject("/XObject")] = objects
    return changed
