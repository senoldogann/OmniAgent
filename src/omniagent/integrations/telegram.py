"""OmniAgent görevlerini eşleştirilmiş özel Telegram sohbetine akışla taşır."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import html
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Dict, List, Optional, Tuple, TypedDict

import httpx
from keyring.backends.macOS import Keyring
from openai import AsyncOpenAI

from omniagent.approval import approval_granted
from .capabilities import CapabilityService
from omniagent.config import (
    API_KEY_VARIABLES, BACKENDS, apply_model_preferences, apply_stored_api_keys, load_api_key,
    register_secret,
)
from omniagent.core import schedule
from omniagent.core.conversation import Exchange, trim_history
from omniagent.core.events import (
    AWAITING_APPROVAL_CODE, AWAITING_DIRECTION_CODE, AgentEvent, compact_count, provider_fallback_text, tool_label,
)
from omniagent.model_catalog import (
    ModelCatalogError, list_provider_models, load_model_preferences, save_model_preferences, valid_model_id,
)
from omniagent.platform.macos.host_lock import HostBusyError, async_host_task_lock_preempting, host_owner, host_task_lock
from omniagent.memory.personal import utc_now_iso
from omniagent.platform.macos import launch_agent
# Testler ve eski çağıranlar bu sabitleri telegram modülünden okur (tests/test_telegram_service.py).
from omniagent.platform.macos.launch_agent import (  # noqa: F401
    BOOTSTRAP_ATTEMPTS, BOOTSTRAP_RETRY_SECONDS, SERVICE_POLL_SECONDS, SERVICE_UNLOAD_TIMEOUT_SECONDS,
    LaunchAgentError,
)
from omniagent.platform.macos.permissions import accessibility_granted
from omniagent.platform.macos.power import start_keep_awake, stop_keep_awake
from omniagent.paths import (
    companion_db_file, imessage_settings_file, memory_learning_lock_file, project_root, resolve_output_path,
    schedules_file, telegram_settings_file,
)
from omniagent.tools.screen import screen_capture_granted, screen_session
from .maintenance import DoctorFacts, doctor_lines, head_commit, pull_updates, source_version, sync_dependencies
from .runtime import DeliveryFailed, IntegrationStopped, boolean_field, data_root, read_json, save_json
from .telegram_activity import (
    ActivityEntry, activity_html, append_entry, elapsed_label, mark_failed, render_entries,
)
from .transcription import TranscriptionFailed, TranscriptionUnavailable, transcribe_audio
from omniagent.integrations.imessage_settings import memory_backend_if_paired
from omniagent.memory import channels, learning
from omniagent.memory.personal import local_timezone
from omniagent.memory.profile import MemoryCommand, parse_memory_command
from omniagent.app.agent import RunOptions, RunReport, STATE_FILE, close_model_clients, create_model_clients, run_agent_with_callback
from omniagent.app.constants import RUN_MODE_PROFILES
from omniagent.app.model_retry import REMOTE_MODEL_RETRY_SECONDS
from omniagent.app.policy import screenshot_requested, source_change_expected
from omniagent.app.tool_schema import AUTO_OBSERVATION_PREVIEW, VERIFICATION_OBSERVATION_PREVIEW


TOKEN_SERVICE = "OmniAgent Telegram"
TOKEN_ACCOUNT = "bot_token"
PAGE_LIMIT = 3500
EDIT_INTERVAL = 1.1
POLL_SECONDS = 20
MAINTENANCE_FINISH_TIMEOUT_SECONDS = 5
# Zamanlanmış görevlerin denetim aralığı (dakika çözünürlüğü için yeterli)
SCHEDULER_TICK_SECONDS = 30
# Bot API getFile yalnız 20 MB'a kadar dosya indirir
DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024
UPLOAD_TIMEOUT = httpx.Timeout(300.0, connect=5.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(120.0, connect=5.0)
# Telegram getFile'ı zaten 20 MB üstü dosyalar için file_path vermiyor; bu, sunucunun
# bildirdiği boyuta güvenmeden aynı sınırı yerelde de zorlayan bağımsız bir ikinci kapıdır.
DOWNLOAD_MAX_BYTES: int = 20 * 1024 * 1024
# Görsel olarak modele verilecek belge türleri (svg/heic Pillow'da güvenilir açılmaz)
_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"})
# Mesaj alanı -> kullanıcıya/modele gösterilen ek türü
_ATTACHMENT_KINDS = (
    ("document", "belge"), ("voice", "sesli mesaj"), ("audio", "ses dosyası"),
    ("video", "video"), ("video_note", "görüntülü not"),
)
_DEFAULT_ATTACHMENT_REQUESTS = {
    "fotoğraf": "Gönderdiğim görseli incele ve ne gördüğünü kısaca anlat.",
}


class TelegramError(RuntimeError):
    """Bot API veya yapılandırma hatası; tokenı hata metnine taşımaz."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class RestartRequested(Exception):
    """Köprü yeni kodla yeniden başlamalı: run() temizliği yapar, main() aynı komutu execv eder."""


class TelegramSettings(TypedDict):
    chat_id: int
    user_id: int


class TelegramAttachment(TypedDict):
    """Mesajdaki tek dosya eki (Bot API alanlarından ayıklanmış)."""
    file_id: str
    kind: str
    name: Optional[str]
    size: int
    image: bool


def settings_path() -> Path:
    return telegram_settings_file()


def offset_path() -> Path:
    return data_root() / "telegram-offset.json"


def history_path() -> Path:
    return data_root() / "telegram-history.json"


def inbox_path() -> Path:
    """Sohbetten gelen eklerin kaydedildiği yerel dizin."""
    return data_root() / "telegram-inbox"


def message_attachment(message: Dict[str, Any]) -> Optional[TelegramAttachment]:
    """
    Mesajdaki fotoğraf/belge/ses/video ekini ayıklar; fotoğrafın en büyük boyutu seçilir.
    Belge olarak gönderilen yaygın görseller de görsel sayılır (sıkıştırılmamış fotoğraf). Saf.
    """
    photos = message.get("photo")
    if isinstance(photos, list):
        sizes = [item for item in photos if isinstance(item, dict) and isinstance(item.get("file_id"), str)]
        if sizes:
            largest = max(sizes, key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0))
            return {"file_id": largest["file_id"], "kind": "fotoğraf", "name": None,
                    "size": int(largest.get("file_size") or 0), "image": True}
    for field, kind in _ATTACHMENT_KINDS:
        item = message.get(field)
        if isinstance(item, dict) and isinstance(item.get("file_id"), str):
            name = item.get("file_name")
            return {
                "file_id": item["file_id"], "kind": kind,
                "name": name if isinstance(name, str) and name.strip() else None,
                "size": int(item.get("file_size") or 0),
                "image": field == "document" and item.get("mime_type") in _IMAGE_MIME_TYPES,
            }
    return None


def safe_file_name(name: str) -> str:
    """Uzak dosya adını yerel, tek bileşenli ve güvenli bir ada çevirir. Saf."""
    base = Path(name.replace("\\", "/")).name
    cleaned = re.sub(r"[^\w.\- ]+", "_", base).strip(" .")
    return cleaned[:120] or "ek"


def attachment_goal(caption: str, attachment: TelegramAttachment, path: Path) -> str:
    """Ek mesajını görev metnine çevirir: açıklama görevdir, yoksa türüne uygun varsayılan istek. Saf."""
    request = caption.strip() or _DEFAULT_ATTACHMENT_REQUESTS.get(
        attachment["kind"], "Gönderdiğim dosyayı incele ve içeriğini kısaca özetle.",
    )
    size_kb = max(1, round(path.stat().st_size / 1024)) if path.exists() else 0
    return f"{request}\n\n[Telegram eki ({attachment['kind']}, {size_kb} KB) kaydedildi: {path}]"


def load_settings() -> TelegramSettings:
    """Yetkili özel sohbet ve kullanıcı kimliğini doğrular."""
    value = read_json(settings_path(), {})
    if not isinstance(value, dict):
        raise TelegramError("Telegram yapılandırması geçersiz; setup komutunu çalıştırın.")
    chat_id, user_id = value.get("chat_id"), value.get("user_id")
    if not isinstance(chat_id, int) or not isinstance(user_id, int) or chat_id <= 0 or user_id <= 0:
        raise TelegramError("Telegram eşleştirmesi eksik; setup komutunu çalıştırın.")
    return {"chat_id": chat_id, "user_id": user_id}


def load_token() -> str:
    """Bot tokenını yalnız macOS Keychain'den alır; redact() kapsamına girmesi için kaydeder."""
    token = Keyring().get_password(TOKEN_SERVICE, TOKEN_ACCOUNT)
    if not token:
        raise TelegramError("Telegram bot tokenı Keychain'de yok; setup komutunu çalıştırın.")
    register_secret("telegram_bot_token", token)
    return token


# iMessage köprüsü servisi: /update ve /restart onu da yeniden başlatır (iki köprü aynı kod ve companion.db şemasıyla).
IMESSAGE_SERVICE_LABEL: str = "com.omniagent.imessage"


def restart_companion_service() -> Optional[str]:
    """
    Kuruluysa iMessage köprüsü servisini yeni kodla yeniden başlatır; sonucu kullanıcıya gidecek not olarak döner (kurulu
    değilse None). Başarısızlık Telegram'ın yeniden başlamasını engellemez; not ve yapılandırılmış uyarı olarak görünür.
    """
    if not launch_agent.plist_path(IMESSAGE_SERVICE_LABEL).exists():
        return None
    result = launch_agent.launchctl(["kickstart", "-k", f"gui/{os.getuid()}/{IMESSAGE_SERVICE_LABEL}"])
    if result.returncode == 0:
        return "iMessage servisi de yeni kodla yeniden başlatıldı."
    logging.warning("iMessage servisi yeniden başlatılamadı",
                    extra={"returncode": result.returncode, "stderr": str(result.stderr)[:200]})
    return (f"iMessage servisi yeniden başlatılamadı (launchctl {result.returncode}); elle: "
            f"launchctl kickstart -k gui/$(id -u)/{IMESSAGE_SERVICE_LABEL}")


def forwarded(message: Dict[str, Any]) -> bool:
    """İletilen (forward) mesaj mı? İçerik başkasının sözüdür; kullanıcının kanıtı sayılmaz. Saf."""
    return "forward_origin" in message or "forward_date" in message


def learning_backend() -> Optional[str]:
    """
    Kanıtlı hafıza öğrenme modeli: iMessage kurulumundaki memory_backend. iMessage kurulu değilse None döner ve öğrenme
    kapalıdır (açık durum, varsayılan model yok); kullanıcı sözleri yine de kaydedilir.
    """
    return memory_backend_if_paired(imessage_settings_file(), BACKENDS.keys())

def authorized(message: Dict[str, Any], settings: TelegramSettings) -> bool:
    """Yalnız eşleştirilmiş kişinin özel sohbetindeki mesajları kabul eder."""
    chat = message.get("chat")
    sender = message.get("from")
    return (
        isinstance(chat, dict) and isinstance(sender, dict)
        and chat.get("type") == "private"
        and chat.get("id") == settings["chat_id"]
        and sender.get("id") == settings["user_id"]
    )


# Satır içi buton satırları: her buton (etiket, callback verisi); Telegram callback verisini 64 baytla sınırlar.
ButtonRows = List[List[Tuple[str, str]]]
# Kullanıcıyı bekleyen sürekli oturum istemlerinin butonları (etiket, eylem); veri "ctl:<görev belirteci>:<eylem>".
CONTROL_PROMPT_BUTTONS: Dict[str, List[Tuple[str, str]]] = {
    AWAITING_APPROVAL_CODE: [("✅ Onayla", "approve"), ("⏹ Durdur", "stop")],
    AWAITING_DIRECTION_CODE: [("⏹ Durdur", "stop")],
}
# "/" menüsü: yalnız argümansız çalışan komutlar; argümanlı /btw, /verbose ve /unschedule /help metnindedir.
BOT_COMMANDS: List[Tuple[str, str]] = [
    ("provider", "Sağlayıcı ve model seç"),
    ("mode", "Sonraki görevin çalışma modunu seç"),
    ("new", "Yeni oturum: sohbet geçmişini temizle"),
    ("stop", "Çalışan görevi durdur"),
    ("status", "Görev, süre, model ve token durumu"),
    ("approve", "Sürekli oturumun hedefini onayla"),
    ("schedules", "Planlı görevleri listele"),
    ("doctor", "Sürüm ve izinleri göster"),
    ("help", "Komutlar ve kullanım"),
]
# Model seçicisinde sayfa başına model butonu.
MODEL_PAGE_SIZE: int = 8
# İş günlüğü iletisinin HTML üst sınırı (Telegram 4096'da keser) ve en sık düzenleme aralığı (hız sınırı).
LOG_LIMIT: int = 3500
LOG_EDIT_SECONDS: float = 1.5


class QuestionReply(TypedDict):
    """Soru butonunun etiketi ve dokunulunca ajana dönen yanıt."""
    label: str
    answer: Dict[str, Any]


def question_replies(fields: Dict[str, Any], token: str) -> Dict[str, QuestionReply]:
    """
    Tek yanıt alanlı sorunun butonlarını callback verisiyle eşler: onay kutusu Onayla/Reddet, "choices" taşıyan
    metin alanı seçenek başına bir buton olur; diğer sorular butonsuzdur (yanıt yazılır). Veri soruya özgü
    belirteci taşır: eski sorunun butonu yenisini yanıtlayamaz. Saf.
    """
    if len(fields) != 1:
        return {}
    name, spec = next(iter(fields.items()))
    if boolean_field(spec):
        return {
            f"q:{token}:1": {"label": "✅ Onayla", "answer": {name: True}},
            f"q:{token}:0": {"label": "❌ Reddet", "answer": {name: False}},
        }
    choices: object = spec.get("choices") if isinstance(spec, dict) else None
    if not isinstance(choices, list):
        return {}
    return {
        f"q:{token}:{index}": {"label": str(choice)[:40], "answer": {name: str(choice)}}
        for index, choice in enumerate(choices)
    }


def control_buttons(code: str, token: str) -> ButtonRows:
    """Sürekli oturum isteminin butonları; veri istemi üreten görevin belirtecini taşır. Saf."""
    return [[(label, f"ctl:{token}:{action}") for label, action in CONTROL_PROMPT_BUTTONS[code]]]


def model_label(backend: Optional[str]) -> str:
    """Model profilinin okunur adı: 'Otomatik' ya da 'profil · model'. Saf."""
    return "Otomatik" if backend is None else f"{backend} · {BACKENDS[backend]['model']}"


def provider_buttons(selected: Optional[str], ready: frozenset[str]) -> ButtonRows:
    """Otomatik ve hazır sağlayıcı profillerinin seçici butonları (BACKENDS sırasıyla, seçili olan ✓). Saf."""
    options: List[Optional[str]] = [None, *(name for name in BACKENDS if name in ready)]
    return [[(("✓ " if name == selected else "") + model_label(name), f"prov:{name or 'auto'}")]
            for name in options]


class ModelPicker(TypedDict):
    """Açık model seçicisi: profil, sayfalı model listesi ve eski butonları geçersiz kılan belirteç."""
    token: str
    profile: str
    models: List[str]


def model_page_buttons(picker: ModelPicker, page: int, current: str) -> ButtonRows:
    """Model listesinin bir sayfası (seçili olan ✓) ve gerekiyorsa ◀ ▶ gezinme satırı. Saf."""
    start: int = page * MODEL_PAGE_SIZE
    rows: ButtonRows = [
        [(("✓ " if model == current else "") + model[:60], f"pm:{picker['token']}:{index}")]
        for index, model in enumerate(picker["models"][start:start + MODEL_PAGE_SIZE], start)
    ]
    navigation: List[Tuple[str, str]] = []
    if page > 0:
        navigation.append(("◀ Önceki", f"page:{picker['token']}:{page - 1}"))
    if start + MODEL_PAGE_SIZE < len(picker["models"]):
        navigation.append(("▶ Sonraki", f"page:{picker['token']}:{page + 1}"))
    return rows + ([navigation] if navigation else [])


def inline_keyboard(buttons: ButtonRows) -> Dict[str, List[List[Dict[str, str]]]]:
    """Buton satırlarını Bot API satır içi klavye nesnesine çevirir. Saf."""
    return {"inline_keyboard": [
        [{"text": label, "callback_data": data} for label, data in row] for row in buttons
    ]}


def mode_buttons(selected: str) -> ButtonRows:
    """Çalışma modu seçici butonları (seçili olan ✓). Saf."""
    return [[(("✓ " if key == selected else "") + profile["label"], f"mode:{key}")]
            for key, profile in RUN_MODE_PROFILES.items()]


_INTEGRATION_STATUS_OVERRIDES: Dict[str, str] = {"waiting_user": "Yanıtınız bekleniyor"}


def integration_status_label(stage: str, text: str, completed: int, total: int) -> str:
    """
    İç durum kodunu (`stage`) kullanıcıya okunur etikete çevirir; arayüz zaten yalnız `text`
    gösterir ve doğru çalışır, Telegram eskiden ham `stage` kodunu ("waiting_user" gibi)
    basıyordu. Çoğu aşamada `text` kendi başına açıklayıcıdır. Yalnız 'waiting_user' özel:
    oradaki `text` sorunun kendisidir ve ask_user akışıyla zaten ayrıca gösterilir, geçici
    "düşünüyor" balonunda tekrarlanmaz. Saf.
    """
    base = _INTEGRATION_STATUS_OVERRIDES.get(stage, text)
    progress = f" {completed}/{total}" if total else ""
    return base + progress


def event_text(event: AgentEvent) -> str:
    """Tipli ajan olayunu kısa, okunur Telegram metnine dönüştürür."""
    kind = event["kind"]
    if kind == "run_started":
        return f"▶ {event['goal'][:500]}\nModel: {event['model']} · {event['backend']}\n"
    if kind == "turn_started":
        return f"\n↻ Tur {event['turn']}/{event['max_turns']} · {event['backend']}\n"
    if kind in ("text_delta", "reasoning_delta"):
        return event["text"]
    if kind == "tool_started":
        return f"\n⏺ {tool_label(event['name'])} {event['preview'][:250]}\n"
    if kind == "tool_output":
        return f"  {event['text'][:500]}"
    if kind == "tool_finished":
        mark = "✓" if event["ok"] else "✗"
        return f"\n{mark} {event['seconds']:.1f} sn · {event['text'][:800]}\n"
    if kind == "backend_changed":
        return f"\n↻ Model değişti: {event['backend']} · {event['reason'][:220]}\n"
    if event["kind"] == "provider_fallback":
        return f"\n⚠ {provider_fallback_text(event)}\n"
    if kind == "notice":
        return f"\n! {event['text'][:500]}\n"
    if kind == "integration_status":
        label = integration_status_label(
            event["stage"], event["text"][:300], event["completed"], event["total"]
        )
        return f"\n◦ {label}\n"
    if kind == "stream_reset":
        return f"\n↺ Akış sıfırlandı: {event['reason'][:200]}\n"
    if kind == "run_finished":
        metrics = event["metrics"]
        mark = "✓" if event["success"] else "✗"
        return (
            f"\n{mark} {event['outcome'][:1200]}\n"
            f"{metrics['elapsed_seconds']:.1f} sn · {metrics['turns']} tur · "
            f"{metrics['tool_calls']} araç · {metrics['backend']}\n"
            f"Token: giriş {metrics['prompt_tokens']}, önbellek {metrics['cached_tokens']}, "
            f"çıkış {metrics['completion_tokens']}\n"
        )
    return ""


class TelegramAPI:
    """Uzun yoklama ve mesaj düzenleme için dar Bot API bağlayıcısı."""

    def __init__(self, token: str, client: Optional[httpx.AsyncClient] = None) -> None:
        self.token = token
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(35.0, connect=5.0), trust_env=False,
        )
        self.owns_client = client is None

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()

    async def call(self, method: str, payload: Dict[str, Any]) -> Any:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for attempt in range(2):
            try:
                response = await self.client.post(url, json=payload)
                body = response.json()
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                if attempt == 0:
                    await asyncio.sleep(0.25)
                    continue
                raise TelegramError(f"{method}: ağ veya yanıt hatası ({type(error).__name__}).") from None
            except (httpx.HTTPError, ValueError) as error:
                raise TelegramError(f"{method}: ağ veya yanıt hatası ({type(error).__name__}).") from None
            if not isinstance(body, dict):
                raise TelegramError(f"{method}: geçersiz Bot API yanıtı.")
            if response.status_code == 429 and attempt == 0:
                parameters = body.get("parameters", {})
                seconds = parameters.get("retry_after", 1) if isinstance(parameters, dict) else 1
                try:
                    delay = min(60, max(0, int(seconds)))
                except (TypeError, ValueError):
                    delay = 1
                await asyncio.sleep(delay)
                continue
            if response.status_code != 200 or not body.get("ok"):
                description = str(body.get("description", "istek başarısız"))[:200]
                raise TelegramError(f"{method}: HTTP {response.status_code}: {description}", response.status_code)
            return body.get("result")
        raise TelegramError(f"{method}: hız sınırı devam ediyor.")

    async def send(self, chat_id: int, text: str) -> int:
        return await self._send_message({"chat_id": chat_id, "text": text[:PAGE_LIMIT]})

    async def send_buttons(self, chat_id: int, text: str, buttons: ButtonRows) -> int:
        """Metni satır içi butonlarla gönderir; dokunuş callback_query güncellemesi olarak döner."""
        return await self._send_message({
            "chat_id": chat_id, "text": text[:PAGE_LIMIT], "reply_markup": inline_keyboard(buttons),
        })

    async def edit_buttons(self, chat_id: int, message_id: int, text: str, buttons: ButtonRows) -> None:
        """İleti metnini ve butonlarını birlikte günceller (çok adımlı seçici)."""
        await self.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text[:PAGE_LIMIT],
            "reply_markup": inline_keyboard(buttons),
        })

    async def _send_message(self, payload: Dict[str, Any]) -> int:
        result = await self.call("sendMessage", payload)
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramError("sendMessage: ileti kimliği eksik.")
        return result["message_id"]

    async def answer_callback(self, callback_id: str, text: str) -> None:
        """Buton dokunuşunu kapatır (istemcideki bekleme göstergesi durur); metin kısa açılır bildirim olur."""
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    async def set_commands(self, commands: List[Tuple[str, str]]) -> None:
        """Sohbette '/' yazınca açılan komut menüsünü kaydeder."""
        await self.call("setMyCommands", {
            "commands": [{"command": name, "description": description} for name, description in commands],
        })

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        await self.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text[:PAGE_LIMIT],
        })

    async def send_html(self, chat_id: int, content: str) -> int:
        """Eski Bot API için güvenli HTML biçimli ileti gönderir."""
        result = await self.call("sendMessage", {
            "chat_id": chat_id, "text": content, "parse_mode": "HTML",
        })
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramError("sendMessage: ileti kimliği eksik.")
        return result["message_id"]

    async def edit_html(self, chat_id: int, message_id: int, content: str) -> None:
        await self.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id,
            "text": content, "parse_mode": "HTML",
        })

    async def send_draft(self, chat_id: int, draft_id: int, rich_message: Dict[str, str]) -> None:
        """Aynı taslak kimliğini güncelleyerek yerel akış animasyonunu sürdürür."""
        await self.call("sendRichMessageDraft", {
            "chat_id": chat_id, "draft_id": draft_id, "rich_message": rich_message,
        })

    async def send_photo(self, chat_id: int, path: Path) -> None:
        """Gerçek ekran görüntüsünü eşleştirilmiş sohbete dosya olarak iletir."""
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        try:
            with path.open("rb") as source:
                response = await self.client.post(
                    f"https://api.telegram.org/bot{self.token}/sendPhoto",
                    data={"chat_id": str(chat_id)},
                    files={"photo": (path.name, source, mime)},
                )
                body = response.json()
        except (OSError, httpx.HTTPError, ValueError) as error:
            raise TelegramError(f"sendPhoto: {type(error).__name__}.") from None
        if response.status_code != 200 or not isinstance(body, dict) or not body.get("ok"):
            raise TelegramError(f"sendPhoto: HTTP {response.status_code}.", response.status_code)

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> None:
        """Dosyayı özgün adıyla ve sıkıştırılmadan sohbete gönderir."""
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1000]
        try:
            with path.open("rb") as source:
                response = await self.client.post(
                    f"https://api.telegram.org/bot{self.token}/sendDocument",
                    data=data,
                    files={"document": (path.name, source, "application/octet-stream")},
                    timeout=UPLOAD_TIMEOUT,
                )
                body = response.json()
        except (OSError, httpx.HTTPError, ValueError) as error:
            raise TelegramError(f"sendDocument: {type(error).__name__}.") from None
        if response.status_code != 200 or not isinstance(body, dict) or not body.get("ok"):
            description = str(body.get("description", ""))[:200] if isinstance(body, dict) else ""
            raise TelegramError(f"sendDocument: HTTP {response.status_code}: {description}", response.status_code)

    async def download(self, file_id: str, directory: Path, preferred_name: Optional[str]) -> Path:
        """
        getFile ile sohbet ekini indirir ve yalnız kullanıcıya açık dosya olarak kaydeder. Ad,
        zaman damgası önekiyle çakışmasız yapılır; token hata metnine taşınmaz. Gövde parça
        parça okunup DOWNLOAD_MAX_BYTES ile sınırlanır; sunucunun bildirdiği boyuta güvenilmez,
        indirme sırasında bellek/diskte sınırsız birikme engellenir.
        """
        info = await self.call("getFile", {"file_id": file_id})
        remote = info.get("file_path") if isinstance(info, dict) else None
        if not isinstance(remote, str) or not remote:
            raise TelegramError("getFile: dosya yolu yok (dosya 20 MB sınırını aşmış olabilir).")
        directory.mkdir(parents=True, exist_ok=True)
        name = safe_file_name(preferred_name or Path(remote).name)
        target = directory / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}-{name}"
        written = 0
        try:
            async with self.client.stream(
                "GET", f"https://api.telegram.org/file/bot{self.token}/{remote}", timeout=DOWNLOAD_TIMEOUT,
            ) as response:
                if response.status_code != 200:
                    raise TelegramError(f"dosya indirme: HTTP {response.status_code}.", response.status_code)
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as sink:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > DOWNLOAD_MAX_BYTES:
                            raise TelegramError(
                                f"dosya indirme: {DOWNLOAD_MAX_BYTES // (1024 * 1024)} MB sınırını aştı."
                            )
                        sink.write(chunk)
        except httpx.HTTPError as error:
            target.unlink(missing_ok=True)
            raise TelegramError(f"dosya indirme: {type(error).__name__}.") from None
        except TelegramError:
            target.unlink(missing_ok=True)
            raise
        return target


class TelegramStream:
    """Ajan olaylarını saniyede en çok bir düzenlemeyle sayfalı canlı metne çevirir."""

    def __init__(self, api: TelegramAPI, chat_id: int) -> None:
        self.api = api
        self.chat_id = chat_id
        self.page = ""
        self.message_id: Optional[int] = None
        self.sent = ""
        self.last_edit = 0.0

    async def append(self, text: str) -> None:
        remaining = text
        while remaining:
            space = PAGE_LIMIT - len(self.page)
            if space == 0:
                await self.flush(force=True)
                self.page = ""
                self.message_id = None
                self.sent = ""
                space = PAGE_LIMIT
            piece, remaining = remaining[:space], remaining[space:]
            self.page += piece
            if remaining:
                await self.flush(force=True)
        await self.flush()

    async def show(self, text: str, force: bool = False) -> None:
        """Kompakt görünümde aynı mesajı yeniler; uzun finali sayfalara böler."""
        self.page = text[:PAGE_LIMIT]
        await self.flush(force=force)
        if force and len(text) > PAGE_LIMIT:
            remaining = text[PAGE_LIMIT:]
            self.page = ""
            self.message_id = None
            self.sent = ""
            await self.append(remaining)

    async def flush(self, force: bool = False) -> None:
        if not self.page or self.page == self.sent:
            return
        now = time.monotonic()
        if self.message_id is not None and not force and now - self.last_edit < EDIT_INTERVAL:
            return
        if self.message_id is None:
            self.message_id = await self.api.send(self.chat_id, self.page)
        else:
            await self.api.edit(self.chat_id, self.message_id, self.page)
        self.sent = self.page
        self.last_edit = time.monotonic()


def _legacy_markdown_html(markdown: str) -> str:
    """Yaygın Markdown'ı eski Telegram HTML biçimine güvenli biçimde çevirir."""
    tokens: list[str] = []

    def inline(raw: str) -> str:
        raw = raw.replace("\x00", "�")

        def reserve(value: str) -> str:
            key = f"OMNITOKEN{len(tokens)}END"
            tokens.append(value)
            return key

        raw = re.sub(
            r"`([^`\n]+)`",
            lambda match: reserve("<code>" + html.escape(match.group(1)) + "</code>"),
            raw,
        )
        raw = re.sub(
            r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
            lambda match: reserve(
                '<a href="' + html.escape(match.group(2), quote=True) + '">'
                + html.escape(match.group(1)) + "</a>"
            ),
            raw,
        )
        escaped = html.escape(raw)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"~~(.+?)~~", r"<s>\1</s>", escaped)
        escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
        for index, value in enumerate(tokens):
            escaped = escaped.replace(f"OMNITOKEN{index}END", value)
        tokens.clear()
        return escaped

    result: list[str] = []
    code_lines: list[str] = []
    fenced = False
    for line in markdown.split("\n"):
        if line.lstrip().startswith("```"):
            if fenced:
                result.append("<pre>" + html.escape("\n".join(code_lines)) + "</pre>")
                code_lines = []
                fenced = False
            else:
                fenced = True
            continue
        if fenced:
            code_lines.append(line)
            continue
        if not line.strip():
            continue
        stripped = line.lstrip()
        if re.match(r"^#{1,6}\s+", stripped):
            line = re.sub(r"^#{1,6}\s+", "", stripped)
            result.append("<b>" + inline(line) + "</b>")
        elif re.match(r"^[-*+]\s+", stripped):
            line = re.sub(r"^[-*+]\s+", "", stripped)
            result.append("• " + inline(line))
        else:
            result.append(inline(line))
    if fenced:
        result.append("<pre>" + html.escape("\n".join(code_lines)) + "</pre>")
    return "\n".join(result)


def _markdown_pages(text: str) -> list[str]:
    """Yanıtı Telegram ileti sınırını aşmadan satır başlarından böler."""
    pages: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= PAGE_LIMIT:
            pages.append(remaining)
            break
        cut = remaining.rfind("\n", 0, PAGE_LIMIT + 1)
        if cut < PAGE_LIMIT // 2:
            cut = PAGE_LIMIT
        pages.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return pages or ["Yanıt yok."]


class TelegramDraftStream:
    """Geçici zengin taslağı akıtır; finali sıkı aralıklı biçimli mesaj yapar."""

    def __init__(self, api: TelegramAPI, chat_id: int, fallback: TelegramStream) -> None:
        self.api = api
        self.chat_id = chat_id
        self.fallback = fallback
        self.draft_id = secrets.randbelow(2**31 - 1) + 1
        self.native = True
        self.finished = False
        self.visible = ""
        self.rich_message: Dict[str, str] = {"html": "<tg-thinking>Düşünüyor…</tg-thinking>"}
        self.sent = ""
        self.last_draft = 0.0
        self.last_draft_error_log = 0.0

    async def show(
        self, text: str, *, rich_message: Optional[Dict[str, str]] = None,
        force: bool = False, immediate: bool = False,
    ) -> None:
        if force:
            await self.finish(text)
            return
        if self.finished:
            return
        self.visible = text[:PAGE_LIMIT]
        self.rich_message = rich_message or {"markdown": self.visible}
        await self.tick(immediate=immediate)

    async def tick(self, immediate: bool = False) -> None:
        if self.finished:
            return
        if not self.native:
            await self.fallback.flush()
            return
        now = time.monotonic()
        if self.sent == self.visible and now - self.last_draft < 4.0:
            return
        if not immediate and self.sent != self.visible and now - self.last_draft < EDIT_INTERVAL:
            return
        try:
            await self.api.send_draft(self.chat_id, self.draft_id, self.rich_message)
        except TelegramError as error:
            # Yarım Markdown (örn. kapanmamış ** veya bağlantı) taslağı reddedilebilir.
            if error.status == 400 and "markdown" in self.rich_message:
                plain = {"html": html.escape(self.visible)}
                try:
                    await self.api.send_draft(self.chat_id, self.draft_id, plain)
                except TelegramError as plain_error:
                    error = plain_error
                else:
                    self.sent = self.visible
                    self.last_draft = time.monotonic()
                    return
            if error.status is None or error.status == 429 or (
                error.status is not None and error.status >= 500
            ):
                # Geçici taslak hatası gerçek ajan işini iptal etmez; sonraki tick yeniden dener.
                self.last_draft = time.monotonic()
                if self.last_draft - self.last_draft_error_log >= 30.0:
                    logging.warning("Telegram taslak akışı geçici olarak erişilemiyor: %s", error)
                    self.last_draft_error_log = self.last_draft
                return
            if error.status not in (400, 404):
                raise error
            self.native = False
            await self.fallback.show(self.visible or "⏳ Düşünüyor…")
            return
        self.sent = self.visible
        self.last_draft = time.monotonic()

    async def finish(self, answer: str) -> None:
        if self.finished:
            return
        self.finished = True
        for page in _markdown_pages(answer.strip()):
            formatted = _legacy_markdown_html(page)
            try:
                if self.fallback.message_id is None:
                    self.fallback.message_id = await self.api.send_html(self.chat_id, formatted)
                else:
                    await self.api.edit_html(self.chat_id, self.fallback.message_id, formatted)
            except TelegramError as error:
                if error.status not in (400, 404):
                    raise
                await self.fallback.show(page, force=True)
            self.fallback.page = page
            self.fallback.sent = page
            self.fallback.message_id = None


def _compact_tool_status(name: str, preview: str, output: str, elapsed: str) -> tuple[str, Dict[str, str]]:
    """Araç olayunu kısa canlı durum ve güvenli zengin içeriğe çevirir."""
    labels = {
        "web_search": "Web’de arıyor…",
        "browse_url": "Sayfayı açıyor…",
        "fetch_raw": "Web içeriğini okuyor…",
        "execute_shell": "Komut çalıştırıyor…",
        "execute_js": "Kod çalıştırıyor…",
        "read_file": "Dosya okuyor…",
        "write_file": "Dosyayı yazıyor…",
    }
    label = labels.get(name, f"{tool_label(name)} çalışıyor…") + f" · {elapsed}"
    detail = preview.strip()[:240]
    tail = output.strip()[-300:]
    visible = "⏳ " + label
    if detail:
        visible += "\n" + detail
    if tail:
        visible += "\n" + tail
    rich = "<tg-thinking>" + html.escape(label) + "</tg-thinking>"
    if detail:
        rich += "\n<pre>" + html.escape(detail) + "</pre>"
    if tail:
        rich += "\n<pre>" + html.escape(tail) + "</pre>"
    return visible, {"html": rich}


def message_is_not_modified(error: TelegramError) -> bool:
    """editMessageText'in "içerik zaten aynı" yanıtı mı (400 'message is not modified')? Saf."""
    return error.status == 400 and "message is not modified" in str(error)


class ActivityLog:
    """
    Görevin canlı iş günlüğü: her araç adımı bir satır olarak tek iletide yerinde güncellenir. Düzenleme en sık
    LOG_EDIT_SECONDS'ta bir yapılır; ileti LOG_LIMIT'i aşacaksa son hâliyle bırakılır ve yeni adımlar yeni iletide
    sürer. Gösterim hatası ajan işini asla iptal etmez: geçici hata (ağ, hız sınırı, 5xx) sonraki düzenlemede yeniden
    denenir; "message is not modified" başarıdır (içerik zaten görünüyor); kalıcı 4xx'te (ör. günlük iletisi silinmiş)
    yeni ileti açılır, yeni ileti de reddedilirse günlük bu koşu için kapanır.
    """

    def __init__(self, api: TelegramAPI, chat_id: int) -> None:
        self.api = api
        self.chat_id = chat_id
        self.entries: List[ActivityEntry] = []
        self.message_id: Optional[int] = None
        self.shown = ""
        self.last_edit = 0.0
        self.closed = False

    async def started(self, call_id: str, name: str, preview: str) -> None:
        """Başlayan adımı günlüğe ekler; ileti dolacaksa önceki iletiyi son hâliyle bırakıp yenisini açar."""
        line: str = activity_html(name, preview)
        candidate: List[ActivityEntry] = append_entry(self.entries, call_id, line)
        if self.entries and len(render_entries(candidate)) > LOG_LIMIT:
            await self.flush()
            self.entries, self.message_id, self.shown = [], None, ""
            candidate = append_entry([], call_id, line)
        self.entries = candidate
        await self.tick()

    async def finished(self, call_id: str, ok: bool) -> None:
        """Başarısız biten adımın satırını ⚠️ ile işaretler."""
        if not ok:
            self.entries = mark_failed(self.entries, call_id)
            await self.tick()

    async def tick(self) -> None:
        """Bekleyen değişikliği düzenleme aralığı dolduysa gönderir."""
        if time.monotonic() - self.last_edit >= LOG_EDIT_SECONDS:
            await self.flush()

    async def flush(self) -> None:
        """Bekleyen değişikliği hemen gönderir; Telegram hatası yükseltilmez (bkz. sınıf açıklaması)."""
        content: str = render_entries(self.entries)
        if self.closed or not content or content == self.shown:
            return
        try:
            if self.message_id is None:
                self.message_id = await self.api.send_html(self.chat_id, content)
            else:
                await self.api.edit_html(self.chat_id, self.message_id, content)
        except TelegramError as error:
            if not message_is_not_modified(error):
                self._absorb_failure(error)
                return
            # "message is not modified": içerik zaten görünüyor, gönderilmiş sayılır.
        self.shown = content
        self.last_edit = time.monotonic()

    def _absorb_failure(self, error: TelegramError) -> None:
        """
        Gösterim hatasını soğurur ve uyarır: geçici hata sonraki düzenlemede yeniden denenir; iletiyi düzenleyemezsek
        (silinmiş olabilir) sonraki gönderimde tam günlük yeni iletide açılır; yeni ileti de reddedilirse günlük kapanır.
        """
        self.last_edit = time.monotonic()
        fields: Dict[str, object] = {"status": error.status, "error": str(error)[:200]}
        if error.status is None or error.status == 429 or error.status >= 500:
            logging.warning("İş günlüğü güncellenemedi; sonraki düzenlemede yeniden denenecek", extra=fields)
        elif self.message_id is not None:
            logging.warning("İş günlüğü iletisi düzenlenemedi; yeni ileti açılacak", extra=fields)
            self.message_id, self.shown = None, ""
        else:
            logging.warning("İş günlüğü açılamadı; bu koşu için günlük kapatıldı", extra=fields)
            self.closed = True


class CompactPresenter:
    """Kısa görünümde canlı iş günlüğünü, süreli durum taslağını ve biçimli son yanıtı yönetir."""

    def __init__(self, stream: TelegramDraftStream) -> None:
        self.stream = stream
        self.started_at = time.monotonic()
        self.log = ActivityLog(stream.api, stream.chat_id)
        self.turn_text = ""
        self.hide_turn = False
        self.finished = False
        self.active_tool: Optional[tuple[str, str, str, str]] = None
        self.last_status_stage: Optional[str] = None
        self.announced_fallbacks: set[tuple[str, bool]] = set()

    async def event(self, event: AgentEvent) -> None:
        kind = event["kind"]
        if kind in ("run_started", "turn_started", "stream_reset"):
            self.turn_text = ""
            self.hide_turn = False
            self.active_tool = None
            await self.stream.show(
                "⏳ Düşünüyor…",
                rich_message={"html": "<tg-thinking>Düşünüyor…</tg-thinking>"},
                immediate=True,
            )
        elif kind == "text_delta" and not self.hide_turn:
            self.turn_text += event["text"]
            visible = self.turn_text.lstrip()
            if "STATE:".startswith(visible.upper()):
                return
            if visible.upper().startswith("STATE:"):
                self.hide_turn = True
                return
            await self.stream.show(self.turn_text)
        elif kind in ("tool_call_preview", "tool_started"):
            self.hide_turn = True
            name = event["name"]
            preview = event["preview"]
            call_id = event.get("call_id", "") if kind == "tool_started" else ""
            self.active_tool = (call_id, name, preview, "")
            visible, rich = _compact_tool_status(name, preview, "", self._elapsed())
            await self.stream.show(
                visible, rich_message=rich, immediate=kind == "tool_started",
            )
            if kind == "tool_started":
                await self.log.started(call_id, name, preview)
        elif kind == "tool_output" and self.active_tool is not None:
            call_id, name, preview, output = self.active_tool
            if event["call_id"] != call_id:
                return
            output = (output + event["text"])[-300:]
            self.active_tool = (call_id, name, preview, output)
            visible, rich = _compact_tool_status(name, preview, output, self._elapsed())
            await self.stream.show(visible, rich_message=rich)
        elif kind == "tool_finished":
            await self.log.finished(event["call_id"], event["ok"])
            if self.active_tool is not None and event["call_id"] == self.active_tool[0]:
                self.active_tool = None
                label = "Tamamlandı" if event["ok"] else "Araç başarısız"
                await self.stream.show(
                    ("✓ " if event["ok"] else "⚠️ ") + label + " · düşünüyor…",
                    rich_message={"html": "<tg-thinking>" + label + "</tg-thinking>"},
                )
        elif event["kind"] == "provider_fallback":
            # Gizlilik olayı geçici taslakta kaybolmasın: (hedef sağlayıcı, ekran görüntülü mü) başına görev
            # başına bir kalıcı ileti; metinden ekran görüntüsüne geçiş yeni ileti üretir.
            # Gönderim hatası ajan görevini iptal etmesin diye yakalanıp loglanır (kayıt audit.jsonl'dadır).
            announcement: tuple[str, bool] = (event["to_backend"], event["image_count"] > 0)
            if announcement not in self.announced_fallbacks:
                try:
                    await self.stream.api.send(self.stream.chat_id, event_text(event).strip())
                except TelegramError as error:
                    # Başarısız bildirim yeniden DENENMEZ: ajan her çifti görev boyunca bir kez yayınlar
                    # (runtime.announced_fallbacks), aynı çift için ikinci olay gelmez; yani kullanıcı bu geçişi
                    # Telegram'da görmeyebilir. Yalnızca loglanır; kalıcı kayıt audit.jsonl'dadır. Çift ancak gönderim
                    # başarılıysa bu sunumcunun kümesine girer (olası yinelenen olay o durumda bir kez daha denenir).
                    logging.warning(
                        "Yedek sağlayıcı bildirimi gönderilemedi",
                        extra={"to_backend": event["to_backend"], "error": str(error)[:200]},
                    )
                else:
                    self.announced_fallbacks.add(announcement)
        elif kind == "integration_status":
            stage = event["stage"]
            label = integration_status_label(
                stage, event["text"], event["completed"], event["total"]
            )
            # Aşama gerçekten değiştiyse (ör. waiting_user -> resumed) hemen gösterilir; aksi
            # halde art arda gelen aynı aşamalı ilerleme güncellemeleri (ör. "3/10 ileti
            # taşındı") normal düzenleme aralığıyla kısılır. Hızlı ardışık geçişte "resumed"
            # eskiden bu kısıtlamaya takılıp taslakta hâlâ önceki aşama görünüyordu.
            await self.stream.show(
                "⏳ " + label,
                rich_message={"html": "<tg-thinking>" + html.escape(label) + "</tg-thinking>"},
                immediate=stage != self.last_status_stage,
            )
            self.last_status_stage = stage
        elif event["kind"] == "notice" and event.get("code") in CONTROL_PROMPT_BUTTONS:
            # İstemin kendisini köprü butonlu kalıcı ileti olarak gönderir; taslak eskiden "✓ Tamamlandı ·
            # düşünüyor…" hâlinde takılı kalıp oturumun kullanıcıyı beklediğini gizliyordu.
            await self.stream.show(
                "⏸ Yanıtınız bekleniyor",
                rich_message={"html": "<tg-thinking>Yanıtınız bekleniyor</tg-thinking>"},
                immediate=True,
            )
        elif kind == "run_finished":
            self.finished = True
            await self.log.flush()
            answer = str(event["outcome"]).strip() or str(event["reason"]).strip() or "Yanıt yok."
            await self.stream.finish(answer if event["success"] else f"⚠️ {answer}")

    def _elapsed(self) -> str:
        return elapsed_label(time.monotonic() - self.started_at)

    async def tick(self) -> None:
        """Etkin aracın süresini tazeler ve bekleyen günlük düzenlemesini gönderir."""
        if self.active_tool is not None:
            _, name, preview, output = self.active_tool
            visible, rich = _compact_tool_status(name, preview, output, self._elapsed())
            await self.stream.show(visible, rich_message=rich)
        else:
            await self.stream.tick()
        await self.log.tick()

    async def finish(self, report: RunReport) -> None:
        await self.log.flush()
        if not self.finished:
            answer = str(report["outcome"]).strip() or str(report.get("reason", "")).strip() or "Yanıt yok."
            await self.stream.finish(answer if report["success"] else f"⚠️ {answer}")


class TelegramBridge:
    """Tek özel sohbetten görev başlatır, soruları yanıtlar ve Esc eşdeğeri durdurur."""

    def __init__(self, api: TelegramAPI, settings: TelegramSettings) -> None:
        self.api = api
        self.settings = settings
        saved = read_json(offset_path(), {"offset": 0})
        self.offset = int(saved.get("offset", 0)) if isinstance(saved, dict) else 0
        self.clients: Dict[str, AsyncOpenAI] = {}
        self.integrations: Optional[CapabilityService] = None
        loaded_history = read_json(history_path(), [])
        safe_history = [
            entry for entry in loaded_history
            if isinstance(entry, dict) and isinstance(entry.get("goal"), str)
            and isinstance(entry.get("answer"), str) and isinstance(entry.get("tools"), list)
            and all(isinstance(tool, str) for tool in entry["tools"])
        ] if isinstance(loaded_history, list) else []
        self.history: list[Exchange] = trim_history(safe_history)
        self.active: Optional[asyncio.Task[None]] = None
        self.stop_event = threading.Event()
        self._stop_generation = 0
        self.pending_answer: Optional[asyncio.Future[Dict[str, Any]]] = None
        self.pending_fields: Dict[str, Any] = {}
        # Bekleyen sorunun butonları (callback verisi → yanıt); soru kapanınca boşalır, eski butonlar işlemez
        self.pending_replies: Dict[str, QuestionReply] = {}
        # Çalışan görevin belirteci: sürekli oturum butonları yalnız kendi görevini yönetir
        self.run_token = ""
        # Çalışan görevin /status özeti: başlangıç anı, son araç, gerçek model ve token toplamı
        self.run_started = 0.0
        self.run_tool = ""
        self.run_model = ""
        self.run_tokens = 0
        # Açık model seçicisi (/provider → sağlayıcı → model); yeni seçici eskisinin butonlarını geçersiz kılar
        self.model_picker: Optional[ModelPicker] = None
        self.backend: Optional[str] = None
        self.run_mode = "normal"
        self.active_run_mode = "normal"
        self.control_messages: Queue[str] = Queue()
        self.verbose = False
        self.goal = ""
        # Çalışan kodun commit'i (run() başında okunur); /update ve /doctor diskteki kodla karşılaştırır
        self.loaded_commit: Optional[str] = None
        # /update veya /restart sürerken zamanlayıcı görev başlatmaz
        self.maintenance = False
        # Köprü açıkken prizdeki Mac'in uyumasını engelleyen caffeinate süreci (run() yönetir)
        self.keep_awake: Optional["subprocess.Popen[bytes]"] = None

    async def answer(self, title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """
        Soruyu sohbete gönderir ve yetkili yanıtı bekler. "_" ile başlayan alanlar (açıklama,
        bağlantı) yanıt alanı değildir, mesajda gösterilir. Tek onay kutusu alanı Onayla/Reddet
        butonuyla ya da "evet/hayır" metniyle, seçenekli metin alanı seçenek butonuyla ya da metinle yanıtlanır.
        """
        if self.pending_answer is not None:
            raise TelegramError("Zaten bir kullanıcı yanıtı bekleniyor.")
        future: asyncio.Future[Dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending_answer = future
        answerable: Dict[str, Any] = {name: spec for name, spec in fields.items() if not name.startswith("_")}
        self.pending_fields = answerable
        self.pending_replies = question_replies(answerable, secrets.token_hex(4))
        notes = "\n".join(str(fields[name]) for name in ("_help", "_url") if fields.get(name))
        if len(answerable) == 1 and boolean_field(next(iter(answerable.values()))):
            instruction = "Butona dokunun ya da onay için 'evet', ret için 'hayır' yazın. /stop iptal eder."
        elif self.pending_replies:
            instruction = "Butona dokunun ya da yanıtınızı yazın. /stop iptal eder."
        elif len(answerable) == 1:
            instruction = "Yanıtınızı yazın. /stop iptal eder."
        else:
            instruction = (f"Alanlar: {', '.join(answerable)}\n"
                           "Birden çok alan için JSON nesnesi gönderin. /stop iptal eder.")
        text = f"❔ {title[:1800]}\n" + (f"{notes[:1500]}\n" if notes else "") + instruction
        try:
            if self.pending_replies:
                await self.api.send_buttons(self.settings["chat_id"], text, [
                    [(reply["label"], data) for data, reply in self.pending_replies.items()],
                ])
            else:
                await self.api.send(self.settings["chat_id"], text)
            return await future
        finally:
            self.pending_answer = None
            self.pending_fields = {}
            self.pending_replies = {}

    async def deliver(self, path: Path, caption: str) -> None:
        """send_file aracının teslim kanalı: dosyayı eşleştirilmiş sohbete belge olarak gönderir."""
        try:
            await self.api.send_document(self.settings["chat_id"], path, caption)
        except TelegramError as error:
            raise DeliveryFailed(str(error)) from None

    def _drain_control_messages(self) -> List[str]:
        """Telegram komutlarını çalışan ajan turuna bir kez aktarır."""
        commands: List[str] = []
        while True:
            try:
                commands.append(self.control_messages.get_nowait())
            except Empty:
                return commands

    async def _refresh_clients(self) -> None:
        """
        Model istemcilerini güncel hazır profillerle yeniden kurar ve eskileri kapatır. Hazırlık yoklaması
        (yerel Ollama) engelleyici olduğu için ayrı iş parçacığında çalışır; görevler sıralı olduğundan
        kapatılan istemciyi kullanan çalışan görev yoktur.
        """
        previous: Dict[str, AsyncOpenAI] = self.clients
        self.clients = await asyncio.to_thread(create_model_clients)
        await close_model_clients(previous)

    async def _send_screenshot(self, stream: TelegramStream, image: Path) -> None:
        """Ekran görüntüsünü fotoğraf olarak gönderir; dosya kaybolduysa ya da gönderim düştüyse akışa yazar."""
        if not image.is_file():
            await stream.append(f"\n! Ekran görüntüsü dosyası bulunamadı: {image}\n")
            return
        try:
            await self.api.send_photo(self.settings["chat_id"], image)
        except TelegramError as error:
            await stream.append(f"\n! Ekran görüntüsü gönderilemedi: {error}\n")

    async def _execute(
        self, goal: str, images: Optional[List[str]] = None, scheduled_id: Optional[str] = None,
    ) -> None:
        if scheduled_id is not None:
            # Bildirim görevin içinde gider: zamanlayıcı görevi await etmeden atar, araya mesaj giremez
            try:
                await self.api.send(self.settings["chat_id"], f"⏰ Zamanlanmış görev başlıyor [{scheduled_id}]: {goal[:500]}")
            except TelegramError as error:
                logging.warning("Zamanlanmış görev bildirimi gönderilemedi", extra={"error": str(error)[:200]})
        # Ayarlar başka süreçte değişmiş olabilir; görev başında güncel modeli yükle.
        apply_model_preferences()
        # Butonlu sürekli oturum istemleri bu göreve bağlanır; önceki görevin butonları işlemez.
        self.run_token = secrets.token_hex(4)
        self.run_started, self.run_tool, self.run_model, self.run_tokens = time.monotonic(), "", "", 0
        started_at = utc_now_iso()
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        # Ajan, boş çıktılı başarısız görevin kısmi raporunu run_finished çıktısı olarak yayınlamadan hemen önce
        # ayrıca uyarı olarak da yayınlar (masaüstü ve CLI run_finished çıktısını göstermez). İki olay aynı eşzamanlı
        # blokta yayınlandığı için uyarı tüketilmeden önce çıktı buraya girer; ayrıntılı görünümde aynı metin
        # ikinci kez basılmaz. Kısa görünüm uyarıları zaten göstermez.
        finished_outcomes: set[str] = set()

        def emit(event: AgentEvent) -> None:
            if event["kind"] == "run_finished":
                finished_outcomes.add(event["outcome"])
            loop.call_soon_threadsafe(queue.put_nowait, event)

        def verbose_text(event: AgentEvent) -> str:
            """
            Ayrıntılı görünüm metni; run_finished çıktısının birebir tekrarı olan uyarı ve butonlu kalıcı ileti
            olarak ayrıca gönderilen oturum istemi atlanır.
            """
            if event["kind"] == "notice" and (
                event["text"] in finished_outcomes or event.get("code") in CONTROL_PROMPT_BUTTONS
            ):
                return ""
            return event_text(event)

        if self.integrations is None:
            self.integrations = CapabilityService()
        options: RunOptions = {
            "integrations": self.integrations,
            "requested_backend": self.backend,
            "should_stop": self.stop_event.is_set,
            "state_file": STATE_FILE,
            "history": trim_history(self.history),
            "answer": channels.recording_answer("telegram", self.answer),
            "deliver": self.deliver,
            "run_mode": self.run_mode,
            # Kullanıcı makinenin başında değil: kısa ağ kopmalarında görev düşmez, model çağrısı bekler.
            "model_retry_seconds": REMOTE_MODEL_RETRY_SECONDS,
        }
        if self.active_run_mode == "continuous":
            options["unattended"] = True
            options["pop_control_messages"] = self._drain_control_messages
        if images:
            options["images"] = images
        if scheduled_id is not None:
            options["scheduled_run"] = True

        async def work() -> RunReport:
            # Hazır profiller görevler arasında değişebilir (Ollama sonradan açılıp kapanabilir): Otomatik seçim
            # köprü açılışındaki bayat listeye dayanmasın diye istemciler her görev başında yeniden kurulur.
            await self._refresh_clients()
            async with async_host_task_lock_preempting():
                return await run_agent_with_callback(goal, emit, options, self.clients)

        worker = asyncio.create_task(work())
        stream = TelegramStream(self.api, self.settings["chat_id"])
        live = TelegramDraftStream(self.api, self.settings["chat_id"], stream)
        compact = CompactPresenter(live)
        verbose = self.verbose

        async def render(event: AgentEvent) -> None:
            """Olayı seçili görünüme işler; kullanıcıyı bekleyen oturum istemi iki görünümde de butonlu kalıcı iletidir."""
            self._track(event)
            if event["kind"] == "notice" and event.get("code") in CONTROL_PROMPT_BUTTONS:
                await self._send_control_prompt(event["text"], str(event.get("code")))
            if verbose:
                rendered = verbose_text(event)
                if rendered:
                    await stream.append(rendered)
            else:
                await compact.event(event)

        # Model ekrana bakmak için de take_screenshot çağırır; her gözlemi sohbete göndermek sohbeti
        # dolduruyordu. Ayrıntılı görünüm hepsini anında gönderir; kısa görünüm yalnız kullanıcı
        # görüntü istediyse ve görev sonunda son görüntüyü tek kez gönderir.
        send_final_screenshot = not verbose and screenshot_requested(goal)
        # Araç göreli ekran görüntüsü adını ajanla aynı kuralla yazar (bkz. resolve_output_path);
        # köprü de fotoğrafı aynı yerden okur. Bayrak ajanın kullandığı hedef ve geçmişten türer.
        # Ajanın kendi gözlemleri (otomatik gözlem, bitiş doğrulaması) de take_screenshot olayıdır ama
        # önizlemesi dosya adı değil etiket taşır; kullanıcı görüntüsü sayılmaz.
        allow_source_relative: bool = source_change_expected(goal, options["history"])
        screenshot_paths: Dict[str, Path] = {}
        final_screenshot: Optional[Path] = None
        saw_finished = False
        report: Optional[RunReport] = None
        try:
            while not worker.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.25)
                except TimeoutError:
                    if verbose:
                        await stream.flush()
                    else:
                        await compact.tick()
                    continue
                saw_finished = saw_finished or event["kind"] == "run_finished"
                if (
                    event["kind"] == "tool_started" and event["name"] == "take_screenshot"
                    and event["preview"] not in (AUTO_OBSERVATION_PREVIEW, VERIFICATION_OBSERVATION_PREVIEW)
                ):
                    try:
                        screenshot_paths[event["call_id"]] = resolve_output_path(
                            event["preview"], allow_source_relative=allow_source_relative,
                        )
                    except (RuntimeError, OSError) as error:
                        # Araç tarafında aynı hata kurtarılabilir ToolError'dur ("~kullanici" çözülemez); tool_started
                        # olayı araçtan önce geldiği için burada yükselen istisna Telegram görevini iptal ederdi.
                        # Bu olay için fotoğraf yolu kaydedilmez.
                        logging.warning(
                            "Ekran görüntüsü yolu çözülemedi",
                            extra={"call_id": event["call_id"], "preview": event["preview"][:200],
                                   "error_type": type(error).__name__, "error": str(error)[:200]},
                        )
                await render(event)
                if event["kind"] == "tool_finished" and event["ok"]:
                    image = screenshot_paths.pop(event["call_id"], None)
                    if image is not None and verbose:
                        await self._send_screenshot(stream, image)
                    elif image is not None:
                        final_screenshot = image
            report = await worker
            # Rapor sunumu veya geçmiş yazımı hata verse de tamamlanan iş tek kez kaydedilir.
            await asyncio.to_thread(channels.record_report, "telegram", report)
            # Aynı turdaki call_soon_threadsafe olaylarını son sayfadan önce işle.
            await asyncio.sleep(0)
            while not queue.empty():
                event = queue.get_nowait()
                saw_finished = saw_finished or event["kind"] == "run_finished"
                await render(event)
            if verbose:
                if not saw_finished:
                    await stream.append(
                        f"\n{'✓' if report['success'] else '✗'} {report['outcome'][:1200]}\n"
                    )
            else:
                if send_final_screenshot and final_screenshot is not None:
                    await self._send_screenshot(stream, final_screenshot)
                await compact.finish(report)
            self.history = trim_history(self.history + [report["exchange"]])
            save_json(history_path(), self.history)
        except (HostBusyError, TelegramError) as error:
            try:
                if verbose:
                    await stream.append(f"\n✗ {error}\n")
                else:
                    await live.finish(f"⚠️ {error}")
            except TelegramError:
                pass
        except Exception as error:
            try:
                message = f"Görev hatası: {type(error).__name__}: {str(error)[:300]}"
                if verbose:
                    await stream.append(f"\n✗ {message}\n")
                else:
                    await live.finish(f"⚠️ {message}")
            except TelegramError:
                pass
        finally:
            if not worker.done():
                self.stop_event.set()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            if report is None:
                # Ajan sunumdan önce bitmiş olabilir; gerçek rapor varsa onu koru, yoksa hata türünü kaydet.
                if not worker.cancelled() and worker.exception() is None:
                    await asyncio.to_thread(channels.record_report, "telegram", worker.result())
                else:
                    error_type = "CancelledError" if worker.cancelled() else type(worker.exception()).__name__
                    await asyncio.to_thread(channels.record_failed_task, "telegram", goal, error_type,
                                            started_at, self.run_tokens)
            self.stop_event.clear()
            self.goal = ""
            self.active = None
            self.active_run_mode = "normal"
            self.run_token = ""
            try:
                await stream.flush(force=True)
            except TelegramError:
                pass

    async def _send_control_prompt(self, text: str, code: str) -> None:
        """
        Sürekli oturum istemini butonlu kalıcı ileti olarak gönderir. Gösterim hatası (ağ, 429, 5xx) saatlerdir süren
        oturumu iptal etmemeli: butonlar gönderilemezse uyarı loglanır ve istem düz metin gönderilir (kullanıcı
        /approve ya da /stop yazabilir); düz metin de gönderilemezse yalnız uyarı loglanır.
        """
        chat_id: int = self.settings["chat_id"]
        try:
            await self.api.send_buttons(chat_id, text, control_buttons(code, self.run_token))
            return
        except TelegramError as error:
            logging.warning("Oturum istemi butonlarla gönderilemedi; düz metin gönderiliyor",
                            extra={"code": code, "status": error.status, "error": str(error)[:200]})
        try:
            await self.api.send(chat_id, text)
        except TelegramError as error:
            logging.warning("Oturum istemi gönderilemedi; oturum kullanıcının /approve ya da /stop yazmasını bekler",
                            extra={"code": code, "status": error.status, "error": str(error)[:200]})

    async def _reply_to_question(self, text: str) -> None:
        future = self.pending_answer
        if future is None or future.done():
            return
        try:
            if len(self.pending_fields) == 1:
                name, spec = next(iter(self.pending_fields.items()))
                value = {name: approval_granted(text) if boolean_field(spec) else text}
            else:
                value = json.loads(text)
                if not isinstance(value, dict) or not all(name in value for name in self.pending_fields):
                    raise ValueError("Gerekli alanları içeren JSON nesnesi bekleniyor.")
        except ValueError as error:
            await self.api.send(self.settings["chat_id"], f"Yanıt biçimi hatalı: {error}")
            return
        future.set_result(value)
        await self.api.send(self.settings["chat_id"], "Yanıt alındı; görev sürüyor.")

    async def _handle_button(self, query: Dict[str, Any]) -> None:
        """
        Satır içi buton dokunuşunu işler; yalnız eşleşmiş kişinin özel sohbetindeki dokunuş kabul edilir.
        Sonuç kısa açılır bildirim olarak gösterilir, ileti kararla güncellenir ve butonları kalkar.
        """
        message = query.get("message")
        data = query.get("data")
        callback_id = query.get("id")
        if (
            not isinstance(message, dict) or not isinstance(data, str) or not isinstance(callback_id, str)
            or not authorized({"chat": message.get("chat"), "from": query.get("from")}, self.settings)
        ):
            return
        kind, _, value = data.partition(":")
        if kind in ("prov", "page") and value != "auto":
            toast, text, buttons = await self._provider_screen(kind, value)
        else:
            outcome: str = self._button_outcome(data)
            toast, text, buttons = outcome, f"{message.get('text', '')}\n\n→ {outcome}", []
        message_id = message.get("message_id")
        try:
            await self.api.answer_callback(callback_id, toast)
        except TelegramError as error:
            # Eylem uygulandı; köprü kapalıyken birikmiş eski dokunuşun sorgusu zaman aşımına uğramış olabilir.
            # İleti güncellemesi ayrı denenir: süresi geçmiş sorgu iletiyi eski hâliyle bırakmasın.
            logging.warning("Buton dokunuşu yanıtlanamadı", extra={"error": str(error)[:200]})
        try:
            if isinstance(message_id, int) and buttons:
                await self.api.edit_buttons(self.settings["chat_id"], message_id, text, buttons)
            elif isinstance(message_id, int):
                await self.api.edit(self.settings["chat_id"], message_id, text)
        except TelegramError as error:
            logging.warning("Buton sonucu gösterilemedi", extra={"error": str(error)[:200]})

    def _button_outcome(self, data: str) -> str:
        """Buton verisindeki eylemi uygular; kullanıcıya gösterilecek sonucu döner."""
        kind, _, value = data.partition(":")
        if kind == "q":
            return self._answer_by_button(data)
        if kind == "prov":
            return self._select_model(value)
        if kind == "pm":
            return self._pick_model(value)
        if kind == "mode":
            return self._select_mode(value)
        if kind == "ctl":
            return self._control_by_button(value)
        return "Bilinmeyen buton."

    async def _provider_screen(self, kind: str, value: str) -> Tuple[str, str, ButtonRows]:
        """
        Sağlayıcı seçicisinin sonraki ekranı (bildirim, metin, butonlar): dokunulan sağlayıcının model listesi
        (mevcut model başta) ya da listenin başka sayfası. Liste alınamazsa mevcut modelle seçim sunulur.
        """
        note = ""
        if kind == "prov":
            if value not in BACKENDS:
                return "Bilinmeyen model profili.", "Bilinmeyen model profili.", []
            try:
                listed: Tuple[str, ...] = await self._catalog_models(value)
            except ModelCatalogError as error:
                listed, note = (), f" (liste alınamadı: {error})"
            models: List[str] = [
                model for model in dict.fromkeys((BACKENDS[value]["model"], *listed)) if valid_model_id(model)
            ]
            picker: ModelPicker = {"token": secrets.token_hex(4), "profile": value, "models": models}
            self.model_picker = picker
            page = 0
        else:
            token, _, number = value.partition(":")
            current_picker: Optional[ModelPicker] = self.model_picker
            if current_picker is None or current_picker["token"] != token or not number.isdecimal():
                return "Bu seçici artık geçerli değil.", "Bu seçici artık geçerli değil.", []
            picker, page = current_picker, int(number)
        profile: str = picker["profile"]
        return (
            f"{profile}: {len(picker['models'])} model",
            f"{profile} modelleri{note}. Sonraki görev için seçin:",
            model_page_buttons(picker, page, BACKENDS[profile]["model"]),
        )

    async def _catalog_models(self, profile: str) -> Tuple[str, ...]:
        """Profilin sağlayıcısındaki modeller; masaüstü Ayarlar ile aynı katalog ve önbellek."""
        variable: Optional[str] = API_KEY_VARIABLES.get(profile)
        return await list_provider_models(
            BACKENDS[profile]["provider"], BACKENDS[profile]["base_url"],
            load_api_key(variable) if variable else None,
        )

    def _pick_model(self, value: str) -> str:
        """Seçicideki modeli profilin tercihi olarak kaydeder (masaüstüyle ortak) ve profili sonraki göreve seçer."""
        token, _, number = value.partition(":")
        picker: Optional[ModelPicker] = self.model_picker
        if picker is None or picker["token"] != token or not number.isdecimal() or int(number) >= len(picker["models"]):
            return "Bu seçici artık geçerli değil."
        profile, model = picker["profile"], picker["models"][int(number)]
        save_model_preferences({**load_model_preferences(), profile: model})
        apply_model_preferences()
        self.backend = profile
        self.model_picker = None
        return f"Sonraki görev modeli: {profile} · {model}."

    def _track(self, event: AgentEvent) -> None:
        """/status için çalışan görevin gerçek modelini, son aracını ve token toplamını izler."""
        if event["kind"] == "run_started" or event["kind"] == "backend_changed":
            self.run_model = f"{event['backend']} · {event['model']}"
        elif event["kind"] == "tool_started":
            self.run_tool = tool_label(event["name"])
        elif event["kind"] == "model_finished":
            self.run_tokens += event["usage"]["prompt_tokens"] + event["usage"]["completion_tokens"]

    def _status_text(self) -> str:
        """
        Çalışan görevi (süre, son araç, model, token), sonraki görevin ayarlarını ve varsa kanıtlı hafıza hatasını
        özetler.
        """
        upcoming: str = (
            f"Sonraki görev: {model_label(self.backend)} · {RUN_MODE_PROFILES[self.run_mode]['label']} modu"
        )
        failure: Optional[str] = channels.record_failure_line(local_timezone())
        memory_note: str = f"\n⚠️ {failure}" if failure is not None else ""
        if self.active is None:
            return f"Hazır. {upcoming} · Geçmiş: {len(self.history)} konuşma{memory_note}"
        return (
            f"Çalışıyor: {self.goal[:400]}\n"
            f"Süre: {elapsed_label(time.monotonic() - self.run_started)} · Araç: {self.run_tool or '—'} · "
            f"Model: {self.run_model or '—'} · Token: {compact_count(self.run_tokens)}\n{upcoming}{memory_note}"
        )

    def _answer_by_button(self, data: str) -> str:
        """Bekleyen soruyu dokunulan butonun yanıtıyla kapatır; eski ya da kapanmış sorunun butonu işlemez."""
        reply = self.pending_replies.get(data)
        future = self.pending_answer
        if reply is None or future is None or future.done():
            return "Bu soru artık geçerli değil."
        future.set_result(reply["answer"])
        return reply["label"]

    def _select_model(self, value: str) -> str:
        """Sonraki görevin model profilini ayarlar ('auto' = Otomatik)."""
        if value != "auto" and value not in BACKENDS:
            return "Bilinmeyen model profili."
        self.backend = None if value == "auto" else value
        return f"Sonraki görev modeli: {model_label(self.backend)}."

    def _select_mode(self, value: str) -> str:
        """Sonraki görevin çalışma modunu ayarlar."""
        if value not in RUN_MODE_PROFILES:
            return "Mod: normal, long, autonomous veya surekli."
        self.run_mode = value
        return f"Sonraki görev modu: {RUN_MODE_PROFILES[value]['label']}."

    def _control_by_button(self, value: str) -> str:
        """Sürekli oturum istemi butonunu uygular; yalnız istemi üreten çalışan görevde geçerlidir."""
        token, _, action = value.partition(":")
        if self.active is None or token != self.run_token:
            return "Bu oturum artık açık değil."
        if action == "approve":
            self.control_messages.put("/approve")
            return "Onay iletildi; oturum kapanıyor."
        if action == "stop":
            self._request_stop()
            return "Durdurma istendi."
        return "Bilinmeyen buton."

    def _request_stop(self) -> None:
        """Çalışan görevi durdurur; yanıt bekleyen soru varsa iptal eder."""
        self._stop_generation += 1
        self.stop_event.set()
        if self.pending_answer is not None and not self.pending_answer.done():
            self.pending_answer.set_exception(IntegrationStopped("Kullanıcı tarafından durduruldu."))

    async def scheduler_tick(self, now: datetime) -> None:
        """
        Kaçmış eski çalışmaları bildirip atlar; bilgisayar boşsa zamanı gelen ilk planı başlatır.
        Plan başlarken sonraki zamanına ilerletilir: görev uzun sürse de aynı plan iki kez başlamaz.
        `self.active` denetimi ile görev ataması arasında await yoktur; aksi hâlde araya giren
        kullanıcı mesajının görevi ezilirdi. Bakım (/update, /restart) sürerken görev başlamaz:
        yeniden başlatma yeni başlamış görevi keserdi.
        """
        path = schedules_file()
        chat_id = self.settings["chat_id"]
        try:
            records = schedule.load_schedules(path)
            records, skipped = schedule.skip_missed(records, now)
            if skipped:
                schedule.save_schedules(path, records)
        except (OSError, ValueError) as error:
            logging.warning("Plan deposu okunamadı", extra={"error_type": type(error).__name__})
            return
        for record in skipped:
            await self.api.send(chat_id, f"⏰ Kaçırıldı (bilgisayar kapalı/uykudaydı): {record['goal'][:300]}")
        if self.active is not None or self.maintenance:
            return
        ready = schedule.due_schedules(records, now)
        if not ready:
            return
        try:
            with host_task_lock():
                pass
        except HostBusyError:
            if host_owner() != "autonomous":
                return  # kullanıcı görevi sürüyor; bir sonraki turda yeniden denenir
        record = ready[0]
        try:
            schedule.save_schedules(path, schedule.advance_schedule(records, record["id"], now))
        except OSError as error:
            logging.warning("Plan ilerletilemedi", extra={"error_type": type(error).__name__})
            return
        self.goal = record["goal"]
        self.stop_event.clear()
        self.active_run_mode = self.run_mode
        self.control_messages = Queue()
        self.active = asyncio.create_task(self._execute(record["goal"], scheduled_id=record["id"]))

    async def _scheduler_loop(self) -> None:
        """Sürekli çalışan hizmette tek bir hata planların tümünü durdurmasın: her tur ayrı korunur."""
        while True:
            try:
                await self.scheduler_tick(datetime.now().astimezone())
            except TelegramError as error:
                logging.warning("Zamanlayıcı bildirimi gönderilemedi", extra={"error": str(error)[:200]})
            except Exception:
                logging.exception("Zamanlayıcı turu başarısız")
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)

    async def _schedule_command(self, text: str) -> None:
        """/schedules planları listeler; /unschedule <kimlik> planı siler."""
        chat_id = self.settings["chat_id"]
        path = schedules_file()
        try:
            records = schedule.load_schedules(path)
            if text.startswith("/unschedule"):
                parts = text.split(None, 1)
                records, removed = schedule.remove_schedule(records, parts[1] if len(parts) > 1 else "")
                if removed is None:
                    await self.api.send(chat_id, "Plan bulunamadı. Kimlikleri /schedules gösterir.")
                    return
                schedule.save_schedules(path, records)
                await self.api.send(chat_id, f"Plan silindi: {schedule.describe_record(removed)}")
                return
        except (OSError, ValueError) as error:
            await self.api.send(chat_id, f"Plan deposu okunamadı: {type(error).__name__}")
            return
        if not records:
            await self.api.send(chat_id, "Planlanmış görev yok. Örnek: \"Her sabah 9'da gündemi özetle\".")
            return
        await self.api.send(chat_id, "Planlanmış görevler:\n" + "\n".join(schedule.describe_record(r) for r in records))

    def _doctor_facts(self) -> DoctorFacts:
        """Köprünün sürümünü, izinlerini ve hazır yeteneklerini toplar (git çağırır; iş parçacığında)."""
        root = project_root()
        head = head_commit(root)
        try:
            planned: Optional[int] = len(schedule.load_schedules(schedules_file()))
        except (OSError, ValueError):
            planned = None
        return {
            "version": source_version(root),
            "stale": head is not None and self.loaded_commit is not None and head != self.loaded_commit,
            "service": "launchd hizmeti" if os.environ.get("XPC_SERVICE_NAME") == SERVICE_LABEL
            else "elle başlatılmış süreç",
            "python": sys.executable,
            "screen_capture": screen_capture_granted(),
            "accessibility": accessibility_granted(),
            "screen": screen_session(),
            "keep_awake": self.keep_awake is not None and self.keep_awake.poll() is None,
            "models": sorted(self.clients),
            "voice": bool(load_api_key(API_KEY_VARIABLES["openai"])),
            "schedules": planned,
        }

    async def _update_notes(self) -> Optional[List[str]]:
        """
        Kodu çeker, bağımlılıklar değiştiyse eşitler. Yeniden başlatılacaksa özet satırlarını döner;
        kod güncelse veya bir adım başarısızsa sonucu sohbete yazar ve None döner.
        """
        chat_id = self.settings["chat_id"]
        root = project_root()
        await self.api.send(chat_id, "Güncelleme denetleniyor (git pull)…")
        result = await asyncio.to_thread(pull_updates, root, self.loaded_commit)
        if not result["ok"] or not result["changed"]:
            await self.api.send(chat_id, result["message"])
            return None
        notes = [result["message"], *result["commits"]]
        if result["dependencies_changed"]:
            await self.api.send(chat_id, "Bağımlılıklar değişti; eşitleniyor (uv sync)…")
            failure = await asyncio.to_thread(sync_dependencies, root)
            if failure is not None:
                await self.api.send(chat_id, "\n".join(
                    notes + [failure, "Köprü eski kodla çalışmayı sürdürüyor; sorun giderilince /restart."]
                ))
                return None
            notes.append("Bağımlılıklar eşitlendi.")
        return notes + ["Masaüstü arayüzü açıksa yeni kodu yeniden açılınca yükler."]

    async def _maintenance_command(self, text: str) -> None:
        """
        /doctor durumu raporlar. /update kodu çekip köprüyü yeni kodla yeniden başlatır; /restart
        yalnız yeniden başlatır. Görev çalışırken yeniden başlatılmaz.
        """
        chat_id = self.settings["chat_id"]
        if text == "/doctor":
            facts = await asyncio.to_thread(self._doctor_facts)
            await self.api.send(chat_id, "\n".join(doctor_lines(facts)))
            return
        if self.active is not None:
            await self.api.send(chat_id, "Bir görev çalışıyor; bitince ya da /stop sonrası yeniden deneyin.")
            return
        self.maintenance = True
        try:
            notes = await self._update_notes() if text == "/update" else []
            if notes is None:
                return
            companion: Optional[str] = await asyncio.to_thread(restart_companion_service)
            if companion is not None:
                notes = notes + [companion]
            await self.api.send(chat_id, "\n".join(notes + ["Yeniden başlatılıyor…"]))
        finally:
            self.maintenance = False
        raise RestartRequested()

    async def _start_attachment_task(self, message: Dict[str, Any], attachment: TelegramAttachment) -> None:
        """Eki indirir ve açıklamasıyla (yoksa varsayılan istekle) görevi başlatır."""
        generation = self._stop_generation
        chat_id = self.settings["chat_id"]
        if self.pending_answer is not None:
            await self.api.send(chat_id, "Bir soruya yanıt bekleniyor; lütfen yanıtı metin olarak yazın.")
            return
        if self.active is not None:
            await self.api.send(chat_id, "Bir görev çalışıyor. /stop veya /status kullanın.")
            return
        if attachment["size"] > DOWNLOAD_LIMIT_BYTES:
            await self.api.send(chat_id, "Dosya 20 MB'tan büyük; Telegram botları bunu indiremez. "
                                         "Dosyayı bulut bağlantısıyla veya bilgisayardan paylaşın.")
            return
        try:
            path = await self.api.download(attachment["file_id"], inbox_path(), attachment["name"])
        except (OSError, TelegramError) as error:
            await self.api.send(chat_id, f"Ek indirilemedi: {error}")
            return
        caption = message.get("caption")
        caption = caption.strip() if isinstance(caption, str) else ""
        if attachment["kind"] == "sesli mesaj" and not caption:
            # Açıklamasız sesli mesaj bir komuttur: yazıya çevrilir, anlaşılan metin önce gösterilir
            try:
                transcript = await transcribe_audio(path)
            except TranscriptionUnavailable as error:
                await self.api.send(chat_id, f"🎙️ {error} İsteğinizi yazı olarak da gönderebilirsiniz.")
                return
            except (OSError, TranscriptionFailed) as error:
                await self.api.send(chat_id, f"🎙️ Sesli mesaj yazıya çevrilemedi: {error}")
                return
            await self.api.send(chat_id, f"🎙️ Anlaşılan: {transcript[:1500]}")
            goal = transcript
        else:
            goal = attachment_goal(caption, attachment, path)
        if generation != self._stop_generation:
            await self.api.send(chat_id, "Ekten görev başlatma iptal edildi.")
            return
        if self.active is not None:
            # İndirme/yazıya çevirme sürerken zamanlanmış görev başlamış olabilir; onu ezme
            await self.api.send(chat_id, f"Bir görev çalışıyor; ek kaydedildi: {path}. Görev bitince yeniden isteyin.")
            return
        self.goal = goal
        self.stop_event.clear()
        self.active_run_mode = self.run_mode
        self.control_messages = Queue()
        self.active = asyncio.create_task(self._execute(goal, [str(path)] if attachment["image"] else None))
        # Kanıtlı hafıza: ekin açıklaması ya da sesli mesaj dökümü kullanıcının sözüdür; varsayılan ek isteği değildir.
        spoken_or_written: str = caption or (goal if attachment["kind"] == "sesli mesaj" else "")
        if spoken_or_written and not forwarded(message):
            await channels.record_user_message_async("telegram", spoken_or_written)

    async def handle(self, update: Dict[str, Any]) -> None:
        query = update.get("callback_query")
        if isinstance(query, dict):
            await self._handle_button(query)
            return
        message = update.get("message")
        if not isinstance(message, dict) or not authorized(message, self.settings):
            return
        attachment = message_attachment(message)
        if attachment is not None:
            await self._start_attachment_task(message, attachment)
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        text = text.strip()
        chat_id = self.settings["chat_id"]
        if text == "/stop":
            self._request_stop()
            if self.active is None:
                await self.api.send(chat_id, "Çalışan görev yok.")
            else:
                await self.api.send(chat_id, "Durdurma istendi; çalışan işlem iptal ediliyor.")
            return
        if text.startswith("/btw ") or text == "/approve":
            if self.active is None or self.active_run_mode != "continuous":
                await self.api.send(chat_id, "Bu komut çalışan Sürekli oturumda kullanılabilir.")
            else:
                self.control_messages.put(text)
                if text.startswith("/btw "):
                    # Sürekli oturuma verilen yön kullanıcının kendi sözüdür (kanıtlı hafızaya).
                    await channels.record_user_message_async("telegram", text[len("/btw "):])
                await self.api.send(chat_id, "Yönlendirme oturuma eklendi." if text.startswith("/btw ")
                                    else "Onay isteği oturuma iletildi; kanıt bekliyorsa kapanacak.")
            return
        if text == "/schedules" or text.startswith("/unschedule"):
            await self._schedule_command(text)
            return
        memory_command: Optional[MemoryCommand] = parse_memory_command(text)
        if memory_command is not None:
            # Kullanıcının doğrudan hafıza komutu; depo iş parçacığında kısa ömürlü bağlantıyla açılır.
            await self.api.send(chat_id, await asyncio.to_thread(
                channels.run_memory_command, companion_db_file(), memory_command))
            return
        if text == "/status":
            await self.api.send(chat_id, self._status_text())
            return
        if text == "/new":
            if self.active is not None:
                await self.api.send(chat_id, "Görev sürerken yeni oturum açılamaz; önce /stop.")
                return
            self.history = []
            save_json(history_path(), self.history)
            await self.api.send(chat_id, "Yeni oturum: sohbet geçmişi temizlendi.")
            return
        if text in ("/doctor", "/update", "/restart"):
            await self._maintenance_command(text)
            return
        if text in ("/start", "/help"):
            await self.api.send(
                chat_id,
                "Hedefinizi yazın. /stop durdurur, /status durumu gösterir. "
                "/verbose on ayrıntılı akışı açar; /verbose off kısa yanıtı kullanır. "
                "/provider sağlayıcıyı ve modelini, /mode çalışma modunu butonla seçtirir "
                "(yazarak da olur: /model <profil>, /mode <normal|long|autonomous|surekli>); "
                "/new sohbet geçmişini temizler; "
                "onay soruları Onayla/Reddet butonuyla ya da 'evet'/'hayır' yazarak yanıtlanır; "
                "surekli, siz durdurana veya hedefi /approve ile onaylayana kadar çalışır; "
                "çalışırken /btw <mesaj> ile yön verebilirsiniz. "
                "Fotoğraf, belge, ses veya video da gönderebilirsiniz: açıklaması görev olur; "
                "ajan istediğiniz dosyaları size buradan geri gönderebilir. "
                "\"Her sabah 9'da …\" gibi görevler planlanır; /schedules listeler, /unschedule <kimlik> siler. "
                "/hafıza kanıtlı hafızadaki bilgileri listeler, /unut <numara> bir bilgiyi unutturur. "
                "/update kodu günceller ve köprüyü yeniden başlatır, /restart yalnız yeniden başlatır, "
                "/doctor sürümü ve izinleri gösterir.",
            )
            return
        if self.pending_answer is not None:
            await self._reply_to_question(text)
            return
        if text in ("/verbose on", "/verbose off"):
            self.verbose = text.endswith("on")
            await self.api.send(chat_id, "Sonraki görev: ayrıntılı akış." if self.verbose else "Sonraki görev: kısa görünüm.")
            return
        if text in ("/provider", "/model"):
            await self.api.send_buttons(
                chat_id, f"Sonraki görev modeli: {model_label(self.backend)}. Sağlayıcı seçin:",
                provider_buttons(self.backend, frozenset(self.clients)),
            )
            return
        if text.startswith("/model "):
            await self.api.send(chat_id, self._select_model(text.split(None, 1)[1].strip()))
            return
        if text == "/mode":
            await self.api.send_buttons(
                chat_id, f"Sonraki görev modu: {RUN_MODE_PROFILES[self.run_mode]['label']}. Değiştirmek için seçin:",
                mode_buttons(self.run_mode),
            )
            return
        if text.startswith("/mode "):
            selected = text.split(None, 1)[1].strip()
            aliases = {"long": "extended", "surekli": "continuous", "sürekli": "continuous"}
            await self.api.send(chat_id, self._select_mode(aliases.get(selected, selected)))
            return
        if self.active is not None:
            await self.api.send(chat_id, "Bir görev çalışıyor. /stop veya /status kullanın.")
            return
        self.goal = text
        self.stop_event.clear()
        self.active_run_mode = self.run_mode
        self.control_messages = Queue()
        self.active = asyncio.create_task(self._execute(text))
        # Kanıtlı hafıza: kullanıcının kendi hedef metni. Görev başladıktan sonra yazılır; kayıt hatası görevi
        # durdurmaz. `self.active` denetimi ile atama arasına await girmez (zamanlayıcı yarışı, bkz. scheduler_tick).
        if not forwarded(message):
            await channels.record_user_message_async("telegram", text)

    async def run(self, announce: bool = False) -> None:
        """Güncellemeleri yoklar. `announce`: /update veya /restart sonrası açılışı sohbete bildirir."""
        self.clients = create_model_clients()
        self.loaded_commit = await asyncio.to_thread(head_commit, project_root())
        self.keep_awake = start_keep_awake(os.getpid())
        scheduler = asyncio.create_task(self._scheduler_loop())
        # Kanıtlı hafıza öğrenme hattı (iMessage köprüsüyle ortak kilit ve imleç); iMessage kurulu değilse kapalı.
        learner = asyncio.create_task(
            learning.learning_loop(learning_backend, companion_db_file(), memory_learning_lock_file()))
        # Normal güncellemeler sıralı kalır; yavaş indirme veya SQLite yazımı yeni /stop'u bekletemez.
        pending_updates: asyncio.Queue[Tuple[int, Dict[str, Any]]] = asyncio.Queue()
        controls: set[asyncio.Task[None]] = set()

        async def consume_updates() -> None:
            while True:
                generation, update = await pending_updates.get()
                try:
                    if generation == self._stop_generation:
                        await self.handle(update)
                finally:
                    pending_updates.task_done()

        def control_done(task: asyncio.Task[None]) -> None:
            controls.discard(task)
            if not task.cancelled() and task.exception() is not None:
                logging.warning("Durdurma bildirimi gönderilemedi",
                                extra={"error_type": type(task.exception()).__name__})

        def fast_stop(update: Dict[str, Any]) -> bool:
            message = update.get("message")
            if isinstance(message, dict) and authorized(message, self.settings):
                return isinstance(message.get("text"), str) and message["text"].strip() == "/stop"
            query = update.get("callback_query")
            if not isinstance(query, dict) or not isinstance(query.get("message"), dict):
                return False
            return (isinstance(query.get("id"), str)
                    and authorized({"chat": query["message"].get("chat"), "from": query.get("from")}, self.settings)
                    and bool(self.run_token) and query.get("data") == f"ctl:{self.run_token}:stop")

        consumer = asyncio.create_task(consume_updates())
        polling: Optional[asyncio.Task[object]] = None
        try:
            try:
                await self.api.set_commands(BOT_COMMANDS)
            except TelegramError as error:
                # Menü yalnız kolaylık: kaydedilemezse komutlar yazılarak yine çalışır.
                logging.warning("Komut menüsü kaydedilemedi", extra={"error": str(error)[:200]})
            if announce:
                version = await asyncio.to_thread(source_version, project_root())
                try:
                    await self.api.send(self.settings["chat_id"], f"✓ Köprü yeniden başladı: {version}")
                except TelegramError as error:
                    logging.warning("Açılış bildirimi gönderilemedi", extra={"error": str(error)[:200]})
            failures = 0
            while True:
                try:
                    polling = asyncio.create_task(self.api.call("getUpdates", {
                        "offset": self.offset, "timeout": POLL_SECONDS,
                        "allowed_updates": ["message", "callback_query"],
                    }))
                    ready, _ = await asyncio.wait({polling, consumer}, return_when=asyncio.FIRST_COMPLETED)
                    if consumer in ready:
                        consumer.result()  # İşleyici hatası tüketilir ve köprünün normal kapanışından geçer.
                    updates = await polling
                except TelegramError as error:
                    if consumer.done():
                        raise  # Normal işleyici hatası yoklama hatası gibi sonsuz yeniden denenmez.
                    if error.status is not None and error.status < 500 and error.status != 429:
                        # Kabul edilmiş /restart, eşzamanlı yoklama hatasından önce kapanışı
                        # tamamlayabilsin. Takılmış bakım ağ hatasını süresiz gizlemez.
                        if self.maintenance:
                            done, _ = await asyncio.wait(
                                {consumer}, timeout=MAINTENANCE_FINISH_TIMEOUT_SECONDS)
                            if consumer in done:
                                consumer.result()
                        raise
                    failures += 1
                    await asyncio.sleep(min(8, 2 ** min(failures - 1, 3)))
                    continue
                failures = 0
                if not isinstance(updates, list):
                    raise TelegramError("getUpdates: liste bekleniyor.")
                for update in updates:
                    if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                        continue
                    update_id = update["update_id"]
                    if update_id < self.offset:
                        continue
                    self.offset = update_id + 1
                    # Tekrarlanan uzaktan komut yan etkiyi yeniden başlatmasın.
                    save_json(offset_path(), {"offset": self.offset})
                    if fast_stop(update):
                        control = asyncio.create_task(self.handle(update))
                        controls.add(control)
                        control.add_done_callback(control_done)
                    else:
                        pending_updates.put_nowait((self._stop_generation, update))
                    # Stop bayrağı ağdaki bildirimi beklemeden, sonraki güncellemeden önce uygulanır.
                    await asyncio.sleep(0)
        finally:
            if polling is not None:
                if not polling.done():
                    polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            for control in controls:
                control.cancel()
            await asyncio.gather(*controls, return_exceptions=True)
            # Önce: /restart execv'den sonra yenisini kurar, eski engel geride kalmasın
            stop_keep_awake(self.keep_awake)
            self.keep_awake = None
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
            learner.cancel()
            await asyncio.gather(learner, return_exceptions=True)
            self.stop_event.set()
            if self.active is not None:
                try:
                    await asyncio.wait_for(self.active, timeout=5)
                except (TimeoutError, asyncio.CancelledError):
                    self.active.cancel()
                    await asyncio.gather(self.active, return_exceptions=True)
            if self.integrations is not None:
                await self.integrations.close()
            await close_model_clients(self.clients)
            await self.api.close()


async def pair(api: TelegramAPI, nonce: str, timeout: float = 180.0) -> TelegramSettings:
    """Yerel ekrandaki tek kullanımlık kodu gönderen özel sohbeti yetkilendirir."""
    deadline = time.monotonic() + timeout
    offset = -1
    while time.monotonic() < deadline:
        updates = await api.call("getUpdates", {
            "offset": offset, "timeout": min(POLL_SECONDS, max(1, int(deadline - time.monotonic()))),
            "allowed_updates": ["message"],
        })
        if not isinstance(updates, list):
            continue
        for update in updates:
            if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                continue
            offset = update["update_id"] + 1
            message = update.get("message")
            if not isinstance(message, dict) or message.get("text") != f"/pair {nonce}":
                continue
            chat, sender = message.get("chat"), message.get("from")
            if not isinstance(chat, dict) or not isinstance(sender, dict) or chat.get("type") != "private":
                continue
            chat_id, user_id = chat.get("id"), sender.get("id")
            if isinstance(chat_id, int) and isinstance(user_id, int) and chat_id > 0 and user_id > 0:
                save_json(offset_path(), {"offset": offset})
                return {"chat_id": chat_id, "user_id": user_id}
    raise TelegramError("Eşleştirme süresi doldu. setup komutunu yeniden çalıştırın.")


async def setup() -> None:
    token = getpass.getpass("BotFather tokenı (Keychain'e kaydedilir): ").strip()
    if ":" not in token:
        raise TelegramError("BotFather tokenı geçersiz görünüyor.")
    api = TelegramAPI(token)
    try:
        identity = await api.call("getMe", {})
        name = identity.get("username", "bot") if isinstance(identity, dict) else "bot"
        nonce = secrets.token_urlsafe(12)
        print(f"Telegram'da @{name} botuna /pair {nonce} gönderin (3 dakika).")
        settings = await pair(api, nonce)
        Keyring().set_password(TOKEN_SERVICE, TOKEN_ACCOUNT, token)
        save_json(settings_path(), settings)
        await api.send(settings["chat_id"], "OmniAgent eşleştirildi. /help yazarak başlayabilirsiniz.")
        print("Eşleştirme tamamlandı; token yalnız Keychain'de.")
    finally:
        await api.close()


SERVICE_LABEL = "com.omniagent.telegram"


def bridge_command(announce: bool = False) -> List[str]:
    """Köprüyü bu yorumlayıcıyla çalıştıran komut; launchd kaydı ve /restart aynı komutu kullanır."""
    command = [sys.executable, "-m", "omniagent.integrations.telegram", "run"]
    return command + ["--announce"] if announce else command


def service_plist_path() -> Path:
    """Kullanıcı LaunchAgent plist yolunu tek yerde tanımlar."""
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def build_launchd_record() -> Dict[str, object]:
    """Kaynak dosya konumundan bağımsız launchd kaydını üretir."""
    root = data_root()
    return launch_agent.launchd_record(
        SERVICE_LABEL, bridge_command(), root / "telegram-stdout.log", root / "telegram-stderr.log",
    )


def install_service() -> None:
    """LaunchAgent'i güncel paket koduyla idempotent biçimde kurar veya yeniler."""
    load_settings()
    load_token()
    data_root().mkdir(parents=True, exist_ok=True)
    path = service_plist_path()
    try:
        launch_agent.install(SERVICE_LABEL, path, build_launchd_record(), "Telegram")
    except LaunchAgentError as error:
        raise TelegramError(str(error)) from error
    print(f"Telegram hizmeti kuruldu/güncellendi: {path}")


async def run_bridge(announce: bool = False) -> None:
    """Tek yoklayıcıyı çalıştırır; iki süreç aynı komutu iki kez işlemez."""
    settings = load_settings()
    with host_task_lock(data_root() / "telegram-bridge.lock"):
        api = TelegramAPI(load_token())
        bridge = TelegramBridge(api, settings)
        await bridge.run(announce)


def main() -> None:
    parser = argparse.ArgumentParser(description="OmniAgent Telegram köprüsü")
    parser.add_argument("action", choices=("setup", "run", "install-service"))
    parser.add_argument("--announce", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    # Arka plan servisi kabuk ortamını miras almaz: Ayarlar'da kayıtlı anahtarları uygula.
    apply_stored_api_keys()
    try:
        if arguments.action == "setup":
            asyncio.run(setup())
        elif arguments.action == "install-service":
            install_service()
        else:
            # launchd süreci salt okunur `/` dizininde başlatır: modelin göreli yolları (ekran
            # görüntüsü, write_file, kabuk) "Read-only file system" ile düşüyor, her ekran görevi
            # bir tur kaybediyordu. Köprü, refactor öncesindeki gibi proje kökünde çalışır; plist
            # yerine burada ayarlanır ki /update sonrası execv ile gelen kod da uygulasın.
            os.chdir(project_root())
            asyncio.run(run_bridge(arguments.announce))
    except RestartRequested:
        # Kilit ve bağlantılar kapandı; aynı PID yeni kodu yükler (launchd hizmeti kesilmez)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, bridge_command(announce=True))
    except (TelegramError, HostBusyError, KeyboardInterrupt) as error:
        print(f"Telegram: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
