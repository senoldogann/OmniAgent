# Açık yol silme hedefinde son durum doğrulaması

## Sorun

Eylem kanıtı şu anda göreve ait herhangi bir başarılı yan etkili aracı kabul eder.
Kullanıcı `"/tmp/hedef.txt" dosyasını sil` dediğinde ajan başka bir dosyayı yazıp
"hedef silindi" diyebilir; host başarılı araç gördüğü için yanlış finali kabul eder.

## Tasarım

- Yalnız hedefte açıkça yazılmış **tek mutlak yerel yol** silme fiiliyle bitişikse
  (`sil: /tmp/a`, `sil "/tmp/a"`, `` `/tmp/a` dosyasını sil ``, `delete /tmp/a`)
  son durum kapısı açılır. Birden fazla aday, URL, göreli veya belirsiz ifadede mevcut
  davranış korunur. Böylece başka bir adımın çıktı yolu yanlışlıkla silme hedefi olmaz.
  Çıplak yolun sonundaki `.` veya `)` dosya adı da cümle noktalaması da olabilir;
  sondan noktalama karakterleri birer birer kaldırıldığında oluşan her yorum
  kontrol edilir; biri mevcutsa yolun yokluğu kanıtlanmış sayılmaz.
- Finalde `lstat` ile yolun veya kırık sembolik bağın hâlâ var olup olmadığı okunur.
  Mevcutsa eylem kanıtı tek başına başarı sayılmaz. Erişim/istatistik hatası da
  "doğrulanamadı" sonucudur. Yoksa bu ek kapı geçilir; mevcut eylem kanıtı şartı
  yine uygulanır.
- Host önce mevcut eylem kanıtını, ardından açık yolun son durumunu kontrol eder.
  İki kapı **aynı** `action_evidence_recoveries` bütçesini kullanır. İlk finalde
  hiç eylem kanıtı yokken kurtarma hakkı tüketilip sonraki turda yalnız alakasız
  dosya yazılırsa hedef hâlâ mevcut olduğu için ikinci kurtarma açılmaz;
  `Doğrulanmadı: ...` sonucu döner. Erken model metni önceki güvenli teslim
  kapısı tarafından gizlenir.
- Bu kapı yalnız son durumun **yokluğunu** kanıtlar. Dosyanın görev başlangıcında
  varlığını veya onu kimin sildiğini kanıtlamaz; bunu iddia etmez.

## Doğrulama

- Eski kodda alakasız dosya yazımı + sahte silme finali başarı vererek kırmızı test
  üretir. Yeni kodda hedef duruyorsa final ve geçmiş güvenli başarısızlık gösterir;
  çıplak yolun `)` veya `.` ile biten doğal cümleleri de aynı uçtan uca testten geçer.
- Kurtarma turunda hedef gerçekten kaldırılırsa başarıya izin verilir. Belirsiz ve
  çok yollu hedefler için kapı açılmadığı test edilir; kırık sembolik bağ `lstat`
  ile mevcut sayılır. Eylem kanıtı kurtarması önce harcandığında son durumun
  ikinci kurtarma turu açmadığı ayrıca test edilir.
- Tam test, `compileall`, `git diff --check`, çekirdek ve kurtarma canlı ölçümleri
  önceki birleşmiş sürümle karşılaştırılır.
