"""
Gizlilik (TCC) izinlerini kesinleştiren komut satırı aracı.

Kullanım:
    uv run python permissions.py            # izin durumu + izin verilecek uygulama
    uv run python permissions.py --request  # ekran kaydı iznini sistem istemiyle sorar

macOS ekran kaydı iznini süreci başlatan uygulamaya verir (Terminal'den çalıştırıldıysa
Terminal'e, arka plan servisinde python ikilisine). Bu araç, sistem listesinde neye izin
verileceğini uygulama adı, bundle kimliği ve tam python yoluyla yazar; `--request` ile
macOS'un kendi izin istemini gösterip uygulamayı listeye kaydettirir.
"""
import argparse
import platform
import sys
from typing import Dict

import tools


def report() -> str:
    """Ekran kaydı izninin durumunu ve izin verilecek hedefi tek metinde toplar."""
    owner: Dict[str, str] = tools.screen_capture_owner()
    granted: bool = tools.screen_capture_granted()
    lines: list[str] = [
        f"macOS          : {platform.mac_ver()[0]}",
        f"Ekran kaydı    : {'izinli' if granted else 'İZİN YOK'}",
        f"Sorumlu uygulama: {owner['app_name'] or owner['parent_process'] or 'bilinmiyor'}",
        f"bundle kimliği : {owner['bundle_id'] or 'yok (arka plan süreci)'}",
        f"python ikilisi : {owner['python']}",
        f"Ayarlar sayfası: open \"{tools.SCREEN_SETTINGS_URL}\"",
    ]
    if not granted:
        lines.append("")
        lines.append(tools.screen_capture_help())
    return "\n".join(lines)


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="OmniAgent gizlilik izinleri")
    parser.add_argument("--request", action="store_true",
                        help="Eksik ekran kaydı iznini macOS istemiyle sor ve uygulamayı listeye kaydet")
    arguments: argparse.Namespace = parser.parse_args()
    if arguments.request and not tools.screen_capture_granted(request=True):
        print("Ekran kaydı izni hâlâ yok. Açılan istemi onaylayın ya da yukarıdaki adımları izleyin.",
              file=sys.stderr)
    print(report())


if __name__ == "__main__":
    main()
