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

## İkinci oturum: hız sınırı sonrası tam ölçüm

Hız sınırı temizlendikten sonra aynı çekirdek komut tekrarlandı ve planın "gerilemeyecek" kapsamındaki
`chrome_maas`/`chrome_form`, bağlayıcı düzeltmesiyle ilk kez ölçüldü.

| Senaryo | Başarı | Medyan süre | Tur | Not |
| --- | ---: | ---: | ---: | --- |
| Genel dokuz senaryo (çekirdek) | 27/27 | 2,1 sn | 2,0 | Önceki `429`'lar geçiciydi, kod kaynaklı değildi |
| `chrome_maas` | 3/3 | 197,6 sn | 7,0 | İlk ölçüm; 10/10 ilan detayı her koşuda açıldı |
| `chrome_form` | 2/3 | 42,6 sn | 12,0 | İlk ölçüm; üç koşunun üçünde de en az bir `ToolError` var |
| `chrome_ilan` (ek örneklem, n=6) | 4/6 | 26,8 sn | 5,0 | Bağlayıcı düzeltmesiyle birleşik toplam: 5/9 |

Tam test paketi bu oturumda iki kez çalıştırıldı; her ikisinde de farklı birer test, eşzamanlı ağır
yük altında başarısız oldu: `test_discovery_cache_and_aggregate_budget` (150ms bütçe, GUI ölçümüyle
aynı anda çalışırken) ve `test_worker_delivers_host_rejection_without_model_success_claim`
(`HostBusyError` — `host_lock.py`, muhtemelen bu makinedeki başka bir OmniAgent/ajan sürecinden).
İkisi de izole tekrarda geçti/atlandı. Bu makinede aynı anda en az yedi başka worktree/ajan
çalıştığından (`git worktree list`), bu iki başarısızlık ortam rekabetiyle açıklanıyor; ikisi de
dalın değiştirmediği dosyalarda ve dalın kod değişikliğiyle ilgisi yok.

### `chrome_ilan`: iki ayrı somut hata türü

Ek 6 koşudaki 2 başarısızlıktan biri boş çıktıyla bitti (14 tur — `chrome_form`'un başarısız
koşusuyla aynı örüntü: doğrulama aşamasına hiç ulaşmadan tur bütçesi tükendi). Diğeri tam 5 turda
tamamlandı ama tek karakter hatalıydı: model `IL-0C1EAF` yerine `IL-OC1EAF` yazdı (rakam sıfır
yerine harf O — saf ASCII, Kiril karışımı değil; ilk oturumdaki Kiril gözlemi ayrı, kaydedilmemiş
bir koşuya aitti). `job_code()` yalnızca `0-9A-F` hex karakterleri ürettiğinden bu karışıklık kesin
biçimde model/OCR kaynaklı, test verisi kaynaklı değildir.

### Sonuç ve öneri

`chrome_ilan`'ın birleşik oranı (5/9), başlangıç ölçümüyle (2/3) örtüşen bir belirsizlik aralığında;
dokuz koşuda gözlenen fark bağlayıcı düzeltmesinin bir gerilemesi olarak yorumlanamaz. `chrome_benzer`
(6/6), genel dokuz senaryo (27/27) ve `chrome_maas` (3/3) net biçimde geriye gitmedi. `chrome_form`'un
tek başarısızlığı ile `chrome_ilan`'ın iki başarısızlığının ortak noktası aynı: çok adımlı/çok hedefli
akışlarda (3 ilan; ya da alan doldurma + onay kutusu + gönderim) bir adımın etkisi doğrulanmadan
sıradakine geçiliyor ve döngü bazen son yanıta ulaşmadan tur bütçesini tüketiyor. Bu, Aşama 1'in
ölçüm/teşhis hedefini karşılar: Aşama 2'nin ("eylem makbuzu ve hedef kimliği") önceliği bu çok adımlı
doğrulama boşluğu olmalı.

Ham veriler: [çekirdek yeniden ölçüm](core_recheck.json),
[chrome_maas/chrome_form](gui_regression_check.json),
[chrome_ilan geniş örneklem](chrome_ilan_larger_sample.json).
