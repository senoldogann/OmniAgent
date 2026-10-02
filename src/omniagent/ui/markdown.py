"""Tk transkripti için yan etkisiz, komut metinlerini koruyan Markdown dönüşümü."""
import re
import unicodedata
from typing import List, NotRequired, Tuple, TypedDict


class Span(TypedDict):
    text: str
    style: str
    url: NotRequired[str]


class Block(TypedDict):
    kind: str
    level: int
    spans: List[Span]
    lines: List[str]
    rows: List[List[str]]


Part = Tuple[str, Tuple[str, ...]]
_INLINE = re.compile(
    r"(?P<code>(?<!`)(?P<ticks>`+)(?!`)(?P<body>.*?)(?<!`)(?P=ticks)(?!`))"
    r"|(?P<link>\[(?P<label>[^\]\n]+)\]\((?P<url>[^\s)]+)\))"
    r"|(?P<bold>(?<!\w)\*\*(?=\S)(?P<strong>[^\n]*?\S)\*\*(?!\w))"
    r"|(?P<italic>(?<!\S)\*(?=\S)(?P<em>[^*\n]*?\S|[^*\s])\*(?=\s|$|[.,!?;:]))"
)
_LATEX_TEXT = re.compile(r"\$\\(?:text|mathrm|mathbf)\{([^{}]*)\}\$")


def _plain_text(value: str) -> str:
    """Modellerin düz metinde ürettiği basit LaTeX yazı komutlarını okunur metne çevirir."""
    return _LATEX_TEXT.sub(r"\1", value)


def _parse_markup(line: str) -> List[Span]:
    """Korunmuş kod parçalarının dışındaki vurguları çözümler."""
    spans: List[Span] = []
    position = 0
    for match in _INLINE.finditer(line):
        if match.start() > position:
            spans.append({"text": _plain_text(line[position:match.start()]), "style": "plain"})
        if match.group("code"):
            spans.append({"text": match.group("body"), "style": "code"})
        elif match.group("link"):
            spans.append({"text": match.group("label"), "style": "link", "url": match.group("url")})
        elif match.group("bold"):
            # İçteki kod biçimi yıldızlardan bağımsız korunur.
            spans.extend({**s, "style": "bold" if s["style"] == "plain" else s["style"]}
                         for s in _parse_markup(match.group("strong")))
        else:
            spans.append({"text": _plain_text(match.group("em")), "style": "italic"})
        position = match.end()
    if position < len(line):
        spans.append({"text": _plain_text(line[position:]), "style": "plain"})
    return spans


def parse_spans(line: str) -> List[Span]:
    """Kodları önce ayırır; dıştaki vurgu kod içindeki ayraçlara erişemez."""
    marker = "\x00"
    while marker in line:
        marker += "\x00"
    codes: List[str] = []

    def protect(match: re.Match[str]) -> str:
        codes.append(match.group("body"))
        return marker + str(len(codes) - 1) + marker

    code_pattern = re.compile(r"(?<!`)(?P<ticks>`+)(?!`)(?P<body>.*?)(?<!`)(?P=ticks)(?!`)")
    protected = code_pattern.sub(protect, line)
    token = re.compile(re.escape(marker) + r"(\d+)" + re.escape(marker))
    result: List[Span] = []
    for span in _parse_markup(protected):
        position = 0
        for match in token.finditer(span["text"]):
            if match.start() > position:
                result.append({**span, "text": span["text"][position:match.start()]})
            result.append({"text": codes[int(match.group(1))], "style": "code"})
            position = match.end()
        if position < len(span["text"]):
            result.append({**span, "text": span["text"][position:]})
    return result


def _cells(line: str) -> List[str]:
    """Kaçırılmış ve kod içindeki boru işaretlerini hücre sınırı saymaz."""
    text = line.strip().strip("|")
    cells: List[str] = []
    current = ""
    fence = ""
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] == "|":
            current += "|"
            index += 2
            continue
        if char == "`":
            end = index + 1
            while end < len(text) and text[end] == "`":
                end += 1
            ticks = text[index:end]
            fence = "" if fence == ticks else ticks if not fence else fence
            current += ticks
            index = end
            continue
        if char == "|" and not fence:
            cells.append(current.strip())
            current = ""
        else:
            current += char
        index += 1
    return cells + [current.strip()]


def _separator(line: str) -> bool:
    cells = _cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _block(kind: str, text: str = "", level: int = 0) -> Block:
    return {"kind": kind, "level": level, "spans": parse_spans(text), "lines": [], "rows": []}


def parse_blocks(text: str) -> List[Block]:
    """Başlık, liste, alıntı, tablo ve kapanmamış kod çitlerini çözümler."""
    lines = text.splitlines()
    blocks: List[Block] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            marker = fence.group(1)
            block = _block("code")
            index += 1
            while index < len(lines):
                if re.fullmatch(r"\s{0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*", lines[index]):
                    index += 1
                    break
                block["lines"].append(lines[index])
                index += 1
            blocks.append(block)
            continue
        if index + 1 < len(lines) and "|" in line and _separator(lines[index + 1]):
            block = _block("table")
            block["rows"].append(_cells(line))
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                block["rows"].append(_cells(lines[index]))
                index += 1
            blocks.append(block)
            continue
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+)$", line)
        item = re.match(r"^(\s*)([-+*]|\d+[.)])\s+(.+)$", line)
        quote = re.match(r"^\s{0,3}>\s?(.*)$", line)
        if heading:
            blocks.append(_block("heading", heading.group(2), min(3, len(heading.group(1)))))
        elif re.fullmatch(r"\s{0,3}(?:\*\s*){3,}|\s{0,3}(?:-\s*){3,}|\s{0,3}(?:_\s*){3,}", line):
            blocks.append(_block("rule"))
        elif item:
            prefix = "•" if item.group(2) in "-+*" else item.group(2)
            blocks.append(_block("list_item", " " * len(item.group(1)) + prefix + " " + item.group(3)))
        elif quote:
            blocks.append(_block("quote", "│ " + quote.group(1)))
        elif re.match(r"^Kaynak \d+:\s", line):
            blocks.append(_block("heading", line, 3))
        else:
            blocks.append(_block("paragraph", line))
        index += 1
    return blocks


def _width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def format_table(rows: List[List[str]]) -> List[str]:
    """Hücreleri biçim işaretleri olmadan mono sütunlara hizalar."""
    if not rows:
        return []
    plain = [["".join(s["text"] for s in parse_spans(cell)) for cell in row] for row in rows]
    count = max(map(len, plain))
    widths = [max((_width(row[i]) if i < len(row) else 0) for row in plain) for i in range(count)]
    result: List[str] = []
    for number, row in enumerate(plain):
        cells = row + [""] * (count - len(row))
        result.append(" │ ".join(cell + " " * (width - _width(cell)) for cell, width in zip(cells, widths, strict=True)))
        if number == 0:
            result.append("─┼─".join("─" * width for width in widths))
    return result


def render_markdown(text: str, max_columns: int) -> List[Part]:
    """
    Doğrudan UI._insert_parts tarafından tüketilen metin/etiket çiftlerini üretir.
    max_columns: sütunlu tablonun satır kaydırmadan sığdığı en geniş mono karakter sayısı.
    """
    parts: List[Part] = []
    for block in parse_blocks(text):
        kind = block["kind"]
        base = ("assistant",)
        if kind == "code":
            lines = block["lines"] or [""]
            for index, line in enumerate(lines):
                # Blok içi satırlar sıkı kalır; iç boşluk yalnız ilk ve son satıra eklenir.
                edges = (("md_codeblock_first",) if index == 0 else ()) + (
                    ("md_codeblock_last",) if index == len(lines) - 1 else ())
                parts.append((line + "\n", base + ("md_codeblock",) + edges))
        elif kind == "table":
            rows = block["rows"]
            table_lines = format_table(rows)
            # Yalnız başlıktan oluşan tablonun dikey karta dönüşecek satırı yoktur; sütunlu kalır.
            if len(rows) == 1 or max(_width(line) for line in table_lines) <= max_columns:
                for index, line in enumerate(table_lines):
                    parts.append((line + "\n", base + ("md_table",) + (("md_table_head",) if index == 0 else ())))
            else:
                # Geniş hücreler, sabit sütun aralıklarında okunamayacak kadar taşar.
                headers = rows[0]
                for row in rows[1:]:
                    for column, value in enumerate(row):
                        label = headers[column] if column < len(headers) else f"Sütun {column + 1}"
                        parts.append((label + "\n", base + ("md_table_label",)))
                        for span in parse_spans(value):
                            tags = base + ("md_table_value",)
                            if span["style"] != "plain":
                                tags += (f"md_{span['style']}",)
                            if span.get("url", "").startswith(("https://", "http://")):
                                tags += (f"md_href:{span['url']}",)
                            parts.append((span["text"], tags))
                        parts.append(("\n", base + ("md_table_value",)))
                    parts.append(("\n", base + ("md_table_gap",)))
        elif kind == "rule":
            parts.append(("─" * 32 + "\n", base + ("md_rule",)))
        else:
            tag = f"md_h{block['level']}" if kind == "heading" else {"list_item": "md_bullet", "quote": "md_quote"}.get(kind)
            tags = base + ((tag,) if tag else ())
            if kind == "list_item" and block["spans"]:
                first = block["spans"][0]
                marker = re.match(r"^(\s*(?:•|\d+[.)])\s+)(.*)$", first["text"])
                if marker:
                    parts.append((marker.group(1), tags + ("md_list_marker",)))
                    block["spans"][0] = {**first, "text": marker.group(2)}
            for span in block["spans"]:
                span_tags = tags + ((f"md_{span['style']}",) if span["style"] != "plain" else ())
                if span.get("url", "").startswith(("https://", "http://")):
                    span_tags += (f"md_href:{span['url']}",)
                parts.append((span["text"], span_tags))
            parts.append(("\n", tags))
    return parts
