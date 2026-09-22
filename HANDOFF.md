# OmniAgent — Devir Notu (2026-09-23)

Kurallar, mimari ve performans kararlarının tek kaynağı `AGENTS.md`'dir; bu not yalnızca
kaldığı yerden devam etmek için gereken durumu içerir.

## Durum
- Değişiklikler çalışma ağacında, **commit edilmedi**. Önceki ajanın commit edilmemiş işi
  `git stash list` içindeki "omni: duzeltmeler oncesi temel durum" kaydında saklı
  (`git diff stash@{0}` ile karşılaştırılabilir, gerekmezse `git stash drop`).
- `requirements.txt` kaldırıldı → `pyproject.toml` + `uv.lock` (`uv sync`).
- `cognitive_memory.json` sıfırlandı (eski hâli `.omni_backups/cognitive_memory.json.*.bak`).

## Çalıştırma
```bash
.venv/bin/python main.py "<hedef>"                     # CLI
.venv/bin/python ui.py                                 # arayüz
.venv/bin/python -m pytest tests/ -q                   # 11 passed, 1 skipped (canlı API: OMNI_LIVE_API_TEST=1)
.venv/bin/python benchmark.py --runs 3 --concurrency 3 # hız + doğruluk ölçümü
```

## Ölçüm (benchmark.py, 9 senaryo × 3 koşu, eşzamanlılık 3)
| Sürüm | Başarı | Medyan | Ortalama |
|---|---|---|---|
| Önce (önceki ajanın hâli) | 19/27 | 17,3sn | 17,8sn |
| Sonra, `opencode` (düşünme kapalı, varsayılan) | 27/27 | 4,5sn | 4,6sn |

## Açık konular
- Hatalardan kalıcı öğrenme kaldırıldı (ölçümde zararlıydı); yeniden eklenecekse `AGENTS.md`
  Hedefler bölümündeki koşullarla.
- Electron/web içerikli uygulamalarda AX ağacı yalnızca pencere çerçevesini gösterebiliyor
  (Hermes: 3 öğe; `AXManualAccessibility` denendi, fark yaratmadı) → bu uygulamalarda
  `take_screenshot` gerekir.
- Chromium ilk açılışı ~7sn (soğuk başlatma); statik sayfalar için `fetch_raw` tercih edilmeli.
