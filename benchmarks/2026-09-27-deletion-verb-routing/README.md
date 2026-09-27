# Silme fiili yönlendirmesi — 27 Eylül 2026

`ollama-cloud` ile dokuz görünmez çekirdek senaryosu üçer kez, `self_repair`
ve `stagnation` üçer kez çalıştırıldı. Önceki birleşmiş sürümün ham kayıtları
[açık yol silme ölçümünde](../2026-09-27-explicit-deletion-state/README.md),
bu sürümün ham kayıtları `core.json` ve `recovery.json` dosyalarındadır.

| Ölçüm | Önceki birleşmiş sürüm | Bu düzeltme |
| --- | ---: | ---: |
| Çekirdek başarı | 27/27 | 27/27 |
| Çekirdek medyan süre | 3,57 sn | 3,94 sn |
| Çekirdek medyan model süresi | 3,48 sn | 3,91 sn |
| Çekirdek medyan araç süresi | 0,06 sn | 0,06 sn |
| `self_repair` başarı | 3/3 | 3/3 |
| `stagnation` beklenen sınırlı duruş | 3/3 | 3/3 |

Çekirdek medyanı 0,37 sn arttı; bu koşuda model süresi de 0,43 sn arttı.
Ölçüm, bu değişikliğin hız kazandırdığını göstermiyor. `stagnation`
başarısı, beklenen `ready` durumu görülmeden görevin sınırlı turda
**başarısız** durmasıdır.

Canlı senaryolar yeni `remove`/`kaldır` ve karma yöntem sorusu ayrımını
doğrudan sınamaz. Bunun kanıtı regresyon testleridir: ilgisiz başarılı
dosya yazımından sonra hedef yol dururken sahte “silindi” finali reddedilir;
salt yöntem sorusu eylem sayılmaz; soru ardından açık silme emri, tekrar
edilen yol, farklı hedef, README içi referans ve yerel diskten dosya silme
ayrı ayrı değerlendirilir.

Komutlar:

```sh
PYTHONPATH=src .venv/bin/omniagent-benchmark --runs 3 --concurrency 3 --json core.json
PYTHONPATH=src .venv/bin/omniagent-benchmark --runs 3 --concurrency 1 --only self_repair,stagnation --json recovery.json
```
