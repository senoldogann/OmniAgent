# Salt okunur Git kanıtı ölçümü — 27 Eylül 2026

`main` üzerindeki önceki [son yanıt kapısı ölçümü](../2026-09-27-final-output-guard/README.md)
ile aynı `ollama-cloud` backend'i ve aynı dokuz görünmez çekirdek senaryosu üçer kez
çalıştırıldı. Ham yeni sonuçlar `core.json` ve `recovery.json` dosyalarındadır.

| Ölçüm | Önceki birleşmiş sürüm | Bu düzeltme |
| --- | ---: | ---: |
| Çekirdek başarı | 27/27 | 27/27 |
| Çekirdek medyan süre | 4,03 sn | 3,87 sn |
| Çekirdek medyan model süresi | 3,95 sn | 3,85 sn |
| Çekirdek medyan araç süresi | 0,06 sn | 0,06 sn |
| `self_repair` başarı | 3/3 | 3/3 |
| `stagnation` beklenen sınırlı duruş | 3/3 | 3/3 |

Süre farkı sağlayıcı değişkenliğini de içerir; bu ölçüm tek başına kodun hızlandığını
kanıtlamaz. `stagnation` 3/3, `ready` görülmeyince görevin başarı sayılmadan sınırlı
turda durduğu anlamındadır. Yeni davranışın doğrudan kanıtı, başarılı `git status`
sonucundan sonra gelen sahte “dosya silindi” finalini reddeden regresyon testidir;
çekirdek canlı senaryolar bu özel hatayı tetiklemez.

Komutlar:

```sh
PYTHONPATH=src .venv/bin/omniagent-benchmark --runs 3 --concurrency 3 --json core.json
PYTHONPATH=src .venv/bin/omniagent-benchmark --runs 3 --concurrency 1 --only self_repair,stagnation --json recovery.json
```
