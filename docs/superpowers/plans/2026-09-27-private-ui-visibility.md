# Private UI Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `⌘X` ile OmniAgent masaüstü penceresini global gizle/göster ve OmniAgent'ın kendi ekran yakalamalarından UI'yi çıkar.

**Architecture:** `platform/macos/visibility.py` Carbon global hotkey yaşam döngüsünü yönetir; geri çağrı Tk kuyruğuna istek bırakır. `ui/app.py` ana thread'de tüm uygulama pencerelerini gizleyip geri getirir. `tools/screen.py` yalnız kendi tam ekran Quartz görüntülerinden OmniAgent UI süreç pencerelerini çıkarır.

**Tech Stack:** Python 3.11, Tk/CustomTkinter, PyObjC AppKit/Quartz, Carbon `ctypes`, pytest.

---

## Dosya haritası

- Oluştur: `src/omniagent/platform/macos/visibility.py` — hotkey kaydı, geri çağrı ve kaldırma.
- Değiştir: `src/omniagent/ui/app.py` — kayıt, UI kuyruğu, gizle/göster, kapanış temizliği ve hata görünümü.
- Değiştir: `src/omniagent/tools/screen.py` — tam ekran yakalamalarda UI PID filtreleme.
- Oluştur: `tests/test_ui_visibility.py` — hotkey bağlayıcısı ve seçim/filtreleme birim testleri.
- Değiştir: `tests/test_ui_conversation.py` — gerçek Tk gizle/göster duman testi.
- Değiştir: `AGENTS.md`, `docs/CAPABILITIES.md` — davranış ve üçüncü taraf yakalama sınırı.

## Görev 1: Global hotkey bağlayıcısı

- [ ] `tests/test_ui_visibility.py` içinde fake Carbon API ile kayıt, yanlış/başarısız kayıt ve kapanış testlerini yaz; `pytest tests/test_ui_visibility.py -q` ile ilk başarısızlığı gör.
- [ ] `visibility.py` içinde `EventHotKeyID`/`EventTypeSpec` ctypes tanımlarını, `InstallEventHandler` + `RegisterEventHotKey(0x07, 1<<8, ...)` çağrılarını ve `close()` temizliğini uygula. Geri çağrı yalnız dışarıdan verilen fonksiyonu çağırır; UI'ye doğrudan dokunmaz.
- [ ] Testleri yeşile getir; gerçek makinede kayıt/kaldırma durum kodlarının sıfır olduğunu ayrıca doğrula.

## Görev 2: UI gizle/göster

- [ ] Tk testinde görünürlük isteğinin `_tick` ile işlendiğini, çalışan `Future` ve transkriptin korunduğunu, ikinci isteğin geri getirdiğini sınayan testi ekle.
- [ ] UI başlangıcında global hotkey kaydet; callback `Queue`'ya token koysun. `_tick` erken dönüşten önce kuyruğu boşaltıp `_toggle_visibility` çağırmalı. macOS'ta `NSApplication.hide_`/`unhide_` ve etkinleştirme; test ortamında `withdraw`/`deiconify` kullanılabilir. `_sync_menu_status` gizli durumda görev sürse bile menü öğesini kaldırıp çıkmalı. Hotkey kaydı başarısızsa açık uyarı göster ve tek yönlü gizleme kısayolu açma. Kapanışta kaydı kaldır.
- [ ] `OMNI_UI_TEST=1 pytest tests/test_ui_conversation.py tests/test_ui_visibility.py -q` çalıştır.

## Görev 3: OmniAgent yakalamalarından UI çıkarma

- [ ] `screen.py` için UI PID'si bulunduğunda tüm o süreç pencerelerinin listeden çıktığını, ana pencere küçültülüp Ayarlar veya girdi penceresi açıkken de PID'nin bulunduğunu, UI yokken eski hızlı yolun çalıştığını, filtreli görüntü oluşturulamazsa açık hata verdiğini test et.
- [ ] `_display_image` içinde pencere listesini al; adı `OmniAgent` veya `OmniAgent —` ile başlayan pencere PID'lerini çıkar; `CGWindowListCreateImageFromArray` ile kalan on-screen pencereleri aynı ekran sınırlarında birleştir. Yalnız tam ekran yolu değişsin; Chrome pencere kapsamı kalsın.
- [ ] Hedefli testleri ve canlı Quartz penceresi listesini doğrula; filtreli yakalamanın mevcut UI sürecini seçmediğini ölç.

## Görev 4: Doğrulama ve teslim

- [ ] Belgelerde global `⌘X` Kes çakışmasını, kendi yakalama filtresini ve üçüncü taraf kayıt sınırını açık yaz.
- [ ] `compileall`, `git diff --check`, normal ve `OMNI_UI_TEST=1` tam test paketlerini çalıştır. Varsa somut performans farkını ölç; ham pencere yokken eski yolun korunduğunu doğrula.
- [ ] PR aç ve bağla, macOS/Linux CI sonrası birleştir; canlı checkout'u ileri sar. Görev kilidi boşken UI ve gerekiyorsa Telegram hizmetlerini yeniden başlat; PID, pencere, bridge kilidi ve sürümü kontrol et.
