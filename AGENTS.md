# OmniAgent Geliştirme Kuralları ve Yol Haritası

Bu dosya, OmniAgent projesinin geliştirilme sürecinde uyulacak katı kuralları, davranışsal rehberleri ve öncelikli geliştirme hedeflerini içerir.

## Eşzamanlı geliştirme
- Birden fazla geliştirici ajan aynı anda çalışacaksa her biri ayrı Git worktree/branch kullanır.
  Aynı `main.py` veya benchmark dosyasını ortak çalışma ağacında eşzamanlı yazmaz.
- Her ajan yalnız kendi dosyalarını commit eder; kirli çalışma ağacındaki başkasına ait
  değişiklikleri silmez, stash etmez veya commit'e katmaz. Birleştirme sonrası tam test koşulur.
- Canlı macOS ekranı ve açık Chrome tek kaynaktır. GUI benchmark'ı kullanıcı veya başka ajan
  ekranda çalışırken başlatılmaz. UI ve Telegram görevleri `host_lock.py` ile çakışmayı reddeder.
- UI süreci kodu açılışta yükler; kaynak dosya değişince çalışan süreç kendiliğinden güncellenmez.


## 🛡️ Güvenlik Rayları (Safety Rails)
Ajan, kendi kaynak kodunu ve host sistemini değiştirebildiği için `tools.py` içinde kod
seviyesinde koruma katmanı bulunur (sandbox değildir, en iyi çaba korumasıdır):
- `write_file`: hassas sistem/kimlik dosyalarına (`~/.ssh`, `/etc`, kabuk profil dosyaları vb.)
  yazmayı reddeder; `.py` hedeflerinde yazmadan önce `compile()` ile sözdizimini doğrular;
  üzerine yazmadan önce `.omni_backups/` içine zaman damgalı yedek alır; eksik üst dizinleri
  oluşturur ve bunu sonuçta açıkça bildirir (yazım hatalı bir yol sessizce dizin açmasın).
- `execute_shell`: bilinen yıkıcı komut kalıplarını (`rm -rf /`, `mkfs`, fork bomb, disk
  biçimlendirme vb.), çözülemeyen kabuk değişkeni hedeflerini ve korunan yollara `>`/`>>`/`tee`
  yönlendirmesini engeller; `sudo` çağrıları yapılandırılmış log ile kaydedilir ve parolasız (`-n`)
  olmayan sudo oturumlarında güvenli şekilde başarısız olur.
- `fetch_raw`: yalnızca `http`/`https` adreslerini kabul eder; `file://` ve yönlendirme ile yerel
  dosya okuma olasılığı kapatılır.
- Model yalnızca `main.build_tool_schemas` içindeki araç adlarını çağırabilir; `Toolbox`'ın
  özel yöntemlerine (`_read_full`, `close_browser`…) erişemez.
- GUI araçları erişilebilirlik veya ekran kaydı izni yoksa açık hata verir (macOS izinsiz
  sentetik olayları sessizce düşürür; araç "başarılı" deyip hiçbir şey yapmamalı).
- Bu raylar iyi niyetli hatalara karşı geri dönüş sağlar, kötü niyetli kullanıma karşı bir
  güvenlik sınırı değildir.

## 🚨 Kritik Yazım ve Modifikasyon Kuralları
- **Yasak:** Python dosyaları asla kabuk komutlarıyla (`cat`, `echo`, `tee`, `heredoc`) yazılmaz.
- **Zorunluluk:** Dosya içerikleri her zaman `write_file` ile ve TAM içerik olarak yazılır; kırpma
  ve "// rest of code" gibi yer tutucu yoktur. `write_file`'ın ekleme (append) modu yoktur.
- **Kendi kaynak kodu:** yalnızca hedef açıkça istediğinde değiştirilir. Önce `read_file` ile
  okunur, sonra `write_file` ile tam içerik yazılır. `write_file` yazdığını kendisi geri okuyup
  karşılaştırdığı için ayrıca geri okuma yapılmaz (her yazma görevine boşa bir tur ekliyordu).
- Modelin uyduğu davranış kurallarının tek kaynağı `config.SYSTEM_PROMPT`'tur.

## 🛠️ @Chatgpt-System Plugin Entegrasyonu
`@Chatgpt-System` pluginindeki yetenekler `Toolbox` yapısına şu hâlleriyle entegre edildi:
1. **Süreç Yönetimi:** `process_list` aracı (süreç sayısı + CPU'ya göre en ağır 15 süreç).
2. **Koordinat Takibi:** tek ortak koordinat uzayı: 1000×1000 kare (`MODEL_SCREEN_SIZE`, ekran
   oranı korunmaz). Ekran görüntüsü, AX öğe listesi ve bütün tıklama/taşıma noktaları
   (`point: [x, y]`) bu uzaydadır; Retina piksel ↔ nokta dönüşümünü araçlar yapar. qwen
   koordinatı 0-1000 normalize, Claude/GPT görüntü pikseli verir; kare görüntüde ikisi aynı
   sayıdır. Ölçüm: 1280×832 görüntü + ayrı x/y alanlarıyla 10 tıklamada 1 isabet ve 7 bozuk
   argüman (`"x": [x, y]`); kare görüntü + `point` ile 10/10 isabet, 0 bozuk. Ayrı imleç
   konumu aracı araç diyetiyle kaldırıldı.
3. **Oturum ve Yetki:** etkisiz `session_authority_*` araçları kaldırıldı; sudo durumu
   `execute_shell` ile `sudo -n true` çalıştırılarak denetlenir.

## 🎨 Kodlama Standartları
- **Dil:** Tüm yorumlar ve dokümantasyonlar Türkçe olacaktır.
- **Paradigma:** Fonksiyonel programlama öncelikli olacaktır. OOP sadece dış sistem konnektörleri için kullanılacaktır.
- **Saf Fonksiyonlar:** Fonksiyonlar giriş parametrelerini veya global durumu değiştirmeyecek, sadece yeni değerler dönecektir.
- **Tipleme:** `TypedDict`, `Optional`, `Union` gibi yapılarla katı tipleme (strict typing) uygulanacaktır.
- **Sadelik:** DRY, KISS ve YAGNI prensiplerine sadık kalınacaktır.
- **Bağımlılıklar:** `pyproject.toml` + `uv.lock` (`uv sync` ile kurulur).

## ⚡ Performans Notları
- **Ölçüm önce gelir:** `benchmark.py` 9 deterministik senaryoyu gerçek modelle koşturur; başarı
  oranı, medyan/maks süre, tur ve token (önbellek dahil) raporlar, ev dizininde istenmeyen dosya
  oluşursa bildirir. Her değişiklik hız VE doğrulukla birlikte ölçülmelidir.
- **Araç diyeti:** model 14 temel araç ve bir `discover_capabilities` şeması görür (önce 26
  temel araç vardı). Masaüstüne fotoğraf çekme hedefinde `capture_photo` eklenir. 26 araçlı
  şemada model hedefteki tarihi 10 denemenin 5'inde yanlış kopyaladı, tek araçla 10/10
  doğruydu. Fare/klavye adımları `run_action_sequence`, şablon tıklama `smart_click` içindedir.
- **Paralellik:** bağımsız araç çağrıları `asyncio.gather` ile gerçek paralellikte çalışır; yan
  etkili araçlar (`_SIDE_EFFECT_TOOLS`) model sırasıyla seri çalışır (iki tıklama/yazma
  çakışmaz, eylem→gözlem sırası korunur). Her araç sonucunun başında çağrı etiketi vardır;
  paralel sonuçlar yalnızca `tool_call_id` ile eşleşince hızlı model onları karıştırıyordu.
- **Tur azaltma:** `run_action_sequence` fare/klavye zincirlerini (tıkla → yaz → tuş → bekle)
  TEK turda bitirir; `browse_url` kalıcı sekmede "doldur → gönder → oku" akışını TEK çağrıda
  yapar ve sayfadaki öğeleri hazır seçicileriyle döner (yanlış seçici 30sn değil 5sn'de düşer).
- **GUI:** `cua_get_ax_state` gerçek AX ağacından etkileşimli öğeleri (numara, tür, etiket, merkez)
  listeler (Chrome ~150ms, Notlar ~650ms); `cua_click` AXPress ile tıklar. Ekran görüntüsü
  yalnızca AX yetmediğinde gerekir. Metin, klavye düzeninden bağımsız Unicode olaylarıyla yazılır
  (Türkçe/Fince karakterler ve emoji doğrulandı; pyautogui bunları sessizce atlıyordu).
- **Eylem → gözlem:** `take_screenshot`, son ekran girdisinden sonra sabit uyku yerine ekranın
  durulmasını bekler: girdi öncesi kareye göre tepki (≤1 sn), ardından 0,45 sn sakinlik, en çok
  3 sn. Karşılaştırma son değişim karesine göredir; yükleme iskeletinin düşük kontrastlı
  parıltısı ancak böyle yakalandı. Yakalama Quartz + CoreGraphics ile ~55 ms (screencapture alt
  süreci ~260 ms idi).
- **Açık Chrome yolu:** eylem içeren her tur, görüntü istenmediyse ekran durulunca otomatik
  gözlemle biter (ayrı "ekran görüntüsü al" turu yok). `chrome_active_tab` aynı kökenli sekmeyi
  kimlikle bulup yüklenmesini bekler; ara/gönder tek `cua_submit_text` çağrısıdır. Chrome AX
  ağacı web içeriğini vermediği (`AXManualAccessibility` desteklenmiyor, `AXEnhancedUserInterface`
  ayarlanamıyor) için AX araçları bu yolda kapalıdır. `benchmark.py --only chrome_ilan
  --concurrency 1` (2026-09-23): önce 1/2 başarı, medyan 73 sn, 14-24 tur → sonra 3/3, medyan
  30 sn, 6 tur. Uzun görevlerde model artık her araç turunda kısa `STATE:` çalışma kaydı
  tutmaya yönlendirilir; aynı tam Chrome URL'sinin ikinci ve sonraki başarılı açılışlarında
  yeniden gezinme uyarısı alır.
- **Bağlam:** son `FULL_DETAIL_TURNS` turdan eski uzun araç çıktıları, görseller ve argümanlar
  budanır (`_trim_old_turns`). Yaş TUR ile ölçülür, son tur asla kırpılmaz; pencere tur tur
  kaydığı için önek bayt bayt aynı kalır ve sağlayıcı önek önbelleği isabet eder (~%70).
  Eski multimodal gözlemde görüntü atılırken aynı mesajdaki metinsel kısım korunur; böylece
  `STATE`/gözlem metni görselle birlikte kaybolmaz. Modelin `reasoning_content`'i geçmişe
  eklenmez. 60 bin önbelleksiz giriş tokenı veya 24 araç çağrısından sonra tek seferlik yumuşak
  bütçe uyarısı yeni/opsiyonel keşfi kesip hesaplama, yazma, doğrulama ve cleanup gibi zorunlu
  teslim adımlarına öncelik verir; görevi kendiliğinden abort etmez.
- **Zaman sınırları:** model isteği 60sn (bağlantı 5sn) ve SDK içi yeniden deneme kapalıdır
  (SDK varsayılanı 600sn + 2 gizli deneme idi); composer'da Normal 25 tur/10dk,
  Uzun 50 tur/20dk, Otonom 100 tur/45dk bütçeleri sunar. Dört ardışık tamamen başarısız
  araç turu ilerleme yok sayılır.
- **Akış:** model yanıtı `stream=True` ile alınır. Metin, düşünme metni, araç çağrısı
  önizlemesi (komut model yazarken harf harf) ve komut çıktısı (`run_streaming_process`,
  satır satır) `events.py`'deki tipli olaylarla yayınlanır; yarıda kesilen akış yeniden
  denenirse önce `stream_reset` gelir. Durdurma isteği model akışını ve çalışan komutu (süreç
  grubuyla) anında keser.
- **Bellek:** `cognitive_memory.json` son 30 görevi ölçümleriyle kaydeder ve modele geri
  enjekte EDİLMEZ. `user_memory.json` ise yalnızca kullanıcının açıkça istediği tercih, sık yol
  ve karar kayıtlarını atomik olarak tutar; `user_memory` aracı olmadan okunmaz, her görevde
  otomatik olarak enjekte edilmez ve parola/token/API anahtarı gibi gizli bilgileri reddeder.
  Otomatik ders/rota enjeksiyonu ölçümde zararlı bulundu (alakasız "çözümler", başka görevlerin
  yolları, "görev belirtilmedi" yanıtları) ve kaldırıldı; macOS'a özgü bilinen tuzaklar
  `SYSTEM_PROMPT` içindeki sabit ENVIRONMENT bloğundadır.

## 🖥️ Arayüz (ui.py)
- Yalnızca `events.py` olaylarını tüketir; log metni ayrıştırılmaz. Olaylar ajan thread'lerinden
  kuyruğa gelir, tüm çizim Tk thread'inde tek `_tick` döngüsünde yapılır. Metin akarken
  16 ms, boşta 100 ms kullanılır; boş karede büyük transkript yeniden çizilmez.
- Tasarım dili Claude Code + Codex: nötr koyu yüzeyler, Menlo mono transkript, Claude turuncusu
  (`#D97757`) vurgu; araçlar `⏺ Ad(önizleme)` blokları, komutlar `$` satırları, çıktılar `⎿`
  altında (çalışırken canlı son 6 satır, bitince ilk 4 satır + "… +N satır").
- Animasyonlar: daktilo akışı, yanıp sönen imleç ve çalışan araç işareti, yıldız spinner'lı ve
  parıltılı durum satırı (süre, token, "esc ile durdur").
- Görevler kalıcı bir event loop'ta paylaşımlı model istemcileriyle çalışır; `Esc` durdurur,
  `⌘K` temizler. Composer'da Normal/Uzun/Otonom bütçe profili ve macOS yerel mikrofon
  düğmesi bulunur; mikrofon SVG ikonludur, `AVAudioEngine` buffer'ları konuşma sırasında
  partial metni composer'a akıtır ve stop sonrası final sonucu otomatik gönderilmez. Header'daki
  SVG kopyala düğmesi görünen transcript'in tamamını panoya alır.

## 🔀 Çoklu Model Backend'i (config.py: BACKENDS)
- Yedi profil: varsayılan `ollama-cloud` (yerel Ollama API'si üzerinden `gemma4:cloud`),
  `openai` (yerel ChatGPT oturumlu Codex CLI üzerinden GPT-6-Luna), `zen-free` (OpenCode CLI
  üzerinden Muse Spark Contributor Free), ayrıca paralı API profilleri `opencode`,
  `opencode-think`, `claude` ve `minimax`. Yalnız paralı API profilleri opencode'un
  `auth.json` dosyasındaki anahtarları kullanır. Ollama modeli `OMNI_OLLAMA_CLOUD_MODEL`
  ile değiştirilebilir; seçilen modelin `ollama list` içinde bulunması gerekir.
- **Kalite merdiveni:** art arda `CONSECUTIVE_FAILURE_ESCALATION_THRESHOLD` (2) tamamen başarısız
  araç turunda `QUALITY_LADDER` boyunca çıkılır: `ollama-cloud` → `openai` → `zen-free`.
- **API hataları:** geçici 5xx/bağlantı hatasında aynı backend bir kez yeniden denenir.
  429'da hazır başka profil varsa sağlayıcıya erken yeniden istek atılmadan ona geçilir;
  tek profil varsa Retry-After en çok 30 sn ise beklenir. 401/402/403 ve alternatifli 429
  görev boyunca karantinaya alınır; sonraki turda aynı başarısız profil çağrılmaz.
  Geçici fallback yalnız o turdadır; kalıcı erişim hatası veya kalite merdiveni geçişi
  mevcut profili görev boyunca değiştirir.
- **Önbellek:** `claude` profili `cache_control` gönderir (OpenRouter'da Anthropic önek önbelleği
  yalnızca bununla açılır); opencode öneki kendiliğinden önbellekler.
- **Manuel seçim:** `OMNI_BACKEND=<profil>` ya da arayüzdeki seçim. Yerel CLI veya Ollama modeli
  hazır değilse kullanılabilir kalite basamağına düşer ve uyarı verir.
- **Ölçüm (2026-09-24):** `ollama-cloud` 9 senaryo × 3 koşuda 27/27 başarı ve 4,2 sn medyan;
  `openai` (Codex CLI) aynı 9 senaryonun tek koşusunda 9/9 ve 11,3 sn medyan verdi.
  CLI bağlayıcıları her model turunda ayrı süreç başlatır; Ollama Cloud yerel HTTP API'si
  sıcak bağlantı kullanır. CLI istemleri stdin üzerinden gider (süreç argümanlarına yazılmaz),
  45 saniye sınırına ve iptal/süreç grubu temizliğine tabidir. Ollama Cloud kullanım sınırı
  dolarsa CLI yedekleri denenir.

## 📱 Telegram ve yetenek farkındalığı
- `telegram_bridge.py`, olay akışını eşleştirilmiş özel Telegram sohbetine taşır: metin,
  düşünme/araç/çıktı/durum olayları, ekran görüntüsü, süre ve token istatistikleri görünür.
  `/stop`, `/status`, `/model` ve `/mode` desteklenir. Bot tokenı Keychain'de, sohbet
  ve kullanıcı kimliği özel izinli yerel dosyadadır. Kurulum: [TELEGRAM.md](TELEGRAM.md).
- Modelin gerçek yürütme yetkisi her turdaki araç şemalarıdır. `discover_capabilities` hazır
  API/MCP'yi ve gerekirse kısa kaynak keşfini açar; skill dosyası yöntem bilgisidir, hesap
  erişimi değildir. Kalıcı plugin yalnız güvenilir, sabit sürümlü kayıtla kurulur.
- Tekrarlı yerel iş için `execute_js` veya temizlenen geçici script kullanılabilir. Görevin
  istemediği kalıcı aracı ya da kendi kaynak değişikliğini ajan kendiliğinden eklemez.
- Epizodik kayıtlar otomatik ders olarak modele verilmez: önceki ölçümde hedef sapmasına yol
  açtı. Kalıcı kullanıcı tercihleri yalnız açık istekle `user_memory` aracına yazılır.
  Hata öğrenmesi eklenecekse aynı argüman/hata imzasına koşullanmalı ve benchmark ile
  doğrulanmalıdır. Başarısız çağrı aynı girdilerle sonsuz tekrar edilmez.

## 🎯 Hedefler
- [x] `@Chatgpt-System` yeteneklerini `tools.py` içerisine gömmek. (bkz. Plugin Entegrasyonu)
- [x] Ajanın kendi yetki seviyesini yönetebildiği bir güvenlik katmanı eklemek. (bkz. 🛡️ Güvenlik Rayları)
- [x] Açıkça istenen tercih, yol ve kararları atomik `user_memory.json` deposunda tutup otomatik
  enjeksiyon yapmadan hatırlama/arama/silme akışı eklemek.
- [x] Vizyon ve koordinat sistemini hibrit hale getirmek. (`smart_click`: AX → görsel şablon; tek ortak koordinat uzayı)
- [x] Ajan döngüsünü canlı hedeflerle ölçüp hız/doğruluk sınırlarını ayarlamak.
  (2026-09-23, `benchmark.py`, 9 senaryo × 3 koşu: başarı 19/27 → 27/27, medyan 17,3sn → 4,5sn.)
- [ ] Hatalardan kalıcı öğrenme: yeniden eklenecekse argüman farkına dayanan, yalnızca aynı
  hatalı argümanla eşleşen dersler olarak ve `benchmark.py` ile ölçülerek eklenmeli.
