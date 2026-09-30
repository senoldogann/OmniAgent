"""
OmniAgent arayüzünün saf sunum yardımcıları: transkript satır özetleri, görev istatistikleri, kenar çubuğu
gruplaması/durum noktası ve composer/yanıt penceresi biçimleri.

Tk/customtkinter'a bağlı değildir; bu yüzden gerçek arayüzden (ui/app.py) ayrı tutulur ve
başsız testlerde de doğrudan kullanılabilir.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple, TypedDict

from omniagent.app.types import RunReport
from omniagent.core.events import AgentEvent, compact_count, tool_label
from omniagent.core.state import EpisodeMetrics
from omniagent.integrations.capabilities import Capability
from omniagent.ui.chats import OUTCOME_FAILED, ChatSummary, TranscriptSpan
from omniagent.ui.markdown import Part
from omniagent.ui.theme import CHAT_TITLE_MAX_CHARS, COMPOSER_MAX_LINES, COMPOSER_MIN_LINES

# Gösterim sınırları: en dar pencereye sığan satır sayısı ve mono karakter uzunluğu.
LIVE_TAIL_LINES: int = 6
SUMMARY_LINES: int = 4
COMMAND_LINES: int = 6
INSERT_MARK: str = "omni_insert"
# Sütunlu Markdown tablosu en dar pencerede de (560 px) satır kaydırmadan bu kadar mono
# karaktere sığar; daha geniş tablo etiket/değer satırlarına dönüşür.
TABLE_MAX_COLUMNS: int = 64
# Araç satırı: başlıktaki önizleme uzunluğu, hata ipucu uzunluğu; açılır ayrıntıdaki çıktı önizlemesi
# ve tek satırın en çok gösterilen uzunluğu (komut ve çıktı satırları için).
TITLE_PREVIEW_CHARS: int = 90
ERROR_HINT_CHARS: int = 70
DETAIL_PREVIEW_LINES: int = 8
DETAIL_LINE_CLIP: int = 400
COMMAND_TOOLS: Tuple[str, ...] = ("execute_shell", "execute_js")


class ToolView(TypedDict):
    """Transkriptteki bir araç bloğunun durumu."""
    region: str
    name: str
    preview: str
    status: str
    call_id: str
    head: List[str]
    tail: List[str]
    line_count: int
    result: str
    seconds: float
    started_at: float
    # Katlama kimlikleri: grup (g), araç ayrıntısı (t) ve 'tümünü göster' (m) etiketleri bunlardan türer.
    group_id: int
    fold_id: int
    more_id: int


class ToolGroup(TypedDict):
    """Art arda araç çağrılarının tek başlık altındaki grubu."""
    id: int
    region: str
    views: List[ToolView]


class TurnView(TypedDict):
    number: int
    text_region: Optional[str]
    reasoning_region: Optional[str]
    tools: Dict[int, ToolView]


class UiItem(TypedDict):
    """Ajan thread'lerinden arayüz thread'ine giden kuyruk öğesi."""
    event: Optional[AgentEvent]
    done: bool
    error: str
    report: Optional[RunReport]


def format_capability_inventory(entries: List[Capability], show_skills: bool = False) -> str:
    """Yerel kataloğu model veya ağ çağrısı olmadan kısa metne dönüştürür."""
    executable = sorted((entry for entry in entries if entry.get("kind") != "skill"),
                        key=lambda entry: entry.get("id", ""))
    skills = sorted((entry for entry in entries if entry.get("kind") == "skill"),
                    key=lambda entry: entry.get("id", ""))
    if show_skills:
        lines = [f"Kurulu skill sayısı: {len(skills)}"]
        lines.extend("• " + str(entry.get("id", "")).removeprefix("skill:") for entry in skills)
        return "\n".join(lines)
    lines = ["Kayıtlı entegrasyonlar:"]
    for entry in executable:
        name = entry.get("id", "")
        kind = entry.get("kind", "")
        status = entry.get("connection", "unknown")
        lines.append(f"• {name} ({kind}) · {status}")
    lines.append(f"Kurulu skill sayısı: {len(skills)} · adlar için /skills")
    return "\n".join(lines)


def _thousands(value: int) -> str:
    """Sayıyı Türkçe binlik ayraçla yazar (14631 → 14.631). Saf."""
    return f"{value:,}".replace(",", ".")


def format_run_stats(metrics: EpisodeMetrics) -> List[str]:
    """Görev bitişinde tam token sayılarını ve süre dağılımını okunur satırlara çevirir."""
    prompt: int = metrics["prompt_tokens"]
    cached: int = min(prompt, metrics["cached_tokens"])
    completion: int = metrics["completion_tokens"]
    fmt = _thousands
    first: str = (
        f"{metrics['elapsed_seconds']:.1f} sn · {metrics['turns']} tur · "
        f"{metrics['tool_calls']} araç · {metrics['backend']}"
    )
    if "model_seconds" in metrics and "tool_seconds" in metrics:
        first += f" · model {metrics['model_seconds']:.1f} sn · araç {metrics['tool_seconds']:.1f} sn"
    second: str = (
        f"Giriş {fmt(prompt)} (önbellek {fmt(cached)}, yeni {fmt(prompt - cached)})"
        f" · çıkış {fmt(completion)} · toplam {fmt(prompt + completion)} token"
    )
    lines: List[str] = [first, second]
    integration = metrics.get("integrations", {})
    if integration:
        pieces: List[str] = []
        for key, label, unit in (
            ("discovery_seconds", "keşif", " sn"),
            ("install_seconds", "kurulum", " sn"),
            ("network_seconds", "ağ", " sn"),
            ("wait_seconds", "bekleme", " sn"),
            ("user_wait_seconds", "kullanıcı", " sn"),
            ("network_requests", "ağ isteği", ""),
            ("operations_ok", "işlem başarılı", ""),
            ("operations_failed", "işlem hatalı", ""),
        ):
            value = integration.get(key, 0)
            if value:
                pieces.append(f"{label} {value}{unit}")
        if pieces:
            lines.append("Entegrasyon: " + " · ".join(pieces))
    return lines


def clip_text(line: str, limit: int) -> str:
    """Tek satırı verilen uzunlukta kırpar; sondaki satır sonu atılır. Saf."""
    single: str = line.rstrip("\n")
    return single if len(single) <= limit else single[:limit] + "…"


# --- Süre, token ve grup başlığı biçimleri ---

def format_elapsed(seconds: float) -> str:
    """Geçen süreyi kısa yazar: 10sn, 6dk 10sn, 1sa 2dk. Saf."""
    total: int = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}sa {minutes}dk"
    if minutes:
        return f"{minutes}dk {secs}sn" if secs else f"{minutes}dk"
    return f"{secs}sn"


def activity_meta(elapsed_seconds: float, tokens: int, approximate: bool) -> str:
    """
    Akış durum satırının ölçümleri: '6dk 10sn · ~420 token'. Token sayısı 0 ise yalnız süre
    yazılır; tahmine dayalıysa '~' ile belirtilir. Saf.
    """
    pieces: List[str] = [format_elapsed(elapsed_seconds)]
    if tokens > 0:
        pieces.append(f"{'~' if approximate else ''}{compact_count(tokens)} token")
    return " · ".join(pieces)


def group_title(count: int, running: bool) -> str:
    """Araç grubunun başlığı: '3 araç çalıştırıldı' / '3 araç çalışıyor'. Saf."""
    return f"{count} araç çalışıyor" if running else f"{count} araç çalıştırıldı"


def format_run_summary(metrics: EpisodeMetrics) -> str:
    """Görev bitişinin tek satırlık sessiz özeti: süre, tur, araç ve toplam token. Saf."""
    total: int = metrics["prompt_tokens"] + metrics["completion_tokens"]
    return (f"{metrics['elapsed_seconds']:.1f} sn · {metrics['turns']} tur · "
            f"{metrics['tool_calls']} araç · toplam {_thousands(total)} token")


# --- Katlama (elide) etiketleri ---
# Açılır her bölümün dört etiketi vardır: h (tıklanan başlık), b (kapalıyken gizlenen gövde),
# o (yalnız açıkken görünen işaret), c (yalnız kapalıyken görünen işaret). Tür: g grup, t araç
# ayrıntısı, m 'tümünü göster', s görev özeti. Durum yalnız etiketlerin `elide` ayarındadır;
# kayıtlı sohbet yüklenince aynı etiketlerden yeniden kurulur.
_FOLD_TAG: re.Pattern[str] = re.compile(r"([gtms])([hboc])(\d+)")


class FoldTags(TypedDict):
    click: str
    body: str
    open: str
    closed: str


def fold_tags(kind: str, ident: int) -> FoldTags:
    """Bir katlanabilir bölümün dört etiket adı. Saf."""
    return {"click": f"{kind}h{ident}", "body": f"{kind}b{ident}",
            "open": f"{kind}o{ident}", "closed": f"{kind}c{ident}"}


def parse_fold_tag(tag: str) -> Optional[Tuple[str, str, int]]:
    """Katlama etiketini (tür, parça, kimlik) olarak çözer; başka etiketlerde None. Saf."""
    match: Optional[re.Match[str]] = _FOLD_TAG.fullmatch(tag)
    return None if match is None else (match.group(1), match.group(2), int(match.group(3)))


def remap_fold_tags(spans: Sequence[TranscriptSpan], first_id: int) -> Tuple[List[TranscriptSpan], int]:
    """
    Kayıtlı katlama etiketlerinin kimliklerini oturumda çakışmayacak yeni kimliklere çevirir
    (aynı bölümün dört etiketi aynı yeni kimliği alır); (yeni parçalar, sonraki boş kimlik)
    döner. Girdiyi değiştirmez. Saf.
    """
    mapping: Dict[Tuple[str, int], int] = {}
    next_id: int = first_id
    remapped: List[TranscriptSpan] = []
    for span in spans:
        tags: List[str] = []
        for tag in span["tags"]:
            parsed: Optional[Tuple[str, str, int]] = parse_fold_tag(tag)
            if parsed is None:
                tags.append(tag)
                continue
            kind, part, old_id = parsed
            if (kind, old_id) not in mapping:
                mapping[(kind, old_id)] = next_id
                next_id += 1
            tags.append(f"{kind}{part}{mapping[(kind, old_id)]}")
        remapped.append({"text": span["text"], "tags": tags})
    return remapped, next_id


# --- Araç satırı ve ayrıntısı ---

def tool_title(name: str, preview: str) -> Tuple[str, str]:
    """Araç satırının (ad, kısa önizleme) çifti: önizleme ilk satırla ve TITLE_PREVIEW_CHARS ile sınırlıdır. Saf."""
    lines: List[str] = preview.splitlines()
    first: str = lines[0].strip() if lines else ""
    return tool_label(name), clip_text(first, TITLE_PREVIEW_CHARS)


def error_hint(text: str) -> str:
    """Hatalı araç satırında görünen kısa ipucu: hata metninin ilk dolu satırı. Saf."""
    for line in text.splitlines():
        if line.strip():
            return clip_text(line.strip(), ERROR_HINT_CHARS)
    return ""


def command_block_lines(name: str, preview: str) -> List[str]:
    """
    Açılır ayrıntıdaki komut/argüman bloğunun satırları. Komut araçlarında (kabuk, node) her zaman;
    diğer araçlarda yalnız başlığa sığmayan (uzun ya da çok satırlı) önizleme gösterilir. Saf.
    """
    lines: List[str] = preview.splitlines()
    if not lines:
        return []
    if name not in COMMAND_TOOLS and len(lines) == 1 and len(lines[0]) <= TITLE_PREVIEW_CHARS:
        return []
    shown: List[str] = lines[:COMMAND_LINES] + (["…"] if len(lines) > COMMAND_LINES else [])
    return [clip_text(line, DETAIL_LINE_CLIP) for line in shown]


_SHELL_STREAMS: re.Pattern[str] = re.compile(
    r"STDOUT: (?P<out>.*?)\nSTDERR: (?P<err>.*)\nÇıkış Kodu: -?\d+\s*", re.DOTALL)


def _search_titles(text: str) -> List[str]:
    """web_search sonucundaki (JSON) başlıkları çıkarır; biçim tanınmazsa boş liste. Saf."""
    try:
        results: object = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(results, list):
        return []
    return [str(item.get("title", "")) for item in results if isinstance(item, dict)]


def detail_output_lines(name: str, ok: bool, result: str) -> List[str]:
    """
    Bitmiş aracın açılır ayrıntıda gösterilecek çıktı satırları (satırlar kırpılmış, sondaki
    boşluklar atılmış). Kabuk/node sonucunun 'STDOUT/STDERR/Çıkış Kodu' sarmalı çözülür, arama
    sonuçları başlık listesine döner. Saf.
    """
    text: str = result
    if ok and name in COMMAND_TOOLS:
        match: Optional[re.Match[str]] = _SHELL_STREAMS.fullmatch(result)
        if match is not None:
            text = match["out"].rstrip("\n")
            if match["err"].strip():
                text = (text + "\n" if text else "") + "— stderr —\n" + match["err"].strip("\n")
    elif ok and name == "web_search":
        titles: List[str] = _search_titles(result)
        if titles:
            text = "\n".join(f"• {title}" for title in titles)
    return [clip_text(line, DETAIL_LINE_CLIP) for line in text.rstrip().splitlines()]


def _detail_parts(view: ToolView, now: float, fold: FoldTags, more: FoldTags, body: str) -> List[Part]:
    """Araç satırının altındaki açılır ayrıntı: komut/argüman bloğu ve çıktı. Saf."""
    detail: Tuple[str, ...] = (body, fold["body"])
    status: str = view["status"]
    parts: List[Part] = []
    command: List[str] = command_block_lines(view["name"], view["preview"])
    for index, line in enumerate(command):
        edges: Tuple[str, ...] = (("cmd_first",) if index == 0 else ()) + (
            ("cmd_last",) if index == len(command) - 1 else ())
        prompt: str = ("$ " if view["name"] == "execute_shell" else "› ") if index == 0 else "  "
        parts.append((prompt, ("cmd_block", "cmd_prompt") + detail + edges))
        parts.append((line + "\n", ("cmd_block",) + detail + edges))
    if status == "running":
        if view["tail"]:
            hidden: int = view["line_count"] - len(view["tail"])
            if hidden > 0:
                parts.append((f"… {hidden} satır daha\n", ("out_block", "out_note") + detail))
            parts.extend((clip_text(line, DETAIL_LINE_CLIP) + "\n", ("out_block",) + detail)
                         for line in view["tail"])
        else:
            elapsed: float = max(0.0, now - view["started_at"])
            parts.append((f"çalışıyor… {elapsed:.1f}sn\n", ("out_block", "out_note") + detail))
    elif status in ("ok", "error"):
        failed: bool = status == "error"
        lines: List[str] = detail_output_lines(view["name"], not failed, view["result"])
        tone: Tuple[str, ...] = ("out_block", "out_error") if failed else ("out_block",)
        if not lines:
            parts.append(("(çıktı yok)\n", ("out_block", "out_note") + detail))
        parts.extend((line + "\n", tone + detail) for line in lines[:DETAIL_PREVIEW_LINES])
        rest: List[str] = lines[DETAIL_PREVIEW_LINES:]
        if rest:
            parts.append((f"… +{len(rest)} satır · tümünü göster ▾\n",
                          ("out_block", "out_more", more["click"], more["closed"]) + detail))
            parts.extend((line + "\n", tone + detail + (more["body"],)) for line in rest)
            parts.append(("daha az ▴\n", ("out_block", "out_more", more["click"], more["open"]) + detail))
    return parts


def tool_parts(view: ToolView, now: float, spinner: str) -> List[Part]:
    """
    Araç bloğunun etiketli parçaları: satır (durum glifi, ad, önizleme, süre, ▸/▾), açılır
    ayrıntı (komut bloğu + çıktı önizlemesi) ve altta ince ayraç çizgisi. Grup ve ayrıntı
    katlaması yalnız etiketlerle (elide) yapılır; bu işlev Tk'ye dokunmaz. `spinner`, çalışan
    satırın o anki dönen glifidir. Saf.
    """
    group: FoldTags = fold_tags("g", view["group_id"])
    fold: FoldTags = fold_tags("t", view["fold_id"])
    more: FoldTags = fold_tags("m", view["more_id"])
    status: str = view["status"]
    if status in ("streaming", "running"):
        glyph, glyph_tag = spinner, "tool_spin"
    elif status == "ok":
        glyph, glyph_tag = "✓", "glyph_ok"
    else:
        glyph, glyph_tag = "✗", "glyph_error"
    row: Tuple[str, ...] = ("tool_row", group["body"], fold["click"])
    label, preview = tool_title(view["name"], view["preview"])
    parts: List[Part] = [(glyph, row + (glyph_tag,)), ("\t", row), (label, row + ("row_label",))]
    if preview:
        parts.append(("  " + preview, row + ("row_preview",)))
    if status == "error":
        hint: str = error_hint(view["result"])
        if hint:
            parts.append(("  — " + hint, row + ("row_error",)))
    if status in ("ok", "error"):
        parts.append((f"  {view['seconds']:.1f}sn", row + ("row_time",)))
    elif status == "running" and view["started_at"] > 0:
        parts.append((f"  {max(0.0, now - view['started_at']):.1f}sn", row + ("row_time",)))
    parts.append(("  ▾", row + ("tool_chev", fold["open"])))
    parts.append(("  ▸", row + ("tool_chev", fold["closed"])))
    parts.append(("\n", row))
    parts.extend(_detail_parts(view, now, fold, more, group["body"]))
    parts.append(("\n", ("tool_rule", group["body"])))
    return parts


def group_head_parts(group_id: int, count: int, running: bool) -> List[Part]:
    """Araç grubunun başlık satırı ('3 araç çalıştırıldı ▾') ve üst ayraç çizgisi. Saf."""
    tags: FoldTags = fold_tags("g", group_id)
    head: Tuple[str, ...] = ("tool_head", tags["click"])
    return [
        (group_title(count, running), head),
        ("  ▾", head + ("tool_chev", tags["open"])),
        ("  ▸", head + ("tool_chev", tags["closed"])),
        ("\n", head),
        ("\n", ("tool_rule", tags["body"])),
    ]


def summary_parts(fold_id: int, success: bool, reason: str, metrics: EpisodeMetrics) -> List[Part]:
    """
    Görev bitişinin tek satırlık sessiz özeti; tıklanınca token/süre dağılımı satırları açılır
    (varsayılan kapalı). Saf.
    """
    tags: FoldTags = fold_tags("s", fold_id)
    if success:
        head, head_tag = "✓ Tamamlandı", "sum_ok"
    elif reason == "durduruldu":
        head, head_tag = "■ Durduruldu", "sum_error"
    else:
        head, head_tag = f"✗ Tamamlanamadı: {reason}", "sum_error"
    click: Tuple[str, ...] = (tags["click"],)
    parts: List[Part] = [
        (head, (head_tag,) + click),
        ("  ·  " + format_run_summary(metrics), ("sum_meta",) + click),
        ("  ▾", ("sum_meta", "tool_chev", tags["open"]) + click),
        ("  ▸", ("sum_meta", "tool_chev", tags["closed"]) + click),
        ("\n", ("sum_meta",) + click),
    ]
    parts.extend((f"{line}\n", ("sum_detail", tags["body"])) for line in format_run_stats(metrics))
    return parts


_FOLD_MARKERS: re.Pattern[str] = re.compile(r"  [▾▸]")


def plain_transcript(raw: str) -> str:
    """
    Transkript metnini panoya kopyalanacak düz metne çevirir: her katlama bölümünde iki biçimde
    de bulunan ▾/▸ işaretleri atılır, sekme (araç satırı hizası) boşluğa döner. Saf.
    """
    return _FOLD_MARKERS.sub("", raw).replace("\t", "  ")


def sent_time_text(stamp: str) -> str:
    """
    Kayıtlı ISO gönderim zamanını yerel saat olarak 'HH:MM' biçiminde gösterir. Kullanıcı mesajının
    altındaki not bunu kullanır; okunamayan değer boş metne düşer (eski kayıtlar saatsiz kalır). Saf.
    """
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%H:%M")
    except ValueError:
        return ""


# --- Kenar çubuğu: tarihe göre gruplama, satır görünümü ve durum noktası ---
GROUP_TODAY: str = "Bugün"
GROUP_YESTERDAY: str = "Dün"
GROUP_WEEK: str = "Önceki 7 gün"
GROUP_OLDER: str = "Daha eski"
GROUP_ORDER: Tuple[str, ...] = (GROUP_TODAY, GROUP_YESTERDAY, GROUP_WEEK, GROUP_OLDER)
# Satırın sol sütunundaki durum noktası türleri; yalnız gerçekten bilinen durum gösterilir.
DOT_RUNNING: str = "running"
DOT_FAILED: str = "failed"
# Satır sol sütununun renk tonu: durum noktaları ve seçim modundaki işaretler.
TONE_NONE: str = "none"
TONE_CHECKED: str = "checked"
TONE_UNCHECKED: str = "unchecked"


class ChatGroup(TypedDict):
    """Kenar çubuğunda aynı başlık altında listelenen sohbetler (kaynak sırası korunur)."""
    title: str
    chats: List[ChatSummary]


def chat_group_title(updated_at: datetime, now: datetime) -> str:
    """
    Sohbetin grubu: `now`'ın saat diliminde takvim günü farkına göre Bugün, Dün, Önceki 7 gün
    (2-7 gün önce) ya da Daha eski. Gelecek tarihli (saat sapması) sohbet Bugün sayılır. Saf.
    """
    days: int = (now.date() - updated_at.astimezone(now.tzinfo).date()).days
    if days <= 0:
        return GROUP_TODAY
    if days == 1:
        return GROUP_YESTERDAY
    return GROUP_WEEK if days <= 7 else GROUP_OLDER


def group_chats_by_date(chats: Sequence[ChatSummary], now: datetime) -> List[ChatGroup]:
    """
    Sohbetleri başlıklara böler; başlık sırası sabittir (Bugün, Dün, Önceki 7 gün, Daha eski),
    boş başlık atlanır, grup içinde girdi sırası korunur. Zaman damgası ISO 8601 olmalıdır
    (ui.chats.load_catalog doğrular). Saf.
    """
    buckets: Dict[str, List[ChatSummary]] = {title: [] for title in GROUP_ORDER}
    for chat in chats:
        buckets[chat_group_title(datetime.fromisoformat(chat["updated_at"]), now)].append(chat)
    return [{"title": title, "chats": buckets[title]} for title in GROUP_ORDER if buckets[title]]


def chat_dot(outcome: Optional[str], running: bool) -> Optional[str]:
    """
    Sohbet satırının durum noktası: görev çalışıyorsa 'running', son görev başarısızsa 'failed',
    aksi halde nokta yok (None). Başarı ve durdurma nokta göstermez. Saf.
    """
    if running:
        return DOT_RUNNING
    return DOT_FAILED if outcome == OUTCOME_FAILED else None


class ChatRowLook(TypedDict):
    """Sohbet satırının yalnız görünümü belirleyen değerleri; değişmediyse widget'a dokunulmaz."""
    label: str
    marker: str
    tone: str
    highlighted: bool
    bright: bool


def chat_row_look(title: str, active: bool, checked: bool, select_mode: bool, dot: Optional[str]) -> ChatRowLook:
    """
    Satırın başlığını, sol sütun işaretini (durum noktası ya da seçim kutusu) ve vurgusunu hesaplar.
    Seçim modunda işaret ☐/☑'dir ve durum noktası gösterilmez. Saf.
    """
    if select_mode:
        marker, tone = ("☑", TONE_CHECKED) if checked else ("☐", TONE_UNCHECKED)
    elif dot is not None:
        marker, tone = "●", dot
    else:
        marker, tone = "", TONE_NONE
    # Kısaltma noktasından önce boşluk kalmasın diye clip_text yerine burada kesilir.
    label: str = title if len(title) <= CHAT_TITLE_MAX_CHARS else title[:CHAT_TITLE_MAX_CHARS].rstrip() + "…"
    return {"label": label, "marker": marker, "tone": tone,
            "highlighted": checked or (active and not select_mode), "bright": checked or active}


def path_within(widget_path: str, container_path: str) -> bool:
    """
    Tk pencere yolu kapsayıcının kendisi ya da altındaki bir çocuk mu ('.a.b' için '.a.b.c' evet, '.a.bc' hayır)?
    Satırın kendi çocuklarına geçen fare satırdan çıkmış sayılmasın diye kullanılır. Saf.
    """
    return widget_path == container_path or widget_path.startswith(container_path + ".")


# --- Composer ve yanıt penceresi ---

def composer_lines(content_lines: int) -> int:
    """Composer giriş alanının görünen satır sayısı: içerik kadar, COMPOSER_MIN_LINES ile COMPOSER_MAX_LINES arasında. Saf."""
    return max(COMPOSER_MIN_LINES, min(COMPOSER_MAX_LINES, content_lines))


def format_input_deadline(timeout_seconds: float, requested_at: datetime) -> str:
    """
    Yanıt bekleyen isteğin süresini, son saatini (isteğin yapıldığı andan) ve süre dolunca ne olacağını
    söyler. Pencere gizlilik nedeniyle ertelenip sonra açılsa da son saat gerçek olanı gösterir. Saf.
    """
    span: str = f"{round(timeout_seconds)} sn" if timeout_seconds < 60 else f"{round(timeout_seconds / 60)} dk"
    deadline: datetime = requested_at + timedelta(seconds=timeout_seconds)
    return f"Yanıt süresi {span} · son saat {deadline:%H:%M} · süre dolarsa işlem yapılmaz."
