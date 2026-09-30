"""Telegram canlı iş günlüğü: satır biçimi, tekrar birleştirme, hata işareti, süre etiketi ve ileti yönetimi."""
import logging

import pytest

from omniagent.config import register_secret
from omniagent.integrations import telegram
from omniagent.integrations.telegram_activity import (
    activity_html, append_entry, elapsed_label, mark_failed, render_entries,
)
from test_telegram_bridge import FakeAPI


def test_shell_command_is_a_code_block_and_secrets_are_masked() -> None:
    register_secret("test_activity_token", "sk-gizli-7f3a9c21")
    line = activity_html("execute_shell", "curl -H 'Bearer sk-gizli-7f3a9c21' https://x.test <y>")
    assert line.startswith('💻 Kabuk\n<pre><code class="language-shell">')
    assert "sk-gizli-7f3a9c21" not in line and "&lt;y&gt;" in line


def test_file_step_is_one_clipped_line() -> None:
    line = activity_html("write_file", "/Users/dogan/Desktop/" + "a" * 120)
    assert line.startswith("✍️ Yazıyor /Users/dogan/Desktop/") and line.endswith("…") and "\n" not in line


def test_repeated_steps_collapse_and_failures_are_marked() -> None:
    line = activity_html("read_file", "/tmp/a.txt")
    entries = append_entry(append_entry(append_entry([], "c1", line), "c2", line), "c3", line)
    assert render_entries(entries) == line + " (×3)"
    failed = mark_failed(entries, "c2")
    assert render_entries(failed) == "⚠️ " + line + " (×3)"
    # Başarısız satırdan sonraki aynı adım yeni satır açar; işaret yanlış adıma geçmez.
    assert render_entries(append_entry(failed, "c4", line)).splitlines()[-1] == line


def test_elapsed_label_counts_minutes_and_hours() -> None:
    assert elapsed_label(30.0) == "<1 dk"
    assert elapsed_label(600.0) == "10 dk"
    assert elapsed_label(3900.0) == "1 sa 5 dk"


@pytest.mark.asyncio
async def test_activity_log_edits_one_message_and_opens_a_new_one_when_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram, "LOG_EDIT_SECONDS", 0.0)
    api = FakeAPI()
    log = telegram.ActivityLog(api, 123)
    await log.started("c1", "read_file", "/tmp/a.txt")
    await log.started("c2", "write_file", "/tmp/b.txt")
    assert api.html_sent == ["📖 Okuyor /tmp/a.txt"]
    assert api.html_edited[-1] == "📖 Okuyor /tmp/a.txt\n✍️ Yazıyor /tmp/b.txt"
    await log.finished("c2", False)
    assert api.html_edited[-1].endswith("⚠️ ✍️ Yazıyor /tmp/b.txt")
    monkeypatch.setattr(telegram, "LOG_LIMIT", 200)
    for index in range(4):
        await log.started(f"k{index}", "execute_shell", f"echo {index} " + "x" * 40)
    assert len(api.html_sent) >= 2 and all(len(text) <= 200 for text in api.html_sent + api.html_edited)


NOT_MODIFIED = telegram.TelegramError(
    "editMessageText: HTTP 400: Bad Request: message is not modified: specified new message content and reply "
    "markup are exactly the same as a current content and reply markup of the message", 400)


def api_error(method: str, status: int, description: str) -> telegram.TelegramError:
    return telegram.TelegramError(f"{method}: HTTP {status}: {description}", status)


@pytest.mark.asyncio
async def test_activity_log_treats_message_is_not_modified_as_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    """İçerik zaten görünüyorsa (400 'not modified') hata değil başarıdır: yükseltilmez, yeniden denenmez."""
    monkeypatch.setattr(telegram, "LOG_EDIT_SECONDS", 0.0)
    api = FakeAPI()
    log = telegram.ActivityLog(api, 123)
    await log.started("c1", "read_file", "/tmp/a.txt")
    attempts: list[str] = []

    async def already_current(chat_id: int, message_id: int, content: str) -> None:
        attempts.append(content)
        raise NOT_MODIFIED

    api.edit_html = already_current
    await log.started("c2", "write_file", "/tmp/b.txt")
    assert attempts == ["📖 Okuyor /tmp/a.txt\n✍️ Yazıyor /tmp/b.txt"] and log.shown == attempts[0]
    await log.flush()
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_activity_log_retries_transient_errors_and_reopens_a_message_that_is_gone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """5xx/429/ağ sonraki düzenlemede yeniden denenir; silinmiş günlük iletisinin yerine tam günlükle yeni ileti açılır."""
    monkeypatch.setattr(telegram, "LOG_EDIT_SECONDS", 0.0)
    api = FakeAPI()
    log = telegram.ActivityLog(api, 123)
    await log.started("c1", "read_file", "/tmp/a.txt")
    failures = [api_error("editMessageText", 503, "Service Unavailable"),
                api_error("editMessageText", 400, "Bad Request: message to edit not found")]
    edited: list[str] = []

    async def scripted_edit(chat_id: int, message_id: int, content: str) -> None:
        edited.append(content)
        if failures:
            raise failures.pop(0)
        api.html_edited.append(content)

    api.edit_html = scripted_edit
    with caplog.at_level(logging.WARNING):
        await log.started("c2", "write_file", "/tmp/b.txt")
        assert log.shown == "📖 Okuyor /tmp/a.txt"  # 503: gösterilmedi, sonra yeniden denenecek
        await log.started("c3", "read_file", "/tmp/c.txt")
        assert len(edited) == 2  # 400 'bulunamadı': yükseltilmedi
        await log.flush()
    full = "📖 Okuyor /tmp/a.txt\n✍️ Yazıyor /tmp/b.txt\n📖 Okuyor /tmp/c.txt"
    assert api.html_sent == ["📖 Okuyor /tmp/a.txt", full] and log.shown == full
    assert [record.getMessage() for record in caplog.records].count(
        "İş günlüğü güncellenemedi; sonraki düzenlemede yeniden denenecek") == 1


@pytest.mark.asyncio
async def test_activity_log_closes_for_the_run_when_new_messages_are_rejected(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Yeni ileti de kalıcı 4xx ile reddedilirse (ör. bot engellendi) günlük bu koşu için kapanır; koşu iptal olmaz."""
    monkeypatch.setattr(telegram, "LOG_EDIT_SECONDS", 0.0)
    api = FakeAPI()
    attempts: list[str] = []

    async def blocked(chat_id: int, content: str) -> int:
        attempts.append(content)
        raise api_error("sendMessage", 403, "Forbidden: bot was blocked by the user")

    api.send_html = blocked
    log = telegram.ActivityLog(api, 123)
    with caplog.at_level(logging.WARNING):
        await log.started("c1", "read_file", "/tmp/a.txt")
        await log.started("c2", "write_file", "/tmp/b.txt")
        await log.finished("c2", False)
        await log.flush()
    assert len(attempts) == 1 and api.html_edited == []
    closed = [record for record in caplog.records if record.getMessage() == "İş günlüğü açılamadı; bu koşu için günlük kapatıldı"]
    assert [record.status for record in closed] == [403]
