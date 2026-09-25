# OmniAgent

macOS üzerinde yerel araçlar, açık Chrome, web, model sağlayıcıları ve isteğe bağlı entegrasyonlarla görev yürüten ajan.

## Başlangıç

```sh
uv sync
.venv/bin/omniagent-ui
```

Komut satırı: `.venv/bin/omniagent "<hedef>"`. Telegram köprüsü (`.venv/bin/omniagent-telegram`) için
[kurulum kılavuzuna](docs/TELEGRAM.md) bakın. İzin tanısı: `.venv/bin/omniagent-permissions`.

## Dizinler

| Yol | İçerik |
| --- | --- |
| `src/omniagent/` | Uygulama paketi: ajan döngüsü (`app/`), araçlar (`tools/`), entegrasyonlar ve Telegram (`integrations/`), arayüz (`ui/`), macOS katmanı (`platform/macos/`). |
| `tests/` | Otomatik testler. |
| `docs/` | Kullanım, entegrasyon ve geliştirme belgeleri. |
| `docs/superpowers/` | Tarihsel tasarım ve uygulama planları. |
| `benchmarks/` | Tarihli, sürüm kontrolündeki ölçüm kayıtları. |
| `var/legacy/` | Eski, kodda kullanılmayan yerel çıktılar; Git dışında tutulur. |
| `.omni_backups/` | `write_file` aracının canlı yedekleri; silinmez. |

`cognitive_memory.json` ve varsa `user_memory.json`, mevcut uygulamanın canlı durum dosyalarıdır. `.venv/`, önbellekler, `.freebuff/` ve `var/` Git dışında kalır. Kullanıcı verilerini veya yedekleri temizlik sırasında silmeyin.

## Belgeler

- [Yetenekler ve sınırlar](docs/CAPABILITIES.md)
- [Entegrasyonlar ve Outlook](docs/INTEGRATIONS.md)
- [Telegram kurulumu](docs/TELEGRAM.md)
- [Geliştirme devir notu](docs/HANDOFF.md)
- [Ajan geliştirme kuralları](AGENTS.md)

Doğrulama: `.venv/bin/python -m pytest tests/ -q` (macOS dışında pyobjc çerçeveleri `tests/conftest.py` ile sahtelenir; Linux'ta `xvfb-run` ve `python3-tk` gerekir, Keychain/Quartz'a bağlı birkaç test yalnız macOS'ta geçer). Canlı model veya GUI benchmark'ı ayrı koşullarda çalıştırın.
