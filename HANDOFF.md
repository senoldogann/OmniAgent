# OmniAgent — Devir Notu (2026-09-23)

Kurallar, mimari ve performans kararlarının tek kaynağı `AGENTS.md`'dir; bu not yalnızca
kaldığı yerden devam etmek için gereken durumu içerir.

## Durum
- Dal: `perf/hiz-dogruluk-arayuz` (master'dan). Commitler: önceki ajanın commit edilmemiş işi
  (`9c390f3`), hız/doğruluk düzeltmeleri (`b8157e2`), akışlı arayüz (son commit).
- `requirements.txt` kaldırıldı → `pyproject.toml` + `uv.lock` (`uv sync`).
- `cognitive_memory.json` sıfırlandı (eski hâli `.omni_backups/cognitive_memory.json.*.bak`).

## Çalıştırma
```bash
.venv/bin/python ui.py                                 # akışlı arayüz
.venv/bin/python main.py "<hedef>"                     # CLI (terminale akış)
.venv/bin/python -m pytest tests/ -q                   # canlı API testi: OMNI_LIVE_API_TEST=1
.venv/bin/python benchmark.py --runs 3 --concurrency 3 # hız + doğruluk ölçümü
```

## Ölçüm (benchmark.py, 9 senaryo × 3 koşu, eşzamanlılık 3)
| Sürüm | Başarı | Medyan | Ortalama |
|---|---|---|---|
| Önce (önceki ajanın hâli) | 19/27 | 17,3sn | 17,8sn |
| Sonra, `opencode` (düşünme kapalı, varsayılan) | 27/27 | 4,5sn | 4,6sn |
| Sonra, `opencode-think` (düşünme açık) | 27/27 | 6,6sn | 7,2sn |

## Açık konular
- Hatalardan kalıcı öğrenme kaldırıldı (ölçümde zararlıydı); yeniden eklenecekse `AGENTS.md`
  Hedefler bölümündeki koşullarla.
- Electron/web içerikli uygulamalarda AX ağacı yalnızca pencere çerçevesini gösterebiliyor
  (Hermes: 3 öğe; `AXManualAccessibility` denendi, fark yaratmadı) → bu uygulamalarda
  `take_screenshot` gerekir.
- Chromium ilk açılışı ~7sn (soğuk başlatma); statik sayfalar için `fetch_raw` tercih edilmeli.
