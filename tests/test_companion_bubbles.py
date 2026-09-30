"""Model metnini balonlara çevirme: markdown temizliği, 4 balon sınırı, akış satırları, insan benzeri aralık."""
from omniagent.companion.bubbles import (
    MAX_BUBBLE_CHARS, bubble_delay, clean_line, split_bubbles, split_complete_lines,
)


def test_markdown_is_removed_from_bubbles() -> None:
    assert clean_line("- **tamam** bakıyorum") == "tamam bakıyorum"
    assert clean_line("## başlık") == "başlık"
    assert clean_line("1. `ls` çalıştırdım") == "ls çalıştırdım"


def test_more_than_four_lines_merge_into_last_bubble() -> None:
    assert split_bubbles("a\n\nb\nc\nd\ne") == ["a", "b", "c", "d e"]


def test_long_line_is_clipped() -> None:
    assert len(split_bubbles("x" * 1000)[0]) == MAX_BUBBLE_CHARS


def test_stream_lines_complete_only_at_newline() -> None:
    assert split_complete_lines("sel", "am\nna") == (["selam"], "na")
    assert split_complete_lines("na", "ber") == ([], "naber")


def test_bubble_delay_is_bounded() -> None:
    assert bubble_delay("") == 0.4
    assert bubble_delay("x" * 100) == 1.5
