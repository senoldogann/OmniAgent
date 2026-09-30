"""
Telegram canlı iş günlüğünün saf biçimi: her araç adımı emoji, eylem ve kısa hedefle tek satır olur;
komut çalıştıran adımlar kopyalanabilir kod bloğunda gösterilir, art arda aynı satır sayaçla birleşir.
İletinin Telegram'a gönderilmesi ve düzenlenmesi telegram.ActivityLog'dadır.
"""
import html
from typing import Dict, List, Tuple, TypedDict

from omniagent.config import redact
from omniagent.core.events import tool_label

# Satırdaki hedefin (yol, adres, uygulama) en çok gösterilen karakteri; fazlası "…" ile kesilir.
DETAIL_LIMIT: int = 70
# Kod bloğundaki komutun en çok gösterilen karakteri.
CODE_LIMIT: int = 400
# Araç → (emoji, eylem). Tanımsız araç genel satırla ve arayüz adıyla gösterilir.
ACTIVITY_STYLES: Dict[str, Tuple[str, str]] = {
    "read_file": ("📖", "Okuyor"),
    "write_file": ("✍️", "Yazıyor"),
    "edit_file": ("✏️", "Düzenliyor"),
    "web_search": ("🔎", "Arıyor"),
    "fetch_raw": ("🌐", "Getiriyor"),
    "browse_url": ("🌐", "Tarayıcı"),
    "chrome_active_tab": ("🌐", "Chrome sekmesi"),
    "take_screenshot": ("📸", "Ekran görüntüsü"),
    "capture_photo": ("📷", "Fotoğraf çekiyor"),
    "cua_get_app": ("🪟", "Uygulama"),
    "cua_get_ax_state": ("🔍", "Arayüzü okuyor"),
    "cua_snapshot": ("🔍", "Öğeleri listeliyor"),
    "cua_click": ("🖱", "Tıklıyor"),
    "cua_click_point": ("🖱", "Noktaya tıklıyor"),
    "cua_click_text": ("🖱", "Tıklıyor"),
    "cua_click_element": ("🖱", "Öğeye tıklıyor"),
    "smart_click": ("🖱", "Tıklıyor"),
    "cua_type_text": ("⌨️", "Yazıyor"),
    "cua_fill_field": ("⌨️", "Alanı dolduruyor"),
    "cua_set_text_element": ("⌨️", "Öğeyi dolduruyor"),
    "cua_submit_text": ("⌨️", "Yazıp gönderiyor"),
    "cua_press_key": ("⌨️", "Tuş"),
    "cua_scroll": ("📜", "Kaydırıyor"),
    "cua_read_scrollable": ("📜", "Baştan sona okuyor"),
    "run_action_sequence": ("🔁", "Eylem dizisi"),
    "process_list": ("📋", "Süreçler"),
    "discover_capabilities": ("🧩", "Bağlantı keşfi"),
    "ask_user": ("❔", "Soruyor"),
    "user_memory": ("🧠", "Hafıza"),
    "send_file": ("📎", "Dosya gönderiyor"),
    "schedule_task": ("⏰", "Planlıyor"),
    "report_goal_met": ("🎯", "Hedef bildirimi"),
}
# Önizlemesi komut olan araçlar: (emoji, kod bloğu dili); Telegram dil adını bloğun başlığı yapar.
CODE_TOOLS: Dict[str, Tuple[str, str]] = {
    "execute_shell": ("💻", "shell"),
    "execute_js": ("🟨", "javascript"),
}


class ActivityEntry(TypedDict):
    """Günlükte bir satır: aynı satırın art arda tekrarları tek satırda sayılır."""
    call_ids: List[str]
    html: str
    count: int
    failed: bool


def clip(text: str, limit: int) -> str:
    """Metni tek satıra indirir, sınırı aşarsa '…' ile keser. Saf."""
    flat: str = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def activity_html(name: str, preview: str) -> str:
    """Araç adımının günlük satırını güvenli HTML olarak üretir; kayıtlı sırlar maskelenir."""
    detail: str = redact(preview.strip())
    if name in CODE_TOOLS:
        emoji, language = CODE_TOOLS[name]
        head: str = f"{emoji} {html.escape(tool_label(name))}"
        if not detail:
            return head
        code: str = detail if len(detail) <= CODE_LIMIT else detail[: CODE_LIMIT - 1] + "…"
        return f'{head}\n<pre><code class="language-{language}">{html.escape(code)}</code></pre>'
    emoji, action = ACTIVITY_STYLES.get(name, ("🔧", tool_label(name)))
    shown: str = clip(detail, DETAIL_LIMIT)
    return f"{emoji} {html.escape(action)}" + (f" {html.escape(shown)}" if shown else "")


def append_entry(entries: List[ActivityEntry], call_id: str, line: str) -> List[ActivityEntry]:
    """
    Yeni adımı ekler; son satırla aynıysa ve o satır başarısız değilse sayacını artırır. Girdiyi
    değiştirmez. Saf.
    """
    if entries and entries[-1]["html"] == line and not entries[-1]["failed"]:
        last: ActivityEntry = entries[-1]
        merged: ActivityEntry = {"call_ids": [*last["call_ids"], call_id], "html": last["html"],
                                 "count": last["count"] + 1, "failed": False}
        return [*entries[:-1], merged]
    return [*entries, {"call_ids": [call_id], "html": line, "count": 1, "failed": False}]


def mark_failed(entries: List[ActivityEntry], call_id: str) -> List[ActivityEntry]:
    """call_id'yi taşıyan satırı başarısız işaretler; girdiyi değiştirmez. Saf."""
    return [
        {"call_ids": entry["call_ids"], "html": entry["html"], "count": entry["count"], "failed": True}
        if call_id in entry["call_ids"] else entry
        for entry in entries
    ]


def render_entries(entries: List[ActivityEntry]) -> str:
    """Satırları günlük iletisinin HTML'ine dizer: başarısız adım ⚠️, tekrar (×N). Saf."""
    return "\n".join(
        ("⚠️ " if entry["failed"] else "") + entry["html"]
        + (f" (×{entry['count']})" if entry["count"] > 1 else "")
        for entry in entries
    )


def elapsed_label(seconds: float) -> str:
    """Görev süresini dakika çözünürlüğünde gösterir ('<1 dk', '10 dk', '1 sa 5 dk'). Saf."""
    minutes: int = int(seconds // 60)
    if minutes < 1:
        return "<1 dk"
    if minutes < 60:
        return f"{minutes} dk"
    return f"{minutes // 60} sa {minutes % 60} dk"
