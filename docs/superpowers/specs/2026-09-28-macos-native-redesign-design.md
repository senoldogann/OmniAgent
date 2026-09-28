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
- Gerçek vibrancy (`NSVisualEffectView`) stok Tk'de yok; projede zaten `rubicon-objc` bağımlılığı olduğundan (AX/Quartz araçları için kullanılıyor) bunun üzerinden native bir katman eklenecek — bu, spec'teki tek gerçek "native köprü" parçasıdır ve ayrı, izole bir modülde (`ui/native_macos.py`) yaşayacak, import edilemezse (Linux/CI, test ortamı) sessizce devre dışı kalacak şekilde yazılacak.
- Tk'nin animasyon yeteneği ilkeldir (`after()` ile kare kare); yay fiziği/gerçek SwiftUI akıcılığı hedeflenmiyor, yalnızca ease-out eğrili sade geçişler.
- Native metin render'ı (kerning/ligature) AppKit'inkiyle piksel piksel aynı olmayacak; sistem fontu + doğru boyut/ağırlık ile görsel olarak yakınsanacak.

## Tasarım jetonları (başlangıç değerleri — görsel QA'da ayarlanabilir)

Mevcut palet (`ui/app.py:78-95`) korunur ve genişletilir, yeniden icat edilmez:

| Jeton | Mevcut | Değişiklik |
| --- | --- | --- |
| `BG` `#141413`, `SURFACE` `#1C1C1A`, `SURFACE_RAISED` `#262624`, `BORDER` `#34332F` | var | Vibrancy açıkken bu yüzeyler yarı saydam varyantlarıyla (`SURFACE_TRANSLUCENT` gibi %70-85 alfa) değiştirilebilir; vibrancy kapalıyken (Linux/test) aynen kalır. |
| `TEXT` `#ECEAE3`, `TEXT_DIM` `#A3A199`, `TEXT_FAINT` `#6E6C66` | var | değişmiyor |
| `ACCENT` `#D97757` ailesi | var | değişmiyor — marka kimliği |
| `MONO_FAMILY` `"Menlo"` | var | değişmiyor — kod/komut bloklarında kalır |
| `UI_FAMILY` (yeni) | yok (CustomTkinter varsayılanı) | `.AppleSystemUIFont` denenir (macOS'ta gerçek SF Pro'ya karşılık gelir); `tkfont.families()` içinde yoksa `"Helvetica Neue"`'ye, o da yoksa CustomTkinter varsayılanına düşer — sessiz istisna yutmadan, tek bir `_resolve_system_font()` saf fonksiyonuyla. |
| `RADIUS_SM/MD/LG` (yeni) | CustomTkinter varsayılanları (widget başına dağınık) | 6 / 10 / 14 piksel olarak tek yerden standardize edilir |
| `SPACE_UNIT` (yeni) | yok (elle seçilmiş boşluklar) | 8px taban birim; bileşenler `SPACE_UNIT * n` kullanır |

## Native köprü modülü: `ui/native_macos.py`

Yeni, izole, saf-olmayan tek dosya (dış sisteme bağlandığı için OOP/sınıf kullanımı burada meşru — global kurallardaki "OOP yalnız dış sistem konnektörleri için" istisnası):

- `apply_vibrancy(tk_window, material="sidebar") -> bool`: `rubicon-objc` ile pencerenin `contentView`'ının arkasına `NSVisualEffectView` yerleştirir; başarısız olursa (rubicon yok, pencere tanıtıcısı alınamadı, İmport hatası) `False` döner ve **çağıran taraf sessizce eski opak yüzeye devam eder** — vibrancy dekoratif bir katmandır, arayüz onsuz da tam işlevseldir.
- `system_appearance() -> Literal["light", "dark"]`: açılışta `NSApplication`'ın etkin görünümünü okur; okunamazsa `"dark"` varsayılanına düşer (mevcut davranış zaten koyu tema).
- `install_native_menu_bar(app_name)`: standart uygulama menüsü (Hakkında/Tercihler ⌘,/Çıkış) kurar.

Bu üç fonksiyon da **çağrıldıkları an başarısız olabileceklerini varsayarak** yazılır (dönüş değeriyle bildirir, istisna yutmaz); `ui/app.py` bunları en-iyi-çaba (best-effort) olarak çağırır, sonucunu loglar, akışı bloklamaz.

## Alt projeler / yol haritası

Her biri kendi worktree/dalında, kendi PR'ında, bir öncekini bozmadan teslim edilir:

1. **Temel (bu spec'in ilk uygulama dilimi):** `native_macos.py`, jeton modülü, `_resolve_system_font()`, açılışta vibrancy + menü çubuğu kurulumu. Görünür etki: pencere arka planı bulanık/vibrant olur, gövde metni sistem fontuna geçer — kenar çubuğu/transkript içeriği henüz yeniden tasarlanmaz.
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

- `.AppleSystemUIFont`'un bu Tk/Tcl sürümünde gerçekten SF Pro'ya çözüldüğü varsayımı; ilk uygulama diliminde doğrulanacak, çözülmezse `Helvetica Neue`'ye sessizce düşülecek (kullanıcıya görünür bir hata değil).
- `rubicon-objc` ile `NSVisualEffectView` yerleştirmenin CustomTkinter'ın kendi çizim döngüsüyle çakışma ihtimali; ilk uygulama diliminde tek bir izole pencere üzerinde doğrulanacak.
