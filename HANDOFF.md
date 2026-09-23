# OmniAgent — Devir Notu (2026-09-23)

Kurallar, mimari ve performans kararlarının tek kaynağı `AGENTS.md`'dir; bu not yalnızca
kaldığı yerden devam etmek için gereken durumu içerir.

## Durum
- Dal: `perf/hiz-dogruluk-arayuz`, son commit `e019aaf`. Açık Chrome yolunun GUI hızlandırması
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
- Hatalardan kalıcı öğrenme kaldırıldı (ölçümde zararlıydı); yeniden eklenecekse `AGENTS.md`
  Hedefler bölümündeki koşullarla.
- Tercih/ortak yol kalıcı belleği `user_memory.json` + `user_memory` aracı olarak eklendi:
  yalnızca açıkça istenen kısa kayıtlar saklanır, atomik yazılır ve her görevde otomatik
  olarak model istemine enjekte edilmez. Parola/token/API anahtarı kayıtları reddedilir.
  Aktif görev checkpoint/resume ve semantik karar çıkarımı henüz yoktur.
- Chromium ilk açılışı ~7sn (soğuk başlatma); statik sayfalar için `fetch_raw` tercih edilmeli.
