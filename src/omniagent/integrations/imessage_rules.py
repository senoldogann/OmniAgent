"""iMessage köprüsünün saf kuralları: süzgeç, kendi yansımamız, komutlar, üç durumlu onay cevabı, soru balonları."""
from __future__ import annotations

from pathlib import Path
import math
from typing import Dict, List, Optional

from omniagent.approval import approval_granted
from omniagent.companion.bubbles import MAX_BUBBLE_CHARS
from omniagent.core.text_norm import ascii_fold
from omniagent.integrations.imessage_settings import ImessageConfigError, normalize_handle
from omniagent.integrations.imsg import OBJECT_REPLACEMENT, IncomingMessage
from omniagent.integrations.runtime import boolean_field

# Telegram'ın onay kelimelerine (approval_granted) ek olarak arkadaşça evetler.
_EXTRA_AFFIRMATIVE: frozenset[str] = frozenset({"olur", "yap", "tabi", "tabii", "hadi", "okey", "evet yap"})
_NEGATIVE: frozenset[str] = frozenset({
    "hayir", "h", "no", "n", "yok", "iptal", "vazgec", "olmaz", "istemiyorum", "reddet", "yapma", "false", "0",
})
_STOP_WORDS: frozenset[str] = frozenset({"dur", "/dur", "/stop"})
_STATUS_WORDS: frozenset[str] = frozenset({"/durum", "/status"})


def _same_handle(raw: str, handle: str) -> bool:
    try:
        return normalize_handle(raw) == handle
    except ImessageConfigError:
        # Telefon/e-posta olmayan gönderen (ör. işletme sohbeti) eşleşmiş kullanıcı olamaz.
        return False


def accepted(message: IncomingMessage, handle: str) -> bool:
    """Yalnız eşleşmiş kullanıcıdan gelen, grup olmayan, bizim göndermediğimiz mesaj. Saf."""
    return not message["is_from_me"] and not message["is_group"] and _same_handle(message["sender"], handle)


def own_echo(message: IncomingMessage, handle: str) -> bool:
    """Eşleşmiş birebir sohbette bizim gönderdiğimiz balonun izlemedeki yansıması mı? Saf."""
    return (message["is_from_me"] and not message["is_group"]
            and any(_same_handle(participant, handle) for participant in message["participants"]))


def message_text(message: IncomingMessage) -> str:
    """Ek yer tutucusu (U+FFFC) atılmış, kırpılmış metin. Saf."""
    return message["text"].replace(OBJECT_REPLACEMENT, "").strip()


def image_paths(message: IncomingMessage) -> List[str]:
    """Mesajdaki görsel eklerin diskteki yolları. Saf."""
    return [item["path"] for item in message["attachments"] if item["mime_type"].startswith("image/")]


def user_text(message: IncomingMessage) -> str:
    """
    Kullanıcının metni ve görsel olmayan eklerinin (PDF, video, ses) '[dosya: ad]' işaretleri, satır satır. Metinsiz
    dosya kaybolmaz: arşive ve sohbet katmanına işaretle girer. Saf.
    """
    markers: List[str] = [f"[dosya: {Path(item['path']).name}]" for item in message["attachments"]
                          if not item["mime_type"].startswith("image/")]
    return "\n".join(part for part in (message_text(message), *markers) if part)


def parse_command(text: str) -> Optional[str]:
    """Burst beklemeden işlenen kontrol komutu: 'stop', 'status' ya da None (yalnız tam kelime). Saf."""
    folded: str = ascii_fold(text.strip()).rstrip(".!")
    if folded in _STOP_WORDS:
        return "stop"
    if folded in _STATUS_WORDS:
        return "status"
    return None


def parse_proactive_command(text: str) -> Optional[Dict[str, object]]:
    folded = ascii_fold(text.strip())
    if folded in {"/proaktif ac", "/proaktif kapat"}:
        return {"proactive": folded.endswith(" ac")}
    if folded.startswith("/sessiz "):
        try:
            hours = float(folded.split(maxsplit=1)[1].replace(",", "."))
        except ValueError:
            return None
        if math.isfinite(hours) and 0 < hours <= 8760:
            return {"mute": hours}
    return None


def answer_polarity(text: str) -> Optional[bool]:
    """Onay cevabı: olumlu True, olumsuz False; ikisi de değilse None (mesaj normal sohbete gider). Saf."""
    folded: str = ascii_fold(text.strip()).rstrip(".!")
    if approval_granted(text) or folded in _EXTRA_AFFIRMATIVE:
        return True
    if folded in _NEGATIVE:
        return False
    return None


def field_prompt(name: str, spec: object) -> str:
    """Çok alanlı sorunun tek alanı için balon metni (alan etiketi, yoksa adı). Saf."""
    label: object = spec.get("label") if isinstance(spec, dict) else None
    return f"{label if isinstance(label, str) and label.strip() else name}?"


def _split_text(text: str, size: int) -> List[str]:
    """Metni en çok `size` karakterlik ardışık parçalara böler; karakter atılmaz, boş metin boş liste verir. Saf."""
    return [text[start:start + size] for start in range(0, len(text), size)]


def question_bubbles(title: str, fields: Dict[str, object]) -> List[str]:
    """
    Onay/soru balonları. Onay metni birebir gönderilir, modelle yeniden yazılmaz: kullanıcı neyi onayladığını
    ajanın özetinden değil işlemin kendi tanımından okur. Uzun başlık ve notlar ardışık balonlara bölünür, asla
    kesilmez: onay metninin sonu (ör. ödeme hedefi) da kullanıcıya ulaşmalı. Saf.
    """
    answerable: List[str] = [name for name in fields if not name.startswith("_")]
    notes: List[str] = [
        part
        for name in ("_help", "_url") if fields.get(name)
        for part in _split_text(str(fields[name]), MAX_BUBBLE_CHARS)
    ]
    single_boolean: bool = len(answerable) == 1 and boolean_field(fields[answerable[0]])
    bubbles: List[str] = [
        "bi onay lazım:" if single_boolean else "bi şey sormam lazım:",
        *_split_text(title.strip(), MAX_BUBBLE_CHARS),
        *notes,
    ]
    if single_boolean:
        bubbles.append("evet mi hayır mı?")
    elif len(answerable) == 1:
        bubbles.append("cevabını yazar mısın?")
    else:
        bubbles.append(f"{len(answerable)} şey soracağım, sırayla cevapla")
    return bubbles


def status_lines(running_goal: Optional[str], progress: List[str], tasks_today: int, tokens_today: int,
                 latency_p50_ms: Optional[float]) -> List[str]:
    """/durum cevabının satırları. Saf."""
    lines: List[str] = [f"şu an: {running_goal[:200]}" if running_goal else "şu an çalışan iş yok"]
    if running_goal and progress:
        lines.append(f"son adım: {progress[-1][:200]}")
    lines.append(f"bugün {tasks_today} iş, {tokens_today} token")
    if latency_p50_ms is not None:
        lines.append(f"cevap gecikmesi (medyan): {latency_p50_ms / 1000:.1f} sn")
    return lines
