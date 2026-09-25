# Telegram uzaktan görev köprüsü

OmniAgent, eşleştirilmiş **tek bir özel Telegram sohbetinden** görev alır. Varsayılan kısa görünüm Telegram'ın yerel zengin taslağında **Düşünüyor…** durumunu, araç çalışırken kısa adını ve komut önizlemesini animasyonlu gösterir. Model metni aynı taslakta akar; görev bitince Markdown vurguları ve bağlantıları korunarak normal Telegram mesajı gönderilir. Boş satırlar kompakt görünümde ayıklanır; kod bloklarının içindeki satırlar korunur. Taslaklar geçicidir; sohbet geçmişinde ayrı ara mesajlar bırakmaz. Bot API zengin taslağı desteklemiyorsa mevcut tek mesajı güncelleyen yol kullanılır. Hedef, tur, model ve token dökümü tekrarlanmaz. `/verbose on` sonraki görevlerde model metni, araç/komut olayları, model değişimi ve istatistikleri gösteren ayrıntılı akışı açar; `/verbose off` kısa görünüme döner. Ekran görüntüsü aracı başarılı olursa görüntü ayrıca gönderilir. Uzun metinler sayfalanır.

## İlk kurulum

1. Telegram'da [@BotFather](https://core.telegram.org/bots/features#botfather) ile size ait bir bot oluşturup tokenı alın.
2. Mac'te proje dizininde çalıştırın:

   ```sh
   .venv/bin/omniagent-telegram setup
   ```

3. Token terminalde görünmeden okunur. Komutun gösterdiği `/pair <kod>` mesajını botunuzun **özel sohbetine** 3 dakika içinde gönderin. Botun sohbet ve kullanıcı kimliği `~/Library/Application Support/OmniAgent/telegram.json` dosyasına, token yalnız macOS Keychain'e kaydedilir.
4. Önce ön planda doğrulayın:

   ```sh
   .venv/bin/omniagent-telegram run
   ```

5. Oturum açıldığında otomatik başlaması için:

   ```sh
   .venv/bin/omniagent-telegram install-service
   ```

Hizmet `launchd` kullanıcı oturumunda çalışır. Bilgisayar açık, uyanık ve internete bağlı olmalıdır. Telegram webhook'u etkinse `getUpdates` çalışmaz; webhook yapılandırmasını kaldırmanız gerekir. Bot API tokenını proje dosyasına veya Git'e eklemeyin.

## Sohbet komutları

- Doğrudan mesaj: yeni görev.
- `/stop`: çalışan görevi ve kullanıcı yanıtı beklemesini durdurur.
- `/status`: çalışan hedefi gösterir.
- `/verbose on` veya `/verbose off`: sonraki görevde ayrıntılı veya kısa görünümü seçer; varsayılan kısa görünümdür.
- `/model auto` veya `/model <profil>`: sonraki görevin modelini seçer. Profil adları `config.BACKENDS` içindedir: `ollama-cloud`, `openai`, `opencode`, `opencode-think`, `openrouter`. İlgili anahtar (`OPENAI_API_KEY`, `OPENCODE_API_KEY`, `OPENROUTER_API_KEY`) tanımlı değilse o profil kullanılamaz; anahtarlar arayüzdeki **Ayarlar** sayfasından girilip Keychain'de saklanabilir ve köprü açılışta bunları kendi süreç-içi deposuna alır. Anahtarlar ortam değişkenlerine yazılmaz, alt süreçlere geçmez ve araç çıktısında maskelenir; kayıtlı anahtar kabukta tanımlı bir değişkeni geçersiz kılar.
- `/mode normal`, `/mode long`, `/mode autonomous`: sonraki görevin tur/zaman bütçesini seçer.
- `/schedules`: planlanmış görevleri kimlik, kural ve sonraki çalışma zamanıyla listeler.
- `/unschedule <kimlik>`: planı siler.
- Entegrasyon soru sorarsa tek alan için düz metin, birden fazla alan için alan adlarını içeren JSON nesnesi gönderin.

## Zamanlanmış görevler

"Her sabah 9'da gündemi özetle", "hafta içi 18:00'de yedek al", "2 saatte bir sitemi kontrol et" veya
"yarın 14:30'da toplantıyı hatırlat" gibi istekler (Telegram'dan ya da arayüzden) plan olarak saklanır.
Planları sürekli açık Telegram köprüsü çalıştırır: zamanı gelen görev sohbete "⏰ Zamanlanmış görev
başlıyor" mesajıyla başlar ve sonucu normal görev gibi gelir. Bu yüzden planlar yalnız köprü eşleştirilmiş
ve çalışırken yürür.

- Kurallar: bir kez (tarih-saat), her gün, haftanın seçili günleri veya 15 dakika-7 gün arası aralık.
  Saatler Mac'in yerel saatidir; yaz saati geçişinde de aynı yerel saatte çalışır. En çok 20 plan tutulur.
- Bilgisayar kapalı/uykudayken kaçan çalışma 6 saat içindeyse bir kez yetişilir; daha eskisi atlanır ve
  sohbete "Kaçırıldı" yazılır.
- Arayüzde görev sürerken zamanı gelen plan beklemeye alınır ve kilit boşalınca başlar.
- Zamanlanmış görev yeni plan kuramaz (kendini çoğaltmaz); para hareketi onayı gibi kurallar aynen geçerlidir.

## Dosya alışverişi

- **Size gelen dosyalar:** fotoğraf, belge, ses, sesli mesaj veya video gönderebilirsiniz. Ekin açıklaması (caption)
  görev olur; açıklama yoksa türüne uygun varsayılan istek kullanılır ("görseli incele", "dosyayı özetle").
  Ek `~/Library/Application Support/OmniAgent/telegram-inbox/` altına yalnız size açık (0600) izinle kaydedilir ve
  yolu göreve eklenir. Fotoğraflar ve belge olarak gönderilen JPEG/PNG/WebP/GIF/BMP görseller modele ayrıca
  görüntü olarak verilir (en-boy oranı korunur). Telegram botları en çok 20 MB indirebilir; daha büyük ek görev başlatmaz.
- **Sizden istenen dosyalar:** Telegram görevlerinde ajan `send_file` aracıyla bilgisayardaki bir dosyayı (en çok
  50 MB) sohbete belge olarak gönderebilir: "masaüstündeki rapor.pdf'i bana gönder" gibi.
- Bir soruya yanıt beklenirken veya görev çalışırken gelen ek yeni görev başlatmaz; bot durumu yazar.

Görev geçmişinin son sekiz kaydı yerel `telegram-history.json` dosyasında tutulur. Bot, mesajları yalnız eşleştirilen kullanıcıdan ve özel sohbetten kabul eder. Güncelleme sırası `telegram-offset.json` ile korunur; yeniden başlatma aynı komutu tekrar çalıştırmaz. UI ile Telegram görevi aynı anda ekranı/klavyeyi kullanamaz: ikinci görev hemen “başka görev çalışıyor” yanıtı alır.

Telegram sohbeti bulutta tutulduğu için gönderdiğiniz hedefler, ajan çıktıları ve ekran görüntüleri Telegram'da da bulunur. Bot hesabı tam bilgisayar otomasyonu yetkisi verir; tokenı ve eşleştirilmiş hesabı koruyun. Sesli mesaj dosya olarak kaydedilir; metne çevirme ayrı bir konuşma tanıma aracı gerektirir ve ajan bunu kurulu araçlarla dener. Canlı bot testi için gerçek BotFather tokenı ve eşleştirme gerekir.
