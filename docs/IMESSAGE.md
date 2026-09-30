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
- `/durum`: çalışan iş, bugünkü işler ve token, cevap gecikmesi medyanı.

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
