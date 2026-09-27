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
  dizinine göre `os.path.abspath` ile leksik olarak çözülür; sembolik bağ
  hedefi takip edilmez. Ayrıştırıcı önce
  mevcut `action_execution_expected`/`_action_scope` ayrımını kullanır:
  `How can I delete ./a.txt?` bilgi sorusudur, ardından `Please delete it`
  gelirse soru içindeki yol emre bağlanır.
- Silmede her seçilen hedef için başlangıçtaki `lstat` ve bitişteki `lstat`
  karşılaştırılır. Birden çok hedefte hepsinin yokluğu ve her hedefe yönelik
  başarılı `rm`/`unlink` araç makbuzu gerekir; başka yere `mv` silme makbuzu
  sayılmaz. Başlangıçta zaten olmayan dosya için “silindi” iddiası onaylanmaz.
- Düzenleme ve taşıma için içerik özeti akışlı okunur; düzenleme mevcut 8 MiB
  dosya aracı sınırını, taşıma 64 MiB doğrulama sınırını aşarsa veya dosya
  okunamaz/özelse (ör. aygıt) bu sözleşme belirsiz sayılır.
  Sembolik bağın kendisi taşınıyorsa bağ hedefi karşılaştırılır; bağlı
  dosyanın içeriği okunmaz. Sembolik bağ üzerinden düzenleme, mevcut
  `write_file` ve `edit_file` farklı nesneleri değiştirebildiği için bu
  dilimde doğrulanmış düzenleme sözleşmesine alınmaz.
- Taşımada açık kaynak ve hedef yolu varsa başlangıçta kaynak mevcut,
  bitişte kaynak yok ve hedef mevcut, bitişteki içerik/bağ kimliği de ilk
  kaynakla eşleşmiş olmalıdır. Hedef bir dizinse etkin hedef
  `hedef_dizin/kaynak_adı` olur. Etkin hedef başlangıçta mevcutsa bu ilk
  sürüm sözleşme kurmaz; yalnız kaynağın yokluğu yanlış başarı üretemez.
  Düzenlemede açık hedefin başlangıç ve bitiş içerik özeti farklı olmalıdır;
  `edit_file` aynı metni geri döndürdüğünde başarı sayılmaz.
- Hedefte açık yol bulunmasa da “dosyayı sil” gibi dosya işlemi istekleri
  alakasız `write_file` ile kanıtlanmaz. İşlemle uyumlu araç kanıtı ve mümkünse
  araç argümanından çıkarılan yerel hedefin son durumu aranır; doğru hedef
  bağlanamıyorsa sonuç `Doğrulanmadı` olur.
- Açık `.txt`/`.md` gibi dosya düzenleme hedeflerinde `edit_file` şeması da
  göreve açılır; yalnız “kaynak kodu” heuristiğine bağlı kalmaz.
- Yeni dosya sözleşmesi görev başındaki `guarded_final_output` kararına
  katılır; doğrulanmamış canlı final metni Telegram ve UI'ye akmaz. Aynı
  içeriğin yeniden yazılmasını isteyen hedefler, bu ilk dilimdeki düzenleme
  sözleşmesinin dışında kalır.
- Host, görev başında alınan küçük dosya durumunu görev içinde tutar; model
  geçmişine ve kalıcı belleğe enjekte etmez. Araçla hedef eşleştirmesi için
  `StepRecord` kullanılmaz: bu kayıt argümanı 300 karakterde kırpar. Gerçek
  `ToolCallDraft` ile sonucundan doğrulanmış işlem türü (`delete`, `move`,
  `edit`), normalleştirilmiş kaynak/hedef yollar ve gerekli içerik özeti
  görev içinde yapılandırılmış makbuz olarak tutulur. Kabukta yalnız tam
  ayrıştırılabilen doğrudan `rm`/`unlink`/`mv` çağrıları bu makbuzu üretir;
  gizli içerik/komut kalıcı kayda eklenmez. Son doğrulama, mevcut tek kurtarma
  turunu kullanır. Başarısız final metni kullanıcıya doğrulanmış başarı olarak
  akmaz. Kaynak yazımı ve eylem kanıtı aynı dosya teslimi için ayrı ayrı
  kurtarma hakkı vermez; tek ortak teslim kurtarma sayacı kullanılır.

## Değerlendirme ve kabul

- Karşıt testlerde başka dosyaya yazma, salt gözlem, var olmayan dosyada
  `rm -f`, hedeflerden yalnız birini silme, yanlış yere taşıma, önceden
  mevcut hedefe karşı yalnız kaynağı silme ve aynı içerikte kalan düzenleme
  yanlış başarı üretmemelidir.
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
