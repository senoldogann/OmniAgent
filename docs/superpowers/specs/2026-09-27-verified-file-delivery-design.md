# Dosya tesliminin hedef bazlı doğrulanması

## Amaç ve kapsam

Kullanıcının açıkça belirttiği yerel dosya işlemlerinde başarılı bir araç çağrısı
tek başına tamamlanma kanıtı olmayacak. İlk uygulama dilimi silme, taşıma ve
düzenleme isteklerinin dar, açık biçimlerini kapsar. Kaynak belirsizse veya son
durum okunamıyorsa host `Doğrulanmadı` bildirir; yapılmış gibi konuşmaz.
Arayüz ve Telegram aynı `run_agent_with_callback` yolunu kullanır, dolayısıyla
aynı son karar ve olay akışını tüketir.

## Seçilen yaklaşım

1. **Yalnız fiil veya başarılı araç sayma:** hızlıdır, fakat başka dosyaya
   yazma ya da `rm -f` ile zaten olmayan dosya bu görevi yanlışlıkla başarılı
   gösterebilir.
2. **Yapılandırılmış dar teslim sözleşmesi (seçilen):** hedefteki açık yerel
   yolları ve istenen işlemi görev başında kaydeder; yalnız bu yolların son
   durumunu host kontrol eder. Belirsiz ifade için sözleşme uydurmaz.
3. **Modelden ikinci bir “doğrulandı” yargısı:** daha esnektir, ancak aynı
   modelin yanlış iddiasını ikinci bir metinsel iddiayla değiştirebilir ve
   her görevde ek süreye mal olur.

## Bileşenler ve veri akışı

- Saf ayrıştırıcı, yalnız doğrudan emir cümlesindeki tırnaklı/backtick'li,
  mutlak veya açık bağıl (`./`, `../`, `dizin/dosya`, `dosya.ext`) yerel yolları
  seçer. `http(s)` URL'leri, README içindeki yol referansı ve birden fazla
  yoruma açık cümleler hedef olmaz. Bağıl yol, görevin başladığı çalışma
  dizinine göre çözülür; sembolik bağ hedefi takip edilmez.
- Silmede her seçilen hedef için başlangıçtaki `lstat` ve bitişteki `lstat`
  karşılaştırılır. Birden çok hedefte hepsinin yokluğu gerekir. Başlangıçta
  zaten olmayan dosya için “silindi” iddiası onaylanmaz.
- Taşımada açık kaynak ve hedef yolu varsa başlangıçta kaynak mevcut,
  bitişte kaynak yok ve hedef mevcut olmalıdır. Düzenlemede açık hedefin
  başlangıç/bitiş içerik özeti farklı olmalı veya hedefe yapılan doğrulanmış
  `write_file`/`edit_file` çağrısı son içerikle eşleşmelidir. Aynı içeriği
  yeniden yazma isteği açıkça verilmişse araç kanıtı yeterlidir.
- Hedefte açık yol bulunmasa da “dosyayı sil” gibi dosya işlemi istekleri
  alakasız `write_file` ile kanıtlanmaz. İşlemle uyumlu araç kanıtı ve mümkünse
  araç argümanından çıkarılan yerel hedefin son durumu aranır; doğru hedef
  bağlanamıyorsa sonuç `Doğrulanmadı` olur.
- Host, görev başında alınan küçük dosya durumunu görev içinde tutar; model
  geçmişine ve kalıcı belleğe enjekte etmez. Son doğrulama, mevcut tek kurtarma
  turunu kullanır. Başarısız final metni kullanıcıya doğrulanmış başarı olarak
  akmaz.

## Değerlendirme ve kabul

- Karşıt testlerde başka dosyaya yazma, salt gözlem, var olmayan dosyada
  `rm -f`, hedeflerden yalnız birini silme, yanlış yere taşıma ve aynı
  içerikte kalan düzenleme yanlış başarı üretmemelidir.
- Gerçek silme, taşıma ve düzenleme; yöntem sorusu ve README referansı
  karşıtlarında doğru sonuç korunmalıdır.
- Tam test paketi ve macOS/Linux CI geçmeli. Görünmez canlı çekirdek ve
  kurtarma ölçümü üçer kez koşulmalı; başarı oranı, yanlış başarı sayısı ve
  medyan süre önceki kayıtla birlikte yayımlanmalı.
- Telegram ve Tk arayüzünde sahte modelle görev sonu `Doğrulanmadı` metni
  doğrulanır. Canlı hizmetler birleştirme sonrasında güncellenir; Mac ekranı
  meşgulse gerçek GUI otomasyonu çalıştırılmaz.

## Bilinen sınır

Serbest dildeki tüm dosya referanslarını ve dış uygulama durumunu deterministik
olarak çözmek mümkün değildir. Yalnız açık, yerel, çözümlenebilir hedefler için
son durum garantisi verilir; diğer durumlarda somut kanıt eksikliği bildirilir.
