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
    assert spans[-1]["text"] == "site (https://example.com)"
    assert parse_spans("a*b*c")[0]["text"] == "a*b*c"


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
    assert render_markdown("") == []
    parts = render_markdown("# Başlık\n- madde\n> alıntı\n---\n```\nx * y")
    tags = {tag for _, group in parts for tag in group}
    assert {"md_h1", "md_bullet", "md_quote", "md_rule", "md_codeblock"} <= tags
    assert "x * y\n" in [text for text, _ in parts]


def test_code_delimiters_inside_bold_are_protected_first() -> None:
    spans = parse_spans("**önce `a**b` sonra**")
    assert spans == [{"text": "önce ", "style": "bold"},
                     {"text": "a**b", "style": "code"},
                     {"text": " sonra", "style": "bold"}]
