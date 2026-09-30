"""Transkript sunumunun saf dönüşümleri: süre/token biçimi, katlama etiketleri, araç satırı parçaları."""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Dict, List

import pytest

from omniagent.ui.chats import TranscriptSpan
from omniagent.ui.markdown import Part
from omniagent.ui.rendering import (
    ToolView, activity_meta, command_block_lines, detail_output_lines, error_hint, fold_tags,
    format_elapsed, format_run_summary, group_head_parts, parse_fold_tag, plain_transcript, sent_time_text,
    remap_fold_tags, summary_parts, tool_parts, tool_title,
)
from omniagent.ui.theme import COLUMN_MAX_WIDTH, COLUMN_MIN_SIDE, column_side, column_width


@pytest.mark.parametrize("seconds,expected", [
    (0, "0sn"), (10.9, "10sn"), (60, "1dk"), (370, "6dk 10sn"), (3725, "1sa 2dk"), (-4, "0sn"),
])
def test_elapsed_time_is_short_and_turkish(seconds: float, expected: str) -> None:
    assert format_elapsed(seconds) == expected


@pytest.mark.parametrize("elapsed,tokens,approximate,expected", [
    (370, 420, True, "6dk 10sn · ~420 token"),
    (12, 0, False, "12sn"),
    (5, 2775, False, "5sn · 2.8k token"),
])
def test_activity_meta_marks_estimated_tokens(
    elapsed: float, tokens: int, approximate: bool, expected: str,
) -> None:
    assert activity_meta(elapsed, tokens, approximate) == expected


@pytest.mark.parametrize("width,side,column", [
    (1500, 310, 880), (1000, 60, 880), (896, COLUMN_MIN_SIDE, 864), (400, COLUMN_MIN_SIDE, 368), (-5, COLUMN_MIN_SIDE, 1),
])
def test_content_column_is_centered_and_capped(width: int, side: int, column: int) -> None:
    assert column_side(width) == side
    assert column_width(width) == column
    assert column <= COLUMN_MAX_WIDTH


def test_fold_tags_round_trip_and_ignore_other_tags() -> None:
    assert fold_tags("t", 5) == {"click": "th5", "body": "tb5", "open": "to5", "closed": "tc5"}
    assert parse_fold_tag("gb12") == ("g", "b", 12)
    assert parse_fold_tag("sh3") == ("s", "h", 3)
    for other in ("goal", "gap", "r12", "md_href:https://x.y", "tool_row", "gb", "xb1", "gz4"):
        assert parse_fold_tag(other) is None


def test_saved_fold_ids_are_renumbered_without_touching_other_tags() -> None:
    spans: List[TranscriptSpan] = [
        {"text": "a", "tags": ["tool_row", "gb3", "th7"]},
        {"text": "b", "tags": ["gh3", "md_href:https://x.y"]},
        {"text": "c", "tags": ["tb7", "gb3"]},
    ]
    snapshot = copy.deepcopy(spans)
    remapped, next_id = remap_fold_tags(spans, 100)
    assert remapped == [
        {"text": "a", "tags": ["tool_row", "gb100", "th101"]},
        {"text": "b", "tags": ["gh100", "md_href:https://x.y"]},
        {"text": "c", "tags": ["tb101", "gb100"]},
    ]
    assert next_id == 102
    assert spans == snapshot


def _view(**overrides: object) -> ToolView:
    view: Dict[str, object] = {
        "region": "r1", "name": "execute_shell", "preview": "git status", "status": "ok",
        "call_id": "c1", "head": [], "tail": [], "line_count": 0,
        "result": "STDOUT: temiz\nSTDERR: \nÇıkış Kodu: 0", "seconds": 0.42, "started_at": 0.0,
        "group_id": 1, "fold_id": 2, "more_id": 3,
    }
    view.update(overrides)
    return view  # type: ignore[return-value]


def _text(parts: List[Part]) -> str:
    return "".join(content for content, _tags in parts)


def _tags_of(parts: List[Part], needle: str) -> List[str]:
    return [tag for content, tags in parts if needle in content for tag in tags]


def test_finished_tool_row_shows_status_time_command_and_folds_by_tags() -> None:
    parts = tool_parts(_view(), 0.0, "✻")
    text = _text(parts)
    assert "✓\tKabuk  git status  0.4sn" in text
    assert "$ git status\n" in text
    assert "temiz\n" in text and "STDOUT" not in text
    # Satır grup gövdesine ve kendi tıklama etiketine bağlı; ayrıntı yalnız araç gövdesine.
    row_tags = next(tags for content, tags in parts if content == "\t")
    assert {"tool_row", "gb1", "th2"} <= set(row_tags)
    command_tags = next(tags for content, tags in parts if content == "git status\n")
    assert {"cmd_block", "gb1", "tb2"} <= set(command_tags)
    assert parts[-1] == ("\n", ("tool_rule", "gb1"))


def test_running_tool_row_uses_the_spinner_frame_and_live_output() -> None:
    view = _view(status="running", started_at=10.0, tail=["bir\n", "iki\n"], line_count=9, result="")
    parts = tool_parts(view, 12.5, "✶")
    assert parts[0] == ("✶", ("tool_row", "gb1", "th2", "tool_spin"))
    text = _text(parts)
    assert "2.5sn" in text and "… 7 satır daha" in text and "iki\n" in text


def test_failed_tool_row_shows_error_hint_and_red_tone() -> None:
    view = _view(status="error", result="\nSHELL_EXIT: komut başarısız\nayrıntı", name="read_file",
                 preview="/tmp/yok.txt")
    parts = tool_parts(view, 0.0, "✻")
    assert parts[0][0] == "✗" and "glyph_error" in parts[0][1]
    assert "  — SHELL_EXIT: komut başarısız" in _text(parts)
    assert error_hint("\n\n  ilk dolu satır  \nikinci") == "ilk dolu satır"
    assert "out_error" in _tags_of(parts, "ayrıntı")


def test_long_output_keeps_a_hidden_remainder_behind_a_show_all_link() -> None:
    lines = "\n".join(f"satır {number}" for number in range(1, 13))
    parts = tool_parts(_view(result=f"STDOUT: {lines}\nSTDERR: \nÇıkış Kodu: 0"), 0.0, "✻")
    more = fold_tags("m", 3)
    assert ("… +4 satır · tümünü göster ▾\n") in _text(parts)
    assert more["body"] in _tags_of(parts, "satır 12")
    assert more["body"] not in _tags_of(parts, "satır 8\n")
    assert more["closed"] in _tags_of(parts, "tümünü göster") and more["open"] in _tags_of(parts, "daha az")


@pytest.mark.parametrize("name,ok,result,expected", [
    ("execute_shell", True, "STDOUT: a\nb\nSTDERR: \nÇıkış Kodu: 0", ["a", "b"]),
    ("execute_shell", True, "STDOUT: a\nSTDERR: uyarı\nÇıkış Kodu: 0", ["a", "— stderr —", "uyarı"]),
    ("execute_shell", True, "STDOUT: \nSTDERR: \nÇıkış Kodu: 0", []),
    ("execute_shell", False, "SHELL_EXIT: kötü\nayrıntı", ["SHELL_EXIT: kötü", "ayrıntı"]),
    ("web_search", True, '[{"title": "Bir"}, {"title": "İki"}, "atla"]', ["• Bir", "• İki"]),
    ("read_file", True, "x\ny\n\n", ["x", "y"]),
])
def test_detail_output_unwraps_tool_results(name: str, ok: bool, result: str, expected: List[str]) -> None:
    assert detail_output_lines(name, ok, result) == expected


def test_titles_and_command_blocks_are_bounded() -> None:
    long_command = "echo " + "x" * 200
    label, preview = tool_title("execute_shell", long_command + "\nikinci satır")
    assert label == "Kabuk" and preview.endswith("…") and len(preview) == 91
    assert command_block_lines("read_file", "/kısa/yol.txt") == []
    assert command_block_lines("read_file", "u" * 200)
    lines = command_block_lines("execute_shell", "\n".join(f"komut {n}" for n in range(10)))
    assert lines[:6] == [f"komut {n}" for n in range(6)] and lines[6] == "…" and len(lines) == 7


def test_group_head_and_summary_have_both_chevrons_and_hidden_details() -> None:
    head = group_head_parts(4, 3, False)
    assert _text(head).startswith("3 araç çalıştırıldı  ▾  ▸\n")
    assert fold_tags("g", 4)["open"] in _tags_of(head, "▾") and fold_tags("g", 4)["closed"] in _tags_of(head, "▸")
    assert group_head_parts(4, 3, True)[0][0] == "3 araç çalışıyor"
    metrics = {"turns": 2, "tool_calls": 1, "elapsed_seconds": 5.1, "backend": "opencode",
               "prompt_tokens": 100, "cached_tokens": 80, "completion_tokens": 20,
               "model_seconds": 4.0, "tool_seconds": 1.0}
    assert format_run_summary(metrics) == "5.1 sn · 2 tur · 1 araç · toplam 120 token"  # type: ignore[arg-type]
    parts = summary_parts(9, True, "", metrics)  # type: ignore[arg-type]
    assert _text(parts).startswith("✓ Tamamlandı  ·  5.1 sn · 2 tur · 1 araç · toplam 120 token  ▾  ▸\n")
    assert fold_tags("s", 9)["body"] in _tags_of(parts, "Giriş 100")


def test_clipboard_copy_drops_chevrons_and_tabs() -> None:
    assert plain_transcript("✓\tKabuk  git  0.4sn  ▾  ▸\nmetin ▾ kalır") == "✓  Kabuk  git  0.4sn\nmetin ▾ kalır"


def test_sent_time_text_renders_local_hh_mm_and_ignores_broken_stamps() -> None:
    """Kullanıcı mesajının altındaki saat yerel saatte HH:MM olur; bozuk kayıt saatsiz gösterilir."""
    stamp = "2026-09-29T11:40:00+00:00"
    expected = datetime.fromisoformat(stamp).astimezone().strftime("%H:%M")
    assert sent_time_text(stamp) == expected and len(expected) == 5
    assert sent_time_text("bozuk") == "" and sent_time_text("") == ""
