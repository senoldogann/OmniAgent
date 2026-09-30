# OmniAgent yetenek envanteri

Bu belge, 27 Eylül 2026 itibarıyla kodda bulunan yetenekleri ve bu makinedeki bağlantı durumunu ayırır. Bir aracın kodda bulunması, harici hesaba giriş yapıldığı veya her uygulamada çalışacağı anlamına gelmez.

## Görev akışı

Kullanıcı hedefi arayüzden, CLI'den veya eşleştirilmiş özel Telegram sohbetinden alınır. Ajan, varsayılan `ollama-cloud` modeliyle kısa bir araç çağırma döngüsü çalıştırır; araç sonucunu görüp gerektiğinde sonraki tura geçer. Bir turda bağımsız okumalar paralel, yan etkili işlemler sırayla çalışır. Composer'daki Normal profil 25 tur/10 dakika, Uzun profil 50 tur/20 dakika, Otonom profil 100 tur/45 dakika bütçe sunar; kullanıcı cevabı bekleme süresi bu bütçeden düşülür. Model yanıtı ve kabuk çıktısı arayüze canlı akar. `Esc` çalışan görevi durdurur.

Arayüzde model seçimi, kalıcı sohbet geçmişi, komut/araç önizlemeleri, canlı çıktı, hata ve bağlantı durumları bulunur. Soldaki listeden eski sohbet açılır; **Yeni sohbet** düğmesi ve `⌘K` yeni, boş bir sohbet başlatır. Eski sohbetler silinmez. Masaüstü sohbetleri `~/Library/Application Support/OmniAgent/desktop_chats/` altında sohbet başına ayrı JSON dosyasında saklanır; son açık sohbet uygulama yeniden başlayınca yüklenir. Görev sürerken üst sağda süreli bir durum göstergesi görünür; pencere arka plandaysa macOS menü çubuğunda geçici animasyon ve Dock rozeti gösterilir. Arka planda tamamlanan görev macOS bildirimi verir; bildirim görev içeriğini taşımaz. Görev bitince alt bölümde toplam süre, model/araç süresi, tur ve araç sayısı, kesin giriş/önbellek/yeni giriş/çıkış/toplam token sayısı görünür. Kullanıcı mesajının altında gönderim saati ve yalnız o mesajı kopyalayan simge durur; gönderim zamanları sohbet kaydıyla saklanır, eski kayıtlarda saat bulunmaz. Ana pencerede klavye odağı composer'dır: kartın dolgusuna, üst şeride ya da sohbete tıklamak odağı giriş alanına döndürür (CustomTkinter tıklanan bileşene odak verdiği için aksi hâlde yazılan komut düşüyordu); transkriptte metin seçiliyken odak metinde kalır. Boş alanda yer tutucu yazı, imlecin (caret) sağından başlar; imleç yer tutucunun opak arka planı altında kalıp görünmez olmaz. Entegrasyon kullanıldıysa ağ isteği, keşif, kurulum ve bekleme ölçüleri de gösterilir. Genel `⌘X` tüm OmniAgent pencerelerini gizleyip yeniden gösterir; macOS'taki Kes kısayoluyla çakışır. Gizleme görev durumunu korur ve menü çubuğu göstergesini kaldırır. OmniAgent kendi tam ekran yakalamalarından UI pencerelerini çıkarır; üçüncü taraf ekran görüntüsü veya videosunda görünür pencereyi gizleme garantisi yoktur.

## Yerleşik araçlar

Genel görevde modele 20 şema (açık Chrome yolunda 14) açılır; harici entegrasyon araçları yalnız
ilgili görevde eklenir.
Masaüstüne tek fotoğraf çekme hedefinde (özel dosya adı verilmemişse) ayrıca `capture_photo`
açılır. Araç varsayılan macOS kamerasından tek kare alıp `~/Desktop` altında benzersiz bir
adla kaydeder, görüntüyü doğrular ve başarısız çekimi başarı saymaz.
Doğrudan çekim için `ffmpeg` ve macOS kamera izni gerekir; araç başarısızsa ajan Photo Booth
yoluna geçebilir. Kullanıcının görüntüsü yeni bir canlı testte alınmadı.

| Alan | Araçlar | Yapabildikleri |
| --- | --- | --- |
| Sistem | `execute_shell`, `process_list`, `execute_js`, `execute_python` | macOS kabuğu, Node.js ve Python 3 çalıştırma; süreçleri özetleme |
| Dosya | `read_file`, `write_file` | Dosya okuma/yazma; Python sözdizimi denetimi, önceki sürüm yedeği ve yazma doğrulaması |
| Web | `web_search`, `fetch_raw`, `browse_url` | Arama; hızlı HTTP/HTML/JSON okuma; kalıcı Playwright sekmesinde DOM eylem dizisi |
| Açık Chrome oturumu | `chrome_active_tab`, `cua_click_point`, `cua_type_text`, `cua_press_key`, `cua_submit_text`, `cua_fill_field` | Aynı siteye ait açık sekmeyi bulup yüklenmesini bekleme veya `new_tab=true` ile yeni sekme açma; tek çağrıda arama kutusuna yazıp gönderme; form alanını Enter'a basmadan doldurma |
| macOS arayüzü | `cua_get_app`, `cua_snapshot`, `cua_click_element`, `cua_set_text_element`, `cua_get_ax_state`, `cua_click`, `smart_click`, `run_action_sequence`, `take_screenshot` | Uygulama açma/öne getirme; indeksli görünür öğe listesi (arka planda AXPress/AXValue, etki doğrulaması, gerekirse geçici ön plan tıklaması); eski numaralı erişilebilirlik listesi; AX veya görsel şablonla tıklama; fare/klavye dizisi (sağ/çift/üçlü tıklama, sürükle-bırak); ekran görüntüsünü modele verme |
| Ekran metni ve kaydırma (her GUI yolunda) | `cua_click_text`, `cua_scroll`, `cua_read_scrollable` | Görünür metni OCR ile bulup tam ortasına tıklama; paneli kaydırıp içeriğin kayıp kaymadığını (liste/sayfa sonu) ölçme; paneli baştan sona kaydırarak tüm metni tek sonuçta okuma |
| Entegrasyon | `discover_capabilities` | Yerel kataloğu ve gerektiğinde kısa çevrimiçi keşfi kullanıp görev için uygun API/MCP/skill yolunu bulma |
| Kullanıcı hafızası | `user_memory` | Açıkça istenen tercih, sık yol ve kararı atomik JSON dosyasında saklama, arama ve silme |
| Telegram dosya teslimi | `send_file` (yalnız Telegram görevinde) | Bilgisayardaki dosyayı (en çok 50 MB) eşleştirilmiş sohbete belge olarak gönderme |
| Zamanlanmış görevler | `schedule_task` (zamanlama niyetli hedefte, Telegram eşleştirilmişse) | Görevi bir kez, her gün, seçili günlerde veya aralıkla planlama; sonuç Telegram'a gelir |

Çoklu monitörde `take_screenshot(display_index=2)` ikinci ekranı ayrı yakalar. Araç bu ekranı sonraki görüntüler için seçili tutar; ekran görüntüsü, AX merkezleri, OCR kutuları ve tıklama/dizi koordinatları aynı 0-1000 uzaya bağlıdır. Ekran ana monitörün üstünde veya solundaysa negatif global başlangıç hesaba katılır. Ekran yerleşimi değişirse eski koordinatla tıklama reddedilir ve yeni görüntü istenir. Yerel doğrulamada ikinci ekran 1920×1080 olarak yakalandı; gerçek ikinci ekran tıklaması kullanıcı ekranını etkilememek için çalıştırılmadı.

Kabuk koruması bilinen yıkıcı komutları, çözülemeyen kabuk değişkeni hedeflerini ve hassas yollara yazmayı engeller; `fetch_raw` yalnızca http(s) adres kabul eder. Hassas dosya okuması varsayılan olarak kapalıdır. Bu kod seviyesi raylar tam güvenlik yalıtımı değildir. GUI eylemleri macOS erişilebilirlik/ekran kaydı izinlerine bağlıdır. `browse_url` görünmeyen, ayrı bir Chromium oturumu kullanır ve araç sonucu bunu açıkça belirtir. Kullanıcı açık Chrome oturumunu açıkça istediğinde bu araç, entegrasyon keşfi ve CDP araştırmasına yol açan kabuk/Node araçları o görevde kapatılır; `chrome_active_tab` aynı siteye ait açık sekmeyi bulup kullanır. Chrome görevlerinde hataya açık iç içe eylem dizisi yerine düz parametreli tıklama/yazma/tuş araçları kullanılır; bir turda birden çok çağrı model sırasıyla işlenir. Chrome yolunda öğe tabanlı AX araçları (`cua_snapshot`, `cua_click_element`, `cua_set_text_element`) açıktır (Chrome web erişilebilirliği ilk çağrıda `AXEnhancedUserInterface` ile açılır, Chrome yeniden başlayana kadar kalır); eski numaralı liste araçları (`cua_get_ax_state`, `cua_click`, `smart_click`) bu yolda kapalıdır; ekran modele 1000×1000 kare görüntü olarak gider ve noktalar `point: [x, y]` biçiminde verilir. Kaydedilen ve Telegram'a gönderilen ekran görüntüsü ise ekranın gerçek en-boy oranındadır. Ekran görüntüsü, son eylemden sonra sabit bekleme yerine ekranın durulmasını bekler. Takip mesajı ("devam et", "formda eksik alan var") önceki görev açık Chrome yolunda yürüdüyse aynı yolda sürer.

Görünür metne tıklama (`cua_click_text`) macOS Vision OCR ile tam Retina çözünürlükte çalışır; aynı metin birden çok yerdeyse yakınlık noktası olmadan tıklamaz. Kaydırma aracı içeriğin kayıp kaymadığını ölçer: "KAYMADI" sonucu ya sona gelindiğini ya da kaydırılamaz/yanlış bir alan seçildiğini gösterir; tam kapsamın kanıtı `cua_read_scrollable` sonucunun kısaltma uyarısız "sona ulaşıldı" ifadesidir. `cua_read_scrollable` bir paneli baştan sona kaydırıp tüm metnini tek sonuçta verir ve paneli yeniden başa döndürür; sayfa OCR'ları kaydırma/durulma sürerken arka planda koşar (birleştirme sayfa sırasıyla, çıktı sıralı okumayla aynı) ve okuma ekranı durulmuş bırakır: sonraki OCR/gözlem ek durulma beklemez. Chrome'un ana penceresinin önüne düşebilen küçük yardımcı pencere ekran kapsamı olarak seçilmez. Gerçek ekran boyutu değişiminde açık hata verir ve görev yeniden deneyebilir. Her GUI yolunda eylem turu ekran durulunca otomatik gözlemle biter; ekranda tıklama/yazma/kaydırma yapılan görev, ilk final yanıtta bir kez güncel ekranla her zorunlu maddeyi doğrulamaya yönlendirilir. Açık yeni sekme ve LinkedIn akışı gibi somut isteklerde araç kanıtı eksikse bitiş engellenir; model içeriği okuyamadığını bildirirse arayüz görevi tamamlandı saymaz.

## Entegrasyonlar

Outlook/Hotmail için yerleşik Microsoft Graph adaptörü bulunur. Kuralı hesap başına saklayabilir, postaları sunucu tarafında filtreleyebilir, adayları seçebilir, 20'lik Graph batch istekleriyle Çöp Kutusu'na taşıyabilir ve işlem kaydından geri yükleyebilir. Giriş Microsoft OAuth ile kullanıcı tarafından tamamlanır; token Keychain'de tutulur. Bu makinede Outlook uygulama kimliği ve hesap bağlantısı **henüz yapılandırılmamış**; canlı posta kutusu testi yapılmadı. Kurulum ve izin ayrıntıları [INTEGRATIONS.md](INTEGRATIONS.md) içindedir.

Genel MCP katmanı `stdio` ve Streamable HTTP bağlantılarını destekler. Yalnız kaynak/sabit sürüm/güven durumu katalogda açıkça tanımlanan paketler otomatik kurulabilir; Python ve Node bağımlılıkları ana ajan ortamından ayrı tutulur. Uzak araçların yalnız ilgili şemaları modele açılır. Skill metinleri yöntem bilgisi sağlar, kendi başlarına hesap erişimi veya yürütme yetkisi sağlamaz. Kurulu yerel skill dosyaları `~/.agents/skills` ve `~/.codex/skills` altından başlangıçta ve eşleşme bulunamadığında dizinlenir; `OMNI_SKILLS_DIRS` ile özel dizinler seçilebilir. `discover_capabilities(query="catalog", operations=[], allow_online=false)` yerel yetenek envanterini ağ çağrısı olmadan verir. Tekrarlı yerel işlemlerde ajan tek `execute_js` ya da `execute_python` çağrısında geçici yardımcı kod kullanabilir. Aynı kod yeniden gerekiyorsa `// omni:save ad` (JS) ya da `# omni:save ad` (Python) ile başarılı kodu yalnız görev belleğine kaydeder ve `// omni:run ad` / `# omni:run ad` ile JSON girdisiyle tekrar çalıştırır. Kalıcı eklenti ve kendi kaynak değişikliği yalnız açık görev kapsamında yapılır. Context7 anahtarsız uzak MCP olarak kayıtlıdır; bilinmeyen registry sonucu incelenmeden kurulmaz.

Telegram köprüsü `telegram_bridge.py` ile ayrı bir uzak arayüzdür; model aracı veya MCP
değildir. Varsayılan kısa görünüm yanıtı tek balonda canlı günceller; araç turunda yalnız
çalışma durumu görünür. `/verbose on` sonraki görevde araç/çıktı/model/süre/token
ayrıntılarını açar. Ekran görüntüsü ayrıca gönderilir. `/stop`, `/status`, `/model`,
`/mode` ve kullanıcı sorusuna yanıt desteklenir. Fotoğraf, belge, ses ve video ekleri indirilir; açıklaması görev olur ve görseller modele görüntü olarak verilir. Ajan istenen dosyayı `send_file` ile sohbete gönderir. Zamanlanmış görevleri köprü çalıştırır; `/schedules` ve `/unschedule` planları yönetir. `/update` kodu çekip köprüyü yeniden başlatır, `/restart` yalnız yeniden başlatır, `/doctor` sürüm, izin ve model durumunu gösterir. Açıklamasız sesli mesaj, OpenAI anahtarı tanımlıysa yazıya çevrilip komut olarak çalıştırılır (ses OpenAI'a gönderilir). Token Keychain'de, izin verilen özel
sohbet/kullanıcı yerel dosyada tutulur. UI ile aynı anda host görevi çalıştırılmaz.
Ekran kaydı izni eksikse araç "izin yok" demekle kalmaz: izni alacak uygulamayı adı + bundle
kimliğiyle, yoksa eklenecek tam python yolunu ve Sistem Ayarları komutunu yazar; ilk eksik
denemede macOS'un izin istemi bir kez gösterilir (uygulama sisteme ancak böyle kaydolur).
`.venv/bin/omniagent-permissions` aynı tanıyı (ekran kaydı ve erişilebilirlik) komut satırında verir.
Ekran kilitliyse GUI araçları kilit ekranına tıklamadan/yazmadan "ekran kilitli" hatasıyla durur; kilitsiz
ama uyuyan ekran uyandırılır. Telegram köprüsü açıkken prizdeki Mac uyumaz.

Klavye girdisi (`cua_type_text`, `cua_press_key`, `cua_submit_text`, `cua_fill_field`, `run_action_sequence` type/press
adımları, Chrome'un görünür UI yedeği) olaydan hemen önce sistemin klavye odağındaki uygulamayı doğrular (AX sistem geneli
`AXFocusedApplication`, `tools/foreground.py`; ölçülen 0,2-5 ms, en çok 1,5 sn beklenir). Hedef `chrome_active_tab`, `cua_get_app`,
`cua_click`, `smart_click`, `cua_click_element` ya da `cua_set_text_element` ile seçilmişse ön plan o uygulama olmalıdır
(`FOREGROUND_MISMATCH`); seçilmemişse yalnız hassas ön plan reddedilir: OmniAgent'ın kendisi ya da onu çalıştıran uygulama,
terminal/IDE, Sistem Ayarları/güvenlik pencereleri, parola yöneticileri. Ön plan okunamıyorsa (yanıtsız uygulama, OmniAgent'ın
kendi penceresi önde: AX kendi sürecini okuyamaz) girdi gönderilmez (`FOREGROUND_UNKNOWN`). `cua_set_text_element` hassas
uygulamaya hiç yazmaz (`SENSITIVE_TARGET`). cmd+tab, cmd+shift+tab ve cmd+space denetlenmez ve hedefi bırakır. `cua_get_app`
uygulamanın öne gelmesini ve penceresini (en çok 5 sn) doğrular; uygulama adı betiğe değil argümana gider. Chrome sekme
betiği yüklemeyi duvar saatiyle en çok 5 sn bekler; süreç zaman aşımı 13 sn'dir ve aşılırsa `CHROME_SCRIPT_TIMEOUT` hatasıdır
(betik gezinmeyi bekleme döngüsünden önce yaptığı için görünür UI yedeğiyle yeniden gezinmek çift sekme açardı). AppleScript
yalnız otomasyon izni reddinde (`-1743`/`-1744`) ya da `osascript` çalıştırılamadığında oturum boyu kapanır ve görünür UI yoluna
geçilir; diğer betik hataları yalnız o çağrıyı UI yoluna düşürür.

Kurulum ve sınırlar [TELEGRAM.md](TELEGRAM.md) içindedir. Bu değişiklikte canlı
Telegram mesajı gönderilmedi; yerel hizmetin çalıştığı doğrulandı.

Hazır katalog çözümü ağ beklemesi gerektirmez. Yeni hizmette çevrimiçi keşif toplam 8 saniyeyle, paket kurulumu 60 saniyeyle sınırlıdır. Olumsuz keşif 15 dakika, olumlu keşif 24 saat önbelleklenir. Hazır bağlantılar aynı uygulama oturumunda yeniden kullanılır.

## Model ve hız davranışı

Varsayılan profil `ollama-cloud` (`gemma4:cloud`, yerel Ollama API'si); bu makinede Ollama oturumu ve model doğrulandı. `OMNI_OLLAMA_CLOUD_MODEL` ile `ollama list` içinde bulunan başka bir bulut modeli seçilebilir. API anahtarıyla çağrılan `openai` (OpenAI API, GPT-6-Luna) ve `openrouter` (`anthropic/claude-sonnet-5`) elle seçilebilir; model çağrısı hata verdiğinde yalnız kullanıcı açıkça izin verdiyse yedek olur (aşağıya bakın), araç başarısızlıkları model değiştirmez; `opencode` ve `opencode-think` (Opencode Go, `qwen3.8-flash`) elle seçilebilir. Bilgisayardaki Codex/OpenCode oturumuna dayanan CLI bağlayıcıları kaldırıldı: her tur ayrı süreç başlatıyordu. Anahtarlar `OPENAI_API_KEY`, `OPENCODE_API_KEY` ve `OPENROUTER_API_KEY` değişken adlarıyla süreç-içi depodan okunur (kayıt yoksa kabuk ortam değişkeni yedektir); anahtarı tanımlı olmayan profil o görevde kullanılamaz. Arayüzün sağ üstündeki **Ayarlar** (⚙) sayfasından girilen anahtarlar macOS Keychain'de saklanır, kaydedildikleri anda süreç-içi depoya ve model istemcilerine uygulanır (görev sürüyorsa görev bitince), sonraki açılışlarda da yüklenir. Kayıtlı anahtar kabuk değişkenini geçersiz kılar, çünkü kabuk değişkeni ajanın başlattığı alt süreçlere miras kalır: anahtarlar `os.environ`'a bilinçli olarak yazılmaz, alt süreçlere anahtar değişkenleri çıkarılmış ortam verilir ve araç çıktısı modele/transkripte gitmeden önce maskelenir. Tüm API bağlantıları görevler arasında sıcak tutulur. Eski büyük araç sonuçları bağlamdan budanır, son tur tam korunur. Sabit GUI beklemesi yerine uygun olduğunda öğe/durum beklenir. Uygulama belirtilmeyen tek fotoğraf hedefinde doğrudan kamera yakalama yolu tercih edilir; Photo Booth gerektiğinde yedektir.

`gpt-oss:20b-cloud` da bu makinede kurulu ve metin/araçlı görevde canlı doğrulandı;
`OMNI_OLLAMA_CLOUD_MODEL=gpt-oss:20b-cloud` ile seçilebilir. Görsel görevler için doğrulanan
varsayılan `gemma4:cloud` korunur. `qwen3.5:cloud` denendi fakat bu hesapta 402 ile
"ücretsiz kullanıma dahil değil" yanıtı verdi; bu yüzden hazır profil yapılmadı.

Ayarlar (⚙) sayfasındaki model listesi Ollama profili için yerel sunucunun `/api/tags`
yanıtından üretilir ve 15 dakika önbelleklenir ("Modelleri yenile" önbelleği atlar). Bir kayıt
bulut sayılır: ad son eki `:cloud`/`-cloud` ise ya da sunucu `remote_model`/`remote_host`
alanlarını bildiriyorsa; yalnız `completion` yeteneği olan kayıtlar listelenir, gömme modeli
(ör. `mxbai-embed-large`) ve yerel sohbet modeli listelenmez. Yerel sunucuya kurulu olmayan
bulut modeli listede görünmez (`ollama pull <ad>` ile kurulur); kutu elle düzenlenebildiği için
listede olmayan geçerli bir ad doğrudan yazılabilir. Not satırı bulut modelinin ollama.com adını
da gösterir (ör. `gemma4:cloud → gemma4:31b`), böylece `gemma4:31b:cloud` gibi var olmayan bir
ad aranmaz. OpenAI ve OpenRouter listeleri anahtar girilmeden sorgulanmaz.

Fast Loop semantik ilerlemeyi host seviyesinde izler. Yeni ve başarılı komut çıktısı en fazla sekiz farklı sonuç için ilerleme sayılır; aynı veya boş çıktı sayılmaz. Eylem isteklerinde yalnız keşif, okuma veya gezinme sonucu başarı kanıtı değildir: host gerçek bir işlem denemesi ister, ardından kanıt yoksa görevi başarısız işaretler. `git status` mutasyon hedefinde yalnız gözlem sayılır; zincirde başka bir komut varsa bütün çağrı ayrıca değerlendirilir. `remove` ve `kaldır` da silme eylemi sayılır. İngilizce `How to ...?`/`How can I ...?` yöntem soruları tek başına eylem sayılmaz; soru sonrası açık emir kendi eylem türüyle doğrulanır. Açık yerel dosya silme hedeflerinde tüm yolların başlangıçta mevcut, bitişte yok olması ve her birine yönelik başarılı silme komutu gerekir. Taşıma için kaynak/hedef ve içerik özeti veya sembolik bağ hedefi; düzenleme için ilk/son içerik özeti ile hedefli dosya aracı eşleştirilir. Bağıl yollar görev başındaki çalışma dizinine göre yorumlanır. Belirsiz hedef, README içindeki yol referansı, okunamayan veya boyut sınırını aşan dosya doğrulanmış teslim sayılmaz; sonuç `Doğrulanmadı` olur. Dosya sözleşmesi yalnız açık ve dar yerel yol biçimlerini kapsar. Eylem kanıtı ve son durum kontrolü aynı tek kurtarma hakkını kullanır. Görsel olmayan turlarda iki anlamsız tur
sonra yeniden planlama, görsel otomatik gözlem taşıyan turlarda üç tur tolerans, teslim aşamasında iki
anlamsız tur sonra kontrollü durma uygulanır. Bu erken durma yalnız Normal, Uzun ve Otonom modlarındadır.
Sürekli mod ayarlanan süre ve toplam token sınırlarına kadar çalışır. Başarısız araç çağrısının
hemen ardından hatayı özetleyip alternatif adım ister; önceki turdaki aynı başarısız çağrı aynı
argümanlarla yinelenirse çalıştırmaz. Farklı bir adımın ardından yeniden deneme mümkündür.
Tekrarlanan başarısızlıkları ve araçsız raporları en fazla iki kez yeniden planlar; yeni sonuç yoksa
model/araç çağrılarını durdurup kullanıcıdan yön ister. Önceki ilerleme imzası sürekli modda da tutulur;
aynı dosyayı/ekranı tekrar okumak ilerleme değildir. Düz metinde bırakılmış açık onay sorusu gerçek
`ask_user(kind=confirm)` çağrısına dönüştürülür. Masaüstündeki sürekli mod etkileşimlidir: onay kartı
sağ üstte, ana uygulamayı öne getirmeden görünür ve **İzin ver / Reddet** seçenekleri sunar.
Yanıt gelmeden bağımlı işlem yürütülmez; kapanan/süresi dolan karta geç yanıt uygulanmaz. ⌘⇧X ile
bilerek gizlenen uygulamada içeriksiz bildirim/Dock uyarısı verilir; uygulama gösterilince kart açılır.
Başarı bildiriminin onay süresi dolması başarı değildir. Çıplak GUI tıklama/yazım kayıtları hedef
kanıtı olarak kullanılamaz; sonuç ayrıca okunmalıdır. Pasif düğmede koordinat yoluyla yeniden
tıklama yerine form doğrulaması istenir.
Önbelleksiz token veya araç çağrısı sayısı opsiyonel keşfi daraltmaz. 24 Eylül'deki üçer stress koşusunda
`long_research` 3/3 başarı, 6 tur/13 araç ve 8,5 saniye medyan; `stagnation` 3/3 beklenen
bounded-failure, 7 tur ve 5,4 saniye medyan verdi. Önceki stagnation politikası 10 tur
harcıyordu. Koşularda ev dizininde istenmeyen dosya oluşmadı.

401/402/403 erişim veya bakiye hatası veren model profili (izinli alternatif varsa) yalnız o
görevde karantinaya alınır; sonraki turda aynı başarısız profile geri dönülmez. 429 hız sınırı
veren profil Retry-After (yoksa 30 sn) kadar atlanır; alternatif yoksa Retry-After'a uyularak
beklenir. Geçici 5xx/ağ hatası, zaman aşımı ve akış içi hata olayı üstel geri çekilmeyle yeniden
denenir: çağrı başına bütçe arayüzde ve CLI'de 60 sn, Telegram'da 300 sn, sürekli veya gözetimsiz
görevde kalan görev süresidir (en çok 30 dk). Bütçe aşılırsa görev "model çağrısı başarısız" ile
biter; bekleme Durdur'a duyarlıdır.

**Yedek sağlayıcı izni.** Model isteği (ekran görüntüleri, AX/OCR metni, geçmiş ve hafıza dahil)
varsayılan olarak yalnız seçili sağlayıcıya gider; aynı sağlayıcıda yeniden deneme serbesttir.
Başka sağlayıcıya geçiş `OMNI_FALLBACK_BACKENDS=openai,openrouter` (ya da veri dizinindeki
`provider_fallback.json`) ile açıkça izin verilmedikçe yapılmaz; ekran görüntülü istekler ayrıca
`OMNI_FALLBACK_IMAGES=1` ister. Otomatik seçim de bu izne tabidir: Otomatik varsayılan profildir
(`ollama-cloud`); o hazır değilse (Ollama kapalı) yalnız `openai` hazır diye görev başka sağlayıcıya
taşınmaz, izin yoksa açık hatayla başlamaz. Ollama'sız kullanıcı için tek seferlik yol
`OMNI_FALLBACK_BACKENDS=openai` (görsel ek ve ekran görüntüsü için ayrıca `OMNI_FALLBACK_IMAGES=1`)
ya da hazır profili açıkça seçmektir; hazır bir profilin açık seçimi izin gerektirmez. Her geçiş,
istek gönderilmeden önce `provider_fallback` olarak yayınlanır (Telegram ve CLI gösterir) ve
`audit.jsonl`'a yazılır; kalıcı yedeğe geçilmiş görevde her yeni görüntü seviyesi (metin, ekran
görüntüsü) ayrıca kaydedilir. Telegram sesli komutunun yazıya çevrilmesi sesi OpenAI'a gönderir:
`openai` izin listesinde değilse ses gönderilmez. Öncelik sırası, dosya şeması ve sınırlar
`omniagent/fallback_policy.py` modül açıklamasındadır.

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

Kaynak kodu değiştirme, eylem gerçekleştirme ve durum bekleme hedeflerinde modelin tur içindeki
metin ve düşünme akışı, host sonuca karar verene kadar kullanıcıya gösterilmez. Kabul edilen
son yanıt veya kısa `Doğrulanmadı: ...` sonucu model turu kapanmadan yayımlanır; araç
olayları akmaya devam eder. Başarısız görev geçmişine modelin doğrulanmamış `STATE` beyanı
alınmaz; yalnız araç çıktılarından çıkarılmış host gerçekleri korunur. Bu kapı mevcut eylem,
kaynak yazımı ve durum kanıtını uygular; hedefteki bütün alt maddeler için genel bir doğrulama
garantisi vermez.

Retry-After görevin yeniden deneme bütçesine sığmıyorsa (ör. aylık kotanın dolduğunu bildiren
gün mertebesindeki bir bekleme) hiç beklenmez: görev, beklemenin yapılmadığını, bunun geçici bir
yavaşlama olmadığını ve başka bir model seçilmesi ya da izinli bir yedek gerektiğini söyleyen hatayla
biter. Bekleme süresi gösterimi 48 saatten sonrasını gün olarak yazar.

Uzun görevlerde model her turda son gerçek araç çağrılarının kimliğini, başarı durumunu ve kısa
sonucunu çalışma kaydında görür. Modelin kendi `STATE` notu açıkça doğrulanmamış olarak etiketlenir
ve tek başına ilerleme sayılmaz. Kontrol noktası, araç çıktılarından çıkarılmış değerleri düz metin
olarak saklar; görev yeniden açıldığında bu değerler sözlük gösterimine dönüşmez.

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
(`omniagent-benchmark --only chrome_ilan --concurrency 1`, gecikmeli XHR ve yükleme iskeletli sayfa)
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
3 tur/2 araç ve 3,6/2,1 sn. Finansal para hareketleri, ödeme kartı numarası yazma, ekranda/tarayıcıda
okunan ödeme/sipariş onayı düğmelerine tıklama (OCR, erişilebilirlik adı, DOM), entegrasyon araçlarıyla geri
alınamaz dış iletişim ve kullanıcının istemediği kalıcı hafıza mutasyonları `approval.py` host kapısından
geçer; onay yoksa (kanal yok, ret, zaman aşımı) ilgili çağrı çalıştırılmaz. İki otomatik onay yolu vardır ve
her ikisi de varsayılan olarak kapalıdır: güvenli host listesi/tutar limiti (`AUTO_APPROVE_HOSTS`,
`AUTO_APPROVE_LIMIT`; ikisi de boş/0 iken kapalı) ve sürekli mod (`AUTO_APPROVE_IN_CONTINUOUS_MODE`).
`AUTO_APPROVE_IN_CONTINUOUS_MODE=False`; sürekli mod tek başına onay yetkisi vermez.
GUI/DOM üzerindeki tanınan yayınla/gönder/yanıtla düğmeleri de host onayından geçer; kart son
yazılan taslak metnini gösterir. Onaydan sonra hedef yeniden kontrol edilir. Açıkça etkinleştirilen
otomatik onayda denetim kaydına `auto_approved` yazılır.
Kapsam ve sınırlar (Enter ile gönderim, ikon-only düğme gibi denetlenmeyen yollar) `approval.py` başlığındadır.

Bot doğrulaması ve erişim engeli sayfaları (Cloudflare ara sayfası, reCAPTCHA/hCaptcha/Turnstile, hız sınırı)
**varsayılan olarak aşılır** (`tools/bot_wall.py`; kapatmak için `BYPASS_ENABLED = False`): sayfa içerik olarak
okunur, `fetch_raw` hata metnine ve tarayıcı sonucuna uygulanacak strateji (`get_bypass_strategy`) ile tıklanacak
seçiciler (`get_bypass_selectors`) eklenir; engel sayfasındaki bağlantı/kutu tıklanabilir. Sıkı modda eski
davranış geçerlidir: içeriğin yerini alan sayfa kurtarılamaz `BOT_WALL_DETECTED` hatası olur ve engelli ana
makineye görev boyunca yeniden gidilmez. Ana makinenin gerçek erişim-reddi yanıtı (loopback, 'Access denied')
her iki modda da bot duvarı sayılmaz. Tarayıcı bot duvarlarına karşı normal bir Chrome gibi görünür: standart
Chrome User-Agent (`AGENT_PRODUCT_TOKEN` boş), stealth init script'i (`navigator.webdriver`, plugin/dil/WebGL
parmak izleri) ve ajanın KENDİ kopyası olan kalıcı Chrome profili; kullanıcının gerçek profili açılmaz, ilk
çalıştırmada çerez/giriş dosyaları oradan kopyalanır (`seed_chrome_profile`, `OMNI_SEED_CHROME_PROFILE=0` ile
kapatılır).
Alternatif web okuma yolu da öğrenilir: `fetch_raw` hatasından sonra aynı tam URL'yi
`browse_url(actions=[])` içerikle okur ve görev başarıyla biterse, bir sonraki aynı hatada
tarayıcı yolu önerilir. Kalıcı derste URL veya hata metni değil özetleri tutulur; farklı
hedefe ders taşınmaz ve önerinin etkisi aynı hedefteki sonraki çağrıyla ölçülür.


24 Eylül ek canlı `self_repair` kontrolünde önce doğru `OZET: 42` komut çıktısı alınmasına rağmen Fast Loop son yanıtı beklemeden başarısız durdu (6 tur/6 araç). Yeni çıktıyı ilerleme sayan düzeltmeden sonra aynı senaryo 1/1 başarıyla 7 tur/6 araçta tamamlandı. Aynı kök altında ardışık iki koşu da başarılıydı; ikinci koşuda deneyim dersi kullanımı araç sayısını 4→2, tur sayısını 5→3 indirdi. Sağlayıcının iki geçici isteği geciktirmesi ikinci koşunun duvar süresini 126,6 saniyeye çıkardı; bu tek başına yerel araç süresinin ölçüsü değildir. Model isteği başına zaman aşımı bunun ardından 60 saniyeden 30 saniyeye indirildi; yeni sınırın canlı sağlayıcı hatasındaki etkisi henüz tekrar ölçülmedi.
