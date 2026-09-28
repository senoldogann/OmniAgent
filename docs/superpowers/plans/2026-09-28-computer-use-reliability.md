# Computer Use güvenilirlik ve hız planı

**Tarih:** 28 Eylül 2026  
**Durum:** Aşama 1 uygulandı (adım kaydı + arıza sınıflandırması); boş model yanıtı sınırlı kurtarmayla karşılandı. Aşama 2-4 sırada.  
**Hedef:** OmniAgent'ın açık Chrome ve macOS uygulamalarında doğru öğeyi seçme, eylemi gerçekleştirme, sonucu doğrulama ve hatadan kurtulma başarısını ölçülebilir biçimde artırmak.

## Mevcut durum ve başlangıç ölçümü

- Yerel uygulamalar için erişilebilirlik (AX), görünür metin için macOS Vision OCR, görsel hedefler için ekran görüntüsü kullanılıyor. Girdi ile görüntü tek 0–1000 koordinat uzayında. Eylemlerden sonra ekran gözlemi ve ilk son yanıtta teslim doğrulaması var.
- Açık Chrome'un web içeriği mevcut macOS AX yolunda okunamıyor; ajan ağırlıkla pencere görüntüsü, OCR ve koordinat kullanıyor. `browse_url` ise kullanıcının açık Chrome oturumundan ayrı bir Chromium oturumu.
- 28 Eylül'de `omniagent-benchmark --headless --only chrome_ilan --runs 2 --concurrency 1` sonucu **2/2 başarı**, **21,9 sn medyan**, **7 ve 10 tur** oldu. İki koşu genel başarı oranı için yeterli örnek değildir; yalnız ilk karşılaştırma noktasıdır.
- Başsız GUI ölçümü gerçek ajan ve araç döngüsünü çalıştırır; macOS Quartz yakalamasını, Chrome'un yerel `<select>` açılır menüsünü ve canlı pencere odağını kapsamaz.

## Başarı ölçütleri

Değişiklikten önce ve sonra aynı model, aynı profil ve aynı senaryo tohumlarıyla en az üçer koşu yapılacak.

1. Mevcut üç GUI senaryosunda (`chrome_ilan`, `chrome_maas`, `chrome_form`) başarı gerilemeyecek. Genel dokuz senaryo ve tam test kümesi geçecek.
2. Genişletilecek GUI kümesinde yanlış hedefe tıklama, yanlış alana yazma veya kanıtsız başarı beyanı **sıfır** olacak. Bu ölçüt, yalnız final metnine değil gerçek eylem kaydına bakacak.
3. Chrome senaryolarında medyan süre, tur, araç sayısı, model/araç süresi ve token ayrı raporlanacak. Hız iyileştirmesi doğruluk gerilerse kabul edilmeyecek.
4. Aynı başarısız eylem aynı durum ve argümanlarla yinelenmeyecek. İzin, odak, geometri, öğe yokluğu ve belirsiz eşleşme ayrı nedenler olarak raporlanacak.
5. Kullanıcının mevcut sohbetleri ve açık Chrome sekmeleri korunacak. Başsız testler kullanıcı ekranını kullanmayacak; canlı GUI denemeleri ekran başka bir iş için kullanılırken başlamayacak.

## Aşama 1 — Ölçüm ve olay kaydı

**İlk uygulanacak iş.** Önce mevcut araçların nerede zaman ve doğruluk kaybettiğini görünür kıl.

- `dev/benchmark.py` ve `dev/headless_screen.py` içinde GUI senaryolarının adım bazında hedef, eylem, gözlem ve doğrulama kayıtlarını incele. Mevcut görev makbuzlarıyla aynı çağrı kimliğini kullan.
- Başarıya ek olarak yanlış eylem, sonuçsuz tıklama, yeniden deneme, OCR/AX/görüntü yolu, yükleme bekleme ve son doğrulama sürelerini JSON raporuna ekle. Hassas ekran metni, parola ve token rapora yazılmayacak.
- Benzer metinli iki düğme, gecikmeli yükleme, açılır menü, modal, iç içe kaydırma ve görev sırasında değişen ekran için deterministik senaryolar ekle. Senaryoların bir kısmı başsız, macOS'a özgü kısmı kontrollü canlı test olacak.
- Önceki ve yeni ölçümü aynı komutlarla kaydet; iki koşuluk `chrome_ilan` sonucu tek başına iyileşme kanıtı sayılmayacak.

**Bitiş kapısı:** Hangi eylemin neden başarısız olduğu ve sürenin model/araç/gözlem dağılımı rapordan anlaşılmalı; yeni ölçüm mevcut sonucu bozmayacak. *(Karşılandı: arıza sınıfları ve yol süreleri `trace_summary` ve özet satırında; ölçüm komutu `omniagent-benchmark --headless --only … --runs N --concurrency 1 --json …`.)*

## Aşama 2 — Eylem makbuzu ve hedef kimliği

- Her GUI eylemine hedef uygulama/pencere/sekme, gözlem sürümü, hedef türü (AX, DOM, OCR, nokta), hedef kimliği ve eylem sonucunu taşıyan bir makbuz bağla. Makbuz modelin `STATE` metninden değil host gözleminden üretilecek.
- Tıklama veya yazma öncesinde pencere/sekme/ekran geometrisinin eski gözlemle uyumunu kontrol et. Bayat hedefte eylemi uygulamak yerine yeni gözlem iste.
- Eylem sonrası doğrulamayı hedefe göre yap: formda alan değeri, gezinmede URL/başlık, listede yeni öğe, gönderimde onay. Piksel farkını tek başına teslim kanıtı sayma; animasyon veya ilgisiz pencere değişimi başarı olmamalı.
- Belirsiz OCR hedefinde mevcut aday listesini koru. Hedefin yakınındaki etiket ve kapsayıcı bağlamı varsa eşleşmeyi onunla daralt; hâlâ belirsizse tıklama yapma.

**Bitiş kapısı:** Bayat koordinat ve belirsiz hedef testlerinde yanlış eylem yok; doğrulanmamış eylem son yanıtta başarıya dönüşmüyor.

## Aşama 3 — Açık Chrome için anlamsal öğe yolu

- Önce küçük bir fizibilite çalışması yap: kullanıcının **mevcut açık sekmesinde** görünür etkileşimli öğeleri etiket, rol, değer ve kararlı kimlikle almanın izin ve gecikme maliyetini ölç. Kullanıcının oturumunu ayrı Chromium'a kopyalama.
- Tercih edilen tasarım, dar izinli bir Chrome eklentisiyle etkin sekmeye erişimdir. `scripting` ve `activeTab` kullanımı kullanıcı etkileşimi ve izin akışına bağlıdır; bu yüzden kurulum/etkinleştirme UX'i fizibilite kapsamında çözülmeli. Geniş ve kalıcı site izni varsayılan yapılmamalı. [Chrome scripting API](https://developer.chrome.com/docs/extensions/reference/api/scripting), [activeTab izni](https://developer.chrome.com/docs/extensions/develop/concepts/activeTab).
- DOM/erişilebilirlik öğesini ekran konumuyla eşleştir. Çapraz kökenli çerçeve, canvas, kapalı shadow DOM veya eklentinin erişemediği sayfada mevcut OCR/görüntü yolu çalışmaya devam etsin.
- Yeni yol özellik bayrağı arkasında başlayacak. Aynı senaryolarda bayrak açık/kapalı A/B ölçümü yap; doğru hedef ve bitiş kanıtı korunmadan varsayılan yapma.

**Bitiş kapısı:** Semantik yolun kullanıldığı senaryolarda yanlış hedef yok, açık Chrome sekmesi korunuyor ve medyan süre/tur ölçülebilir biçimde iyileşiyor. Erişim olmayan sayfalarda görsel yedek yol görevi sürdürüyor.

## Aşama 4 — Yerel macOS uygulamaları ve kurtarma

- AX öğesi varsa onu, görünür metinde OCR'ı, etiketsiz görsel öğede görüntüyü kullan. AX'nin sunulmadığı Electron/web görünümlerinde mevcut `AX_EMPTY_HINT` yönlendirmesini gerçek senaryolarla sınayıp eksik bağlamı tamamla. Apple AX API'si bazı uygulamalarda bir işlemi desteklemeyebilir; bu durum açık hata olarak ele alınmalı. [Apple AXUIElement](https://developer.apple.com/documentation/applicationservices/axuielement_h?changes=l__3_2&language=objc).
- Kurtarma yollarını hata koduna bağla: bayat öğe → yeni AX/gözlem; metin yok → kaydır veya görsel yol; çoklu eşleşme → kapsayıcı/yakınlık; ekran değişmedi → odak ve hedef kontrolü; izin yok → ilgili uygulama ve macOS ayarına yönelik tanı.
- Bir başarısız denemeden sonra araç aynı argümanları tekrar üretirse yürütme yerine neden ve yeni seçenekleri döndür. Uzun döngüde host makbuzları ve kısa görev durumu modelin önceki eylemini hatırlatsın.
- Çoklu monitör, pencere taşıma, Retina ölçeği, kilitli ekran ve ekran izni vakalarını gerçek Mac'te kontrollü olarak doğrula.

**Bitiş kapısı:** Hata koşullarında ajan alternatif yol buluyor veya somut engeli doğru bildiriyor; rastgele koordinat denemeleri ve sonsuz tekrar yok.

## Yayın sırası ve geri alma

1. Her aşama ayrı, küçük değişiklikler ve ilgili testlerle ilerleyecek. Kullanıcının kirli çalışma ağacındaki ayrı değişiklikler korunacak.
2. `OMNI_UI_TEST=1 .venv/bin/python -m pytest -q`, genel benchmark ve GUI A/B ölçümü geçmeden uygulama paketi yeniden kurulmayacak.
3. Chrome semantik yolu özellik bayrağıyla açılacak; hata veya doğruluk gerilemesinde mevcut AX/OCR/görüntü yoluna dönebilecek.
4. Canlı macOS GUI denemesi yalnız ekran boşken yapılacak. `--headless` testi aynı GUI aracı mantığının güvenli ilk ölçümüdür; canlı macOS doğrulamasının yerine geçmez.

## İlk çalışma oturumunun sınırı

Plan onaylandıktan sonra **Aşama 1** ile başlanacak: mevcut benchmark olay akışı okunacak, ölçüm alanları ve en az bir yeni hata senaryosu eklenecek, başlangıç raporu alınacak. Sonraki aşamanın teknik seçimi bu rapordaki en büyük gerçek hata/süre kaynağına göre kesinleştirilecek.

## Uygulananlar (28 Eylül)

- `dev/benchmark.py`: GUI koşuları için `gui_trace_recorder` adım kaydı (çağrı kimliği, araç,
  gözlem/eylem ayrımı, yol, süre, sonuç) ve gizli ekran metni taşımayan sargıçlar içeren
  `gui_trace_summary`. Özet artık `boş_yanıt`, `finish_reasons`, `turns` ve `max_turns` ile
  arızanın modelden mi, araçtan mı, döngü sınırından mı geldiğini ayırıyor; `summarize` GUI
  izleme toplamlarını (eylem/gözlem/başarısız/tekrar/boş yanıt ve yol süreleri) yazdırıyor.
- `core/events.py`: `model_finished` olayına `finish_reason`, `tool_call_count` ve `empty_content`
  eklendi; `tool_started` olayı hedef kimliği (`argument_tag`) ve sayısal nokta taşıyor.
- `app/agent.py`: araçsız ve tamamen boş model yanıtı tek başına görevi bitirmiyor;
  `MAX_EMPTY_ANSWER_RECOVERIES` (1) kez gerçek yanıt veya araç çağrısı isteniyor. Bu, 28 Eylül
  başsız ölçümündeki boş yanıtlı başarısızlığı doğrudan hedefliyor.
- `dev/benchmark.py`: `chrome_ambiguous` gibi benzer metinli hedef senaryosu ve tohumlu koşu
  kimliği (`benchmark_run_id`) eklendi; aynı tohum/koşu aynı senaryoyu üretiyor.

**Sıradaki adım:** aynı komutlarla başsız ölçümü (en az üçer koşu) yeniden alıp `trace_summary`
ile en büyük hata/süre kaynağını belirlemek; ona göre Aşama 2 (bayat hedef/kimlik) veya Aşama 3
(Chrome semantik yol) teknik seçimini kesinleştirmek.
