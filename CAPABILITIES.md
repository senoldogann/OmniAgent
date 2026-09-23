# OmniAgent yetenek envanteri

Bu belge, 23 Eylül 2026 itibarıyla kodda bulunan yetenekleri ve bu makinedeki bağlantı durumunu ayırır. Bir aracın kodda bulunması, harici hesaba giriş yapıldığı veya her uygulamada çalışacağı anlamına gelmez.

## Görev akışı

Kullanıcı hedefi arayüzden veya CLI'den alınır. Ajan, varsayılan `opencode` modeliyle kısa bir araç çağırma döngüsü çalıştırır; araç sonucunu görüp gerektiğinde sonraki tura geçer. Bir turda bağımsız okumalar paralel, yan etkili işlemler sırayla çalışır. Görev başına sınır 25 tur ve kullanıcı cevabı bekleme süresi hariç 10 dakikadır. Model akışı ve kabuk çıktısı arayüze canlı gelir; `Esc` çalışan görevi durdurur.

Arayüzde model seçimi, sohbet geçmişi, komut/araç önizlemeleri, canlı çıktı, hata ve bağlantı durumları bulunur. Görev bitince alt bölümde toplam süre, model/araç süresi, tur ve araç sayısı, kesin giriş/önbellek/yeni giriş/çıkış/toplam token sayısı görünür. Entegrasyon kullanıldıysa ağ isteği, keşif, kurulum ve bekleme ölçüleri de gösterilir. `⌘K` transkripti ve sohbet bağlamını temizler.

## Yerleşik araçlar

Başlangıçta modele 15 şema açılır; harici entegrasyon araçları yalnız ilgili görevde eklenir.
Masaüstüne tek fotoğraf çekme hedefinde (özel dosya adı verilmemişse) ayrıca `capture_photo`
açılır. Araç varsayılan macOS kamerasından tek kare alıp `~/Desktop` altında benzersiz bir
adla kaydeder, görüntüyü doğrular ve başarısız çekimi başarı saymaz.
Doğrudan çekim için `ffmpeg` ve macOS kamera izni gerekir; araç başarısızsa ajan Photo Booth
yoluna geçebilir. Kullanıcının görüntüsü yeni bir canlı testte alınmadı.

| Alan | Araçlar | Yapabildikleri |
| --- | --- | --- |
| Sistem | `execute_shell`, `process_list`, `execute_js` | macOS kabuğu ve Node.js çalıştırma; süreçleri özetleme |
| Dosya | `read_file`, `write_file` | Dosya okuma/yazma; Python sözdizimi denetimi, önceki sürüm yedeği ve yazma doğrulaması |
| Web | `web_search`, `fetch_raw`, `browse_url` | Arama; hızlı HTTP/HTML/JSON okuma; kalıcı Playwright sekmesinde DOM eylem dizisi |
| Açık Chrome oturumu | `chrome_active_tab` | Kullanıcının görünür Chrome penceresindeki etkin sekmenin adresini/başlığını okuma veya aynı sekmede URL açma |
| macOS arayüzü | `cua_get_app`, `cua_get_ax_state`, `cua_click`, `smart_click`, `run_action_sequence`, `take_screenshot` | Uygulama açma/öne getirme; erişilebilirlik ağacı; AX veya görsel şablonla tıklama; fare/klavye dizisi; ekran görüntüsünü modele verme |
| Entegrasyon | `discover_capabilities` | Yerel kataloğu ve gerektiğinde kısa çevrimiçi keşfi kullanıp görev için uygun API/MCP/skill yolunu bulma |

Kabuk koruması bilinen yıkıcı komutları ve hassas yollara yazmayı engeller; hassas dosya okuması varsayılan olarak kapalıdır. Bu kod seviyesi raylar tam güvenlik yalıtımı değildir. GUI eylemleri macOS erişilebilirlik/ekran kaydı izinlerine bağlıdır. `browse_url` ayrı bir Chromium oturumu kullanır. Kullanıcı açık Chrome oturumunu açıkça istediğinde bu araç, entegrasyon keşfi ve CDP araştırmasına yol açan kabuk/Node araçları o görevde kapatılır; `chrome_active_tab` ile mevcut görünür sekme kullanılır. Chrome AX ağacı sayfa içeriğini göstermiyorsa ekran görüntüsü gerekir.

## Entegrasyonlar

Outlook/Hotmail için yerleşik Microsoft Graph adaptörü bulunur. Kuralı hesap başına saklayabilir, postaları sunucu tarafında filtreleyebilir, adayları seçebilir, 20'lik Graph batch istekleriyle Çöp Kutusu'na taşıyabilir ve işlem kaydından geri yükleyebilir. Giriş Microsoft OAuth ile kullanıcı tarafından tamamlanır; token Keychain'de tutulur. Bu makinede Outlook uygulama kimliği ve hesap bağlantısı **henüz yapılandırılmamış**; canlı posta kutusu testi yapılmadı. Kurulum ve izin ayrıntıları [INTEGRATIONS.md](INTEGRATIONS.md) içindedir.

Genel MCP katmanı `stdio` ve Streamable HTTP bağlantılarını destekler. Yalnız kaynak/sabit sürüm/güven durumu katalogda açıkça tanımlanan paketler otomatik kurulabilir; Python ve Node bağımlılıkları ana ajan ortamından ayrı tutulur. Uzak araçların yalnız ilgili şemaları modele açılır. Skill metinleri yöntem bilgisi sağlar, kendi başlarına hesap erişimi veya yürütme yetkisi sağlamaz. Bu makinede ek güvenilir MCP kaydı **yok**; bilinmeyen registry sonucu incelenmeden kurulmaz.

Hazır katalog çözümü ağ beklemesi gerektirmez. Yeni hizmette çevrimiçi keşif toplam 8 saniyeyle, paket kurulumu 60 saniyeyle sınırlıdır. Olumsuz keşif 15 dakika, olumlu keşif 24 saat önbelleklenir. Hazır bağlantılar aynı uygulama oturumunda yeniden kullanılır.

## Model ve hız davranışı

Varsayılan profil `opencode` (`qwen3.8-flash`, düşünme kapalıdır). Araç başarısızlıkları sürerse `opencode-think`, ardından anahtarı kullanılabiliyorsa `claude` profiline yükselir. `minimax` (MiniMax M3, düşünme kapalı) ve `openai` elle seçilebilir. Bağlantılar görevler arasında sıcak tutulur; eski büyük araç sonuçları bağlamdan budanır, son tur tam korunur. Sabit GUI beklemesi yerine uygun olduğunda öğe/durum beklenir. Uygulama belirtilmeyen tek fotoğraf hedefinde doğrudan kamera yakalama yolu tercih edilir; Photo Booth gerektiğinde yedektir.

Son sekiz sohbet alışverişi sınırlı uzunlukta bağlam olarak taşınır. Son 30 görevin ölçüm ve araç adımları yerel epizodik kayıtta tutulur, modele otomatik ders olarak enjekte edilmez. Council, StateTree/time-travel, kendi kendine Python araç üretimi ve AST öngörülü worker bu sürümde bulunmaz.

23 Eylül'deki son genel benchmark 9 senaryoda üçer koşuyla 27/27 başarı ve 4,95 saniye
medyan süre verdi. Aynı koşularda sürenin toplam %99,4'ü model çağrılarında geçti; yerel
araç süresi 27 görevde toplam 0,84 saniyeydi. Fotoğraf görevindeki özel aracın ilk model
kararı üç denemede de doğru aracı seçti (1,63–1,80 saniye); canlı kamera çekiminin uçtan uca
süresi henüz ölçülmedi. Photo Booth'un varsayılan üç saniyelik geri sayımı ve FFmpeg'in
AVFoundation varsayılan kamera erişimi için [Apple kılavuzu](https://support.apple.com/guide/photo-booth/pbhlp3714a9d/mac)
ve [FFmpeg aygıt belgeleri](https://ffmpeg.org/ffmpeg-devices.html) esas alındı.

Açık Chrome yönlendirmesi sonrasında son genel Qwen benchmark'ı 27/27 başarı ve
4,0 saniye medyan verdi. MiniMax M3 aynı dokuz senaryoda 25/27 ve 26/27 başarı,
1,8 ve 3,3 saniye medyan verdi. Özellikle araç kullanma ve kod öneki kopyalama hataları
görüldüğü için M3 varsayılan yapılmadı; sağlayıcı gecikmesi koşular arasında da değişti.
