# OmniAgent — Devir Notu (2026-09-24)

Kurallar, mimari ve performans kararlarının tek kaynağı `AGENTS.md`'dir; bu not yalnızca
kaldığı yerden devam etmek için gereken durumu içerir.

## Güncel model durumu
- `99041f8` ile varsayılan model yerel Ollama API'sinden `gemma4:cloud` oldu. Bu makinede
  `gemma4:cloud` ve `gpt-oss:20b-cloud` kurulu; ikinci model
  `OMNI_OLLAMA_CLOUD_MODEL=gpt-oss:20b-cloud` ile seçilebilir.
- **Oturum tabanlı CLI bağlayıcıları kaldırıldı** (Codex `openai`, OpenCode `zen-free`,
  `cli_backends.py`). Kalan profillerin hepsi API anahtarıyla çağrılır: `ollama-cloud`,
  `openai` (OpenAI API), `opencode`/`opencode-think` (Opencode Go) ve `openrouter`. Anahtarlar
  `OPENAI_API_KEY`, `OPENCODE_API_KEY`, `OPENROUTER_API_KEY` adlarıyla okunur; tanımlı olmayan
  profil kullanılamaz ve bir kez uyarı verir. Kalite merdiveni
  `ollama-cloud` → `openai` → `openrouter`.
- **Ayarlar sayfası:** arayüzdeki ⚙ düğmesi API anahtarlarını düzenler (`api_keys.py`,
  Keychain hizmeti `OmniAgent.APIKeys`). Kaydet → Keychain (silme doğrulama okumasıyla
  kanıtlanır) + süreç-içi depo (`config.set_api_key`) + `refresh_api_keys()` + istemci
  yenileme; görev sürerken değişiklik görev bitince uygulanır. Giriş noktaları açılışta
  `config.apply_stored_api_keys()` çağırır. Anahtarlar `os.environ`'a YAZILMAZ: alt süreç
  ortamından çıkarılır (`tools.child_environment`) ve araç çıktısı `redact()` ile maskelenir.
  Kayıtlı anahtar kabuk değişkenini geçersiz kılar.
- **İzin tanısı:** `uv run python permissions.py` ekran kaydı izin durumunu, izni alacak
  uygulamayı (bundle kimliğiyle) ve eklenecek python ikilisini yazar; `--request` macOS izin
  istemini gösterir. Hata metni artık "Terminal/Python" demiyor, izni alacak uygulamayı söyler.
- **24 Eylül son ölçüm:** `ollama-cloud` 9 temel senaryo × 3 koşuda 27/27 başarı ve 4,0 sn
  medyan verdi. `long_research` üç koşuda 3/3 başarı, 6 tur/13 araç ve 8,5 sn medyan;
  `stagnation` üç koşuda 3/3 beklenen bounded-failure, 7 tur ve 5,4 sn medyan verdi.
  Fast Loop artık görsel olmayan stagnation için 2, görsel turlar için 3 ve delivery için 2
  turluk pencere kullanıyor (eski stagnation yolu 10 turdu).
- Güncel tam doğrulama: `233 passed, 14 skipped`; `py_compile` ve `git diff --check` temiz.
- `browse_url` ayrı ve görünmeyen Chromium kullanır; açık Google Chrome için
  `chrome_active_tab` yolu istenir. CLI süreç yönetimi (stdin istemleri, 45 sn sınırı, iptal
  temizliği) kodla birlikte kaldırıldı.
- Başka ajan aynı çalışma ağacında Fast Loop/benchmark dosyalarını düzenliyor; bu dosyaları
  sürüm kontrolüne alırken ve UI sürecini yeniden başlatırken aktif çalışmasını koru.

## 23 Eylül durumu (tarihsel)
- Dal: `perf/hiz-dogruluk-arayuz`, o günkü son commit `e019aaf`. Açık Chrome yolunun GUI hızlandırması
  (eylem turu sonunda otomatik gözlem, sabit uyku yerine ekran durulma beklemesi, 1000×1000
  koordinat uzayı ve `point: [x, y]` argümanı, tek çağrıda ara/gönder `cua_submit_text`, sekme
  bulma düzeltmesi, `ssl.SSLError` yeniden denemesi, `chrome_ilan` GUI senaryosu) commit
  edilmemiş çalışma ağacındadır.
- `requirements.txt` kaldırıldı → `pyproject.toml` + `uv.lock` (`uv sync`).
- Composer'a Normal/Uzun/Otonom bütçe profilleri ve macOS yerel Speech-framework mikrofon
  girişi eklendi. `AVAudioEngine` + `SFSpeechAudioBufferRecognitionRequest` ile konuşma
  sürerken partial metin composer'a akar (mümkünse on-device), stop sonrası final metin
  kalıcı taslağa yazılır;
  otomatik gönderim yoktur ve geçici ses dosyası oluşturulmaz. Mikrofon düğmesi SVG,
  transcript kopyalama düğmesi de SVG ikonludur. Otonom profil güvenlik raylarını kapatmaz.
- `fetch_raw` artık yalnızca HTTP(S) kabul ediyor; kabuk guard'ı sudo seçeneklerini, çözülemeyen
  değişken hedeflerini ve iç içe `sh -c` yıkıcı komutlarını da yakalıyor.

## Çalıştırma
```bash
.venv/bin/python ui.py                                 # akışlı arayüz
.venv/bin/python main.py "<hedef>"                     # CLI (terminale akış)
.venv/bin/python -m pytest tests/ -q                   # canlı API testi: OMNI_LIVE_API_TEST=1
.venv/bin/python benchmark.py --runs 3 --concurrency 3 # hız + doğruluk ölçümü
# GUI ölçümü: fareyi/klavyeyi kullanır, açık bir Chrome penceresi gerekir
.venv/bin/python benchmark.py --runs 3 --concurrency 1 --only chrome_ilan
# Gerçek Chrome sekme testi (ekranda geçici pencereler açar)
OMNI_CHROME_TEST=1 .venv/bin/python -m pytest tests/test_core.py -q -k live_matching_tab
```

## Ölçüm (2026-09-23)
| Senaryo | Önceki sürüm | Güncel sürüm |
|---|---|---|
| Genel, 9 senaryo × 5, aynı anda | 44/45, medyan 4,6 sn | 42/45, medyan 4,7 sn |
| `chrome_ilan` (ara + 3 ilan kodu) | 1/2, medyan 73 sn, 14-24 tur | 3/3, medyan 30 sn, 6 tur |

Genel senaryolardaki hatalar iki sürümde aynı türdedir (kod önekini düşürme, sonucu dosyaya ekleme).

## Uzun görev dayanıklılığı
- Çok adımlı/çok adaylı GUI görevlerinde sistem istemi her araç turunda kısa `STATE:` çalışma
  kaydı ister; doğrulanmış değerler, elenen adaylar ve kalan zorunlu adımlar görsel bağlamdan
  bağımsız metin olarak taşınır.
- Eski ekran görüntüsü budanırken aynı multimodal mesajdaki metin artık korunur. Aynı tam Chrome
  URL'sinin tekrarlı başarılı açılışı modele uyarı verir; 60k önbelleksiz giriş tokenı veya
  24 araç çağrısında tek seferlik yumuşak bütçe uyarısı opsiyonel keşfi durdurmaya yönlendirir.
- Geçici API fallback'i artık sticky değildir: bir tur Claude ile tamamlanmış olsa bile sonraki
  tur normal backend yeniden denenir. Araçsız final yanıtı `max_tokens` ile kesilirse bir kez
  kısa tamamlama turu yapılır.
- Doğrulama (bu ses düzeltmesi): hedefli Voice/UI testleri `9 passed, 1 skipped`; gerçek Tk
  hedefli testleri `6 passed`; `voice.py` Pyright, Ruff, `compileall` ve `git diff --check` temiz.
  Önceki tam doğrulama `123 passed, 7 skipped` / `OMNI_UI_TEST=1` ile `128 passed, 2 skipped`
  idi. Çalışma ağacındaki eşzamanlı `allow_memory_mutation` değişiklikleri nedeniyle tam suite
  şu anda ilgisiz memory/agent testlerinde bekliyor; bu değişikliklere dokunulmadı.
  Canlı benchmark, mikrofon izni veya gerçek Chrome/Outlook oturumu çalıştırılmadı.

## Açık konular
- Ekran görüntüleri `FULL_DETAIL_TURNS` tur sonra hâlâ bağlamdan düşer. `STATE`, metin koruma
  ve tekrar-URL uyarısı bunu azaltır fakat modelin ekrandaki bir değeri metne hiç aktarmamasını
  deterministik olarak çözmez. macOS Vision OCR ileride ek güçlendirme olabilir; mevcut sanal
  ortamda `Vision` Python modülü kurulu değildir.
- Chrome'un AX ağacı web içeriğini vermiyor (`AXManualAccessibility` desteklenmiyor,
  `AXEnhancedUserInterface` ayarlanamıyor); Electron uygulamalarında da AX yalnız pencere
  çerçevesini gösterebiliyor → bu uygulamalarda ekran görüntüsü gerekir.
- Otomatik gözlem yalnız açık Chrome yolundadır; genel yolda `take_screenshot` durulma
  beklemesini kullanır ama model gözlemi kendisi ister.
- Hatalardan kalıcı öğrenme yeniden eklendi ama global görev benzerliği olarak değil:
  `experience.py` yalnız aynı araç + hata imzasında, değiştirilmiş çağrının başarıyla
  doğrulandığı ve görevin tamamlandığı kurtarmayı saklar. Ders yalnız aynı hata tekrar
  oluştuğunda araç sonucuna eklenir; başarısız görev ders yazamaz. `self_repair` canlı
  ölçümünde 3/3 başarı; ilk koşu 5 tur/4 araç/4,5 sn, öğrenilen sonraki koşular
  3 tur/2 araç ve 3,6/2,1 sn verdi.
- Tercih/ortak yol kalıcı belleği `user_memory.json` + `user_memory` aracı olarak eklendi:
  açık kullanıcı tercihleri sistem bağlamına alınabilir; parola/token/API anahtarı kayıtları
  reddedilir. İstenmemiş kalıcı hafıza mutasyonu ve finansal para hareketleri modelden bağımsız
  host onay kapısından geçer ve maskelenmiş denetim kaydı üretir.
- Yarım görev finali son `STATE:` ledger'ını korur; devam mesajında geçmiş alışveriş üzerinden
  FACTS/REMAINING yeniden kullanılabilir. Tam süreç checkpoint'i henüz ayrı workflow motoru değildir.
- Chromium ilk açılışı ~7sn (soğuk başlatma); statik sayfalar için `fetch_raw` tercih edilmeli.


## 24 Eylül — arka plan durumu ve yerel skill keşfi
- İzole `codex/background-agent` dalında `ebc7a83` commit'i: `desktop_status.py`,
  arka planda menü çubuğu spinner'ı/Dock rozeti, bitiş bildirimi ve yerel skill indeksi.
  Aynı değişiklikler, diğer ajanın kirli dosyaları korunarak ana çalışma ağacına hedefli
  şekilde uygulandı; ana ağacın geniş farkı başka ajanın çalışmasını da içerdiğinden
  bütün dosyalar toplu commit edilmedi.
- `discover_capabilities(query="catalog", operations=[], allow_online=false)` kurulu
  API/MCP/skill envanterini ağ isteği olmadan listeler. Skill'ler yöntem bilgisidir;
  Outlook API gibi çalıştırılabilir araçların önüne geçmez.
- Tam paket: 226 geçti, 14 atlandı; son UI zamanlama değişikliğinden sonra hedefli
  arka plan testleri 5 geçti. Yerel katalog 57 kayıtla 31,6 ms açılış ve 0,059 ms P95
  sorgu verdi. UI launchctl ile boş görev kilidi sırasında yeniden başlatıldı.
  Canlı arka plan bildirimi kullanıcı ekranında tetiklenmedi; sahte AppKit/başlatıcıyla
  sınandı.
- `176e55e`: `execute_js` görev belleğinde en çok beş, 16 KB geçici JS yardımcısını
  başarılı çalışmadan sonra saklar; `// omni:run ad` JSON girdisiyle tekrar kullanır. Girdi
  süreç argümanında sır olarak taşınmaz, 0600 geçici dosya/preload ile okunup temizlenir.
  Gerçek Node testi ve tam paket 230 geçti, 14 atlandı.
- `e1b33a3`: uygulama oturumu sırasında kurulan yeni yerel skill, katalog sorgusunda veya
  mevcut eşleşme bulunmadığında ağ çağrısı olmadan yeniden dizinlenir. Tam paket 231 geçti,
  14 atlandı.

## 24 Eylül — gerçek Chrome GUI blocker kapatıldı
- Kök neden AppleScript fallback değildi: tam-ekran screenshot/settle başka uygulamanın
  (T3 Code izin penceresi) içeriğini Chrome görevinin görseline karıştırıyordu. Ajan önce aynı
  yabancı bölgeyi tekrar tıklıyor, pencere-scope ilk düzeltmesinden sonra da global settle
  yanlış tepki sinyali verip XHR sonucu gelmeden gözlem ürettiği için aramayı tekrar gönderiyordu.
- `chrome_active_tab` artık görsel scope'u Google Chrome'a kilitler. Quartz
  `kCGWindowListOptionIncludingWindow` ile yalnız öndeki Chrome penceresi yakalanır; screenshot
  geometrisi pencere origin'ini taşır ve `cua_click_point` / `cua_submit_text` aynı geometriyi
  kullanır. Eylem sonrası settle karşılaştırması da yalnız bu pencereyi izler. T3 Code'a dokunulmadı.
- Taze gerçek `chrome_ilan` benchmarkı iki ardışık final koşuda **1/1 + 1/1**, **15,5 sn /
  17,1 sn**, her ikisinde 4 tur / 8 araç ve beklenen üç kodun tamamı doğru; ev dizininde
  istenmeyen yan etki yok.
- Kapanış: tam pytest **265 passed, 14 skipped**; gerçek Tk/UI **17/17**; canlı varsayılan API
  **1/1**; `compileall` ve `git diff --check HEAD` temiz.
