# OmniAgent

macOS üzerinde yerel araçlar, açık Chrome, web, model sağlayıcıları ve isteğe bağlı entegrasyonlarla görev yürüten ajan.

## Başlangıç

```sh
uv sync
.venv/bin/python ui.py
```

Komut satırı: `.venv/bin/python main.py "<hedef>"`. Telegram köprüsü için [kurulum kılavuzuna](docs/TELEGRAM.md) bakın.

## Dizinler

| Yol | İçerik |
| --- | --- |
| `*.py` (kök) | Düz Python modülleri ve doğrudan çalıştırılan giriş noktaları. Import ve launchd yolları nedeniyle burada kalırlar. |
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

Doğrulama: `.venv/bin/python -m pytest tests/ -q`. Canlı model veya GUI benchmark'ı ayrı koşullarda çalıştırın.
