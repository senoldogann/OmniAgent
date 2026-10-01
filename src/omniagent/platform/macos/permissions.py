"""
Gizlilik (TCC) izinlerini kesinleştiren komut satırı aracı.

Kullanım:
    .venv/bin/omniagent-permissions            # izin durumu + izin verilecek uygulama
    .venv/bin/omniagent-permissions --request  # ekran kaydı iznini sistem istemiyle sorar

macOS ekran kaydı ve erişilebilirlik iznini süreci başlatan uygulamaya verir (Terminal'den çalıştırıldıysa
Terminal'e, arka plan servisinde python ikilisine). Bu araç, sistem listesinde neye izin
verileceğini uygulama adı, bundle kimliği ve tam python yoluyla yazar; `--request` ile
macOS'un kendi izin istemini gösterip uygulamayı listeye kaydettirir.
"""
import argparse
import platform
import sys
from typing import Dict

from omniagent import tools
from omniagent.tools import gui_input
from pathlib import Path


def accessibility_granted() -> bool:
    """Sentetik fare/klavye olayları için erişilebilirlik izni var mı (izin sorulmaz)."""
    try:
        return bool(gui_input.AX.AXIsProcessTrusted())
    except Exception:
        return False


MESSAGES_DATABASE: Path = Path.home() / "Library" / "Messages" / "chat.db"


def messages_database_status(path: Path) -> str:
    """iMessage köprüsünün okuduğu Messages veritabanı bu süreçten okunabiliyor mu (Tam Disk Erişimi)?"""
    try:
        with path.open("rb") as database:
            database.read(1)
    except FileNotFoundError:
        return "veritabanı yok (Messages bu kullanıcıda hiç açılmamış)"
    except PermissionError:
        return "İZİN YOK (Tam Disk Erişimi: iMessage köprüsü için gerekli)"
    return "izinli"


def report(*, requested: bool = False) -> str:
    """Ekran kaydı ve erişilebilirlik izninin durumunu ve izin verilecek hedefi tek metinde toplar."""
    owner: Dict[str, str] = tools.screen_capture_owner()
    granted: bool = tools.screen_capture_granted()
    accessible: bool = accessibility_granted()
    lines: list[str] = [
        f"macOS          : {platform.mac_ver()[0]}",
        f"Ekran kaydı    : {'izinli' if granted else 'İZİN YOK'}",
        f"Erişilebilirlik: {'izinli' if accessible else 'İZİN YOK (fare/klavye olayları düşer)'}",
        f"Tam Disk Erişimi: {messages_database_status(MESSAGES_DATABASE)}",
        f"Sorumlu uygulama: {owner['app_name'] or owner['parent_process'] or 'bilinmiyor'}",
        f"bundle kimliği : {owner['bundle_id'] or 'yok (arka plan süreci)'}",
        f"python ikilisi : {owner['python']}",
        f"Ayarlar sayfası: open \"{tools.SCREEN_SETTINGS_URL}\"",
    ]
    if not granted:
        lines.append("")
        lines.append(tools.screen_capture_help(requested=requested))
    if not accessible:
        lines.append("")
        lines.append(tools.accessibility_help())
    return "\n".join(lines)


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="OmniAgent gizlilik izinleri")
    parser.add_argument("--request", action="store_true",
                        help="Eksik ekran kaydı iznini macOS istemiyle sor ve uygulamayı listeye kaydet")
    arguments: argparse.Namespace = parser.parse_args()
    if arguments.request and not tools.screen_capture_granted(request=True):
        print("Ekran kaydı izni hâlâ yok. Açılan istemi onaylayın ya da yukarıdaki adımları izleyin.",
              file=sys.stderr)
    print(report(requested=arguments.request))


if __name__ == "__main__":
    main()
