"""
OmniAgent arayüzünün tasarım jetonları: renk, tipografi, aralık ve sütun ölçüleri tek yerde.

Sıcak-nötr koyu palet (Claude masaüstü uygulamasının Code sekmesine yakın); koyu tema sabittir.
Bu modül Tk'ye bağlı değildir: transkript etiket stilleri saf veri olarak üretilir, arayüz
(ui/app.py) bunları `tag_configure` ile uygular. Yerleşim ölçüleri (sütun genişliği, kabarcık
sınırı, kenar çubuğu, composer) da burada durur; üst şerit, durum satırı ve composer transkriptle
aynı sütuna (column_side/column_width) hizalanır.
"""
from __future__ import annotations

from typing import Dict, Tuple, TypedDict

# --- Renk paleti (sıcak-nötr koyu) ---
BG: str = "#141413"
SURFACE: str = "#1C1C1A"
SURFACE_RAISED: str = "#262624"
# Vurgulu olmayan kenar çubuğu satırının üzerine gelince aldığı zemin: SURFACE ile SURFACE_RAISED arası.
SURFACE_HOVER: str = "#21211F"
COMMAND_BG: str = "#1A1A18"
BORDER: str = "#34332F"
# Araç grubundaki ince ayraç çizgileri: kontrol kenarlığından (BORDER) daha sönük.
BORDER_SOFT: str = "#2A2927"
TEXT: str = "#ECEAE3"
TEXT_DIM: str = "#A3A199"
TEXT_FAINT: str = "#6E6C66"
ACCENT: str = "#D97757"
ACCENT_HOVER: str = "#E48A6C"
ACCENT_DIM: str = "#40312F"
# Vurgu renginin sönük hâli: çalışan sohbetin durum noktası ACCENT ile bu renk arasında yavaşça yanıp söner.
ACCENT_MUTED: str = "#7A4938"
SHINE: str = "#F6C4AE"
SUCCESS: str = "#6FB58A"
ERROR: str = "#EE6B7B"
WARNING: str = "#E5B85C"
INFO: str = "#8FB3E8"
# Satır içi kod parçasının yazı rengi (zemin: SURFACE_RAISED).
CODE_TEXT: str = "#E8846B"

# --- Tipografi ---
MONO_FAMILY: str = "Menlo"
# Görünüm ayarındaki varsayılan yazı boyutu; transkript boyutları bu tabana göre kayar.
BASE_FONT_SIZE: int = 13
# Asistan metni ve kullanıcı kabarcığı (okunan asıl içerik) arayüz metninden iki punto büyüktür.
READING_SIZE: int = 15

# --- Yerleşim ---
# İçerik sütunu en çok bu kadar geniş olur ve pencerede ortalanır.
COLUMN_MAX_WIDTH: int = 880
# Dar pencerede sütunun iki yanında kalan en küçük boşluk.
COLUMN_MIN_SIDE: int = 16
# Kullanıcı kabarcığının sütun genişliğine oranı (üst sınır) ve şekli.
BUBBLE_MAX_RATIO: float = 0.85
BUBBLE_RADIUS: int = 16
BUBBLE_PAD_X: int = 16
BUBBLE_PAD_Y: int = 11
BUBBLE_MIN_TEXT_WIDTH: int = 160
# Kabarcık bu kadar satırı aşarsa 'Daha fazla' ile katlanır ve BUBBLE_COLLAPSED_LINES satır gösterir.
BUBBLE_COLLAPSE_LINES: int = 12
BUBBLE_COLLAPSED_LINES: int = 8
# Kenar çubuğu: genişlik, sohbet başlığının satırda ve üst şeritte gösterilen en çok karakteri
# (uzunsa '…' ile kısalır) ve satırın sol sütunu (durum noktası / seçim kutusu) genişliği.
SIDEBAR_WIDTH: int = 264
CHAT_TITLE_MAX_CHARS: int = 25
HEADER_TITLE_MAX_CHARS: int = 52
CHAT_MARKER_WIDTH: int = 22
# Composer: içerik sütunu genişliğinde tek yuvarlak kart; giriş alanı 2 ile 10 satır arası büyür.
COMPOSER_RADIUS: int = 22
COMPOSER_MIN_LINES: int = 2
COMPOSER_MAX_LINES: int = 10
COMPOSER_PAD_X: int = 18
# Composer'a odaklanınca kart kenarlığı (BORDER'dan biraz açık).
COMPOSER_FOCUS_BORDER: str = "#4B4944"

FontSpec = Tuple[str, int, str]


class TagStyle(TypedDict, total=False):
    """Tk metin etiketinin `tag_configure` seçeneklerinden kullandıklarımız."""
    foreground: str
    background: str
    font: FontSpec
    lmargin1: int
    lmargin2: int
    rmargin: int
    spacing1: int
    spacing2: int
    spacing3: int
    justify: str
    tabs: Tuple[int, ...]
    underline: bool
    elide: bool


def column_side(available_width: int) -> int:
    """Kullanılabilir genişlikte içerik sütununun iki yanındaki boşluk (px). Saf."""
    return max(COLUMN_MIN_SIDE, (available_width - COLUMN_MAX_WIDTH) // 2)


def column_width(available_width: int) -> int:
    """Kullanılabilir genişlikte içerik sütununun genişliği (px). Saf."""
    return max(1, available_width - 2 * column_side(available_width))


def transcript_tag_styles(ui_family: str, delta: int) -> Dict[str, TagStyle]:
    """
    Transkript Text'inin tüm etiket stillerini üretir. Sözlük sırası etiket önceliğidir: sonra
    gelen etiket öncekini geçersiz kılar (ör. md_* etiketleri assistant'ı, satır parçaları
    satır stilini). `delta`, görünüm ayarındaki yazı boyutunun tabana (13) farkıdır. Saf.
    """
    def ui(size: int, weight: str) -> FontSpec:
        return (ui_family, size + delta, weight)

    def mono(size: int, weight: str) -> FontSpec:
        return (MONO_FAMILY, size + delta, weight)

    # Eski kayıtlı sohbetlerin (⏺/⎿ düzeni) okunabilir kalması için kalan girinti ölçüleri.
    legacy_indent: int = 16
    legacy_output: int = 36
    return {
        # --- Karşılama (eski kayıtlarda kalan parçalar) ---
        "welcome_mark": {"foreground": ACCENT, "font": mono(28, "bold"), "spacing1": 32},
        "welcome_title": {"foreground": TEXT, "font": ui(22, "bold"), "spacing1": 16},
        "welcome_dim": {"foreground": TEXT_DIM, "font": ui(13, "normal"), "spacing1": 4, "spacing3": 5},
        "welcome_example": {"foreground": TEXT_FAINT, "font": mono(11, "normal"),
                            "spacing1": 10, "spacing3": 4},
        # --- Boşluklar ---
        "gap": {"font": (MONO_FAMILY, 6, "normal")},
        "gap_lg": {"font": (MONO_FAMILY, 13, "normal")},
        # --- Asistan metni (kabarcıksız düz metin) ve akış imleci ---
        "assistant": {"foreground": TEXT, "font": ui(READING_SIZE, "normal"), "spacing2": 4, "rmargin": 6},
        "cursor": {"foreground": ACCENT},
        "reasoning_head": {"foreground": TEXT_FAINT, "font": mono(11, "italic"), "spacing1": 6},
        "reasoning": {"foreground": TEXT_FAINT, "font": mono(11, "italic")},
        # --- Kullanıcı mesajı: metin gizli kaynaktır, görünen kabarcık gömülü pencerededir ---
        "user_msg": {"elide": True},
        "user_line": {"justify": "right", "spacing1": 2, "spacing3": 2},
        # --- Araç grubu: başlık, ince ayraçlar, satırlar ve açılır ayrıntı ---
        "tool_head": {"foreground": TEXT_DIM, "font": ui(14, "normal"), "spacing1": 10, "spacing3": 8},
        "tool_chev": {"foreground": TEXT_FAINT, "font": ui(12, "normal")},
        "tool_rule": {"background": BORDER_SOFT, "font": (MONO_FAMILY, 1, "normal")},
        # Durum glifinin genişliği farklı olabilir; ad, sekme durağıyla her satırda aynı hizaya oturur.
        "tool_row": {"foreground": TEXT_DIM, "font": ui(14, "normal"), "spacing1": 8, "spacing3": 8,
                     "lmargin1": 14, "lmargin2": 36, "rmargin": 10, "tabs": (36,)},
        "row_label": {"foreground": TEXT},
        "row_preview": {"foreground": TEXT_DIM},
        "row_time": {"foreground": TEXT_FAINT, "font": mono(11, "normal")},
        "row_error": {"foreground": ERROR},
        "glyph_ok": {"foreground": SUCCESS},
        "glyph_error": {"foreground": ERROR},
        "tool_spin": {"foreground": ACCENT},
        "cmd_block": {"foreground": TEXT, "background": COMMAND_BG, "font": mono(12, "normal"),
                      "lmargin1": 30, "lmargin2": 44, "rmargin": 14,
                      "spacing1": 1, "spacing2": 2, "spacing3": 1},
        "cmd_prompt": {"foreground": TEXT_FAINT, "background": COMMAND_BG},
        "cmd_first": {"spacing1": 9},
        "cmd_last": {"spacing3": 9},
        "out_block": {"foreground": TEXT_DIM, "font": mono(12, "normal"),
                      "lmargin1": 30, "lmargin2": 30, "rmargin": 14,
                      "spacing1": 1, "spacing2": 1, "spacing3": 1},
        "out_error": {"foreground": ERROR},
        "out_note": {"foreground": TEXT_FAINT},
        "out_more": {"foreground": INFO, "spacing1": 4, "spacing3": 6},
        # --- Görev özeti (tek satır, sessiz) ---
        "sum_ok": {"foreground": SUCCESS, "font": ui(13, "bold"), "spacing1": 12, "spacing3": 6},
        "sum_error": {"foreground": ERROR, "font": ui(13, "bold"), "spacing1": 12, "spacing3": 6},
        "sum_meta": {"foreground": TEXT_FAINT, "font": ui(13, "normal")},
        "sum_detail": {"foreground": TEXT_FAINT, "font": mono(12, "normal"), "lmargin1": 2,
                       "lmargin2": 2, "spacing3": 2},
        # --- Bildirimler ---
        "notice_info": {"foreground": INFO, "font": mono(12, "normal"), "spacing1": 3},
        "notice_warning": {"foreground": WARNING, "font": mono(12, "normal"), "spacing1": 3},
        "notice_error": {"foreground": ERROR, "font": mono(12, "normal"), "spacing1": 3},
        # --- Markdown ---
        "md_h1": {"font": ui(22, "bold"), "spacing1": 16, "spacing3": 6},
        "md_h2": {"font": ui(19, "bold"), "spacing1": 14, "spacing3": 4},
        "md_h3": {"font": ui(16, "bold"), "spacing1": 10, "spacing3": 3},
        "md_bold": {"font": ui(READING_SIZE, "bold")},
        "md_italic": {"font": ui(READING_SIZE, "italic")},
        "md_code": {"font": mono(13, "normal"), "background": SURFACE_RAISED, "foreground": CODE_TEXT},
        "md_codeblock": {"font": mono(13, "normal"), "background": COMMAND_BG, "foreground": TEXT,
                         "lmargin1": 14, "lmargin2": 14, "rmargin": 14,
                         "spacing1": 1, "spacing2": 2, "spacing3": 1},
        "md_codeblock_first": {"spacing1": 9},
        "md_codeblock_last": {"spacing3": 9},
        "md_bullet": {"lmargin2": 16},
        "md_quote": {"foreground": TEXT_DIM, "lmargin1": 12, "lmargin2": 12},
        "md_rule": {"foreground": BORDER},
        "md_table": {"font": mono(12, "normal")},
        "md_table_head": {"font": mono(12, "bold"), "foreground": TEXT_DIM},
        "md_table_label": {"font": ui(13, "bold"), "foreground": ACCENT, "spacing1": 7},
        "md_table_value": {"lmargin1": 10, "lmargin2": 10},
        "md_table_gap": {"font": mono(4, "normal")},
        "md_link": {"foreground": INFO, "underline": True},
        # --- Eski kayıtlı sohbetlerin araç/hedef düzeni (yeni sohbetlerde kullanılmaz) ---
        "goal": {"foreground": TEXT, "background": SURFACE_RAISED, "lmargin1": 10,
                 "lmargin2": 10 + legacy_indent, "spacing1": 8, "spacing3": 8, "rmargin": 10},
        "goal_prompt": {"foreground": ACCENT, "background": SURFACE_RAISED, "font": mono(12, "bold")},
        "bullet_text": {"foreground": TEXT, "spacing1": 9},
        "bullet_streaming": {"foreground": TEXT_FAINT, "spacing1": 9},
        "bullet_running": {"foreground": ACCENT, "spacing1": 9},
        "bullet_ok": {"foreground": SUCCESS, "spacing1": 9},
        "bullet_error": {"foreground": ERROR, "spacing1": 9},
        "tool_name": {"foreground": TEXT, "font": mono(12, "bold")},
        "tool_args": {"foreground": TEXT_DIM},
        "command": {"foreground": TEXT, "background": COMMAND_BG, "lmargin1": legacy_indent,
                    "lmargin2": legacy_indent + 16},
        "command_prompt": {"foreground": ACCENT, "background": COMMAND_BG, "lmargin1": legacy_indent},
        "gutter": {"foreground": TEXT_FAINT, "lmargin1": legacy_indent, "lmargin2": legacy_output},
        "gutter_hidden": {"foreground": BG, "lmargin1": legacy_indent, "lmargin2": legacy_output},
        "output": {"foreground": TEXT_DIM, "font": mono(11, "normal"), "lmargin2": legacy_output},
        "output_error": {"foreground": ERROR, "font": mono(11, "normal"), "lmargin2": legacy_output},
        "meta": {"foreground": TEXT_FAINT, "font": mono(11, "normal")},
        "summary_ok": {"foreground": SUCCESS, "font": mono(12, "bold")},
        "summary_error": {"foreground": ERROR, "font": mono(12, "bold")},
        "summary_meta": {"foreground": TEXT_FAINT, "font": mono(11, "normal")},
    }
