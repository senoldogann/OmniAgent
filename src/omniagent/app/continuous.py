"""
Sürekli görev modu: kullanıcı durdurana, sınır dolana veya hedef kanıtla doğrulanana kadar
süren görevin saf kuralları ve kullanıcı sınır dosyası. Döngü entegrasyonu app/agent.py içindedir.
"""
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypedDict

from omniagent.integrations.runtime import save_json
from omniagent.paths import data_root
from omniagent.core.text_norm import ascii_fold

CONTINUOUS_MODE: str = "continuous"
DEFAULT_MAX_HOURS: float = 8.0
DEFAULT_MAX_TOTAL_TOKENS: int = 20_000_000
MAX_HOURS_CEILING: float = 168.0
MIN_TOTAL_TOKENS: int = 10_000
# Bağlam en çok bu kadar asistan turu taşır; aşılınca en yeni CONTEXT_KEEP_TURNS tur kalacak
# biçimde toplu kırpılır. Her tur kırpmak sağlayıcı önek önbelleğini her turda bozardı.
CONTEXT_MAX_TURNS: int = 40
CONTEXT_KEEP_TURNS: int = 20
# Araç çalıştırmadan üst üste bu kadar rapor yazan modele yeni yol denemesi söylenir.
MAX_IDLE_REPORTS: int = 3
MAX_CONTINUOUS_REPLANS: int = 2
# Ardışık ilerlemesiz tur sayısı (semantik ilerleme imzası değişmez): bu sayıya ulaşınca sürekli
# görev yeniden planlar. Normal moddaki fast-loop faz makinesinin continuous karşılığıdır; faz/
# teslim makinesi olmadan yalnız durgunluk yakalar ("kısmen başarılı ama etkisiz" döngüleri de).
CONTINUOUS_STAGNATION_LIMIT: int = 6
# Bir görevde kullanıcıya sorulacak hedef bildirimi sayısı. Sınırsızken ajan reddedildikten
# sonra da "goal" bildirmeyi sürdürüyor ve kullanıcıyı boşa meşgul ediyordu.
MAX_GOAL_REPORTS: int = 5
# Sonuç kayıtları olan hedef uzak kanalda /approve beklerken host kilidini tutar; onay
# ya da /btw gelmezse oturum onaylanmamış kapanır. Sessizlik başarı değildir.
GOAL_APPROVAL_TIMEOUT_SECONDS: float = 300.0
WINDOW_MARKER: str = "HOST — BAĞLAM PENCERESİ:"
CONTINUE_PROMPT: str = (
    "HOST — SÜREKLİ MOD: Görev bitmedi; son yanıtın kullanıcıya ilerleme raporu olarak gösterildi.\n"
    "- Kullanıcı çevrimdışı olabilir. Eksik bilgiyi bir kez sor; yanıt ertelendiyse aynı soruyu "
    "yineleme, bağımlılığı çalışma kaydına yaz ve bağımsız somut adımları sürdür. "
    "API anahtarı, parola veya token'ı sohbetle isteme; kullanıcıdan ⚙ Ayarlar'a girmesini iste ve "
    "kind=confirm ile doğrulat.\n"
    "- Onay gerekiyorsa sohbet metninde soru sormak yerine ask_user(kind=confirm) çağır. "
    "Onay gelmeden o eylemi yapma; /btw ile verilen adresler yayın onayı değildir.\n"
    "- Yeni bir sonuç üreten bağımsız adım kaldıysa gerçek araçla uygula; aynı planı, soruyu "
    "ve başarı iddialarını tekrarlama. Bağımsız iş kalmadıysa ask_user ile yön bekle.\n"
    "- Hedefe ulaştığını önceki araç çıktılarıyla kanıtlayabiliyorsan report_goal_met çağır; "
    "kanıtsız başarı iddia etme."
)


def text_confirmation_question(content: str) -> Optional[str]:
    """Modelin yalnız metinde bıraktığı açık onay isteğini gerçek host sorusuna çevirir.

    Genel sorular ya da alıntı içindeki soru işaretleri onay değildir. STATE satırları
    popup'a taşınmaz; taslak ve açıklama korunur ki kullanıcı neyi onayladığını görsün.
    """
    folded = content.casefold()
    waiting = re.search(
        r"(?:onay(?:ınızı|ınızı|ını|iniz[iı]|[ıi]n[ıi]z[ıi])?\s+bekliyorum|"
        r"sizden\s+onay\s+bekliyorum|waiting\s+for\s+your\s+approval)", folded,
    )
    question = re.search(
        r"(?:paylaş|yayınla|gönder|güncelle)[^\n?]{0,140}"
        r"(?:ister\s+misiniz|[aıi]l[ıi]m\s+m[ıi]|[aıi]y[ıi]m\s+m[ıi])\s*\?"
        r"|(?:do you approve|may i (?:publish|post|send)|shall i (?:publish|post|send))[^\n?]*\?",
        folded,
    )
    if not waiting and not question:
        return None
    text = re.sub(r"(?im)^\s*STATE\s*:[^\n]*\n(?:[^\n]+\n)*\n?", "", content).strip()
    return text or content.strip()


def gui_progress_problem(content: str, steps: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """GUI tamamlanma iddiasını, yazım/tıklamadan sonraki gerçek okuma olmadan yayınlama."""
    folded = ascii_fold(content)
    claims = re.search(
        r"\b(?:gonderdim|gonderildi|yayinladim|yayinlandi|paylastim|yorum biraktim)\b"
        r"|\b(?:bio|profil)[^\n]{0,140}(?:guncellendi|guncelledim|optimize edildi|optimize ettim)"
        r"|\b(?:posted|published|sent)\s+(?:the|your|a)\s+(?:post|reply|message)\b", folded,
    )
    if not claims:
        return None
    mutations = {
        "cua_click_point", "cua_click_text", "cua_click_element", "cua_click", "smart_click",
        "cua_type_text", "cua_fill_field", "cua_set_text_element", "cua_press_key",
        "cua_submit_text", "run_action_sequence",
    }
    last = next((i for i in range(len(steps) - 1, -1, -1) if steps[i]["tool"] in mutations), None)
    reads = {"cua_snapshot", "cua_read_visible_text", "cua_read_scrollable"}
    if last is None or not steps[last]["ok"] or not any(
        step["ok"] and step["tool"] in reads for step in steps[last + 1:]
    ):
        return (
            "GUI sonucu doğrulanmadı: tıklama/yazım tamamlanma kanıtı değildir. "
            "Başarı iddiasını göstermedim. Kaydedilen/yayınlanan sonucu cua_snapshot veya "
            "okuma aracıyla kontrol et; yazar, içerik ve alan değerini karşılaştır. "
            "Sonuç yoksa 'doğrulanmadı' diye bildir."
        )
    return None
# Durgun sürekli görevde host'un yeniden planlama yönergesi ("HOST — YENİDEN PLANLA: <neden>. " önekiyle gider).
REPLAN_GUIDANCE: str = (
    "Aynı başarısız çağrıyı aynı argümanlarla yineleme. Hatanın nedenini kullanarak farklı bir araç, "
    "argüman veya yöntem seç ve somut bir adım uygula. Gerçekten eksik kullanıcı bilgisi varsa ask_user çağır."
)
# Görevde bir site doğrulama/erişim engeli gösterdiyse yukarıdaki iki yönerge yerine bunlar gider: 'farklı araç/yöntem
# seç' ve 'sıradaki somut adımı uygula' cümleleri engeli aşmaya iterdi. Engelli adres aşılmaz; kullanıcıya bildirilir,
# yön ask_user ile istenir ve engelden bağımsız iş yoksa yanıt beklenir.
WALL_CONTINUE_PROMPT: str = (
    "HOST — SÜREKLİ MOD: Görev bitmedi; son yanıtın kullanıcıya ilerleme raporu olarak gösterildi.\n"
    "- Bu görevde bir site doğrulama veya erişim engeli gösterdi. Engelli adresi aşmaya çalışma: başka araç, "
    "kimlik, proxy, adres değişikliği veya yeniden deneme yok. Engeli çalışma kaydına yaz, kullanıcıya bildir; "
    "yön gerekiyorsa ask_user (kind=confirm) ile iste ve yanıt gelene dek bekle.\n"
    "- Engelle ilgisiz, bağımsız somut bir adım kaldıysa yalnız onu gerçek bir araçla sürdür; kalmadıysa yeni "
    "araç çağrısı yapma.\n"
    "- Hedefe ulaştığını önceki araç çıktılarıyla kanıtlayabiliyorsan report_goal_met çağır; "
    "kanıtsız başarı iddia etme."
)
WALL_REPLAN_GUIDANCE: str = (
    "Bu görevde bir site doğrulama veya erişim engeli gösterdi; engelli adresi başka araç, kimlik, proxy, adres "
    "değişikliği veya yeniden denemeyle aşmaya ÇALIŞMA. Engeli çalışma kaydına yaz, kullanıcıya bildir ve yön için "
    "ask_user (kind=confirm) çağır; engelle ilgisiz somut bir adım kaldıysa yalnız onu sürdür, kalmadıysa yanıt "
    "gelene dek bekle."
)


class ContinuousLimits(TypedDict):
    max_hours: float
    max_total_tokens: int


def continuous_limits_path() -> Path:
    return data_root() / "continuous_limits.json"


def validate_continuous_limits(limits: Mapping[str, object]) -> ContinuousLimits:
    """Süreyi (0 < saat ≤ 168) ve toplam token sınırını (≥ 10.000) doğrular. Saf."""
    hours: object = limits.get("max_hours")
    tokens: object = limits.get("max_total_tokens")
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < float(hours) <= MAX_HOURS_CEILING:
        raise ValueError(f"Sürekli mod süresi 0-{MAX_HOURS_CEILING:g} saat arasında olmalı; alınan: {hours!r}")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < MIN_TOTAL_TOKENS:
        raise ValueError(f"Sürekli mod token sınırı en az {MIN_TOTAL_TOKENS} olmalı; alınan: {tokens!r}")
    return {"max_hours": float(hours), "max_total_tokens": tokens}


def parse_continuous_limits(hours_text: str, tokens_text: str) -> ContinuousLimits:
    """Ayarlar alanlarını Türkçe sayı yazımıyla ("8,5", "20.000.000") çözer ve doğrular. Saf."""
    try:
        hours: float = float(hours_text.strip().replace(",", "."))
    except ValueError as error:
        raise ValueError(f"Sürekli mod süresi sayı olmalı; alınan: {hours_text!r}") from error
    digits: str = re.sub(r"[\s._]", "", tokens_text)
    if not digits.isdigit():
        raise ValueError(f"Sürekli mod token sınırı tam sayı olmalı; alınan: {tokens_text!r}")
    return validate_continuous_limits({"max_hours": hours, "max_total_tokens": int(digits)})


def load_continuous_limits(path: Path) -> ContinuousLimits:
    """Kayıt yoksa varsayılanları, varsa doğrulanmış kaydı döner; bozuk kayıt açık hata verir."""
    if not path.exists():
        return {"max_hours": DEFAULT_MAX_HOURS, "max_total_tokens": DEFAULT_MAX_TOTAL_TOKENS}
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Sürekli mod sınır dosyası JSON nesnesi olmalı: {path}")
    return validate_continuous_limits(raw)


def save_continuous_limits(path: Path, limits: ContinuousLimits) -> None:
    save_json(path, validate_continuous_limits(limits))


def goal_report_problem(summary: str, evidence_ids: object, evidence: Mapping[str, str]) -> Optional[str]:
    """
    report_goal_met argümanlarını host'un gördüğü başarılı araç çağrılarıyla karşılaştırır;
    geçerliyse None, değilse modele verilecek düzeltme metnini döner. Saf.
    """
    if not summary.strip():
        return "summary boş olamaz: ulaşılan somut sonucu yaz."
    if not isinstance(evidence_ids, list) or not evidence_ids or not all(
        isinstance(item, str) for item in evidence_ids
    ):
        return "evidence_call_ids, sonucu kanıtlayan önceki başarılı araç çağrılarının id listesi olmalı."
    unknown: List[str] = [item for item in evidence_ids if item not in evidence]
    if unknown:
        recent: str = ", ".join(list(evidence)[-8:]) or "yok"
        return (
            f"Bu id'ler görevdeki başarılı bir araç çağrısı değil veya yalnız eylem girdisi kaydı: {', '.join(unknown)}. "
            "Tıklama/yazım/görüntü dosyası başarı kanıtı değildir; kaydedilen/yayınlanan sonucu "
            "cua_snapshot ya da okuma aracıyla kontrol et. "
            f"Son başarılı çağrılar: {recent}."
        )
    return None


def goal_report_repeat_problem(evidence_ids: Sequence[str], seen: frozenset[str]) -> Optional[str]:
    """
    Aynı kanıt id'leriyle yinelenen hedef bildirimini kullanıcıya sormadan reddeder: her
    bildirim yeni bir kanıt getirmeli. Canlı kayıtta ajan reddedildikten sonra aynı kanıtlarla
    bildirimi tekrarlayıp kullanıcıyı boşa meşgul etti. Saf.
    """
    if evidence_ids and all(item in seen for item in evidence_ids):
        return (
            "Bu kanıt id'leri önceki bir hedef bildiriminde zaten değerlendirildi; aynı kanıtla "
            "yeniden bildirme. Önce eksik işi gerçekten tamamla (yeni başarılı araç çağrıları "
            "üret), sonra bildirimi yalnız o yeni çağrıların id'leriyle yap."
        )
    return None


def goal_confirmation_question(summary: str, evidence_ids: Sequence[str], evidence: Mapping[str, str]) -> str:
    """Kullanıcıya hedefin gerçekten gerçekleşip gerçekleşmediğini soran onay metni. Saf."""
    proof: str = "\n".join(f"• {evidence[item]}" for item in evidence_ids)
    return (
        f"Ajan hedefe ulaşıldığını bildiriyor:\n{summary.strip()}\n\nKanıt:\n{proof}\n\n"
        "Hedef gerçekten gerçekleştiyse yalnız 'evet' yaz; değilse neyin eksik olduğunu yaz, görev sürer."
    )


def window_messages(
    messages: List[Dict[str, Any]], head_len: int, max_turns: int, keep_turns: int, dropped: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Sabit başı (sistem, geçmiş, hedef) koruyup bağlamı asistan turu sınırında kırpar: asistan
    turları max_turns'ü aşınca en yeni keep_turns tur kalır, araç sonuçları sahipsiz kalmaz ve
    kırpılan tur sayısı tek bir işaret mesajında birikir. Saf.
    """
    head: List[Dict[str, Any]] = messages[:head_len]
    body: List[Dict[str, Any]] = messages[head_len:]
    if body and body[0].get("role") == "user" and str(body[0].get("content", "")).startswith(WINDOW_MARKER):
        body = body[1:]
    starts: List[int] = [index for index, entry in enumerate(body) if entry.get("role") == "assistant"]
    if len(starts) <= max_turns:
        return messages, dropped
    removed: int = len(starts) - keep_turns
    total: int = dropped + removed
    marker: Dict[str, Any] = {
        "role": "user",
        "content": (
            f"{WINDOW_MARKER} en eski {total} tur bağlamdan çıkarıldı. Kalıcı bilgi TASK "
            "SCRATCHPAD'dedir; çıkarılan işleri yeniden keşfetme."
        ),
    }
    return head + [marker] + body[starts[removed]:], total
