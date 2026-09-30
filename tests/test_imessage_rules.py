"""iMessage köprüsünün saf kuralları: kim kabul edilir, yansıma, komutlar, evet/hayır/sohbet ayrımı, soru balonları."""
from typing import List

from omniagent.companion.bubbles import MAX_BUBBLE_CHARS
from omniagent.integrations.imessage_rules import (
    accepted, answer_polarity, image_paths, message_text, own_echo, parse_command, question_bubbles,
)
from omniagent.integrations.imsg import Attachment, IncomingMessage

HANDLE = "+905551112233"


def message(sender: str, text: str, is_from_me: bool, is_group: bool,
            attachments: List[Attachment]) -> IncomingMessage:
    return {"rowid": 1, "guid": "g1", "chat_id": 7, "sender": sender, "participants": [HANDLE],
            "is_from_me": is_from_me, "is_group": is_group, "text": text, "created_at": "2026-09-29T12:00:00Z",
            "attachments": attachments}


def test_only_paired_direct_messages_are_accepted() -> None:
    assert accepted(message("+90 555 111 22 33", "selam", False, False, []), HANDLE)
    assert not accepted(message("+905559998877", "selam", False, False, []), HANDLE)
    assert not accepted(message(HANDLE, "selam", False, True, []), HANDLE)
    assert not accepted(message(HANDLE, "selam", True, False, []), HANDLE)
    assert not accepted(message("urn:biz:acme", "selam", False, False, []), HANDLE)


def test_own_echo_is_our_send_in_paired_chat() -> None:
    assert own_echo(message("", "naber", True, False, []), HANDLE)
    assert not own_echo(message("", "naber", True, True, []), HANDLE)
    assert not own_echo(message(HANDLE, "naber", False, False, []), HANDLE)


def test_attachment_only_message_has_no_text_but_keeps_image() -> None:
    photo = message(HANDLE, "￼", False, False, [{"path": "/tmp/p.jpg", "mime_type": "image/jpeg"},
                                                    {"path": "/tmp/a.pdf", "mime_type": "application/pdf"}])
    assert message_text(photo) == ""
    assert image_paths(photo) == ["/tmp/p.jpg"]


def test_commands_are_exact_words_only() -> None:
    assert parse_command("Dur") == "stop"
    assert parse_command("/stop") == "stop"
    assert parse_command("/durum") == "status"
    assert parse_command("dur bi saniye") is None


def test_answer_polarity_separates_yes_no_and_chat() -> None:
    assert answer_polarity("evet") is True
    assert answer_polarity("Olur!") is True
    assert answer_polarity("hayır") is False
    assert answer_polarity("yok.") is False
    assert answer_polarity("napıyorsun") is None


def test_approval_title_is_sent_verbatim() -> None:
    bubbles = question_bubbles("Ödeme: 149,90 TL Apple'a",
                               {"approved": {"type": "boolean"}, "_help": "İşlem: kart ile ödeme"})
    assert bubbles == ["bi onay lazım:", "Ödeme: 149,90 TL Apple'a", "İşlem: kart ile ödeme", "evet mi hayır mı?"]


def test_long_approval_note_is_split_not_cut() -> None:
    help_text: str = "İşlem: " + "x" * 593 + "ABCDEFG"
    bubbles: List[str] = question_bubbles("Onay?", {"approved": {"type": "boolean"}, "_help": help_text})
    assert bubbles[0] == "bi onay lazım:" and bubbles[1] == "Onay?" and bubbles[-1] == "evet mi hayır mı?"
    assert "".join(bubbles[2:-1]) == help_text
    assert all(len(bubble) <= MAX_BUBBLE_CHARS for bubble in bubbles)


def test_long_approval_title_is_split_not_cut() -> None:
    title: str = "Ödeme: " + "y" * 593 + "HEDEF-42"
    bubbles: List[str] = question_bubbles(title, {"approved": {"type": "boolean"}})
    assert bubbles[0] == "bi onay lazım:" and bubbles[-1] == "evet mi hayır mı?"
    assert "".join(bubbles[1:-1]) == title
    assert all(len(bubble) <= MAX_BUBBLE_CHARS for bubble in bubbles)
