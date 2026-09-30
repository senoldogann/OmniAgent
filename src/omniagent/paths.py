"""OmniAgent kalıcı veri ve çalışma yolu sözleşmesi."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict


APP_NAME = "OmniAgent"


def data_root() -> Path:
    """
    Kalıcı kullanıcı verisinin canonical kökünü döndürür; ilk çağrıda dizini 0700 izinle
    oluşturur. Kök 0700 olduğu için altındaki her alt dizin/dosya kendi izin biti ne olursa
    olsun aynı makinedeki başka kullanıcıya kapalıdır (üst dizine erişim gerekir).
    """
    configured = os.environ.get("OMNI_DATA_DIR", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / "Library" / "Application Support" / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def state_file() -> Path:
    """Epizodik görev geçmişi dosyası."""
    return data_root() / "cognitive_memory.json"


def experience_file() -> Path:
    """Doğrulanmış hata-kurtarma deneyimleri."""
    return data_root() / "experience_memory.json"


def user_memory_file() -> Path:
    """Kullanıcının açıkça kaydettiği kalıcı tercihler."""
    return data_root() / "user_memory.json"


def checkpoints_dir() -> Path:
    """Yarım görevlerin atomik kontrol noktaları."""
    return data_root() / "checkpoints"


def schedules_file() -> Path:
    """Zamanlanmış/yinelenen görev planları (Telegram köprüsü çalıştırır)."""
    return data_root() / "schedules.json"


def telegram_settings_file() -> Path:
    """Eşleştirilmiş Telegram sohbeti; varlığı zamanlanmış görevleri çalıştıracak köprünün kurulu olduğunu gösterir."""
    return data_root() / "telegram.json"


def imessage_settings_file() -> Path:
    """Eşleşmiş iMessage kanalının ayarları; varlığı köprünün kurulup eşleştiğini gösterir."""
    return data_root() / "imessage.json"


def imessage_pairing_file() -> Path:
    """Kurulumun servise bıraktığı tek kullanımlık eşleştirme isteği (kod, son geçerlilik, taslak ayarlar)."""
    return data_root() / "imessage-pairing.json"


def companion_db_file() -> Path:
    """iMessage yol arkadaşının SQLite deposu: mesaj arşivi, etkinlik günlüğü, durum."""
    return data_root() / "companion.db"


def persona_file() -> Path:
    """Kullanıcının düzenleyebildiği karakter tanımı (isim, kişilik, konuşma tarzı)."""
    return data_root() / "persona.md"


def imessage_history_file() -> Path:
    """iMessage'dan devredilen son görevlerin sohbet kayıtları (Exchange listesi)."""
    return data_root() / "imessage-history.json"


def backups_dir() -> Path:
    """Dosya düzenleme/yazma araçlarının zaman damgalı yedekleri."""
    return data_root() / "backups"


def workspace_dir() -> Path:
    """
    Görev kaynak deposunu açıkça hedeflemediğinde write_file/take_screenshot'ın vardığı
    varsayılan çıktı dizini. Model bir yol vermeden salt dosya adı verdiğinde (ör. "leads.md")
    bu göreli ad artık süreç çalışma dizinine (köprüde proje kökü) değil buraya çözülür;
    kaynak deposu görev dışı üretilen dosyalarla kirlenmez.
    """
    return data_root() / "workspace"


def resolve_output_path(filename: str, allow_source_relative: bool) -> Path:
    """
    Modelin verdiği çıktı dosya adını (ekran görüntüsü, yazılan dosya) gerçek yola çözer.
    Aracın yazdığı yer ile ekin ve sohbet kartının okuduğu yer aynı kuraldan çıkar. "~"
    önce genişler; mutlak yol olduğu gibi kalır. Göreli adda allow_source_relative
    (görev kaynak deposunu hedefliyor) ise yol göreli kalır ve çağıran süreç çalışma
    dizinine bağlar; değilse ad workspace_dir() altına bağlanır. Girdiyi değiştirmez;
    dosya sistemine yalnız workspace_dir() üzerinden veri kökünü oluşturarak dokunur.
    """
    expanded: Path = Path(filename).expanduser()
    if expanded.is_absolute() or allow_source_relative:
        return expanded
    return workspace_dir() / expanded


def project_root() -> Path:
    """Kaynak deposunun kökü (düzenlenebilir kurulumda git çalışma ağacı)."""
    return Path(__file__).resolve().parents[2]


def legacy_project_root() -> Path:
    """Src-layout öncesi canlı runtime dosyalarının bulunduğu proje kökünü döndürür."""
    return project_root()


def _copy_missing_tree(source: Path, destination: Path) -> bool:
    """Bir legacy dizini hedefte var olan dosyaları ezmeden birleştirir."""
    if not source.is_dir():
        return False
    copied = False
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not item.is_file() or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied = True
    return copied


def migrate_legacy_runtime_data(
    legacy_root: Path | None = None,
    target_root: Path | None = None,
) -> Dict[str, bool]:
    """
    Src-layout öncesi canlı kullanıcı verisini canonical data root'a kayıpsız kopyalar.

    Mevcut hedef dosyaları asla ezmez ve legacy kaynakları silmez; bu yüzden çağrı
    idempotenttir. Eski .omni_runs/.omni_backups dizinleri yeni checkpoints/backups
    adlarına eşlenir.
    """
    source_root = (legacy_root or legacy_project_root()).expanduser()
    destination_root = (target_root or data_root()).expanduser()
    destination_root.mkdir(parents=True, exist_ok=True)
    result: Dict[str, bool] = {}
    for name in ("cognitive_memory.json", "user_memory.json", "experience_memory.json"):
        source = source_root / name
        destination = destination_root / name
        copied = bool(source.is_file() and not destination.exists())
        if copied:
            shutil.copy2(source, destination)
        result[name] = copied
    result[".omni_runs"] = _copy_missing_tree(source_root / ".omni_runs", destination_root / "checkpoints")
    result[".omni_backups"] = _copy_missing_tree(source_root / ".omni_backups", destination_root / "backups")
    return result
