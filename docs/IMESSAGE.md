# iMessage yol arkadaşı — kurulum ve canlı kontrol

OmniAgent'ın iMessage kanalı: ajanın kendi Apple ID'si bu Mac'teki Messages'ta oturum açar, sen iPhone'dan ona
yazarsın. Tasarım: `docs/superpowers/specs/2026-09-29-imessage-companion-design.md`.

## Kurulum

1. **Ajana Apple ID aç** (appleid.apple.com). Ajanın iMessage adresi bu e-posta olur.
2. **Bu Mac'te Messages:** Messages > Ayarlar > iMessage → kendi hesabından çık, ajanın Apple ID'siyle gir.
   Kendi iMessage'ın iPhone'da aynen sürer.
3. **imsg:** `brew install steipete/tap/imsg`
4. **Kurulum komutu** (proje kökünde): `uv run omniagent-imessage setup`
   - karakter adını sorar ve `persona.md`'yi yazar (sonradan düzenleyebilirsin),
   - model profillerinin ilk token süresini ölçer; sohbet ve hafıza profilini seçtirir,
   - servisi (`com.omniagent.imessage`) kurar.
5. **Tam Disk Erişimi:** kurulum, servisin python ikilisinin yolunu yazar ve ayar sayfasını açar. Sistem Ayarları >
   Gizlilik ve Güvenlik > Tam Disk Erişimi > "+" → Cmd+Shift+G ile o yolu yapıştır → aç. Servis kendiliğinden
   yeniden dener.
6. **Eşleştirme:** kurulumun gösterdiği 6 haneli kodu iPhone'dan ajanın adresine gönder (3 dk). "python …
   Messages'ı denetlemek istiyor" istemini onayla (Otomasyon). Ajan "eşleştik 👋" yazar.
7. **Rehber:** iPhone'da ajanı isim ve fotoğrafla kişilere kaydet (iOS bilinmeyen gönderenleri süzer).

iPhone'daki "Yeni konuşmaları şuradan başlat" adresini (telefon ↔ e-posta) değiştirirsen yeniden eşleştir:
`imessage.json`'u sil ve kurulumu tekrar çalıştır.

## Komutlar

- `dur` ya da `/dur`: çalışan işi durdurur.
- `/durum`: çalışan iş, bugünkü işler ve token (tüm kanallar), cevap gecikmesi medyanı, kanıtlı hafıza ve son
  öğrenme hatası.
- `/hafıza`: kanıtlı hafızadaki etkin bilgiler, numaralarıyla.
- `unut <numara>` ya da `/unut <numara>`: o bilgiyi unutur (istemden ve aramadan çıkar).
- `/proaktif aç` ya da `/proaktif kapat`: kalp atışının kendiliğinden mesaj ve iş başlatmasını açar/kapatır.
- `/sessiz 2`: proaktifliği iki saat susturur. Bu istekler doğal konuşmadan `mute` ve `set_proactive` araçlarıyla da
  uygulanır.

## Fotoğraf ve ses

Fotoğraf ekleri (HEIC dahil) geçici JPEG'e çevrilir: en çok dört fotoğraf, dosya başına 5 MB, uzun kenar en çok
1600 piksel. Görsel içerik sohbet turuna gider; kişisel hafızaya kanıt olarak girmez. Desteklemeyen sohbet profili
yalnız mevcut sağlayıcı yedek izni ve `allow_images` açıkken başka profile geçebilir; geçiş denetim günlüğüne yazılır.

Sesli mesaj için eşleşmeyi değiştirmeden proje kökünde çalıştır:

```sh
.venv/bin/python -m omniagent.integrations.imessage transcription
```

Komut, anahtarı kayıtlı profillerden sağlayıcıyı ve o sağlayıcının ses dökümü modelini seçtirir; ayarları kaydedip
servisi yeniden başlatır. Sohbet modelinin adı ses modeli yerine kullanılmaz. `transcribe_backend` ve
`transcribe_model` alanları eski kurulumda yoksa ses dökümü kapalıdır; `/durum` bunu gösterir. CAF/AMR gerektiğinde
yerel `afconvert` ile M4A'ya çevrilir. Başarılı döküm `🎤 …` olarak kullanıcının kendi sözüne, özgün mesaj zamanıyla
eklenir; sır süzgecinden geçmeyen söz arşivlenmez. Hata halinde Deniz "sesini açamadım, yazar mısın?" der.

## Kalp atışı ve otonom işler

Kalp atışı yerel saat, son konuşma, kanıtlı bilgiler, son işler/dersler, Mac'in boşta ve kilitli oluşu ve isteğe
bağlı `kalp_atisi.md` kontrol listesini okur. Pencere başlıkları toplanmaz. Son 15 dakika içinde sohbet olmuşsa,
proaktiflik kapalıysa veya susturma sürüyorsa yeni karar üretmez.

- Sessiz saatlerde (kurulumda 23:30–09:00) kendiliğinden mesaj gönderilmez. İki cevapsız proaktif mesajdan sonra
  kullanıcı yazana kadar yeni proaktif mesaj durur.
- Her mesajın aktif bilgi dayanakları ve doğrulayıcı model denetimi vardır. Karar beklenirken yeni kullanıcı
  mesajı gelirse eski karar uygulanmaz.
- Otonom iş gerekçe taşır, aynı anda tek iş çalışır. Kullanıcı işi önceliklidir; otonom iş durdurma isteğini alır
  ve kilidi bıraktığında kullanıcı işi başlar.
- GUI araçları kilitli veya kullanıcının yeni dokunduğu Mac'te bekler. Silme, kamera ve kaynak kod değişikliği
  otonom koşuda onay gerektirir. Sessiz saatte onay sorusu gönderilmez, adım yapılmaz ve rapora bırakılır.
- Sessiz saatte biten işin raporu sabah veya kullanıcı yazınca gönderilir. Model raporu anlatamasa da gerçek
  sonuç gönderilir. Rapor içindeki metin sohbet araçlarını çalıştıramaz.

Öncelik işbirlikli durdurmaya bağlıdır. Kabuk koşuları, model beklemeleri ve otonom curl/Chrome alt süreçleri
durdurma isteğini yoklar ve alt süreç bitmeden host kilidini bırakmaz. Başka bir bloklayan üçüncü taraf aracın
durdurmaya uymaması halinde 15 saniyelik kullanıcı kilit beklemesi açık meşgul hatasıyla biter; bu süre canlıda
ayrıca ölçülmelidir. GUI/silme/kaynak korumaları işletim sistemi sandbox'ı değildir; mevcut araç onay katmanını
tamamlar.

## Faz A canlı kontrol listesi

- [ ] `uv run omniagent-permissions` → "Tam Disk Erişimi: izinli" (servisin python ikilisi için).
- [ ] 30 kısa mesajdan sonra `/durum` gecikme medyanı ≤ 3 sn (spec ölçüt 1); p95 (30 ölçümde 29. değer) ≤ 6 sn.
      Servis `imessage-stderr.log`'a INFO seviyesinde yapılandırılmış log yazar; gecikme "iMessage ilk balon
      gecikmesi" satırının sonundaki `{"latency_ms": ...}` alanındadır:
      `rg -o '"latency_ms": (\d+)' -r '$1' ~/Library/Application\ Support/OmniAgent/imessage-stderr.log | tail -30 | sort -n | sed -n 29p`
- [ ] Sen Mac'te başka bir uygulamada yazarken ajan cevap verdiğinde odak kaymıyor, Messages öne gelmiyor
      (ölçüt 6). Tutmazsa bu bir engeldir: uygulama durur ve kullanıcıya danışılır.
- [ ] "masaüstümdeki dosyaları listele" → "tamam bakıyorum" ve sonuç balonları; onay isteyen bir işte evet/hayır.
- [ ] Uzun bir işte `dur` işi durduruyor.
- [ ] Mesaj yazarken `launchctl kickstart -k gui/$(id -u)/com.omniagent.imessage` → mesaj kaybolmuyor, iki kez
      cevaplanmıyor (ölçüt 4).
- [ ] Başka bir numaradan ajana yazınca cevap yok ve `companion.db`'de iz yok (ölçüt 2).

## Faz B+ canlı kontrol listesi

- [ ] Telegram'a "kızımın adı Ela" yaz; 3–4 dk sonra iMessage'da Deniz'e "kızımın adı neydi?" diye sor. Deniz Ela'yı
      bilir ve `/hafıza`'da `[#N] … — "kızımın adı Ela"` satırı görünür (kabul a).
- [ ] Deniz'e "cuma İzmir'e gidiyorum" yaz; Telegram'a "İzmir'e ne zaman gidiyordum?" yaz. Ajan `personal_memory`
      ile birebir alıntıyı bulur (kabul b).
- [ ] Telegram'a uydurma bir token içeren hedef yaz (`ghp_` + 20 karakter). Ardından
      `sqlite3 ~/Library/Application\ Support/OmniAgent/companion.db "select count(*) from messages where text like '%ghp_%'"`
      çıktısı `0` olmalı (kabul c).
- [ ] İki köprü açıkken 20'den fazla mesaj yaz. `/hafıza`'da aynı bilgi bir kez görünür; köprü günlüklerinde tek
      "Hafıza öğrenme turu tamamlandı", diğer köprüde "atlandı" satırı vardır (kabul d).
- [ ] `unut <numara>` bilgiyi `/hafıza`'dan çıkarır. Deniz'e "şunu unut" deyince bilgi gerçekten silinir; silinmezse
      Deniz bunu açıkça söyler.
- [ ] Masaüstünde bir görev bitince Deniz'e "az önce masaüstünde ne yaptım?" diye sor. Deniz [DURUM]'daki
      "masaüstünden" satırıyla cevaplar.
- [ ] `/durum` "hafıza: N bilgi" satırını gösterir. Öğrenme hatası varsa zamanı ve türü de yazar.
- [ ] `OMNI_LIVE_COMPANION=1 OMNI_LIVE_MEMORY_BACKEND=<memory_backend> uv run python -m pytest tests/test_memory_live.py -v`
      → PASS.

## Faz D canlı kontrol listesi

- [ ] JPEG ve HEIC fotoğraf gönder; yanıt görüntünün gerçek içeriğine uysun. Görülmeyen görüntü için tahmin yapılmasın.
- [ ] Ses dökümü profilini seç, 10 saniyelik ses gönder; döküm cevaplanıp `/hafıza` öğrenme hattına girebilsin.
- [ ] Gece 02:00'de yanıt sakin gece üslubunda; ardışık on yanıtta aynı açılış tekrar etmesin.
- [ ] "ekranımı at" veya "o dosyayı gönder" isteği gerçek iş başlatıp iMessage teslim kanalını kullansın.

## Faz C canlı kontrol listesi

- [ ] `/proaktif kapat` ve `/sessiz 2` yeni proaktif kararı durdursun; `/durum` durumları göstersin.
- [ ] İki cevapsız proaktif mesajdan sonra susulsun; yalnız eşleşmiş kullanıcının cevabı sayacı sıfırlasın.
- [ ] Otonom iş sürerken iMessage, Telegram ve masaüstünden kullanıcı işi başlat; öncelik geçişini 15 saniye hedefiyle ölç.
- [ ] Mac'e dokununca GUI işi beklesin; ekran kilitliyken girdi üretilmesin.
- [ ] Otonom silme/kamera/kaynak değişikliği reddedilen onayla yürütülmesin; sessiz saatte onay istenmesin.
- [ ] Gece biten işin sabah raporu ve rapora dayalı dersi kaydedilsin; kesin gönderim hatası yeniden denenebilsin,
      belirsiz teslim otomatik yinelenmesin.

Otomatik testler gerçek SQLite, macOS medya dönüşümü ve sahte model/taşıma sınırlarıyla doğrulanır. Canlı iPhone
teslimi, doğal üslup, p50/p95 gecikme ve kullanıcı odağı bu kontrol listesindeki ayrı kabul ölçümleridir.
