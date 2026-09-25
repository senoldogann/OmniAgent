# Telegram uzaktan görev köprüsü

OmniAgent, eşleştirilmiş **tek bir özel Telegram sohbetinden** görev alır. Varsayılan kısa görünüm Telegram'ın yerel zengin taslağında **Düşünüyor…** durumunu, araç çalışırken kısa adını ve komut önizlemesini animasyonlu gösterir. Model metni aynı taslakta akar; görev bitince Markdown vurguları ve bağlantıları korunarak normal Telegram mesajı gönderilir. Boş satırlar kompakt görünümde ayıklanır; kod bloklarının içindeki satırlar korunur. Taslaklar geçicidir; sohbet geçmişinde ayrı ara mesajlar bırakmaz. Bot API zengin taslağı desteklemiyorsa mevcut tek mesajı güncelleyen yol kullanılır. Hedef, tur, model ve token dökümü tekrarlanmaz. `/verbose on` sonraki görevlerde model metni, araç/komut olayları, model değişimi ve istatistikleri gösteren ayrıntılı akışı açar; `/verbose off` kısa görünüme döner. Ekran görüntüsü aracı başarılı olursa görüntü ayrıca gönderilir. Uzun metinler sayfalanır.

## İlk kurulum

1. Telegram'da [@BotFather](https://core.telegram.org/bots/features#botfather) ile size ait bir bot oluşturup tokenı alın.
2. Mac'te proje dizininde çalıştırın:

   ```sh
   .venv/bin/python telegram_bridge.py setup
   ```

3. Token terminalde görünmeden okunur. Komutun gösterdiği `/pair <kod>` mesajını botunuzun **özel sohbetine** 3 dakika içinde gönderin. Botun sohbet ve kullanıcı kimliği `~/Library/Application Support/OmniAgent/telegram.json` dosyasına, token yalnız macOS Keychain'e kaydedilir.
4. Önce ön planda doğrulayın:

   ```sh
   .venv/bin/python telegram_bridge.py run
   ```

5. Oturum açıldığında otomatik başlaması için:

   ```sh
   .venv/bin/python telegram_bridge.py install-service
   ```

Hizmet `launchd` kullanıcı oturumunda çalışır. Bilgisayar açık, uyanık ve internete bağlı olmalıdır. Telegram webhook'u etkinse `getUpdates` çalışmaz; webhook yapılandırmasını kaldırmanız gerekir. Bot API tokenını proje dosyasına veya Git'e eklemeyin.

## Sohbet komutları

- Doğrudan mesaj: yeni görev.
- `/stop`: çalışan görevi ve kullanıcı yanıtı beklemesini durdurur.
- `/status`: çalışan hedefi gösterir.
- `/tools` veya `/yetenekler`: kurulu API/MCP bağlantılarını ve skill sayısını yerel katalogdan gösterir.
- `/skills`: kurulu skill adlarını yerel katalogdan gösterir. Skill kurmak veya kullanmak için doğal dilde hedef yazın.
- `/verbose on` veya `/verbose off`: sonraki görevde ayrıntılı veya kısa görünümü seçer; varsayılan kısa görünümdür.
- `/model auto` veya `/model <profil>`: sonraki görevin modelini seçer. Profil adları `config.BACKENDS` içindedir: `ollama-cloud`, `openai`, `opencode`, `opencode-think`, `openrouter`. İlgili anahtar (`OPENAI_API_KEY`, `OPENCODE_API_KEY`, `OPENROUTER_API_KEY`) tanımlı değilse o profil kullanılamaz; anahtarlar arayüzdeki **Ayarlar** sayfasından girilip Keychain'de saklanabilir ve köprü açılışta bunları kendi süreç-içi deposuna alır. Anahtarlar ortam değişkenlerine yazılmaz, alt süreçlere geçmez ve araç çıktısında maskelenir; kayıtlı anahtar kabukta tanımlı bir değişkeni geçersiz kılar.
- `/mode normal`, `/mode long`, `/mode autonomous`: sonraki görevin tur/zaman bütçesini seçer.
- Entegrasyon soru sorarsa tek alan için düz metin, birden fazla alan için alan adlarını içeren JSON nesnesi gönderin.

Görev geçmişinin son sekiz kaydı yerel `telegram-history.json` dosyasında tutulur. Bot, mesajları yalnız eşleştirilen kullanıcıdan ve özel sohbetten kabul eder. Güncelleme sırası `telegram-offset.json` ile korunur; yeniden başlatma aynı komutu tekrar çalıştırmaz. UI ile Telegram görevi aynı anda ekranı/klavyeyi kullanamaz: ikinci görev hemen “başka görev çalışıyor” yanıtı alır.

Telegram sohbeti bulutta tutulduğu için gönderdiğiniz hedefler, ajan çıktıları ve ekran görüntüleri Telegram'da da bulunur. Bot hesabı tam bilgisayar otomasyonu yetkisi verir; tokenı ve eşleştirilmiş hesabı koruyun. Bu sürüm metin ve ekran görüntüsü gönderir; dosya/ek yükleme veya sesli mesaj alma henüz uygulanmadı. Canlı bot testi için gerçek BotFather tokenı ve eşleştirme gerekir.
