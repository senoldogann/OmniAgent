# OmniAgent masaüstü arayüzü: macOS'a yakışan tasarım

**Tarih:** 28 Eylül 2026
**Durum:** Taslak — kullanıcı incelemesi bekliyor.
**Kapsam:** `src/omniagent/ui/app.py` ve `src/omniagent/ui/chats.py` (masaüstü Tkinter/CustomTkinter arayüzü). Ajan çekirdeği (`app/agent.py`, `core/*`, `events.py` olay sözleşmesi) davranış olarak **değişmez**; bu iş yalnızca sunum katmanıdır.

## Amaç

Mevcut arayüz işlevsel olarak sağlam (bkz. `AGENTS.md` "🖥️ Arayüz" bölümü: olay güdümlü çizim, zengin Markdown, artifact kartları, akış animasyonları) ama CustomTkinter'ın verdiği "genel amaçlı koyu tema" hissinin ötesine geçmiyor: gerçek native macOS bileşenleri (vibrancy, sistem fontu, native menü çubuğu) kullanılmıyor. Hedef, mevcut davranışı ve marka kimliğini (`#D97757` vurgu, Menlo kod bloğu) koruyarak uygulamayı bir macOS vatandaşı gibi hissettirmek.

## Kapsam dışı

- Ajan döngüsü, araç şeması, olay sözleşmesi (`events.py`), Telegram köprüsü — hiçbiri değişmiyor.
- Framework değişikliği yok (Toga/SwiftUI vb. — kullanıcıyla birlikte değerlendirilip elendi; bkz. "Alternatifler").
- Gerçek zamanlı açık/koyu tema takibi (uygulama çalışırken sistem teması değişirse anında yansıması) bu spec'in kapsamında değil; yalnızca **açılışta** sistem temasının okunması kapsamda.

## Alternatifler ve neden bu yaklaşım

1. **CustomTkinter üzerinde derin görsel yenileme (seçildi).** Mevcut ajan-arayüz kablolamasına dokunmaz, düşük risk, kademeli teslim edilebilir.
2. Native pencere veren Python araç seti (Toga/BeeWare): orta risk, özel Markdown/artifact kart render'ının büyük kısmı yeniden yazılmalı — bu iş için gerekli değil.
3. Ayrı native Swift/SwiftUI kabuk + Python arka uç: en native sonuç ama yeni bir süreç sınırı, yeni bir dil, `events.py`'nin yeniden tasarlanması gerekir — kapsam dışı bırakıldı.

Kullanıcıyla görüşülüp **1** seçildi.

## Gerçekçi kısıtlar (dürüstlük bölümü)

- Tk üst pencereleri macOS'ta zaten native `NSWindow`'dur — traffic-light düğmeleri zaten native; bu bir kazanım değil, mevcut bir gerçek.
- **Kod incelemesiyle düzeltme (ilk taslaktaki varsayımlar yanlıştı):** `ui/app.py` zaten `import AppKit` (düz PyObjC bağlaması, `rubicon-objc` değil) kullanarak pencere başlık çubuğunu içerikle aynı renge boyuyor (`_style_native_titlebar`, satır ~402) ve SVG ikonları native `NSImage`'a çeviriyor (satır ~136). Yeni native köprü kodu **aynı `import AppKit` deseniyle** yazılacak; `rubicon-objc` bu katmana dahil edilmeyecek (o bağımlılık GUI otomasyon araçlarına ait, ayrı bir kaygı).
- **Sistem fontu zaten çözülüyor, ek iş gerekmiyor:** `self._ui_family = tkfont.nametofont("TkDefaultFont").actual("family")` (satır 287) bu makinede doğrulandı — Tk'nin `TkDefaultFont`'u macOS'ta zaten `.AppleSystemUIFont`'a (gerçek SF Pro) çözülüyor. İlk taslaktaki `_resolve_system_font()`/`UI_FAMILY` fallback zinciri **kapsam dışı bırakıldı**: zaten var olan bir şeyi yeniden icat ederdi.
- **Açılışta tema tespiti kapsam dışı bırakıldı:** uygulama `ctk.set_appearance_mode("dark")` ile kasıtlı olarak sabit koyu temalı (satır 280); `BG`/`SURFACE`/`TEXT` paleti yalnızca koyu yüzeyler için tasarlı. Sistem açık modunu "tespit edip" yine de koyu paletle çizmek anlamsız ve yanıltıcı olurdu; gerçek açık tema desteği ayrı bir renk paleti gerektirir ve bu spec'in (ve kullanıcı isteğinin) kapsamında değildir. Uygulama koyu temalı kalır — bu bir eksiklik değil, projenin zaten benimsediği tasarım dilidir (bkz. `AGENTS.md`: "nötr koyu yüzeyler").
- Gerçek vibrancy (`NSVisualEffectView`) stok Tk'de yok ve şu an hiçbir yerde uygulanmıyor (koda bakıldı, doğrulandı) — bu, spec'teki tek gerçek yeni "native köprü" parçasıdır; `ui/native_macos.py`'de, mevcut `_style_native_titlebar` ile aynı korumalı desenle (`if sys.platform != "darwin": return`) yazılacak.
- Tk'nin animasyon yeteneği ilkeldir (`after()` ile kare kare); yay fiziği/gerçek SwiftUI akıcılığı hedeflenmiyor, yalnızca ease-out eğrili sade geçişler.

## Tasarım jetonları (başlangıç değerleri — görsel QA'da ayarlanabilir)

Mevcut palet (`ui/app.py:78-95`) korunur ve genişletilir, yeniden icat edilmez:

| Jeton | Mevcut | Değişiklik |
| --- | --- | --- |
| `BG` `#141413`, `SURFACE` `#1C1C1A`, `SURFACE_RAISED` `#262624`, `BORDER` `#34332F` | var | Vibrancy açıkken bu yüzeyler yarı saydam varyantlarıyla (`SURFACE_TRANSLUCENT` gibi %70-85 alfa) değiştirilebilir; vibrancy kapalıyken (Linux/test) aynen kalır. |
| `TEXT` `#ECEAE3`, `TEXT_DIM` `#A3A199`, `TEXT_FAINT` `#6E6C66` | var | değişmiyor |
| `ACCENT` `#D97757` ailesi | var | değişmiyor — marka kimliği |
| `MONO_FAMILY` `"Menlo"` | var | değişmiyor — kod/komut bloklarında kalır |
| Gövde fontu (`self._ui_family`) | `tkfont.nametofont("TkDefaultFont").actual("family")` → bu makinede doğrulandı: zaten `.AppleSystemUIFont` | **İş yok** — zaten native. |
| `RADIUS_SM/MD/LG`, `SPACE_UNIT` (yeni) | yok (widget başına dağınık değerler) | Bu sub-project'te **tanımlanmaz** — YAGNI: henüz tüketicisi yok. İlk gerçek tüketicisi olan Alt proje 2 (kenar çubuğu) tarafından, o iş başladığında tanıtılacak. |

## Native köprü modülü: `ui/native_macos.py`

Yeni dosya, mevcut `_style_native_titlebar` ile **aynı düz-fonksiyon + `import AppKit` deseni** (sınıf değil — mevcut kod tabanı bu iş için zaten sınıfsız düz fonksiyon/metot kullanıyor, o desen korunur):

- `apply_vibrancy(window_title: str, material: int) -> bool`: `AppKit.NSApplication.sharedApplication().windows()` içinde başlığı eşleşen pencereyi bulur (mevcut `_style_native_titlebar`'daki arama deseniyle aynı), `contentView`'ının arkasına bir `NSVisualEffectView` yerleştirir. `sys.platform != "darwin"` ise veya herhangi bir `AppKit`/Cocoa çağrısı istisna verirse `False` döner ve **çağıran taraf sessizce eski opak yüzeye devam eder** — vibrancy dekoratif bir katmandır, arayüz onsuz da tam işlevseldir.
- `install_native_menu_bar(app_name: str) -> bool`: standart uygulama menüsü (Hakkında/Tercihler ⌘,/Çıkış) kurar; aynı hata toleransıyla.

(Açılışta tema tespiti — `system_appearance()` — yukarıdaki "Gerçekçi kısıtlar" bölümünde açıklandığı üzere kapsam dışı bırakıldı: uygulama kasıtlı olarak koyu temalı kalıyor.)

Bu iki fonksiyon da **çağrıldıkları an başarısız olabileceklerini varsayarak** yazılır (dönüş değeriyle bildirir, istisna yutmaz); `ui/app.py` bunları `_style_native_titlebar` çağrıldığı yerin hemen yanında, en-iyi-çaba (best-effort) olarak çağırır, sonucunu loglar, akışı bloklamaz.

## Alt projeler / yol haritası

Her biri kendi worktree/dalında, kendi PR'ında, bir öncekini bozmadan teslim edilir:

1. **Temel (bu spec'in ilk uygulama dilimi):** yalnızca `native_macos.py` (vibrancy + native menü çubuğu). Görünür etki: pencere arka planı bulanık/vibrant olur, standart bir uygulama menüsü belirir — kenar çubuğu/transkript içeriği henüz yeniden tasarlanmaz, jetonlar henüz tanımlanmaz.
2. Kenar çubuğu (`chats.py` mantığı korunur) — native liste satırı/seçim/hover.
3. Transkript + composer — asıl sohbet deneyimi.
4. Ayarlar sayfası — gruplu liste stiline geçiş.
5. Artifact kartları + header/toolbar cilası.

## Test ve doğrulama stratejisi

- `native_macos.py` içindeki saf mantık (`_resolve_system_font()`'un aday listesi seçimi gibi) birim testle kapsanır; gerçek `rubicon-objc`/AppKit çağrıları test ortamında mock'lanmaz, yalnızca "mevcut değilse sessizce False" yolu test edilir (gerçek pencereye vibrancy uygulanması otomatik testle doğrulanamaz — bu proje için makul, manuel/görsel doğrulama gerekir).
- Her alt proje adımında: (a) ilgili mevcut testler (`test_ui_settings.py`, `test_ui_conversation.py`, `test_ui_voice_copy.py`, `test_ui_chats.py`) güncellenir — davranışsal assert'ler aynen kalır, görsel sabitlere (renk/font/metin) bağlı assert'ler yeni jetonlara göre güncellenir; (b) uygulama gerçekten başlatılıp gözle doğrulanır (arayüz değişikliği için zorunlu adım).
- `OMNI_UI_TEST=1 .venv/bin/python -m pytest -q` her adımın sonunda tam yeşil olmalı.

## Geri alma stratejisi

Her alt proje bağımsız bir PR; biri sorun çıkarırsa yalnızca o commit geri alınır, önceki adımlar etkilenmez. Vibrancy/menü çubuğu native çağrıları best-effort olduğundan, macOS dışı veya `rubicon-objc`'siz bir ortamda uygulama asla çökmez, yalnızca dekoratif katmanı eksik kalır.

## Açık riskler

- `NSVisualEffectView`'i mevcut `contentView`'ın arkasına yerleştirmenin CustomTkinter'ın kendi Tk çizim döngüsüyle (frame'in her `_tick`'te yeniden boyanması) çakışma ihtimali; ilk uygulama diliminde tek bir izole pencere üzerinde doğrulanacak. Çakışırsa `apply_vibrancy` `False` döner, uygulama mevcut opak yüzeyle çalışmaya devam eder.
