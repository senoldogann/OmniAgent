"""OmniAgent kalıcı veri ve çalışma yolu sözleşmesi."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict


APP_NAME = "OmniAgent"


def data_root() -> Path:
    """Kalıcı kullanıcı verisinin canonical kökünü döndürür."""
    configured = os.environ.get("OMNI_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library" / "Application Support" / APP_NAME


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


def backups_dir() -> Path:
    """Dosya düzenleme/yazma araçlarının zaman damgalı yedekleri."""
    return data_root() / "backups"


def legacy_project_root() -> Path:
    """Src-layout öncesi canlı runtime dosyalarının bulunduğu proje kökünü döndürür."""
    return Path(__file__).resolve().parents[2]


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
