# Kanıtsız son yanıt kapısı ölçümü — 27 Eylül 2026

`ollama-cloud` backend'iyle, görünür ekrana dokunmadan aynı dokuz çekirdek senaryonun
üçer koşusu ve iki kurtarma senaryosunun üçer koşusu ölçüldü. Önceki kayıt birleşmiş
`main` (`6fc3ad9`), sonraki kayıt son yanıt kapısı kodudur. Ham sonuçlar bu klasördeki
`core-before.json`, `core-after.json`, `recovery-before.json` ve `recovery-after.json`
dosyalarındadır.

| Ölçüm | Önce | Sonra |
| --- | ---: | ---: |
| Çekirdek başarı | 27/27 | 27/27 |
| Çekirdek medyan süre | 3,45 sn | 4,03 sn |
| Çekirdek medyan model süresi | 3,38 sn | 3,95 sn |
| Çekirdek medyan araç süresi | 0,06 sn | 0,06 sn |
| `self_repair` başarı | 3/3 | 3/3 |
| `self_repair` medyan süre | 2,07 sn | 2,48 sn |
| `stagnation` beklenen sınırlı duruş | 3/3 | 3/3 |
| `stagnation` medyan süre | 3,86 sn | 4,42 sn |

Çekirdekteki medyan artış model çağrısı süresindeki artışla birlikte oluştu; araç
medyanı aynı kaldı. Son koşudaki `sadakat` senaryosunda sağlayıcı iki isteği yeniden
denedi ve üç koşu yaklaşık 20–34 saniye sürdü. Bu veriler toplam süredeki farkın
tamamını koda veya sağlayıcıya kesin olarak atfetmez. Korunan finalin kullanıcıya
erken sızmadığı ayrıca sahte model akışıyla yapılan regresyon testleriyle doğrulandı;
canlı benchmark kullanıcı arayüzü veya Telegram akışını doğrudan ölçmez.

`stagnation` satırındaki 3/3, hedefin tamamlandığı anlamına gelmez. Beklenen davranış,
`ready` görülmeyince sınırlı turda durmak ve başarı iddia etmemektir. Son koşuda bir
sonuç açıkça `Doğrulanmadı: ...` dedi, iki sonuç boş kaldı.

Komutlar:

```sh
PYTHONPATH=src .venv/bin/omniagent-benchmark --runs 3 --concurrency 3 --json core-after.json
PYTHONPATH=src .venv/bin/omniagent-benchmark --runs 3 --concurrency 1 --only self_repair,stagnation --json recovery-after.json
```
