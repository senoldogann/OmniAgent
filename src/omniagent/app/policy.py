"""Agent karar politikaları: retry sırası, teslim doğrulama ve eylem kanıtı."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import re
from typing import List, Optional, Tuple

from openai import APIStatusError

from omniagent.app.tool_schema import _SIDE_EFFECT_TOOLS, memory_mutation_requested
from omniagent.config import ESCALATION_BACKEND, QUALITY_LADDER
from omniagent.core import state as sm
from omniagent.core.conversation import Exchange
from omniagent.integrations.runtime import CURRENT_RUNTIME
from omniagent.tools import ToolError, shell_command_words


def next_quality_backend(current: str, available: frozenset[str]) -> Optional[str]:
    """
    Art arda başarısız araç turlarında çıkılacak sonraki backend: QUALITY_LADDER'daki bir
    sonraki kullanılabilir basamak (merdiven dışındaki backend'ler doğrudan
    ESCALATION_BACKEND'e çıkar). Çıkılacak basamak yoksa None. Saf fonksiyon.
    """
    if current in QUALITY_LADDER:
        later: Tuple[str, ...] = QUALITY_LADDER[QUALITY_LADDER.index(current) + 1:]
    else:
        later = (ESCALATION_BACKEND,) if current != ESCALATION_BACKEND else ()
    return next((name for name in later if name in available), None)


def attempt_plan(backend: str, available: frozenset[str]) -> Tuple[str, ...]:
    """
    Geçici hatada aynı profili bir kez dener, sonra hazır ve farklı profillerin hepsine
    sırayla geçer. Merdiven içindeki profil önce merdivenin kendi sırasını, merdiven
    dışındaki profil tüm merdiveni izler. Görev kapsamındaki karantinaya alınmış profiller
    available dışındadır. Saf fonksiyon.
    """
    candidates: Tuple[str, ...] = (
        tuple(name for name in QUALITY_LADDER if name != backend)
        if backend in QUALITY_LADDER else QUALITY_LADDER
    )
    return (backend, backend) + tuple(
        name for name in candidates if name in available
    )


def retry_after_seconds(error: APIStatusError) -> float:
    """429 başlığındaki saniye veya HTTP tarihini güvenli bir bekleme süresine çevirir."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) if response is not None else {}
    raw = headers.get("retry-after", "") if headers is not None else ""
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            moment = parsedate_to_datetime(str(raw))
            return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 1.0


def final_verdict(content: str, finish_reason: Optional[str]) -> Tuple[bool, str]:
    """
    Araç çağrısız son yanıtın gerçekten tamamlanmış bir cevap olup olmadığına karar verir:
    token sınırında kesilen, filtrelenen ya da boş yanıtlar başarı sayılmaz. Saf fonksiyon.
    """
    if finish_reason == "length":
        return False, "yanıt max_tokens sınırında kesildi"
    if finish_reason == "content_filter":
        return False, "yanıt içerik filtresine takıldı"
    if not content.strip():
        return False, "model boş yanıt döndü"
    if re.search(
        r"^\s*[✗❌⚠️-]*\s*(?:(?:maalesef|üzgünüm)[,;:\s]*)?"
        r"(?:(?:bunu|işlemi|görevi|işlem|görev)\s*)?"
        r"(?:yapamadım|tamamlayamadım|tamamlanamadı|gerçekleştiremedim|"
        r"başarısız|could not complete|unable to complete)\b",
        content, re.IGNORECASE,
    ):
        return False, "model işlemin başarısız olduğunu bildirdi"
    return True, ""


def contains_unexecuted_tool_call(content: str) -> bool:
    """Metne yazılmış fakat API'nin tool_calls alanına ulaşmamış araç çağrısını saptar."""
    return bool(re.search(
        r"<\|?tool_call\|?>|(?m:^\s*(?:⏺\s*)?call:[A-Za-z_][A-Za-z0-9_]*\s*\{)",
        content,
    ))


def source_change_expected(goal: str, history: List[Exchange]) -> bool:
    """
    Kod değişikliği isteğini doğrudan hedeften veya onaylanmış kısa devam mesajından çıkarır.
    'yaz' fiili sayılmaz: "Tek satır 'KODLAR: …' yaz" final yanıt biçimidir (bkz. SYSTEM_PROMPT);
    ilan/doğrulama/posta kodu gibi yazılım dışı 'kod' da kaynak sayılmaz. İkisi birleşince
    ilan kodu okuma görevi "kod değişikliği istendi ama dosya yazılmadı" diye başarısız sayılıyordu.
    """
    lowered = goal.casefold().strip()
    action = re.search(r"\b(?:uygula|implement|geliştir|düzelt|onar|ekle|değiştir|oluştur|tamamla|iyileştir|hızlandır|gider|düzenle|optimize)\b", lowered)
    source = re.search(
        r"(?:\.py\b|(?<!ilan )(?<!ilanın )(?<!doğrulama )(?<!onay )(?<!posta )(?<!indirim )(?<!qr )\bkod"
        r"|\bproje|\brepo\b|\bkaynak|\bözellik|\bdestek|\bdesteğ|\bentegrasyon)",
        lowered,
    )
    if action is not None and source is not None:
        return True
    followup = re.fullmatch(
        r"(?:(?:evet|tamam|okey)[,\s]+)?(?:başla|basla|dene|devam et|uygula)[.!\s]*",
        lowered,
    )
    if not history:
        return False
    recent = " ".join(
        part for exchange in history[-2:] for part in (exchange["goal"], exchange["answer"])
    ).casefold()
    if not re.search(r"(?:\.py\b|\bkaynak kod\b|\bkod değişikli)", recent):
        return False
    if followup is not None:
        return True
    return len(lowered) <= 80 and bool(re.search(
        r"\b(?:başla|basla|dene|devam et|uygula|implement et|ekle|yap|hallet)\b",
        lowered,
    ))


_ACTION_VERBS: re.Pattern[str] = re.compile(
    r"\b(?:sil|silebilir|siler|temizle|boşalt|boşaltır|taşı|kaydet|kopyala|gönder|"
    r"kur|yükle|indir|başlat|çalıştır|değiştir|düzelt|güncelle|oluştur|ekle|"
    r"kapat|aç|açabilir|git|tıkla|uygula|çek|tamamla|iyileştir|hızlandır|"
    r"gider|onar|düzenle|optimize|listele|"
    r"delete|move|save|send|install|start|run|open|click|edit|update|create|deploy)\b",
    re.IGNORECASE,
)
_INFORMATIONAL_PREFIX: re.Pattern[str] = re.compile(
    r"^\s*(?:nasıl|neden|niçin|sence|hangi|ne kadar|mümkün mü)\b", re.IGNORECASE,
)
_ACTION_EVIDENCE_TOOLS: frozenset[str] = _SIDE_EFFECT_TOOLS - frozenset({
    "take_screenshot", "ask_user", "user_memory",
})
_MUTATION_VERBS: re.Pattern[str] = re.compile(
    r"\b(?:sil|silebilir|siler|temizle|boşalt|boşaltır|taşı|kaydet|kopyala|gönder|"
    r"kur|yükle|indir|başlat|çalıştır|değiştir|düzelt|güncelle|oluştur|ekle|uygula|"
    r"tamamla|iyileştir|hızlandır|gider|onar|düzenle|optimize|"
    r"delete|move|save|send|install|start|run|edit|update|create|deploy)\b",
    re.IGNORECASE,
)

_READ_ONLY_SHELL_PROGRAMS: frozenset[str] = frozenset({
    "cat", "date", "df", "du", "echo", "file", "grep", "head", "id", "ls",
    "lsof", "ps", "pwd", "rg", "stat", "tail", "uname", "wc", "which", "whoami",
})


def _obviously_read_only_shell(arguments: str) -> bool:
    """Salt gözlem komutunu dosya/hizmet değişikliği kanıtı sayma; belirsizi modele bırak."""
    try:
        parsed = json.loads(arguments or "{}")
        command = parsed.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return False
        # Yönlendirme ve komut ikamesi görüldüğünde masum görünen program da yazabilir.
        if re.search(r"[<>\x60]|\$\(", command):
            return False
        segments = shell_command_words(command)
    except (ValueError, AttributeError, ToolError):
        return False
    return bool(segments) and all(
        words and Path(words[0]).name.casefold() in _READ_ONLY_SHELL_PROGRAMS
        for words in segments
    )


# "adresini/sayfayı/veriyi çek" okuma (fetch) isteğidir, "fotoğraf çek" gibi eylem değildir
_FETCH_PHRASES: re.Pattern[str] = re.compile(
    r"\b(?:adres|url|link|bağlantı|sayfa|veri|içerik|json|api|html)\w*\s+çek\b", re.IGNORECASE,
)


def action_execution_expected(goal: str) -> bool:
    """
    Açık uygulama/dosya/hizmet eyleminde gerçek yürütme kanıtı ister. Okuma isteğindeki 'çek'
    sayılmaz: "fetch_raw ile adresini çek, değeri yaz" eylem sanılınca başarılı okuma kanıt
    sayılmıyor, host modele gerçek işlem dayatıyor ve doğru cevap bozuluyordu.
    """
    cleaned: str = _FETCH_PHRASES.sub(" ", goal)
    return not _INFORMATIONAL_PREFIX.search(cleaned) and bool(_ACTION_VERBS.search(cleaned))


def has_action_evidence(goal: str, steps: List[sm.StepRecord]) -> bool:
    """Başarılı okuma/keşfi eylem teslimiyle karıştırmaz."""
    runtime = CURRENT_RUNTIME.get()
    mutation = bool(_MUTATION_VERBS.search(goal))
    for step in steps:
        if not step["ok"]:
            continue
        name = step["tool"]
        if name == "discover_capabilities":
            continue
        if mutation and name in {"chrome_active_tab", "cua_get_app"}:
            continue
        if mutation and name == "browse_url":
            try:
                actions = json.loads(step["args"]).get("actions", [])
            except (ValueError, AttributeError):
                actions = []
            if not isinstance(actions, list) or not any(
                isinstance(action, dict) and action.get("action") in {"click", "fill", "press"}
                for action in actions
            ):
                continue
        if mutation and name == "execute_shell" and _obviously_read_only_shell(step["args"]):
            continue
        if name in _ACTION_EVIDENCE_TOOLS:
            return True
        if name == "take_screenshot" and re.search(r"ekran görüntüsü|screenshot", goal, re.IGNORECASE):
            return True
        if name == "user_memory" and memory_mutation_requested(goal):
            return True
        entry = runtime.published.get(name) if runtime is not None else None
        if entry is not None and not entry["readonly"]:
            return True
    return False


def unmet_wait_status(goal: str, steps: List[sm.StepRecord]) -> Optional[str]:
    """'status X olana kadar bekle' hedefini son başarılı JSON gözlemiyle karşılaştırır."""
    match = re.search(
        r"""(?i)\bstatus\s*['"]([\w-]+)['"]\s*olana\s+kadar\b""", goal,
    )
    if match is None:
        return None
    expected = match.group(1).casefold()
    for step in reversed(steps):
        if step["tool"] != "fetch_raw" or not step["ok"]:
            continue
        try:
            payload = json.loads(step["detail"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
            continue
        observed = payload["status"].casefold()
        if observed != expected:
            return f"ilerleme yok: beklenen status {expected}; son doğrulanan status {observed}"
        return None
    return "beklenen status doğrulanmadı"
