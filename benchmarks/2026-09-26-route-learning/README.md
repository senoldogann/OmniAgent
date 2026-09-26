# Alternatif yol öğrenmesi regresyon ölçümü

**Tarih:** 2026-09-26  
**Senaryo:** `self_repair` (aynı araçta doğrulanmış düzeltme)  
**Backend:** `ollama-cloud`  
**Komut:** `PYTHONPATH=src .../omniagent-benchmark --runs 3 --concurrency 1 --only self_repair --json <çıktı>`

Temiz `f92e0be` worktree'si ile `feat/goal-scoped-recovery` çalışma ağacı sıralı koşuldu. Her üçlü koşu kendi geçici deneyim dosyasına yazar; ilk koşu dersi öğrenir, ikinci ve üçüncü koşu onu kullanır. Bu ölçüm yeni çapraz araç web yolunu değil, mevcut aynı araç öğrenme davranışının regresyonunu sınar. Çapraz araç yolunun doğruluğu ve gizliliği `tests/test_experience.py` ile sınanır.

| Sürüm | Başarı | Medyan süre | Tur dizisi | Araç çağrısı dizisi | Ders kullanımı |
| --- | --- | --- | --- | --- | --- |
| Önce | 3/3 | 2,4 sn | 5, 3, 3 | 4, 2, 2 | 0, 1, 1 |
| Sonra | 3/3 | 2,2 sn | 5, 3, 3 | 5, 2, 2 | 0, 1, 1 |

Süreler canlı sağlayıcı koşullarına bağlıdır; bu küçük örnek hız kazanımı iddiası için yeterli değildir. Başarı ve medyan tur sayısı korundu. Ham kayıtlar [before.json](before.json) ve [after.json](after.json) dosyalarındadır. Benchmark ev dizininde istenmeyen yeni dosya saptamadı.
