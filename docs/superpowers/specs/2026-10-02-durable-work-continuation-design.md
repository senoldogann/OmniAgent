# Deniz — Mac üzerinde kalıcı iş takibi ve devam

Durum: Kullanıcı incelemesi bekleyen tasarım. Yeni çalışma davranışı henüz uygulanmadı.

## Amaç ve kabul edilen bağlam

Kullanıcı üç kanalda aynı konuşma/yetenekleri, gerçek araştırma, kanıt ve ilerleme istiyor.
Sonraki aşama önerisini “Ne gerekiyorsa yap” diyerek yürütme yetkisi verdi.
Mac tek çalışma sunucusu; VPS veya Windows kurulumu bu aşamanın kapsamı değil.
Bu tasarım bir uzun işin durdurulması, uygulamanın kapanması ve kullanıcı isteğiyle
aynı işe devam edilmesi içindir. Kendiliğinden yeni görev başlatmak ayrı kapsamdır.

## Mevcut kaynak bulguları

- `core/checkpoint.py`: UUID oturum kimliği, atomik kayıt, model durumu ve kısaltılmış
  son adımlar mevcut. `find_resume_checkpoint` hedef sözcük örtüşmesiyle seçim yapıyor.
- `app/agent.py`: devam isteğinde son konuşma hedefiyle kontrol noktası aranıyor;
  her tur sonunda kontrol noktası yazılıyor. Bu mekanizma kullanıcı/kanal sahipliğini
  kalıcı iş kimliğiyle modellemiyor.
- `core/conversation.py`: modele verilen konuşma özeti son sekiz değişim ve sınırlı
  yanıt/araç özeti içeriyor; tam uzun iş kaydı yerine kullanılamaz.
- `core/activity.py`: canlı süreç ve ilerleme bilgisi var; PID/heartbeat doğruluğu
  korunmalı. Canlılık kaydı kalıcı iş sonucu yerine kullanılamaz.
- `core/evidence.py`, `memory/personal.py` ve mevcut teslim makbuzları kanıt ve teslim
  belirsizliğinin kaynaklarıdır; yeni iş kaydı bunlara referans verir.

## Alternatifler ve seçim

1. **Kalıcı iş kaydı (önerilen):** mevcut yürütücü, kanıt ve kilidi kullanır; doğru
   işe kimlikle devam sağlar. İş başına küçük yerel kayıt ve eşzamanlılık yönetimi ekler.
2. Kontrol noktalarını yalnız daha uzun tutmak: daha küçük değişiklik; ancak kimlik,
   sahiplik ve sonucu belirsiz işlemlerin yeniden yürütülmesini tek başına çözmez.
3. Ayrı sürekli hizmet: Mac kapalıysa yine çalışamaz; yeni süreç, protokol ve dağıtım
   yükü ekler. İleride sunucu ihtiyacıyla ayrı değerlendirilir.

## Bileşenler ve sınırlar

### Kalıcı kayıt

Yeni WorkStore yerel kullanıcı veri kökünde özel izinli dizinde çalışır (0700/0600).
SQLite, sürümlü şema ve işlem sınırları kullanır; masaüstü ve iki köprü ayrı süreçlerdir.
Kayıt hatası devam güvencesi gereken yeni işin kabulünü engeller; “kaydedildi” denmez.
Mevcut tek seferlik konuşma/araştırma işlemleri bu yeni kabul yolu dışında çalışabilir.

Bir iş: `work_id`, `owner_id`, başlık, hedef, oluşturma/güncelleme zamanı, durum,
revision, mevcut yürütme kimliği, güvenli özet ve kanıt referansları içerir.
Adımlar: `step_id`, niyet, risk sınıfı, durum, işlem makbuzu referansı ve bağımlılıklar.
Ham sırlar, tüm konuşma veya büyük araç çıktıları çoğaltılmaz. Kanıt referansları
sahiplik ve dosya varlığı doğrulanmadan modele yüklenmez. Gözlemler talimat sayılmaz.

### Kimlik ve üç kanal

İlk sürüm tek Mac sahibini destekler. `owner_id` yerel kullanıcı profiline atanır.
Yalnız mevcut yapılandırmada açıkça eşleştirilmiş masaüstü/Telegram/iMessage sahibinin
kanalları bu kimliğe bağlanır. Model, ekran adı veya serbest mesajla eşleme yapamaz.
Eşleştirilmemiş gönderici işleri göremez ve devam ettiremez. İş listesi sahiplik
filtresinden sonra hazırlanır; başlık dahil başka sahibin kaydı sızmaz.

### Ortak koordinatör

Üç adaptör aynı WorkCoordinator list/status/pause/resume arayüzünü kullanır.
Çalıştırma mevcut ajan ve host kilidi üzerinden yapılır. Kayıt katmanı yeni araç
sağlayıcısı, izin sistemi veya model doğrulayıcısı değildir. ActivityStore gerçek
çalışan yürütmeyi gösterir; WorkStore bekleyen/durmuş/tamamlanmış işi gösterir.

## Durum ve veri akışı

İş durumları: ready, running, paused, needs_attention, completed, cancelled.
Adım durumları: pending, started, succeeded, failed, unknown.

1. Uzun iş isteği kabul edilir; hedef ve kontrol edilebilir adımlar kaydedilir.
2. Yürütme kısa bir veritabanı işlemiyle revision ve lease sahibi karşılaştırılarak
   alınır. Lease süreç başlangıç kimliği, yürütme UUID ve süreli heartbeat taşır;
   yalnız PID yeterli değildir. Aynı iş için eşzamanlı iki devam isteği tek kazanan üretir.
3. Araç çalıştırılmadan started yazılır. Sonuç mevcut makbuz/kanıt üzerinden yazılır.
   Veritabanı işlemi açıkken model veya araç beklenmez.
4. Süreç kaybolursa running → needs_attention; started adımlar unknown olur.
   Açılış yalnız kayıtları uzlaştırır ve kullanıcıya devam seçeneği sunar; kendiliğinden
   araç çalıştırmaz. Önceki sürecin canlılığı belirsizse kilit devralınmaz.
5. “Devam et” açık iş kimliğini veya aynı konuşmaya bağlı işi seçer. Yoksa sahibin
   yalnız bir devam edilebilir işi varsa onu sunar; birden fazla varsa kısa seçim ister.
   Hedef sözcük benzerliği kalıcı iş seçim yetkisi değildir.
6. Tamamlandı sonucu, gereken adımların kanıtı ve mevcut yanıt doğrulaması geçtiğinde
   yazılır. Modelin kendi özeti tamamlanma kanıtı değildir.

Duraklatma aktif yürütmenin mevcut iptal/cleanup yolunu bekler. Unknown kalan
adımlar korunur. Kullanıcı “iptal” ederse cancelled terminal olur; sıradan “devam et”
iptal edilmiş işi yeniden açmaz. Yeni yürütme için açık yeni istek gerekir.

## Yeniden yürütme kuralları

- succeeded adımlar tekrar çalıştırılmaz; değişebilir gözlem gerekiyorsa ayrı yeni
  salt okunur doğrulama adımı oluşturulur ve sebebi gösterilir.
- unknown salt okunur adım yeniden gözlemlenebilir.
- unknown dosya değişikliği, komut veya dış gönderim önce gerçek durum/makbuzla
  uzlaştırılır. Sonuç hâlâ bilinmiyorsa needs_attention kalır; otomatik tekrar yoktur.
- Dış iletişim onayı belirli işlem, hedef ve taslak özetine bağlanır. Değişen içerik
  veya hedef eski onayı kullanamaz. Yeni özellik mevcut yetki sınırlarını genişletmez.
- Teslim belirsizliği mevcut own-echo/no-replay sözleşmesini korur.

## Kullanıcıya sunum

“İşlerim”, “hangi aşamadasın”, “bu işi durdur”, “şu işe devam et” üç kanalda aynı
anlamı taşır. Başlık, durum, son doğrulanmış adım, eksik adım ve gerekli kullanıcı
aksiyonu kısa doğal metinle verilir. Gerçek ilerleme sırasında mevcut göstergeler
kullanılır; bekleyen iş araştırıyormuş gibi gösterilmez. iMessage'ın mevcut düz
metin sunumu ve yazıyor göstergesi sınırları korunur. İlk sürüm yeni görsel panel istemez.

## Geçiş ve kapsam

Eski kontrol noktaları değiştirilmeden kalır; yeni iş kayıtlarına otomatik sahip
atanmaz. Kullanıcı belirli eski hedefe devam istediğinde aday gösterilip yeni iş
olarak açık kabul edilir. Yeni kalıcı işlerde seçim WorkStore'dan yapılır; eski
kontrol noktaları yalnız yürütücü bağlamı sağlayabilir. Başlangıç özelliği kapalıdır;
şema geçişi ve üç adaptör hazır olduktan sonra yerel ayarla açılır. Geri dönüşte eski
uygulama WorkStore'u okumaz; yeni kayıtlar silinmez ve eski checkpoint yoluna zorla
aktarılmaz. Kimliği değişen işlem onayları aktarılmaz.

## Doğrulama ve teslim kriterleri

- Gerçek süreç kapanması/yeniden açılışı: succeeded adım tekrar çağrılmaz.
- İki ayrı süreç aynı işe devam ister: yalnız bir yürütme kabul edilir.
- Başka sahip/eşleştirilmemiş kanal: başlıklar dahil hiçbir iş görünmez.
- started yazıldıktan sonra çökme: unknown mutasyon otomatik tekrarlanmaz.
- Veritabanı yazma hatası: yeni uzun iş kabul edilmez ve yanlış kayıt güvencesi verilmez.
- İptal gerçek alt süreç ve cleanup tamamlanmadan terminal gösterilmez.
- Değişen dosya/taslak/hedef eski kanıt veya onayla başarılı kabul edilmez.
- Üç adaptör aynı iş kimliğinde aynı durum ve eksik adımları gösterir.
- Mac üzerinde dosya inceleme + araştırma + yerel taslak hazırlama işi durdurulur,
  uygulama yeniden açılır ve kullanıcının devam isteğiyle kanıtlı tamamlanır.
- Kişisel mesaj gönderimi ve sosyal yayınlama uçtan uca denemeye dahil değildir.

## Tasarım çalışma listesi

- [x] Mevcut kaynak ve teslim bağlamını incele.
- [x] Kullanıcı amacını ve Mac/üç kanal kapsamını mevcut konuşmadan belirle.
- [x] Üç yaklaşımı ve önerilen tasarımı sun.
- [x] Somut tasarım belgesini hazırla.
- [x] Bağımsız belge incelemesini tamamla — durable_spec_review: Approved; planı engelleyen eksik yok.
- [ ] Kullanıcının yazılı tasarım incelemesini al.
- [ ] writing-plans ile uygulama planını çıkar.
