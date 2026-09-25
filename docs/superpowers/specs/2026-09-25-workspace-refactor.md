# OmniAgent Kod Tabanı ve Workspace Yeniden Yapılandırma Spesifikasyonu

## Amaç
OmniAgent kod tabanını davranışını koruyarak okunabilir, paketlenebilir ve test edilebilir bir `src/omniagent` yapısına taşımak; kaynak ağacındaki runtime verisini ayırmak; yüksek karmaşıklıktaki modülleri sorumluluk sınırlarına bölmek; statik analiz ve satır bazlı incelemede bulunan gerçek mantık kusurlarını regresyon testleriyle düzeltmek.

## Davranışsal sözleşme
- Mevcut CLI, UI, Telegram, araç çağırma, Chrome/GUI, hafıza, checkpoint, model fallback ve entegrasyon davranışları korunur.
- Her davranış değişikliği önce başarısız regresyon testiyle kanıtlanır.
- Kullanıcının mevcut eşleştirme/Keychain bilgisi korunur.
- Canlı kullanıcı verisi veya `.omni_backups` silinmez.
- Mevcut dirty worktree resetlenmez veya ilgisiz değişiklikler ezilmez.
- Telegram launchd servisi refactor sonunda yeni paket giriş noktasına geçirilir ve yeniden doğrulanır.

## Hedef dizin yapısı
```text
src/omniagent/
  __init__.py
  cli.py
  paths.py
  config.py
  app/
    agent.py
    model_runtime.py
    routing.py
    tool_execution.py
    verification.py
  core/
    checkpoint.py
    conversation.py
    events.py
    fast_loop.py
    state.py
    task_ledger.py
  memory/
    experience.py
    user.py
  integrations/
    capabilities.py
    runtime.py
    mcp.py
    outlook.py
    outlook_auth.py
    telegram.py
  platform/macos/
    api_keys.py
    desktop_status.py
    headless_screen.py
    host_lock.py
    permissions.py
    screen_text.py
    voice.py
  tools/
    __init__.py
    facade.py
    browser.py
    cdp.py
    filesystem.py
    gui_input.py
    screen.py
    system.py
    types.py
  ui/
    app.py
    markdown.py
  dev/
    benchmark.py
tests/
docs/
benchmarks/
var/
```

## Paketleme
- PyPA `src` layout kullanılacak.
- `pyproject.toml` gerçek installable package tanımlayacak.
- Kullanıcı girişleri `[project.scripts]` ile sağlanacak: `omniagent`, `omniagent-ui`, `omniagent-telegram`, `omniagent-benchmark`, `omniagent-permissions`.
- Testler doğrudan paket modüllerini import edecek; kökte geçici compatibility shim bırakılmayacak.

## Runtime veri konumu
- Varsayılan kalıcı veri kökü `~/Library/Application Support/OmniAgent`.
- State, deneyim, kullanıcı hafızası, checkpoint ve Telegram dosyaları bu kökün altındadır.
- Kaynak ağacındaki mevcut canlı dosyalar veri kaybı olmadan eski konumdan okunabilir/migrate edilebilir; migrasyon hedef mevcutsa üzerine yazmaz.
- State yazıcıları hedef üst dizini yoksa oluşturur.

## Kod sağlığı
- `main.py` benzeri god-module sorumlulukları ayrılır.
- `tools/__init__.py` yalnız dış API re-export katmanı olur; Toolbox ayrı `facade.py` dosyasındadır.
- Kullanılmayan importlar ve açık statik analiz bulguları temizlenir.
- Python `assert` üretim hata denetimi olarak kullanılmaz.
- Broad exception yalnız dış sistem/boundary cleanup gibi gerçekten gerekli sınır noktalarında tutulur; iç mantık hatalarını gizleyen yerler daraltılır.
- Asenkron hot path üzerinde gereksiz blocking dosya işlemleri mümkün olduğunca thread'e taşınır.
- Sistem promptu host davranışıyla çelişmez; mevcut host onay kapıları ve araç izinleri model tarafından yok sayılmaya çalışılmaz.

## Başarı ölçütleri
- Tüm üretim Python dosyaları `src/omniagent` altında mantıksal klasörlerde.
- Kök kaynak dosyası karmaşası kaldırılmış.
- `python -m compileall` başarılı.
- Tam pytest yeşil ve başlangıçtaki 297 passed / 15 skipped seviyesinden gerileme yok.
- `git diff --check` temiz.
- Paket entry point'leri `--help`/smoke test geçiyor.
- Telegram launchd servisi paket giriş noktasıyla running.
- Bot API smoke kontrolü mesaj göndermeden başarılı.
- AST/static audit sonunda syntax hatası, üretim bare-except ve bilinen kullanılmayan üretim importları kalmıyor.
