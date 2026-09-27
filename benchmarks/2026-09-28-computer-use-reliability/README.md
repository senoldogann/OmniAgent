# Bilgisayar kullanımı: Aşama 1 ölçümü

Bu kayıt, planın ilk çalışma oturumundaki olay izini ve yeni hata senaryosunu kapsar.
Komutlar görünmez Chromium ile çalıştırıldı; kullanıcının ekranı ve Chrome sekmeleri
kullanılmadı. Üç veri kümesi aynı `ollama-cloud` başlangıç profiliyle, üçer koşudur.
Çalıştırma kimliği o sırada rastgele üretildiği için koşular eşlenik tohum taşımaz;
bu karşılaştırma nedensel hız iyileşmesi kanıtı değildir.

| Senaryo ve sürüm | Başarı | Medyan süre | Tur | Medyan model / araç süresi |
| --- | ---: | ---: | ---: | ---: |
| `chrome_ilan` başlangıç | 2/3 | 25,1 sn | 7 | 8,9 / 16,2 sn |
| `chrome_ilan` izleme ekli | 1/3 | 23,7 sn | 7 | 7,3 / 16,3 sn |
| `chrome_ilan` bağlayıcı düzeltildi | 1/3 | 22,8 sn | 8 | 8,3 / 17,6 sn |
| `chrome_benzer` izleme ekli | 3/3 | 6,8 sn | 5 | 4,0 / 2,8 sn |
| `chrome_benzer` bağlayıcı düzeltildi | 3/3 | 5,8 sn | 4 | 3,5 / 2,2 sn* |

\* Son `chrome_benzer` koşusunda Ollama çağrısı hata verince `openai` yedeğine geçildi;
17,9 sn süren o koşu model ve araç medyanı hesabında ayrı sağlayıcı koşusudur.

## Olay izinin gösterdiği bulgular

- Başsız bağlayıcı `chrome_active_tab(url, new_tab=...)` çağrısındaki `new_tab`
  parametresini kabul etmiyordu. İzleme ekli altı GUI koşusunun her birinde ilk
  çağrı `TypeError` verdi ve ajan aynı gezinmeyi yineledi. İmza düzeltmesinden
  sonraki altı koşuda bu hata görülmedi. Benzer düğme senaryosu 5 tur/3 araçtan
  4 tur/2 araca indi.
- `chrome_benzer` sayfasında aynı metinli iki düğme var. Düzeltmeden sonraki üç
  koşuda üç tıklamanın üçü de `expected` işaretli düğmeye gitti; yanlış hedef ve
  etkisiz tıklama sıfırdı. Bu hedef denetimi yalnız işaretli test öğelerinde vardır;
  diğer sayfalardaki sıfır sayısı doğru hedefin kanıtı değildir.
- `chrome_ilan` koşularında bir tamamlanmayan uzun döngü, kod kopyasında Kiril/Latin
  karışması ve eksik kod üretimi görüldü. Bunlar bitiş denetiminde başarısız
  sayıldı. Düzeltme sonrası uzun koşuda araç süresi 31,8 sn, ekran durulma
  beklemesi 18,9 sn, otomatik gözlem 22,4 sn idi. Sonraki teknik öncelik, uzun
  gözlem döngüsünde eylem etkisinin ve son metindeki kodun ayrı doğrulanmasıdır.
- Yeni JSON alanları araç çağrı kimliğine bağlı `gui_trace` (araç, yol, aşama,
  sonuç, hata türü, süre), `gui_actions` (test hedef işareti, tıklama etkisi,
  durulma süresi) ve `gui_metrics` toplamlarıdır. Önizleme metni ve araç çıktısı
  bu alanlara yazılmaz. `model_seconds`, `tool_seconds` ve token sayıları mevcut
  rapor alanlarıdır.

Ham veriler: [başlangıç](before.json), [izleme](instrumented.json),
[bağlayıcı düzeltmesi](fixed.json).

Tam test komutu `OMNI_UI_TEST=1 .venv/bin/python -m pytest -q` sonunda
**533 başarılı, 2 atlanan** test verdi. Dokuz çekirdek senaryonun üçer koşuluk
benchmarkı da başlatıldı, fakat sağlayıcı `429` hız sınırı verdiği için 27
koşunun 24'ü başarısız oldu. Kalan üç `gun` koşusu geçti. Bu sonuç
çekirdek doğruluğu için geçerli bir regresyon ölçümü değildir; hız sınırı
yenilendikten sonra aynı komut tekrarlanmalıdır. `chrome_ilan` başarı oranı
üç koşuda 2/3'ten 1/3'e düştüğünden Aşama 1'in "gerilemeyecek" kapısı da
henüz sağlanmış sayılmaz. Eşlenik tohumlar ve daha geniş örneklemle tekrar
ölçüm gerekir.

Komutlar:

```sh
.venv/bin/omniagent-benchmark --headless --only chrome_ilan --runs 3 --concurrency 1 --json before.json
.venv/bin/omniagent-benchmark --headless --only chrome_ilan,chrome_benzer --runs 3 --concurrency 1 --json fixed.json
```
