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
