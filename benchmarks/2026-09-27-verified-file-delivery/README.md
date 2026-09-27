# Hedefe bağlı dosya teslimi — 27 Eylül 2026

`ollama-cloud` ile görünmez çekirdek ve dosya senaryoları üçer kez,
`self_repair` ve `stagnation` üçer kez çalıştırıldı. Ham sonuçlar aynı
dizindeki `core.json`, `recovery.json` ve `file.json` dosyalarındadır.
Önceki sürümün ölçümleri
[silme fiili yönlendirmesinde](../2026-09-27-deletion-verb-routing/README.md).

| Ölçüm | Önceki sürüm | Bu sürüm |
| --- | ---: | ---: |
| Çekirdek başarı | 27/27 | 27/27 |
| Çekirdek medyan duvar süresi | 3,94 sn | 4,82 sn |
| Çekirdek medyan model süresi | 3,91 sn | 4,70 sn |
| Çekirdek medyan araç süresi | 0,06 sn | 0,06 sn |
| `self_repair` başarı | 3/3 | 3/3 |
| `stagnation` beklenen sınırlı duruş | 3/3 | 3/3 |
| Kurtarma medyan duvar süresi | 3,65 sn | 3,80 sn |
| Yeni dosya silme/taşıma/düzenleme | — | 9/9, medyan 3,18 sn |

Çekirdek koşusunda birkaç model çağrısı zaman aşımı ve yeniden deneme yaşadı.
Medyan duvar süresi 0,88 sn arttı; model süresi 0,79 sn arttı, araç medyanı
değişmedi. Bu ölçüm hız kazancı kanıtlamaz. Dosya senaryolarının dokuzu da iki
turda ve tek araç çağrısıyla bitti. `stagnation` başarısı, `ready` gözlenmeden
görevin beklenen biçimde **başarısız** durmasıdır.

Karşıt testler başlangıçta olmayan hedefe `rm -f`, iki hedeften birinin
silinmesi, başka dosyaya yazma, silmek yerine taşıma, taşıma hedefi yerine
kaynağı yok etme, aynı içerikle düzenleme, sembolik bağ düzenleme ve README
içindeki yol referansını gerçek dosya sanma durumlarında yanlış başarıyı
reddeder. Telegram'ın sahte API ve Tk'nin gerçek worker yolu da modelin
doğrulanmamış başarı metnini dışarı aktarmadığını sınar. Gerçek Telegram
ağına mesaj veya kullanıcı ekranına GUI eylemi gönderilmedi.

Yeniden çalıştırma:

```sh
PYTHONPATH=src .venv/bin/omniagent-benchmark --runs 3 --concurrency 3 --only file_delete,file_move,file_edit --json file.json
PYTHONPATH=src .venv/bin/omniagent-benchmark --runs 3 --concurrency 3 --json core.json
PYTHONPATH=src .venv/bin/omniagent-benchmark --runs 3 --concurrency 1 --only self_repair,stagnation --json recovery.json
```
