# Açık yol silme son durumu — 27 Eylül 2026

`ollama-cloud` ile dokuz görünmez çekirdek senaryosu üçer kez, `self_repair` ve
`stagnation` üçer kez çalıştırıldı. Bir önceki birleşmiş sürümün ham kayıtları
[salt okunur Git kanıtı ölçümünde](../2026-09-27-read-only-git-evidence/README.md),
bu sürümün kayıtları `core.json` ve `recovery.json` dosyalarındadır.

| Ölçüm | Önceki birleşmiş sürüm | Bu düzeltme |
| --- | ---: | ---: |
| Çekirdek başarı | 27/27 | 27/27 |
| Çekirdek medyan süre | 3,87 sn | 3,57 sn |
| Çekirdek medyan model süresi | 3,85 sn | 3,48 sn |
| Çekirdek medyan araç süresi | 0,06 sn | 0,06 sn |
| `self_repair` başarı | 3/3 | 3/3 |
| `stagnation` beklenen sınırlı duruş | 3/3 | 3/3 |

Çekirdek süre farkı büyük ölçüde model süresindeki farkla birlikte hareket etti;
tek başına kodun hızlandığını kanıtlamaz. `self_repair` ilk koşusu 13,4 sn,
sonraki iki koşusu 2,6–2,7 sn sürdü. `stagnation` başarısı, `ready`
doğrulanmadan görevin sınırlı turda **başarısız** durmasıdır.

Canlı çekirdek senaryolar bu yeni yerel yol silme durumunu tetiklemez. Doğrudan
kanıt, alakasız dosya yazımından sonra hedef dosya hâlâ varken sahte “silindi”
finalini reddeden; ardından gerçek kaldırma yapılırsa başarıya izin veren ve
noktalama/kırık sembolik bağ durumlarını kapsayan regresyon testleridir.

Komutlar:

```sh
PYTHONPATH=src .venv/bin/omniagent-benchmark --runs 3 --concurrency 3 --json core.json
PYTHONPATH=src .venv/bin/omniagent-benchmark --runs 3 --concurrency 1 --only self_repair,stagnation --json recovery.json
```
