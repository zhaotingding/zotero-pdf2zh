"""Protect inline math across font changes while keeping the PDF's original glyphs."""

import hashlib
import re
import unicodedata
from collections import defaultdict
from functools import lru_cache
from io import BytesIO
from itertools import pairwise
from typing import NamedTuple

import pymupdf as pdf
from pypdf import PdfReader
from pypdf._cmap import get_encoding
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NameObject,
    NumberObject,
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
_DIACRITIC = r"[\u0300-\u036fˆ˜¯ˉ˙¨ˇ˘]"
_VALUE = re.compile(
    rf"(?<!\w)(?:\d+(?:\.\d+)?|\.\d+|{_DIACRITIC}?{_LETTER}{_DIACRITIC}?)(?!\w)"
)
_RELATIONS = frozenset("=<>≈≃≅≡≠≤≥≪≫∈∉∋⊂⊃⊆⊇∝→↔")
_ASCII_OPERATORS = frozenset("+-*/^:!·")
_BRACKETS = {"(": ")", "[": "]", "{": "}"}
_ARG_WORD = re.compile(rf"(?:{_FUNCTIONS})\Z")
_ACCENTS = frozenset("^ˆ˜~¯ˉ˙¨ˇ˘´`→")
_PRIMES = frozenset("′″‴")


def _script_side(base, script):
    if not (script.char.isalnum() or script.char in _PRIMES or script.char in "*+-−"):
        return None
    if (
        script.x <= base.x
        or not -0.2 * base.size <= script.x - base.right <= 0.23 * base.size
    ):
        return None
    ratio = script.size / base.size
    shift = (script.y - base.y) / base.size
    if script.char in _PRIMES and 0.4 <= ratio <= 1.10 and -0.8 <= shift <= 0.08:
        return "upper"
    if 0.4 <= ratio <= 0.89 and 0.085 <= abs(shift) <= 0.75:
        return "lower" if shift > 0 else "upper"
    return None


def _variable_end(glyphs, i):
    base = glyphs[i]
    end = i + 1
    if not base.char.isalpha() or base.size <= 0:
        return end
    # 普通词尾的上标通常是脚注。换行时横坐标可能回退，不能把两行首尾拼成词。
    prev = glyphs[i - 1] if i else None
    in_word = bool(
        prev
        and prev.char.isalnum()
        and prev.font == base.font
        and abs(prev.y - base.y) < 0.1 * base.size
        and base.x >= prev.x
        and base.x - prev.right < 0.16 * base.size
    )
    # 上下标可能占同一列；两者都相对主字母定位，不能用彼此间的高度差断行。
    sides = set()
    while end < len(glyphs):
        script = glyphs[end]
        if script is None:
            break
        side = _script_side(base, script)
        if side is None or side in sides:
            break
        sides.add(side)
        end += 1
        while end < len(glyphs):
            g = glyphs[end]
            last = glyphs[end - 1]
            if (
                g is None
                or not (g.char.isalnum() or g.char in _PRIMES or g.char in "+−-*,")
                or abs(g.y - script.y) > 0.10 * script.size
                or abs(g.size - script.size) > 0.10 * script.size
                or not -0.15 * script.size <= g.x - last.right <= 0.23 * script.size
            ):
                break
            end += 1
    if in_word and end > i + 1:
        # 多字母函数名也可以带脚标；紧随其后的数学括号与普通词尾脚注可明确区分。
        after = glyphs[end] if end < len(glyphs) else None
        if not (
            after
            and after.char in "(["
            and abs(after.y - base.y) < 0.15 * base.size
            and -0.2 * base.size
            <= after.x - max(g.right for g in glyphs[i:end])
            <= 0.35 * base.size
        ):
            return i + 1
    return end


def script_ranges(places, *, include_plain=False):
    """Yield offsets in original paint order, including both scripts of a variable."""
    i = 0
    while i < len(places):
        end = _variable_end(places, i) if places[i] is not None else i + 1
        if include_plain or end > i + 1:
            yield i, end
        i = end


def _bracket_atoms(text):
    """Return balanced groups whose contents can be read as math operands."""
    stack, ranges = [], {}
    for i, ch in enumerate(text):
        if ch in _BRACKETS:
            stack.append(i)
        elif ch in ")]}":
            if not stack or _BRACKETS[text[stack[-1]]] != ch:
                stack.clear()
                continue
            start = stack.pop()
            body = text[start + 1 : i]
            if not body.strip() or not all(
                c.isalnum()
                or c in " .,·…_|!*/+-:^()[]{}"
                or unicodedata.category(c) in {"Sm", "Mn", "Sk"}
                for c in body
            ):
                continue
            if all(
                _ARG_WORD.fullmatch(w[0])
                or body[w.end() : w.end() + 1] in ("(", "[")
                or all(0x1D400 <= ord(c) <= 0x1D7FF for c in w[0])
                for w in _WORD.finditer(body)
            ):
                ranges[start] = i + 1
    return ranges


def math_ranges(text):
    """Recognise function calls and maximal chains joined by a mathematical relation."""
    brackets = _bracket_atoms(text)
    atoms = [*brackets.items(), *(match.span() for match in _VALUE.finditer(text))]
    for match in _CALL.finditer(text):
        end = brackets.get(match.end() - 1)
        if end is not None:
            atoms.append((match.start(), end))
            yield "call", match.start(), end

    # 先取完整操作数（函数、括号组、变量或数值），避免将下标、闭括号或小数截在等号外。
    def relation(left, right):
        # 一元运算符可以是关系式的第一个字形；正文中的连字符仍留在原处。
        while left and text[left - 1] in "√∛∜∇∂":
            left -= 1
        if (
            left
            and text[left - 1] in "+-−±"
            and (left == 1 or not text[left - 2].isalnum())
        ):
            left -= 1
        return "relation", left, right

    start = end = None
    has_relation = False
    for left, right in sorted(atoms, key=lambda item: (item[0], -item[1])):
        if end is not None:
            if left < end:
                continue
            ops = "".join(_normal(text[end:left]).split())
            if ops and all(
                c in _RELATIONS
                or c in _ASCII_OPERATORS
                or unicodedata.category(c) == "Sm"
                for c in ops
            ):
                has_relation |= not _RELATIONS.isdisjoint(ops)
                end = right
                continue
            if has_relation:
                yield relation(start, end)
        start, end, has_relation = left, right, False
    if has_relation:
        yield relation(start, end)


def _line_text(line):
    # 用主字形恢复词界，同时记录各操作数占用的全部上下标字形。
    text, places = [], []
    for i, group in enumerate(line):
        g = group[0]
        if i and g.x - max(ch.right for ch in line[i - 1]) > 0.16 * g.size:
            text.append(" ")
            places.append(())
        text.append(g.char)
        places.append(group)
    return "".join(text), places


def _mark_line(line, selected):
    text, places = _line_text(line)
    if not re.search(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}", text):
        return

    def mark(start, end):
        for group in places[start:end]:
            for g in group:
                selected[g.font].update(
                    range(g.offset, g.offset + len(_normal(g.char)))
                )

    for _, start, end in math_ranges(text):
        mark(start, end)
    # 主字母和上下标必须进入同一公式占位符，译文换行时才能保留原字形与相对位置。
    for i, group in enumerate(places):
        if len(group) > 1:
            mark(i, i + 1)
    for i, (a, b) in enumerate(
        pairwise(group[0] if group else None for group in places)
    ):
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


def _mark_horizontal(glyphs, selected):
    # 按主字形划分行、解析调用和关系式，匹配后连同附属字形一起保护。
    line = []
    for i, end in script_ranges(glyphs, include_plain=True):
        g = glyphs[i]
        if line:
            prev = line[-1][0]
            em = max(g.size, prev.size)
            if (
                abs(g.y - prev.y) > 0.75 * em
                or g.x < prev.x - 0.7 * em
                or g.x > max(ch.right for ch in line[-1]) + 2.2 * em
            ):
                _mark_line(line, selected)
                line = []
        line.append(glyphs[i:end])
    _mark_line(line, selected)


def inline_slots(page, fraction_bars=()):
    if page.rotation:
        return {}
    strings, offsets, selected = defaultdict(list), defaultdict(int), defaultdict(set)
    glyphs = []
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
            if horizontal:
                glyphs.append(g)
            elif glyphs:
                _mark_horizontal(glyphs, selected)
                glyphs.clear()
    _mark_horizontal(glyphs, selected)
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
    result = []
    for raw, char in codes:
        value = cmap.get(char, char)
        # 无 ToUnicode 的 Type 1 可用 /y.alt 等字形名。沿用产生 texttrace 的
        # MuPDF 解码约定，否则一个替代字形就会让整页同字体的坐标校验失败。
        if char not in cmap and len(char) > 1 and char.startswith("/"):
            scalar = pdf.mupdf.fz_unicode_from_glyph_name(char[1:])
            if scalar not in (0, 0xFFFD):
                value = chr(scalar)
        result.append((raw, value))
    return result


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


@lru_cache(maxsize=14)
def _core_descriptor(name):
    # 使用同一渲染器内置的标准字体替身。缓存的只读字体资源可跨页复用。
    with pdf.open() as sample:
        page = sample.new_page()
        page.insert_font(
            fontname="Builtin", fontbuffer=pdf.Font(name).buffer, set_simple=True
        )
        reader = PdfReader(BytesIO(sample.tobytes()))
    return reader.pages[0]["/Resources"]["/Font"]["/Builtin"].raw_get("/FontDescriptor")


def _prepare_core_font(writer, font, source, codes):
    # 标准字体快路径按 Unicode 查字宽。临时私用字符会被当作零宽，故保护期间显式使用字体程序和原字宽。
    name = str(font.get("/BaseFont", "")).lstrip("/")
    if font.get("/Subtype") != "/Type1" or name not in pdf.Base14_fontdict.values():
        return False
    if "/Widths" not in font:
        from pypdf.generic._font import Font

        encoded = DictionaryObject(font)
        encoded.pop("/ToUnicode", None)
        metrics = Font.from_font_resource(encoded)
        first, last = min(codes)[0], max(codes)[0]
        font[NameObject("/FirstChar")] = NumberObject(first)
        font[NameObject("/LastChar")] = NumberObject(last)
        font[NameObject("/Widths")] = ArrayObject(
            FloatObject(metrics.get_text_width(chr(code)))
            for code in range(first, last + 1)
        )
    descriptor = font.get("/FontDescriptor", DictionaryObject()).get_object()
    if any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
        descriptor = DictionaryObject(descriptor)
        descriptor[NameObject("/FontName")] = NameObject("/PDFZHC+" + name)
        ref = writer._add_object(descriptor)
    else:
        ref = _core_descriptor(name).clone(writer)
    font[NameObject("/FontDescriptor")] = ref
    font[NameObject("/BaseFont")] = ref.get_object()["/FontName"]
    font[NameObject("/PDF2zhCoreSource")] = (
        source if isinstance(source, IndirectObject) else writer._add_object(source)
    )
    return True


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
        alias = NameObject(f"/PDF2zhRun{len(originals)}")
        while alias in fonts:
            alias = NameObject(str(alias) + "X")
        clone = DictionaryObject(fonts[font])
        core = _prepare_core_font(writer, clone, fonts.raw_get(font), chars)
        marked = _cmap(
            {raw: "\ue000" for raw in chars},
            ("PDF2zhInlineCore" if core else "PDF2zhInline") + digest,
        )
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
