"""Markdown dönüşümünde komut sadakati ve blok biçimleri."""
from omniagent.ui.markdown import format_table, parse_blocks, parse_spans, render_markdown


def test_commands_remain_literal() -> None:
    text = "ls *.py MAX_WALL_CLOCK_SECONDS snake_case 2 * 3"
    assert parse_spans(text) == [{"text": text, "style": "plain"}]
    assert parse_spans("`*x* **y** snake_case`") == [
        {"text": "*x* **y** snake_case", "style": "code"}]


def test_inline_styles_and_links() -> None:
    spans = parse_spans("**kalın** *italik* [site](https://example.com)")
    assert [s["style"] for s in spans] == ["bold", "plain", "italic", "plain", "link"]
    assert spans[-1] == {"text": "site", "style": "link", "url": "https://example.com"}
    assert parse_spans("a*b*c")[0]["text"] == "a*b*c"


def test_link_keeps_click_target_outside_visible_text() -> None:
    parts = render_markdown("[kaynak](https://example.com/page)", max_columns=64)
    assert "".join(text for text, _ in parts) == "kaynak\n"
    assert "md_href:https://example.com/page" in parts[0][1]


def test_fences_keep_contents_even_if_unclosed() -> None:
    for ending in ("", "\n```"):
        blocks = parse_blocks("```python\n2 * 3\n**ham**" + ending)
        assert len(blocks) == 1
        assert blocks[0]["kind"] == "code"
        assert blocks[0]["lines"] == ["2 * 3", "**ham**"]
    assert parse_blocks("````\n```\nx")[0]["lines"] == ["```", "x"]


def test_table_alignment_and_escaped_pipes() -> None:
    rows = [["Ad", "Değer"], ["uzun ad", "3"], ["x", "42"]]
    lines = format_table(rows)
    assert len(set(len(line) for line in lines)) == 1
    assert lines[0].index("│") == lines[2].index("│")
    block = parse_blocks("| Ad | Değer |\n| --- | ---: |\n| `a|b` | x\\|y |")[0]
    assert block["rows"][1] == ["`a|b`", "x|y"]
    assert "**" not in format_table([["**Ad**"], ["x"]])[0]
    assert format_table([]) == []


def test_block_tags_and_empty_input() -> None:
    assert render_markdown("", max_columns=64) == []
    parts = render_markdown("# Başlık\n- madde\n> alıntı\n---\n```\nx * y", max_columns=64)
    tags = {tag for _, group in parts for tag in group}
    assert {"md_h1", "md_bullet", "md_quote", "md_rule", "md_codeblock"} <= tags
    assert "x * y\n" in [text for text, _ in parts]


def test_code_block_pads_only_its_outer_lines() -> None:
    assert render_markdown("```\nbir\niki\n```", max_columns=64) == [
        ("bir\n", ("assistant", "md_codeblock", "md_codeblock_first")),
        ("iki\n", ("assistant", "md_codeblock", "md_codeblock_last")),
    ]
    assert render_markdown("```\ntek\n```", max_columns=64) == [
        ("tek\n", ("assistant", "md_codeblock", "md_codeblock_first", "md_codeblock_last")),
    ]


def test_code_delimiters_inside_bold_are_protected_first() -> None:
    spans = parse_spans("**önce `a**b` sonra**")
    assert spans == [{"text": "önce ", "style": "bold"},
                     {"text": "a**b", "style": "code"},
                     {"text": " sonra", "style": "bold"}]


def test_long_table_uses_readable_stacked_rows_and_plain_math_text() -> None:
    answer = (
        "| Güçlü Yönler | Geliştirilebilir Alanlar / Riskler |\n"
        "| --- | --- |\n"
        "| Uzun bir açıklama ile anlatılan güçlü yön ve ayrıntılı kanıtlar | "
        "Uzun bir açıklama ile anlatılan risk ve ayrıntılı gerekçeler |\n"
        "\nKalite: $\\text{Yüksek}$; komut: `$\\text{ham}$`"
    )
    parts = render_markdown(answer, max_columns=64)
    visible = "".join(value for value, _ in parts)
    assert "Güçlü Yönler\n" in visible
    assert "Geliştirilebilir Alanlar / Riskler\n" in visible
    assert "─┼─" not in visible
    assert "Kalite: Yüksek" in visible
    assert "$\\text{ham}$" in visible
    assert any("md_table_label" in tags for _, tags in parts)
    assert parse_spans("*$\\text{Yüksek}$*") == [{"text": "Yüksek", "style": "italic"}]


def test_wide_table_without_rows_keeps_its_header_visible() -> None:
    header = " | ".join(f"Uzun sütun başlığı {number}" for number in range(1, 5))
    parts = render_markdown(f"| {header} |\n| --- | --- | --- | --- |", max_columns=64)
    assert "Uzun sütun başlığı 4" in "".join(value for value, _ in parts)


def test_numbered_markers_and_source_headers_are_distinct_and_lossless():
    parts = render_markdown("## **Özet**\n1. Birinci sonuç\n2. **İkinci sonuç**\n\nKaynak 1: web_search — başarılı", max_columns=64)
    assert any(text == "1. " and "md_list_marker" in tags for text, tags in parts)
    assert any(text == "2. " and "md_list_marker" in tags for text, tags in parts)
    assert any("md_h2" in tags and "md_bold" in tags for text, tags in parts)
    assert any(text.startswith("Kaynak 1:") and "md_h3" in tags for text, tags in parts)
    visible = "".join(text for text, tags in parts)
    assert "1. Birinci sonuç" in visible and "2. İkinci sonuç" in visible
