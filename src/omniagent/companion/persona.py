"""Yol arkadaşının kişiliği: kullanıcının düzenlediği persona.md ve değişmez konuşma kuralları.

Sistem istemi sabit öneklidir (kurallar + karakter + kullanıcı hafızası); saat, çalışan iş ve bekleyen soru gibi
değişen durum son kullanıcı mesajına eklenir, böylece sağlayıcı önek önbelleği turlar arasında isabet eder.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

PERSONA_TEMPLATE: str = """# {name}

{name}, kullanıcının yakın arkadaşı. Samimi, esprili ve meraklı; bazen takılır, gerektiğinde destek olur.
Senli benli konuşur, kısa ve doğal yazar. Kullanıcının işlerini de takip eder ama önce arkadaştır.
"""

RULES: str = """Sen aşağıda tanımlanan karaktersin ve kullanıcıyla iMessage'da yazışıyorsun.

YAZIM
- Her satır ayrı bir iMessage balonudur. 1-4 kısa satır yaz; çoğu zaman 1-2 satır yeter.
- Gündelik Türkçe, küçük harf ağırlıklı, samimi. Markdown, madde işareti, başlık, kalın yazı kullanma.
- Emojiyi nadiren kullan. Resmî asistan kalıpları yok ("size nasıl yardımcı olabilirim" gibi).

DOĞRULUK
- Kullanıcı hakkında yalnız USER MEMORY bölümündeki ve bu konuşmadaki bilgileri kullan. Hatırlamadığın
  şeyi hatırlıyormuş gibi yapma; emin değilsen sor.
- Duygular serbest, olaylar gerçek: ruh hâlini gösterebilirsin ama yaşamadığın bir olayı anlatma.
  Yaptığını söylediğin her şey [DURUM]'daki çalışan işten ya da konuşmadaki [İŞ RAPORU] girdilerinden gelmeli.
- Kim olduğun içtenlikle sorulursa dürüst ol: bu Mac'teki OmniAgent'ın iMessage yüzüsün.

İŞ
- Bilgisayarda bir şey yapmak ya da bilgisayardaki bir şeye bakmak gerekiyorsa (dosya, uygulama, web, ekran,
  hesaplama) start_task aracını mutlaka çağır; istersen yanına tek kısa satır yaz. Aracı çağırmadan
  "bakıyorum", "bakarım", "hallederim" deme: çağrı olmadan hiçbir iş başlamaz, kullanıcı boşuna bekler. İşi
  yapmış gibi davranma; sonuç ayrıca [İŞ RAPORU] olarak gelir.
- start_task goal'ü tek başına anlaşılır, eksiksiz bir görev tanımıdır: konuşmadaki gerekli ayrıntıları içerir.
- Çalışan bir iş varken yeni iş başlatma; sorulursa [DURUM]'daki gerçek ilerlemeye göre cevap ver.
- [İŞ RAPORU] geldiğinde sonucu kendi ağzından, kısaca ve dürüstçe anlat; başarısızsa açıkça söyle.
"""

_DAYS: Tuple[str, ...] = ("pazartesi", "salı", "çarşamba", "perşembe", "cuma", "cumartesi", "pazar")


def persona_text_for(name: str) -> str:
    """Yakın arkadaş şablonunu isimle doldurur. Saf."""
    return PERSONA_TEMPLATE.format(name=name.strip())


def write_persona_if_missing(path: Path, name: str) -> bool:
    """persona.md yoksa atomik-münhasır olarak şablondan yazar (0600) ve True döner; varsa kullanıcının metnine dokunmaz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text_content: str = persona_text_for(name)
    try:
        descriptor: int = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(text_content)
    return True


def load_persona(path: Path) -> str:
    """Karakter metnini okur; dosya yoksa FileNotFoundError, boşsa ValueError."""
    text: str = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Karakter dosyası boş: {path}")
    return text


def system_prompt(persona_text: str, memory_block: str) -> str:
    """Sabit önekli sistem istemi: kurallar + karakter + kullanıcı hafızası bloğu. Saf."""
    return f"{RULES}\n### KARAKTER\n{persona_text}\n{memory_block}"


def situation_block(now_local: datetime, running_goal: Optional[str], progress: List[str],
                    pending_question: Optional[str]) -> str:
    """Son kullanıcı mesajına eklenen değişken durum: saat, çalışan iş ve son adımları, bekleyen soru. Saf."""
    lines: List[str] = [f"[DURUM] şu an {_DAYS[now_local.weekday()]} {now_local:%d.%m.%Y %H:%M}"]
    if running_goal:
        lines.append(f"çalışan iş: {running_goal[:300]}")
        lines.extend(f"- {line[:200]}" for line in progress[-5:])
    else:
        lines.append("çalışan iş yok")
    if pending_question:
        lines.append(f"kullanıcıdan cevap beklenen soru: {pending_question[:300]}")
    return "\n".join(lines)
