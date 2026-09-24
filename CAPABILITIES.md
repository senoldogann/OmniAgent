# OmniAgent yetenek envanteri

Bu belge, 24 Eylül 2026 itibarıyla kodda bulunan yetenekleri ve bu makinedeki bağlantı durumunu ayırır. Bir aracın kodda bulunması, harici hesaba giriş yapıldığı veya her uygulamada çalışacağı anlamına gelmez.

## Görev akışı

Kullanıcı hedefi arayüzden, CLI'den veya eşleştirilmiş özel Telegram sohbetinden alınır. Ajan, varsayılan `ollama-cloud` modeliyle kısa bir araç çağırma döngüsü çalıştırır; araç sonucunu görüp gerektiğinde sonraki tura geçer. Bir turda bağımsız okumalar paralel, yan etkili işlemler sırayla çalışır. Composer'daki Normal profil 25 tur/10 dakika, Uzun profil 50 tur/20 dakika, Otonom profil 100 tur/45 dakika bütçe sunar; kullanıcı cevabı bekleme süresi bu bütçeden düşülür. API model akışı ve kabuk çıktısı arayüze canlı gelir; CLI modelleri tamamlanmış turu yayınlar. `Esc` çalışan görevi durdurur.

Arayüzde model seçimi, sohbet geçmişi, komut/araç önizlemeleri, canlı çıktı, hata ve bağlantı durumları bulunur. Görev bitince alt bölümde toplam süre, model/araç süresi, tur ve araç sayısı, kesin giriş/önbellek/yeni giriş/çıkış/toplam token sayısı görünür. Entegrasyon kullanıldıysa ağ isteği, keşif, kurulum ve bekleme ölçüleri de gösterilir. `⌘K` transkripti ve sohbet bağlamını temizler.

## Yerleşik araçlar

Başlangıçta modele 16 şema açılır; harici entegrasyon araçları yalnız ilgili görevde eklenir.
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
| Açık Chrome oturumu | `chrome_active_tab`, `cua_click_point`, `cua_type_text`, `cua_press_key`, `cua_submit_text` | Aynı siteye ait açık sekmeyi bulup yüklenmesini bekleme; tek çağrıda alana yazıp gönderme; her eylem turunun ekran durulunca otomatik gözlemle bitmesi |
| macOS arayüzü | `cua_get_app`, `cua_get_ax_state`, `cua_click`, `smart_click`, `run_action_sequence`, `take_screenshot` | Uygulama açma/öne getirme; erişilebilirlik ağacı; AX veya görsel şablonla tıklama; fare/klavye dizisi; ekran görüntüsünü modele verme |
| Entegrasyon | `discover_capabilities` | Yerel kataloğu ve gerektiğinde kısa çevrimiçi keşfi kullanıp görev için uygun API/MCP/skill yolunu bulma |
| Kullanıcı hafızası | `user_memory` | Açıkça istenen tercih, sık yol ve kararı atomik JSON dosyasında saklama, arama ve silme |

Kabuk koruması bilinen yıkıcı komutları, çözülemeyen kabuk değişkeni hedeflerini ve hassas yollara yazmayı engeller; `fetch_raw` yalnızca http(s) adres kabul eder. Hassas dosya okuması varsayılan olarak kapalıdır. Bu kod seviyesi raylar tam güvenlik yalıtımı değildir. GUI eylemleri macOS erişilebilirlik/ekran kaydı izinlerine bağlıdır. `browse_url` görünmeyen, ayrı bir Chromium oturumu kullanır ve araç sonucu bunu açıkça belirtir. Kullanıcı açık Chrome oturumunu açıkça istediğinde bu araç, entegrasyon keşfi ve CDP araştırmasına yol açan kabuk/Node araçları o görevde kapatılır; `chrome_active_tab` aynı siteye ait açık sekmeyi bulup kullanır. Chrome görevlerinde hataya açık iç içe eylem dizisi yerine düz parametreli tıklama/yazma/tuş araçları kullanılır; bir turda birden çok çağrı model sırasıyla işlenir. Chrome AX ağacı web içeriğini vermediği için bu yolda AX araçları kapalıdır; ekran 1000×1000 görüntü olarak görülür ve noktalar `point: [x, y]` biçiminde verilir. Ekran görüntüsü, son eylemden sonra sabit bekleme yerine ekranın durulmasını bekler.

## Entegrasyonlar

Outlook/Hotmail için yerleşik Microsoft Graph adaptörü bulunur. Kuralı hesap başına saklayabilir, postaları sunucu tarafında filtreleyebilir, adayları seçebilir, 20'lik Graph batch istekleriyle Çöp Kutusu'na taşıyabilir ve işlem kaydından geri yükleyebilir. Giriş Microsoft OAuth ile kullanıcı tarafından tamamlanır; token Keychain'de tutulur. Bu makinede Outlook uygulama kimliği ve hesap bağlantısı **henüz yapılandırılmamış**; canlı posta kutusu testi yapılmadı. Kurulum ve izin ayrıntıları [INTEGRATIONS.md](INTEGRATIONS.md) içindedir.

Genel MCP katmanı `stdio` ve Streamable HTTP bağlantılarını destekler. Yalnız kaynak/sabit sürüm/güven durumu katalogda açıkça tanımlanan paketler otomatik kurulabilir; Python ve Node bağımlılıkları ana ajan ortamından ayrı tutulur. Uzak araçların yalnız ilgili şemaları modele açılır. Skill metinleri yöntem bilgisi sağlar, kendi başlarına hesap erişimi veya yürütme yetkisi sağlamaz. Bu makinede ek güvenilir MCP kaydı **yok**; bilinmeyen registry sonucu incelenmeden kurulmaz.

Telegram köprüsü `telegram_bridge.py` ile ayrı bir uzak arayüzdür; model aracı veya MCP
değildir. Olay akışı, araç çıktıları, ekran görüntüsü ve süre/token raporu eşleştirilmiş
sohbete gönderilir; `/stop`, `/status`, `/model`, `/mode` ve kullanıcı sorusuna yanıt
desteklenir. Token Keychain'de, izin verilen özel sohbet/kullanıcı yerel dosyada tutulur.
UI ile aynı anda host görevi çalıştırılmaz. Kurulum ve sınırlar [TELEGRAM.md](TELEGRAM.md)
içindedir; gerçek bot tokenı verilmediği için canlı Telegram testi henüz yapılmadı.

Hazır katalog çözümü ağ beklemesi gerektirmez. Yeni hizmette çevrimiçi keşif toplam 8 saniyeyle, paket kurulumu 60 saniyeyle sınırlıdır. Olumsuz keşif 15 dakika, olumlu keşif 24 saat önbelleklenir. Hazır bağlantılar aynı uygulama oturumunda yeniden kullanılır.

## Model ve hız davranışı

Varsayılan profil `ollama-cloud` (`gemma4:cloud`, yerel Ollama API'si); bu makinede Ollama oturumu ve model doğrulandı. `OMNI_OLLAMA_CLOUD_MODEL` ile `ollama list` içinde bulunan başka bir bulut modeli seçilebilir. Araç başarısızlıkları sürerse ChatGPT oturumuyla çalışan `openai` (Codex CLI, GPT-6-Luna), ardından `zen-free` (OpenCode CLI, Muse Spark Contributor Free) kullanılır. Paralı API profilleri `opencode`, `opencode-think`, `claude` ve `minimax` elle seçilebilir. API bağlantıları görevler arasında sıcak tutulur; CLI yedekleri her turda süreç başlatır. Eski büyük araç sonuçları bağlamdan budanır, son tur tam korunur. Sabit GUI beklemesi yerine uygun olduğunda öğe/durum beklenir. Uygulama belirtilmeyen tek fotoğraf hedefinde doğrudan kamera yakalama yolu tercih edilir; Photo Booth gerektiğinde yedektir.

`gpt-oss:20b-cloud` da bu makinede kurulu ve metin/araçlı görevde canlı doğrulandı;
`OMNI_OLLAMA_CLOUD_MODEL=gpt-oss:20b-cloud` ile seçilebilir. Görsel görevler için doğrulanan
varsayılan `gemma4:cloud` korunur. `qwen3.5:cloud` denendi fakat bu hesapta 402 ile
"ücretsiz kullanıma dahil değil" yanıtı verdi; bu yüzden hazır profil yapılmadı.

Yeni uzun görev denemelerinde `ollama-cloud`, sekiz adayı kontrol edip raporlayan
`long_research` görevini 8,3 saniyede doğru tamamladı (6 tur, 13 araç). Hiçbir zaman hazır
olmayacak uç noktayı izleyen `stagnation` görevini 12,1 saniyede sınırlandırılmış başarısızlık
olarak durdurdu (10 tur); iki denemede de ev dizininde istenmeyen dosya oluşmadı.

401/402/403 erişim, alternatif varken 429 hız sınırı veya CLI hatası veren model profili yalnız o
görevde karantinaya alınır; sonraki turda aynı başarısız profile geri dönülmez. 429
Retry-After sırasında uygun başka model varsa ona hemen geçilir; tek model varsa sağlayıcının
bildirdiği kısa bekleme uygulanır. Geçici 5xx/ağ hatası ise sınırlı yeniden denemeye tabidir.

Son sekiz sohbet alışverişi sınırlı uzunlukta bağlam olarak taşınır. Son 30 görevin ölçüm ve araç adımları yerel epizodik kayıtta tutulur, modele otomatik ders olarak enjekte edilmez. `user_memory.json` ise yalnızca kullanıcının açıkça istediği tercih/yol/karar kayıtlarını tutar; her görevde otomatik olarak okunmaz, gizli bilgileri reddeder ve atomik olarak yazılır. Council, StateTree/time-travel, kendi kendine Python araç üretimi ve AST öngörülü worker bu sürümde bulunmaz.

24 Eylül'de `ollama-cloud` 9 deterministik senaryoda üçer koşuyla 27/27 başarı ve 4,2 saniye
medyan verdi. Aynı senaryolarda `openai` (Codex CLI) tek koşuda 9/9 ve 11,3 saniye medyan
verdi; örnek sayıları farklı olduğu için bu tek başına kesin hız oranı değildir. İki koşuda da
benchmark ev dizininde istenmeyen dosya bulmadı. Bulut kullanım sınırı ve oturum durumu
görevler arasında değişebilir.

24 Eylül birleşik kodda yeniden ölçüm: dokuz çekirdek senaryo üçer kez 27/27 başarı,
4,3 saniye medyan verdi. Üçer uzun/takılma koşusu 6/6 beklenen sonucu verdi;
`long_research` medyanı 28,8 saniye, `stagnation` medyanı 15,0 saniyeydi.
Bu koşularda model süresi neredeyse toplam sürenin tamamıydı; sağlayıcı gecikmesi
değişkendir. Tek koşuluk çeviri denemesinde Ollama Cloud yaklaşık 2 saniye,
GPT-6-Luna/Codex CLI 7,2 saniye, Muse Spark/OpenCode CLI 41,0 saniye sürdü;
tek örnekten genel hız sıralaması çıkarılmamalıdır. Bu yüzden varsayılan model
değiştirilmedi. Durum bekleme hedeflerinde son gözlenen JSON status beklenen
değere ulaşmadıysa modelin erken son yanıtı başarı sayılmaz.

23 Eylül'deki önceki genel benchmark 9 senaryoda üçer koşuyla 27/27 başarı ve 4,95 saniye
medyan süre verdi. Aynı koşularda sürenin toplam %99,4'ü model çağrılarında geçti; yerel
araç süresi 27 görevde toplam 0,84 saniyeydi. Fotoğraf görevindeki özel aracın ilk model
kararı üç denemede de doğru aracı seçti (1,63–1,80 saniye); canlı kamera çekiminin uçtan uca
süresi henüz ölçülmedi. Photo Booth'un varsayılan üç saniyelik geri sayımı ve FFmpeg'in
AVFoundation varsayılan kamera erişimi için [Apple kılavuzu](https://support.apple.com/guide/photo-booth/pbhlp3714a9d/mac)
ve [FFmpeg aygıt belgeleri](https://ffmpeg.org/ffmpeg-devices.html) esas alındı.

Açık Chrome yönlendirmesi sonrasında son genel Qwen benchmark'ı 27/27 başarı ve
4,0 saniye medyan verdi.

Görünür Chrome'da arama yapıp ilk üç ilanın kodunu okuyan yerel GUI senaryosunda
(`benchmark.py --only chrome_ilan --concurrency 1`, gecikmeli XHR ve yükleme iskeletli sayfa)
önceki sürüm 2 koşuda 1 başarı, 73 saniye medyan ve 14-24 tur verdi; güncel sürüm 3/3 başarı,
30 saniye medyan ve 6 tur verdi. Aynı anda koşulan genel benchmark'ta güncel sürüm 42/45,
önceki sürüm 44/45 başarı ve 4,7/4,6 saniye medyan verdi; hatalar iki sürümde de aynı türdeydi
(kodun önekini düşürme, sonucu dosyaya ekleme). MiniMax M3 aynı dokuz senaryoda 25/27 ve 26/27 başarı,
1,8 ve 3,3 saniye medyan verdi. Özellikle araç kullanma ve kod öneki kopyalama hataları
görüldüğü için M3 varsayılan yapılmadı; sağlayıcı gecikmesi koşular arasında da değişti.
