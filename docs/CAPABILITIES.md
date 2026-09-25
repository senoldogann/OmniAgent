# OmniAgent yetenek envanteri

Bu belge, 24 Eylül 2026 itibarıyla kodda bulunan yetenekleri ve bu makinedeki bağlantı durumunu ayırır. Bir aracın kodda bulunması, harici hesaba giriş yapıldığı veya her uygulamada çalışacağı anlamına gelmez.

## Görev akışı

Kullanıcı hedefi arayüzden, CLI'den veya eşleştirilmiş özel Telegram sohbetinden alınır. Ajan, varsayılan `ollama-cloud` modeliyle kısa bir araç çağırma döngüsü çalıştırır; araç sonucunu görüp gerektiğinde sonraki tura geçer. Bir turda bağımsız okumalar paralel, yan etkili işlemler sırayla çalışır. Composer'daki Normal profil 25 tur/10 dakika, Uzun profil 50 tur/20 dakika, Otonom profil 100 tur/45 dakika bütçe sunar; kullanıcı cevabı bekleme süresi bu bütçeden düşülür. Model yanıtı ve kabuk çıktısı arayüze canlı akar. `Esc` çalışan görevi durdurur.

Arayüzde model seçimi, sohbet geçmişi, komut/araç önizlemeleri, canlı çıktı, hata ve bağlantı durumları bulunur. Görev sürerken üst sağda süreli bir durum göstergesi görünür; pencere arka plandaysa macOS menü çubuğunda geçici animasyon ve Dock rozeti gösterilir. Arka planda tamamlanan görev macOS bildirimi verir; bildirim görev içeriğini taşımaz. Görev bitince alt bölümde toplam süre, model/araç süresi, tur ve araç sayısı, kesin giriş/önbellek/yeni giriş/çıkış/toplam token sayısı görünür. Entegrasyon kullanıldıysa ağ isteği, keşif, kurulum ve bekleme ölçüleri de gösterilir. `⌘K` transkripti ve sohbet bağlamını temizler.

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

Çoklu monitörde `take_screenshot(display_index=2)` ikinci ekranı ayrı yakalar. Araç bu ekranı sonraki görüntüler için seçili tutar; ekran görüntüsü, AX merkezleri ve tıklama/dizi koordinatları aynı 1000×1000 uzaya bağlıdır. Ekran ana monitörün üstünde veya solundaysa negatif global başlangıç hesaba katılır. Ekran yerleşimi değişirse eski koordinatla tıklama reddedilir ve yeni görüntü istenir. Yerel doğrulamada ikinci ekran 1920×1080 olarak yakalandı; gerçek ikinci ekran tıklaması kullanıcı ekranını etkilememek için çalıştırılmadı.

Kabuk koruması bilinen yıkıcı komutları, çözülemeyen kabuk değişkeni hedeflerini ve hassas yollara yazmayı engeller; `fetch_raw` yalnızca http(s) adres kabul eder. Hassas dosya okuması varsayılan olarak kapalıdır. Bu kod seviyesi raylar tam güvenlik yalıtımı değildir. GUI eylemleri macOS erişilebilirlik/ekran kaydı izinlerine bağlıdır. `browse_url` görünmeyen, ayrı bir Chromium oturumu kullanır ve araç sonucu bunu açıkça belirtir. Kullanıcı açık Chrome oturumunu açıkça istediğinde bu araç, entegrasyon keşfi ve CDP araştırmasına yol açan kabuk/Node araçları o görevde kapatılır; `chrome_active_tab` aynı siteye ait açık sekmeyi bulup kullanır. Chrome görevlerinde hataya açık iç içe eylem dizisi yerine düz parametreli tıklama/yazma/tuş araçları kullanılır; bir turda birden çok çağrı model sırasıyla işlenir. Chrome AX ağacı web içeriğini vermediği için bu yolda AX araçları kapalıdır; ekran 1000×1000 görüntü olarak görülür ve noktalar `point: [x, y]` biçiminde verilir. Ekran görüntüsü, son eylemden sonra sabit bekleme yerine ekranın durulmasını bekler.

## Entegrasyonlar

Outlook/Hotmail için yerleşik Microsoft Graph adaptörü bulunur. Kuralı hesap başına saklayabilir, postaları sunucu tarafında filtreleyebilir, adayları seçebilir, 20'lik Graph batch istekleriyle Çöp Kutusu'na taşıyabilir ve işlem kaydından geri yükleyebilir. Giriş Microsoft OAuth ile kullanıcı tarafından tamamlanır; token Keychain'de tutulur. Bu makinede Outlook uygulama kimliği ve hesap bağlantısı **henüz yapılandırılmamış**; canlı posta kutusu testi yapılmadı. Kurulum ve izin ayrıntıları [INTEGRATIONS.md](INTEGRATIONS.md) içindedir.

Genel MCP katmanı `stdio` ve Streamable HTTP bağlantılarını destekler. Yalnız kaynak/sabit sürüm/güven durumu katalogda açıkça tanımlanan paketler otomatik kurulabilir; Python ve Node bağımlılıkları ana ajan ortamından ayrı tutulur. Uzak araçların yalnız ilgili şemaları modele açılır. Skill metinleri yöntem bilgisi sağlar, kendi başlarına hesap erişimi veya yürütme yetkisi sağlamaz. Kullanıcının açık kurulum hedefinde `install_skill`, skills.sh deposunun ilgili dizinini sabit GitHub commitinden kalıcı depoya indirir; betikleri çalıştırmaz. Kurulu yerel skill dosyaları uygulama veri dizinindeki `skills/`, `~/.agents/skills` ve `~/.codex/skills` altından dizinlenir; `OMNI_SKILLS_DIRS` ile ek dizinler seçilebilir. Masaüstü ve Telegram `/tools`, `/skills` komutları katalog bilgisini model çağrısı yapmadan gösterir. `discover_capabilities(query="catalog", operations=[], allow_online=false)` yerel yetenek envanterini ağ çağrısı olmadan verir. Tekrarlı yerel işlemlerde ajan tek `execute_js` çağrısında geçici yardımcı kod kullanabilir. Aynı kod yeniden gerekiyorsa `// omni:save ad` ile başarılı JS kodunu yalnız görev belleğine kaydeder ve `// omni:run ad` ile JSON girdisiyle tekrar çalıştırır. Kalıcı eklenti ve kendi kaynak değişikliği yalnız açık görev kapsamında yapılır. Context7 anahtarsız uzak MCP olarak kayıtlıdır; bilinmeyen registry sonucu incelenmeden kurulmaz.

Telegram köprüsü `telegram_bridge.py` ile ayrı bir uzak arayüzdür; model aracı veya MCP
değildir. Varsayılan kısa görünüm yanıtı tek balonda canlı günceller; araç turunda yalnız
çalışma durumu görünür. `/verbose on` sonraki görevde araç/çıktı/model/süre/token
ayrıntılarını açar. Ekran görüntüsü ayrıca gönderilir. `/stop`, `/status`, `/model`,
`/mode`, `/tools`, `/skills` ve kullanıcı sorusuna yanıt desteklenir. Token Keychain'de, izin verilen özel
sohbet/kullanıcı yerel dosyada tutulur. UI ile aynı anda host görevi çalıştırılmaz.
Ekran kaydı izni eksikse araç "izin yok" demekle kalmaz: izni alacak uygulamayı adı + bundle
kimliğiyle, yoksa eklenecek tam python yolunu ve Sistem Ayarları komutunu yazar; ilk eksik
denemede macOS'un izin istemi bir kez gösterilir (uygulama sisteme ancak böyle kaydolur).
`uv run python permissions.py` aynı tanıyı komut satırında verir.

Kurulum ve sınırlar [TELEGRAM.md](TELEGRAM.md) içindedir. Bu değişiklikte canlı
Telegram mesajı gönderilmedi; yerel hizmetin çalıştığı doğrulandı.

Hazır katalog çözümü ağ beklemesi gerektirmez. Yeni hizmette çevrimiçi keşif toplam 8 saniyeyle, paket kurulumu 60 saniyeyle sınırlıdır. Olumsuz keşif 15 dakika, olumlu keşif 24 saat önbelleklenir. Hazır bağlantılar aynı uygulama oturumunda yeniden kullanılır.

## Model ve hız davranışı

Varsayılan profil `ollama-cloud` (`gemma4:cloud`, yerel Ollama API'si); bu makinede Ollama oturumu ve model doğrulandı. `OMNI_OLLAMA_CLOUD_MODEL` ile `ollama list` içinde bulunan başka bir bulut modeli seçilebilir. Araç başarısızlıkları sürerse API anahtarıyla çağrılan `openai` (OpenAI API, GPT-6-Luna), ardından `openrouter` (`anthropic/claude-sonnet-5`) kullanılır; `opencode` ve `opencode-think` (Opencode Go, `qwen3.8-flash`) elle seçilebilir. Bilgisayardaki Codex/OpenCode oturumuna dayanan CLI bağlayıcıları kaldırıldı: her tur ayrı süreç başlatıyordu. Anahtarlar `OPENAI_API_KEY`, `OPENCODE_API_KEY` ve `OPENROUTER_API_KEY` değişken adlarıyla süreç-içi depodan okunur (kayıt yoksa kabuk ortam değişkeni yedektir); anahtarı tanımlı olmayan profil o görevde kullanılamaz. Arayüzün sağ üstündeki **Ayarlar** (⚙) sayfasından girilen anahtarlar macOS Keychain'de saklanır, kaydedildikleri anda süreç-içi depoya ve model istemcilerine uygulanır (görev sürüyorsa görev bitince), sonraki açılışlarda da yüklenir. Kayıtlı anahtar kabuk değişkenini geçersiz kılar, çünkü kabuk değişkeni ajanın başlattığı alt süreçlere miras kalır: anahtarlar `os.environ`'a bilinçli olarak yazılmaz, alt süreçlere anahtar değişkenleri çıkarılmış ortam verilir ve araç çıktısı modele/transkripte gitmeden önce maskelenir. Tüm API bağlantıları görevler arasında sıcak tutulur. Eski büyük araç sonuçları bağlamdan budanır, son tur tam korunur. Sabit GUI beklemesi yerine uygun olduğunda öğe/durum beklenir. Uygulama belirtilmeyen tek fotoğraf hedefinde doğrudan kamera yakalama yolu tercih edilir; Photo Booth gerektiğinde yedektir.

`gpt-oss:20b-cloud` da bu makinede kurulu ve metin/araçlı görevde canlı doğrulandı;
`OMNI_OLLAMA_CLOUD_MODEL=gpt-oss:20b-cloud` ile seçilebilir. Görsel görevler için doğrulanan
varsayılan `gemma4:cloud` korunur. `qwen3.5:cloud` denendi fakat bu hesapta 402 ile
"ücretsiz kullanıma dahil değil" yanıtı verdi; bu yüzden hazır profil yapılmadı.

Fast Loop semantik ilerlemeyi host seviyesinde izler. Yeni ve başarılı komut çıktısı en fazla sekiz farklı sonuç için ilerleme sayılır; aynı veya boş çıktı sayılmaz. Eylem isteklerinde yalnız keşif, okuma veya gezinme sonucu başarı kanıtı değildir: host gerçek bir işlem denemesi ister, ardından kanıt yoksa görevi başarısız işaretler. Görsel olmayan turlarda iki anlamsız tur
sonra replan, görsel otomatik gözlem taşıyan turlarda üç tur tolerans, delivery aşamasında iki
anlamsız tur sonra bounded-stop uygulanır. 24 Eylül'deki üçer stress koşusunda
`long_research` 3/3 başarı, 6 tur/13 araç ve 8,5 saniye medyan; `stagnation` 3/3 beklenen
bounded-failure, 7 tur ve 5,4 saniye medyan verdi. Önceki stagnation politikası 10 tur
harcıyordu. Koşularda ev dizininde istenmeyen dosya oluşmadı.

401/402/403 erişim veya alternatif varken 429 hız sınırı veren model profili yalnız o
görevde karantinaya alınır; sonraki turda aynı başarısız profile geri dönülmez. 429
Retry-After sırasında uygun başka model varsa ona hemen geçilir; tek model varsa sağlayıcının
bildirdiği kısa bekleme uygulanır. Geçici 5xx/ağ hatası ise sınırlı yeniden denemeye tabidir.

Son sekiz sohbet alışverişi sınırlı uzunlukta bağlam olarak taşınır. Son 30 görevin ölçüm ve araç adımları yerel epizodik kayıtta tutulur, modele otomatik ders olarak enjekte edilmez. `user_memory.json` kullanıcının açıkça istediği tercih/yol/karar kayıtlarını tutar; kısa kayıtlar her görev başında sistem bağlamına eklenir, gizli bilgiler reddedilir ve dosya atomik yazılır. Council, StateTree/time-travel, kendi kendine Python araç üretimi ve AST öngörülü worker bu sürümde bulunmaz.

24 Eylül son çekirdek ölçümünde `ollama-cloud` 9 deterministik senaryoda üçer koşuyla
27/27 başarı ve 4,0 saniye medyan verdi. Model süresi toplam sürenin büyük bölümüdür;
sağlayıcı gecikmesi değişkendir. OpenAI sözleşmesinde `tools` varsa `tool_choice` varsayılanı
zaten `auto`dur; Ollama'nın güncel tool-calling örnekleri de `tools` alanını bu ek parametre
olmadan kullanır. Bu yüzden redundant `tool_choice="auto"` gönderilmez; canlı A/B'de aynı
tool call korunurken tek request 0,727→0,628 saniye ölçüldü. Eski Codex/OpenCode CLI hız
ölçümleri güncel mimariyi temsil etmez; CLI bağlayıcıları kaldırılmıştır. Bulut kullanım
sınırı ve API bakiyesi görevler arasında değişebilir. Durum bekleme hedeflerinde son gözlenen
JSON status beklenen değere ulaşmadıysa modelin erken son yanıtı başarı sayılmaz.

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

24 Eylül'deki Chrome blocker kapanışında açık-Chrome görsel yolu tam ekrandan pencere-scope'a
alındı. `chrome_active_tab` sonrası screenshot ve ekran-durulma karşılaştırması yalnız öndeki
Google Chrome penceresinin Quartz görüntüsünü kullanır; modelin 1000×1000 koordinatı pencerenin
ekran origin'iyle gerçek fare noktasına çevrilir. Böylece başka uygulamaların modal/pencereleri
model görseline karışmaz ve kullanıcıdaki eşzamanlı uygulamalara dokunulmaz. Taze gecikmeli-XHR
`chrome_ilan` final koşuları iki kez 1/1 başarı, 15,5 / 17,1 saniye ve her ikisinde 4 tur/8
araç verdi; üç beklenen ilan kodunun tamamı iki koşuda da doğru okundu.

## Otonom kurtarma ve doğrulanmış öğrenme

`experience.py` yalnız başarısız çağrıdan sonra ilişkili ve değiştirilmiş çağrı başarıya
ulaşıp görev de başarılı biterse ders saklar. Ders görev başında enjekte edilmez; aynı
araç/hata imzası tekrarlandığında hatırlatılır ve işe yaramayan dersler budanır. 24 Eylül
canlı `self_repair` ölçümünde 3/3 başarı: ilk koşu 5 tur/4 araç/4,5 sn; öğrenilmiş iki koşu
3 tur/2 araç ve 3,6/2,1 sn. Finansal para hareketleri ile kullanıcının istemediği kalıcı
hafıza mutasyonları `approval.py` host kapısından geçer; onay yoksa araç çalıştırılmaz.


24 Eylül ek canlı `self_repair` kontrolünde önce doğru `OZET: 42` komut çıktısı alınmasına rağmen Fast Loop son yanıtı beklemeden başarısız durdu (6 tur/6 araç). Yeni çıktıyı ilerleme sayan düzeltmeden sonra aynı senaryo 1/1 başarıyla 7 tur/6 araçta tamamlandı. Aynı kök altında ardışık iki koşu da başarılıydı; ikinci koşuda deneyim dersi kullanımı araç sayısını 4→2, tur sayısını 5→3 indirdi. Sağlayıcının iki geçici isteği geciktirmesi ikinci koşunun duvar süresini 126,6 saniyeye çıkardı; bu tek başına yerel araç süresinin ölçüsü değildir. Model isteği başına zaman aşımı bunun ardından 60 saniyeden 30 saniyeye indirildi; yeni sınırın canlı sağlayıcı hatasındaki etkisi henüz tekrar ölçülmedi.
