# OmniAgent Geliştirme Kuralları ve Yol Haritası

Bu dosya, OmniAgent projesinin geliştirilme sürecinde uyulacak katı kuralları, davranışsal rehberleri ve öncelikli geliştirme hedeflerini içerir.

## 🛡️ Güvenlik Rayları (Safety Rails)
Ajan, kendi kaynak kodunu ve host sistemini değiştirebildiği için `tools.py` içinde kod
seviyesinde koruma katmanı bulunur (sandbox değildir, en iyi çaba korumasıdır):
- `write_file`: hassas sistem/kimlik dosyalarına (`~/.ssh`, `/etc`, kabuk profil dosyaları vb.)
  yazmayı reddeder; `.py` hedeflerinde yazmadan önce `compile()` ile sözdizimini doğrular;
  üzerine yazmadan önce `.omni_backups/` içine zaman damgalı yedek alır; eksik üst dizinleri
  oluşturur ve bunu sonuçta açıkça bildirir (yazım hatalı bir yol sessizce dizin açmasın).
- `execute_shell`: bilinen yıkıcı komut kalıplarını (`rm -rf /`, `mkfs`, fork bomb, disk
  biçimlendirme vb.) ve korunan yollara `>`/`>>`/`tee` yönlendirmesini engeller; `sudo`
  çağrıları yapılandırılmış log ile kaydedilir ve parolasız (`-n`) olmayan sudo oturumlarında
  güvenli şekilde başarısız olur.
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
2. **Koordinat Takibi:** tek ortak koordinat uzayı. Ekran görüntüsü, AX öğe listesi ve bütün
   tıklama/taşıma koordinatları aynı uzaydadır (uzun kenar `MODEL_SCREEN_MAX_EDGE`); Retina
   piksel ↔ nokta dönüşümünü araçlar yapar. Ayrı imleç konumu aracı araç diyetiyle kaldırıldı.
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
- **Bağlam:** son `FULL_DETAIL_TURNS` turdan eski uzun araç çıktıları, görseller ve argümanlar
  budanır (`_trim_old_turns`). Yaş TUR ile ölçülür, son tur asla kırpılmaz; pencere tur tur
  kaydığı için önek bayt bayt aynı kalır ve sağlayıcı önek önbelleği isabet eder (~%70).
  Modelin `reasoning_content`'i geçmişe eklenmez.
- **Zaman sınırları:** model isteği 60sn (bağlantı 5sn) ve SDK içi yeniden deneme kapalıdır
  (SDK varsayılanı 600sn + 2 gizli deneme idi); görev başına 10dk (`MAX_WALL_CLOCK_SECONDS`)
  ve 25 tur.
- **Akış:** model yanıtı `stream=True` ile alınır. Metin, düşünme metni, araç çağrısı
  önizlemesi (komut model yazarken harf harf) ve komut çıktısı (`run_streaming_process`,
  satır satır) `events.py`'deki tipli olaylarla yayınlanır; yarıda kesilen akış yeniden
  denenirse önce `stream_reset` gelir. Durdurma isteği model akışını ve çalışan komutu (süreç
  grubuyla) anında keser.
- **Bellek:** `cognitive_memory.json` son 30 görevi ölçümleriyle kaydeder ve modele geri
  enjekte EDİLMEZ. Otomatik ders/rota enjeksiyonu ölçümde zararlı bulundu (alakasız "çözümler",
  başka görevlerin yolları, "görev belirtilmedi" yanıtları) ve kaldırıldı; macOS'a özgü bilinen
  tuzaklar `SYSTEM_PROMPT` içindeki sabit ENVIRONMENT bloğundadır.

## 🖥️ Arayüz (ui.py)
- Yalnızca `events.py` olaylarını tüketir; log metni ayrıştırılmaz. Olaylar ajan thread'lerinden
  kuyruğa gelir, tüm çizim Tk thread'inde ~60 fps'lik tek kare döngüsünde (`_tick`) yapılır.
- Tasarım dili Claude Code + Codex: nötr koyu yüzeyler, Menlo mono transkript, Claude turuncusu
  (`#D97757`) vurgu; araçlar `⏺ Ad(önizleme)` blokları, komutlar `$` satırları, çıktılar `⎿`
  altında (çalışırken canlı son 6 satır, bitince ilk 4 satır + "… +N satır").
- Animasyonlar: daktilo akışı, yanıp sönen imleç ve çalışan araç işareti, yıldız spinner'lı ve
  parıltılı durum satırı (süre, token, "esc ile durdur").
- Görevler kalıcı bir event loop'ta paylaşımlı model istemcileriyle çalışır; `Esc` durdurur,
  `⌘K` temizler.

## 🔀 Çoklu Model Backend'i (config.py: BACKENDS)
- Dört profil: `opencode` (varsayılan, qwen3.8-flash, düşünme kapalı), `opencode-think` (aynı
  model, düşünme açık), `claude` (openrouter üzerinden claude-sonnet-5, ESCALATION_BACKEND),
  `openai` (gpt-5-mini). Hepsi opencode'un auth.json'ındaki anahtarları kullanır.
- **Kalite merdiveni:** art arda `CONSECUTIVE_FAILURE_ESCALATION_THRESHOLD` (2) tamamen başarısız
  araç turunda `QUALITY_LADDER` boyunca çıkılır: `opencode` → `opencode-think` → `claude`.
- **API hataları:** ilk iki deneme aynı backend'de (5xx/429/bağlantı), son deneme `claude`'da;
  zaman aşımında doğrudan `claude`'a atlanır; kalıcı 4xx hataları yeniden denenmez.
- **Önbellek:** `claude` profili `cache_control` gönderir (OpenRouter'da Anthropic önek önbelleği
  yalnızca bununla açılır); opencode öneki kendiliğinden önbellekler.
- **Manuel seçim:** `OMNI_BACKEND=<profil>` ya da arayüzdeki seçim (anahtar yoksa varsayılana
  düşer, uyarı verir).
- **Bilinen kısıt (2026-09-22 itibarıyla):** `openai` hesabında kredi yok (429). `claude`
  (openrouter) bakiyesi düşük; `max_tokens` 2048 ile sınırlıdır (yüksek rezervasyon 402
  veriyordu). opencode profilleri 8192 token üretebilir.

## 🎯 Hedefler
- [x] `@Chatgpt-System` yeteneklerini `tools.py` içerisine gömmek. (bkz. Plugin Entegrasyonu)
- [x] Ajanın kendi yetki seviyesini yönetebildiği bir güvenlik katmanı eklemek. (bkz. 🛡️ Güvenlik Rayları)
- [x] Vizyon ve koordinat sistemini hibrit hale getirmek. (`smart_click`: AX → görsel şablon; tek ortak koordinat uzayı)
- [x] Ajan döngüsünü canlı hedeflerle ölçüp hız/doğruluk sınırlarını ayarlamak.
  (2026-09-23, `benchmark.py`, 9 senaryo × 3 koşu: başarı 19/27 → 27/27, medyan 17,3sn → 4,5sn.)
- [ ] Hatalardan kalıcı öğrenme: yeniden eklenecekse argüman farkına dayanan, yalnızca aynı
  hatalı argümanla eşleşen dersler olarak ve `benchmark.py` ile ölçülerek eklenmeli.
