# OmniAgent Geliştirme Kuralları ve Yol Haritası

Bu dosya, OmniAgent projesinin geliştirilme sürecinde uyulacak katı kuralları, davranışsal rehberleri ve öncelikli geliştirme hedeflerini içerir.

## Eşzamanlı geliştirme
- Birden fazla geliştirici ajan aynı anda çalışacaksa her biri ayrı Git worktree/branch kullanır.
  Aynı `main.py` veya benchmark dosyasını ortak çalışma ağacında eşzamanlı yazmaz.
- Her ajan yalnız kendi dosyalarını commit eder; kirli çalışma ağacındaki başkasına ait
  değişiklikleri silmez, stash etmez veya commit'e katmaz. Birleştirme sonrası tam test koşulur.
- Canlı macOS ekranı ve açık Chrome tek kaynaktır. GUI benchmark'ı kullanıcı veya başka ajan
  ekranda çalışırken başlatılmaz; o sırada `benchmark.py --headless` aynı GUI senaryolarını gerçek
  ajan döngüsü ve araç mantığıyla görünmez Chromium'da ölçer (`headless_screen.py`; select menüsü
  ve Quartz yakalama yolu orada sınanmaz). UI ve Telegram görevleri `host_lock.py` ile çakışmayı reddeder.
- UI süreci kodu açılışta yükler; kaynak dosya değişince çalışan süreç kendiliğinden güncellenmez.


## 🛡️ Güvenlik Rayları (Safety Rails)
- Kullanıcı onayıyla `tools.py` içindeki yol engelleri (`~/.ssh`, `/etc` vb.) ve kabuk komut kısıtlamaları kaldırılmıştır; ajan tam sistem erişimine sahiptir.
- `write_file`: `.py` hedeflerinde yazmadan önce `compile()` ile sözdizimini doğrular; üzerine yazmadan önce `.omni_backups/` içine zaman damgalı yedek alır; eksik üst dizinleri oluşturur ve bunu sonuçta açıkça bildirir.
- `fetch_raw`: yalnızca `http`/`https` adreslerini kabul eder; `file://` ve yönlendirme ile yerel dosya okuma olasılığı kapatılır.
- Model yalnızca `main.build_tool_schemas` içindeki araç adlarını çağırabilir; `Toolbox`'ın
  özel yöntemlerine (`_read_full`, `close_browser`…) erişemez.
- GUI araçları erişilebilirlik veya ekran kaydı izni yoksa açık hata verir (macOS izinsiz
  sentetik olayları sessizce düşürür; araç "başarılı" deyip hiçbir şey yapmamalı). Ekran
  kaydı izni ilk eksik denemede `CGRequestScreenCaptureAccess` ile BİR KEZ sorulur; uygulama
  sisteme ancak bu çağrıyla kaydolduğu için izin listesinde görünmesi buna bağlıdır. Hata
  metni "Terminal/Python" gibi belirsiz ifade yerine izni alacak uygulamayı adı + bundle
  kimliğiyle, yoksa eklenecek tam python yolunu ve sayfayı açan komutu yazar; `omniagent-permissions`
  aynı tanıyı komut satırında verir.
- Sırlar süreç ağacına yayılmaz: API anahtarları `os.environ` yerine süreç-içi depoda tutulur,
  alt süreçlere `child_environment()` anahtar değişkenleri çıkarılmış ortam verir ve araç
  çıktısı modele/transcripte gitmeden önce `redact()` ile maskelenir. Böylece model
  `printenv` ile anahtarı okuyamaz.
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
2. **Koordinat Takibi:** tek ortak koordinat uzayı: her eksen 0-1000 (`MODEL_SCREEN_SIZE`).
   Modele giden görüntü 1000×1000 karedir (JPEG q90, renk alt örneklemesi kapalı); kaydedilen
   dosya ve Telegram'a giden görüntü ekranın gerçek en-boy oranını korur. Ekran görüntüsü, AX öğe
   listesi, OCR kutuları ve bütün tıklama/taşıma noktaları (`point: [x, y]`) bu uzaydadır; Retina
   piksel ↔ nokta dönüşümünü araçlar yapar. Kare görüntüde piksel veren (GPT, Claude) ve 0-1000
   normalize veren (gemma, qwen) modellerin sayıları aynıdır. Ölçüm (2026-09-25, 6 gerçek sayfada
   48 DOM hedefi): gpt-6-luna kare görüntüde 39-42/48, en-boy korunmuş görüntü + normalize
   koordinatta 12/48; gemma4 kare kayıpsız görüntüde 38-39/48, en-boy korunmuşta 33-37/48. Eski
   JPEG q70 (4:2:0) gemma4'ü 20/48'e düşürüp noktaları hedefin üstüne kaydırıyordu (medyan
   -13 px); q90 4:4:4 38/48 verir ve PNG'nin üçte biri boyuttadır (~200 KB; PNG görüntülü isteğe
   ~0,8 sn ekliyordu). gemma4 görüntüyü boyuttan bağımsız ~280 tokenlık sabit ızgarada işler;
   küçük metinde piksel hassasiyeti bu yüzden OCR'dan gelir (bkz. GUI). Ayrı x/y alanlarıyla
   10 tıklamanın 7'sinde bozuk argüman (`"x": [x, y]`) üretildiği için nokta tek `point` alanıdır.
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
- **Araç diyeti:** genel yolda model 19 temel araç ve bir `discover_capabilities` şeması, açık
  Chrome yolunda 14 araç görür (önce 26 temel araç vardı). Masaüstüne fotoğraf çekme hedefinde
  `capture_photo`, kaynak değişikliği görevinde `edit_file`, dosya teslim kanalı olan Telegram görevinde
  `send_file`, zamanlama niyetli hedefte (köprü eşleştirilmişse) `schedule_task` eklenir. 26 araçlı şemada model
  hedefteki tarihi 10 denemenin 5'inde yanlış kopyaladı, tek araçla 10/10 doğruydu. Fare/klavye
  adımları `run_action_sequence`, şablon tıklama `smart_click` içindedir.
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
  (Türkçe/Fince karakterler ve emoji doğrulandı; pyautogui bunları sessizce atlıyordu). Çift/üçlü
  tıklama ve sürükleme doğrudan Quartz olaylarıyla gönderilir: pyautogui macOS'ta tıklama durumunu hep 1
  yazdığı için çift tıklama Finder'da dosya açmıyor, metinde kelime seçmiyordu.
- **Ekran metni (`screen_text.py`):** macOS Vision OCR tam Retina çözünürlükte satır ve kelime
  kutularını ~200-450 ms'de okur. `cua_click_text` görünür metni bulup tam ortasına tıklar; metin
  birden çok yerdeyse `near` olmadan tıklamaz, adayları konumlarıyla döner; birebir eşleşme yoksa
  sonuç bunu söyler. Ölçüm (48 hedef, gemma4, gerçek sistem istemi ve şemalar): uçtan uca
  isabet 20/48'den 42/48'e çıktı; model 48 hedefin 44'ünde metin aracını seçti. Görünür metni
  olmayan ikonda araç açık hata verir, model noktaya tıklar.
- **Kaydırma ve baştan sona okuma:** `cua_scroll` imleci panelin üstüne götürüp piksel birimli
  sürekli kaydırma olayları gönderir ve içeriğin kayıp kaymadığını ölçer; "KAYMADI" o yönde
  içeriğin bittiğinin kanıtıdır. `cua_read_scrollable` paneli başa döndürür, görünür yüksekliğin
  %80'i kadar adımlarla sonuna kadar kaydırıp her görünümü OCR ile okur, kenardaki kesik satırları
  ve sayfa örtüşmesini atar (sayıları farklı satırlar asla aynı sayılmaz), paneli yeniden başa
  döndürür ve tüm metni tek sonuçta verir. Kayan panel, fark maskesinin imleci içeren bileşeniyle
  bulunur; başka yerdeki animasyon karışmaz. Örtüşme bulunamazsa araya "olası atlama" işareti
  konur, sessiz atlama olmaz.
- **Eylem → gözlem:** `take_screenshot`, son ekran girdisinden sonra sabit uyku yerine ekranın
  durulmasını bekler: girdi öncesi kareye göre tepki (≤1 sn), ardından 0,45 sn sakinlik, en çok
  3 sn. Karşılaştırma son değişim karesine göredir; yükleme iskeletinin düşük kontrastlı
  parıltısı ancak böyle yakalandı. Yakalama Quartz + CoreGraphics ile ~55 ms (screencapture alt
  süreci ~260 ms idi).
- **Eylem sonu gözlem ve bitiş doğrulaması:** her GUI yolunda eylem içeren tur, görüntü
  istenmediyse ekran durulunca otomatik gözlemle biter (ayrı "ekran görüntüsü al" turu yok;
  eskiden yalnız açık Chrome yolundaydı). Gözlem öncekiyle piksel piksel aynıysa host modele
  eylemin ıskaladığını söyler. Ekranda tıklama/yazma/kaydırma yapılan görev, ilk final yanıtta
  bir kez güncel ekranla doğrulama turuna döner: her zorunlu madde (dolu alan, işaretli kutu,
  gönderim onayı, listenin sonu) kanıtla eşleşmeden bitmez. Canlı kayıtta model formun yarısını
  doldurup "gönderdim", paneli kaydırmadan "tüm ilanlara baktım" demişti.
- **Açık Chrome yolu:** `chrome_active_tab` aynı kökenli sekmeyi kimlikle bulup yüklenmesini
  bekler; ara/gönder tek `cua_submit_text`, çok alanlı form `cua_fill_field` (Enter'a basmaz)
  çağrısıdır. Takip mesajı ("devam et", "formda eksik alan var") 'chrome' kelimesi geçmese de
  önceki görev bu yolda yürüdüyse ve yerel dosya/kabuk işine geçilmiyorsa aynı yolda sürer.
  Pencere görüntüsü Chrome'un önündeki kendi açılır pencerelerini (select menüsü, otomatik
  doldurma) de içerir; başka uygulamaların pencereleri karışmaz. Chrome AX
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
- **Fast Loop:** tool başarısı tek başına ilerleme sayılmaz; host `STATE`/ledger değişimi,
  deterministik teslim araçları ve görsel gözlem imzasını ayrı izler. Görsel olmayan turlarda
  2 anlamsız turdan sonra tek replan, görsel turlarda animasyon/yükleme toleransı için 3 tur
  beklenir; delivery aşamasında 2 anlamsız tur bounded-stop üretir. 24 Eylül 3 koşuluk stress
  ölçümünde `long_research` 3/3 başarı, 6 tur/13 araç ve 8,5 sn medyan; `stagnation` 3/3
  beklenen bounded-failure, 7 tur ve 5,4 sn medyan verdi (önceki politika 10 turdu).
- **Zaman sınırları:** model isteği 30sn (bağlantı 5sn) ve SDK içi yeniden deneme kapalıdır
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
  ve karar kayıtlarını atomik olarak tutar; kısa kayıtlar her görev başında sistem bağlamına
  eklenir, parola/token/API anahtarı gibi gizli bilgileri reddeder.
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
- Görev başlarken üst sağda süreli durum etiketi, arka planda geçici macOS menü çubuğu
  spinner'ı ve Dock rozeti görünür. Arka planda biten görev özel içerik taşımayan macOS
  bildirimi verir; pencere öne gelince rozet/menü öğesi temizlenir.
- Header'daki ⚙ Ayarlar sayfası API anahtarlarını düzenler: alanlar maskelidir, her kartta
  ilgili profil/model/endpoint ve kaynağı söyleyen rozet ("ayarlardan", "ortam değişkeni",
  "yok") bulunur, "Anahtarları göster" kutusu maskeyi kaldırır. Kaydet, değişen alanı
  Keychain'e yazar, süreç-içi depoya alır ve profil anahtarını günceller, model istemcilerini
  yeniden kurar; kısmi başarıda da başarılı anahtar bekletilmez. Görev sürüyorsa değişiklik
  görev bitince uygulanır (eski bağlantı havuzu o an kapatılır). Silme işlemi doğrulama
  okumasıyla kanıtlanır: Keychain silmeyi reddederse kayıt "silindi" sayılmaz ve kullanıcı
  açık hata görür (aksi hâlde sır sonraki açılışta geri geliyordu). Sonuç hem sayfada hem
  transkriptte "hazır profiller" olarak görünür.
- İzin tanısı: `.venv/bin/omniagent-permissions` ekran kaydı ve erişilebilirlik durumunu, izni alacak uygulamayı
  (bundle kimliğiyle) ve eklenecek python ikilisini yazar; `--request` sistem istemini gösterir.
- Görevler kalıcı bir event loop'ta paylaşımlı model istemcileriyle çalışır; `Esc` durdurur,
  `⌘K` temizler. Composer'da Normal/Uzun/Otonom bütçe profili ve macOS yerel mikrofon
  düğmesi bulunur; mikrofon SVG ikonludur, `AVAudioEngine` buffer'ları konuşma sırasında
  partial metni composer'a akıtır ve stop sonrası final sonucu otomatik gönderilmez. Header'daki
  SVG kopyala düğmesi görünen transcript'in tamamını panoya alır.

## 🔀 Çoklu Model Backend'i (config.py: BACKENDS)
- Beş profil, hepsi API anahtarıyla çağrılır: varsayılan `ollama-cloud` (yerel Ollama API'si
  üzerinden `gemma4:cloud`), `openai` (OpenAI API, GPT-6-Luna), `opencode` ve `opencode-think`
  (Opencode Go, `qwen3.8-flash`) ve `openrouter` (`anthropic/claude-sonnet-5`). Bilgisayardaki
  oturum kimliğine dayanan CLI bağlayıcıları (Codex, OpenCode) kaldırıldı: her model turunda
  ayrı süreç başlattıkları için yavaş kalıyorlardı; yerlerini sıcak HTTP bağlantısı aldı.
- **Anahtarlar:** anahtar süreç-içi depoda tutulur (`config._RUNTIME_KEYS`, `set_api_key`),
  ortam değişkeni yalnız kayıt yoksa kullanılan yedektir (`load_api_key`); kabuk değişkeni
  (`OPENAI_API_KEY`, `OPENCODE_API_KEY`, `OPENROUTER_API_KEY`; eşleme `config.API_KEY_VARIABLES`)
  böylece ajanın başlattığı alt süreçlere miras kalmaz. Arayüzdeki Ayarlar sayfası (⚙)
  anahtarı macOS Keychain'e (`api_keys.py`, hizmet `OmniAgent.APIKeys`) yazar ve süreç-içi
  depoya alır; `config.apply_stored_api_keys()` giriş noktalarında (ui/main/telegram/benchmark)
  kayıtlı anahtarları depoya alır, ortama YAZMAZ. Kayıtlı anahtar bilinçli olarak kabuk
  değişkenini geçersiz kılar (arayüzden girilen anahtar sessizce yok sayılmasın); rozet iki
  kaynağı ayrı gösterir. Hiçbir anahtar dosyası veya opencode `auth.json` okunmaz; anahtar
  log'a, olay akışına, araç çıktısına veya dokümana yazılmaz, arayüzde maskeli gösterilir ve
  `redact()` ile araç çıktısından temizlenir. Anahtarı tanımlı olmayan profil istemci kurmaz,
  hangi değişkenin gerektiğini BİR KEZ uyarı olarak bildirir ve o görevde kullanılamaz sayılır. Model adları `OMNI_OPENAI_MODEL`, `OMNI_OPENCODE_MODEL`,
  `OMNI_OPENROUTER_MODEL` ve `OMNI_OLLAMA_CLOUD_MODEL` ile değiştirilebilir; seçilen Ollama
  modelinin `ollama list` içinde bulunması gerekir.
- **Kalite merdiveni:** `QUALITY_LADDER` (`ollama-cloud` → `openai` → `openrouter`) başlangıç
  profilini ve API hatasında denenecek yedek sırasını belirler. Araç hataları model değiştirmez:
  dış sağlayıcı hatası (arama servisi, ağ) başka modelle düzelmez, yalnız maliyet ve süre ekler
  (`test_tool_failures_do_not_switch_model_backend`).
- **API hataları:** geçici 5xx/bağlantı hatasında aynı backend bir kez yeniden denenir.
  429'da hazır başka profil varsa sağlayıcıya erken yeniden istek atılmadan ona geçilir;
  tek profil varsa Retry-After en çok 30 sn ise beklenir. 401/402/403 ve alternatifli 429
  görev boyunca karantinaya alınır; sonraki turda aynı başarısız profil çağrılmaz.
  Geçici fallback yalnız o turdadır; kalıcı erişim hatası veya kalite merdiveni geçişi
  mevcut profili görev boyunca değiştirir.
- **Önbellek:** `openrouter` profili `cache_control` gönderir (OpenRouter'da Anthropic önek
  önbelleği yalnızca bununla açılır); opencode öneki kendiliğinden önbellekler.
- **Manuel seçim:** `OMNI_BACKEND=<profil>` ya da arayüzdeki seçim. Ollama modeli kurulu değilse
  veya ilgili anahtar tanımlı değilse kullanılabilir kalite basamağına düşer ve uyarı verir.
- **Ollama request uyumu:** OpenAI sözleşmesinde `tools` varsa `tool_choice` varsayılanı zaten
  `auto`dur; Ollama'nın güncel tool-calling örnekleri de yalnız `tools` gönderir. Bu yüzden
  Ollama hot path'inde redundant `tool_choice="auto"` alanı taşınmaz; canlı A/B'de aynı doğru
  tool call korunurken istek 0,727→0,628 sn ölçüldü.
- **Ölçüm (2026-09-24):** `ollama-cloud` 9 senaryo × 3 koşuda 27/27 başarı ve 4,0 sn medyan
  verdi. Oturum tabanlı CLI yedekleriyle alınan eski ölçüm (Codex CLI 9/9, 11,3 sn medyan)
  geçersizdir: CLI bağlayıcıları kaldırıldı. Artık her tur tek bir sıcak HTTP çağrısıdır;
  yedekler yalnız API anahtarı tanımlıysa ve merdiven sırası geldiğinde denenir.

## 📱 Telegram ve yetenek farkındalığı
- `telegram_bridge.py`, eşleştirilmiş özel Telegram sohbetinde varsayılan olarak tek
  balonda kısa, canlı yanıt gösterir. `/verbose on` sonraki görevde düşünme/araç/çıktı,
  model, süre ve token ayrıntılarını açar; `/verbose off` kısa görünüme döner. Ekran
  görüntüsü ayrıca gönderilir. `/stop`, `/status`, `/model` ve `/mode` desteklenir.
  Bot tokenı Keychain'de, sohbet ve kullanıcı kimliği özel izinli yerel dosyadadır.
  Kurulum: [TELEGRAM.md](docs/TELEGRAM.md).
- Zamanlanmış görevler (`core/schedule.py`, `schedules.json`): yerel duvar saatiyle hesaplanır (yaz saati
  geçişinde kaymaz); köprü 30 sn'de bir denetler, kaçan çalışmayı 6 saat içinde bir kez yetiştirir,
  eskisini atlayıp bildirir; host kilidi meşgulse bekletir. Zamanlanmış çalışmada `schedule_task`
  kapalıdır (kendini çoğaltmaz). `/schedules` ve `/unschedule` modelsiz yönetir.
- Açıklamasız Telegram sesli mesajı komuttur: `integrations/transcription.py` OpenAI Speech-to-Text ile
  (kullanıcının OpenAI anahtarı; `gpt-transcribe`, yoksa `whisper-1`) yazıya çevirir, anlaşılan metni önce
  sohbete yazar. macOS Speech launchd altındaki Python sürecinde TCC izni alamadığı için kullanılmaz;
  anahtar yoksa ses hiçbir yere gönderilmez.
- Telegram ekleri (fotoğraf/belge/ses/video, en çok 20 MB) `telegram-inbox/` altına 0600 izinle
  indirilir; açıklama görev olur, yolu göreve eklenir. Görseller ilk kullanıcı mesajına en-boy oranı
  korunarak görüntü olarak eklenir (ekran görüntüsü gibi kareye sündürülmez: koordinat uzayı yoktur).
- Modelin gerçek yürütme yetkisi her turdaki araç şemalarıdır. `discover_capabilities` hazır
  API/MCP'yi ve gerekirse kısa kaynak keşfini açar; skill dosyası yöntem bilgisidir, hesap
  erişimi değildir. Kurulu yerel skill'ler `~/.agents/skills` ve `~/.codex/skills` altında
  dizinlenir; `query=catalog` yerel envanteri ağ olmadan döner. Skill, çalıştırılabilir API/MCP
  kaydını sıralamada geçemez. Kalıcı plugin yalnız güvenilir, sabit sürümlü kayıtla kurulur.
- Tekrarlı yerel iş için `execute_js` veya temizlenen geçici script kullanılabilir.
  `// omni:save ad` başarılı JS kodunu en çok beş adla yalnız görevde saklar;
  `// omni:run ad` JSON girdisiyle yeniden çalıştırır. Başarısız kod kaydedilmez. Görevin
  istemediği kalıcı aracı ya da kendi kaynak değişikliğini ajan kendiliğinden eklemez.
- Epizodik kayıtlar otomatik ders olarak modele verilmez: önceki ölçümde hedef sapmasına yol
  açtı. Kalıcı kullanıcı tercihleri yalnız açık istekle `user_memory` aracına yazılır.
  Hata öğrenmesi eklenecekse aynı argüman/hata imzasına koşullanmalı ve benchmark ile
  doğrulanmalıdır. Başarısız çağrı aynı girdilerle sonsuz tekrar edilmez.

## 🎯 Hedefler
- [x] `@Chatgpt-System` yeteneklerini `tools.py` içerisine gömmek. (bkz. Plugin Entegrasyonu)
- [x] Ajanın kendi yetki seviyesini yönetebildiği bir güvenlik katmanı eklemek. (bkz. 🛡️ Güvenlik Rayları)
- [x] Açıkça istenen tercih, yol ve kararları atomik `user_memory.json` deposunda tutup
  kısa kayıtları görev başında yükleyen hatırlama/arama/silme akışı eklemek.
- [x] Vizyon ve koordinat sistemini hibrit hale getirmek. (`smart_click`: AX → görsel şablon;
  tek ortak koordinat uzayı.) Açık Chrome görevinde görüntü + settle yalnız öndeki Chrome
  penceresinden alınır; model koordinatları pencerenin ekran origin'ine çevrilir.
- [x] Ajan döngüsünü canlı hedeflerle ölçüp hız/doğruluk sınırlarını ayarlamak.
  (2026-09-23, `benchmark.py`, 9 senaryo × 3 koşu: başarı 19/27 → 27/27, medyan 17,3sn → 4,5sn.)
- [x] Hatalardan doğrulanmış kalıcı öğrenme: `experience.py` aynı araç/hata imzasına koşullu,
  yalnız başarılı görevde doğrulanmış düzeltmeyi saklar; `self_repair` canlı ölçümü 3/3,
  öğrenme sonrası 4 araçtan 2 araca düştü.
- [x] Finansal para hareketi ve istenmemiş kalıcı hafıza mutasyonu için host onayı + maskeli audit.
