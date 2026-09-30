# Deniz yol haritası eki — kanallar arası kanıtlı hafıza (Faz B+) ve Deniz'in duyuları (Faz D)

Tarih: 2026-09-30 · Temel: `2026-09-29-imessage-companion-design.md` (bu ek onu genişletir; çelişkide bu ek geçerlidir).
Faz A canlıda çalışıyor (gerçek iPhone ile eşleşti). Sıra: **Faz A son inceleme + commit → Faz B+ → Faz D → Faz C.**
Faz C (kalp atışı, otonomi) temel spec'teki gibidir; B+ hafızası ve D kişiliği üzerine kurulduğu için sona alındı.

## Kararlar

| Konu | Karar |
| --- | --- |
| Mimari | **Tek beyin, iki yüz.** Deniz (iMessage) arkadaş yüzü; Telegram ve masaüstü çalışma yüzü. Ortak: kanıtlı hafıza (`facts`) ve iş günlüğü (`activity`). Ayrı: konuşma geçmişleri ve üslup. |
| Hafıza deposu | `companion.db` tüm kanalların kişisel hafızası olur (0600, WAL, `busy_timeout` 5 sn; üç süreç — iMessage köprüsü, Telegram köprüsü, masaüstü — aynı dosyayı kullanır). |
| Kanıt | Yalnız kullanıcının kendi sözleri (`direction=in`): iMessage mesajları, Telegram/masaüstü görev metinleri, yazılı yanıtlar, `/btw` ve sesli mesaj dökümleri. Ajanın, aracın ve görsellerin içeriği kanıt değildir. |
| Kapsam dışı | Yazıyor göstergesi, okundu bilgisi ve IMCore tapback (SIP kapatmayı gerektirir; güvenliği düşürür). Arayüz otomasyonlu tepki (`imsg react`: Messages'ı öne getirir, kullanıcının ekranını böler). |

## Faz B+ — kanallar arası kanıtlı hafıza

Temel spec'teki Faz B'nin (`facts`, öğrenme hattı, Kapı 1/Kapı 2, çekirdek profil, `recall`/`forget`, `/hafıza`) tamamı geçerlidir; aşağıdakiler eklenir.

1. **Şema göçü (sürüm 2).** `messages.channel TEXT NOT NULL DEFAULT 'imessage'` (`imessage`/`telegram`/`desktop`) ve `activity.channel` aynı varsayılanla eklenir; `messages.imsg_rowid` Telegram/masaüstü kayıtlarında boştur. Göç `state` tablosundaki `schema_version` ile bir kez yapılır; bozuk ya da ileri sürüm açıkça hata verir.
2. **Kayıt — `memory/channels.py` (yeni, ince katman).**
   - `record_user_message(channel, text, created_at) -> None`: kısa ömürlü `PersonalStore` açar, `in` mesajı yazar. Gizli bilgi süzgecinden geçmeyen metin (anahtar, parola, token kalıbı) **hiç yazılmaz**, yalnız sayısı loglanır.
   - `record_task(channel, goal, outcome, success, started_at, finished_at, tokens) -> None`: `activity` satırı (`kind=task`, `origin=user`).
   - Çağıranlar: Telegram köprüsü (`_execute` başında hedef, yazılı soru yanıtları ve `/btw`; sonunda görev raporu), masaüstü arayüzü (görev başlatma ve yazılı yanıtlar; bitişte rapor), iMessage köprüsü (Faz A'daki kayıtlar `channel=imessage` ile).
   - Kayıt hatası görevi durdurmaz: yapılandırılmış hata loglanır ve `/status`'ta "hafıza kaydı başarısız" görünür (görev kullanıcının asıl işidir; hafıza yan kayıttır).
3. **Tek öğrenme hattı.** Tetik (temel spec): son kullanıcı mesajından 180 sn sessizlik ya da 20 işlenmemiş `in` mesajı — artık **tüm kanallarda**. Aynı anda tek hat: `data_root()/memory-learning.lock` bloklamadan alınır; iMessage ve Telegram köprüleri tetikte dener, kilidi alan bir tur çalıştırır. Masaüstü öğrenme çalıştırmaz (yalnız kaydeder).
4. **Tüm kanallarda kullanım.**
   - Deniz: çekirdek profil (6000 karakter) + `recall`/`forget` sohbet araçları (temel spec).
   - Ana ajan (Telegram/masaüstü/devredilen işler): sistem istemine `USER MEMORY` bloğunun ardından **"KANITLI PROFİL"** bloğu (3000 karakter; en son güncellenenler kalır; satır biçimi temel spec'teki gibi `[#id] ifade — "alıntı" (tarih)`). Yeni araç `personal_memory` (`recall` sorgu → en çok 8 birebir parça; `forget` id). Profil bir talimat değil kanıttır: istem bunu açıkça söyler.
   - `/hafıza` iMessage'da ve Telegram'da: etkin bilgiler kimlikleriyle; `unut 12` / `/unut 12`.
   - Deniz'in [DURUM] bloğu son 5 işi kanal etiketiyle gösterir ("Telegram'dan: rapor — bitti ✓"); Deniz, Telegram'dan yaptırılan işi sorulunca bilir.
5. **Gizli bilgi süzgeci tek yerde:** `memory/user.py` içindeki `_SENSITIVE_TERMS` / `_SECRET_VALUE_PATTERN` kuralları genel `sensitive_text(value) -> bool` fonksiyonuna çıkarılır; `user.py`, `channels.py` ve öğrenme hattı onu kullanır (kopya kural yok).
6. **Kabul:** temel spec ölçüt 3'e ek olarak: (a) Telegram'a "kızımın adı Ela" yazılınca Deniz iMessage'da Ela'yı alıntısıyla bilir; (b) Deniz'e "cuma İzmir'e gidiyorum" denince Telegram ajanı `personal_memory recall İzmir` ile birebir alıntıyı bulur; (c) gizli bilgi içeren Telegram hedefi `messages` tablosuna hiç girmez; (d) iki köprü aynı anda tetiklense de aynı mesaj iki kez işlenmez.

## Faz D — Deniz'in duyuları ve kişiliği

1. **Fotoğraf görür.** Gelen görsel ekler (`image/*`; HEIC dahil) macOS yerleşik `sips` ile JPEG'e çevrilip uzun kenar 1600 px'e küçültülür ve sohbet turuna görsel parçası olarak eklenir (burst başına en çok 4, dosya başına en çok 5 MB). Sohbet profili görsel desteklemiyorsa görsel destekli profile yalnız mevcut yedek izni kuralıyla (`fallback_policy`, `allow_images`) geçilir; izin yoksa Deniz görseli göremediğini söyler. Arşive `[fotoğraf: ad]` yazılır; görsel içeriği kanıt sayılmaz. Çevrilen dosyalar geçici dizinde tutulur ve tur sonunda silinir.
2. **Sesli mesajı dinler.** Ses ekleri (`.caf`, `.m4a`, `.amr`) gerekiyorsa macOS yerleşik `afconvert` ile `m4a`'ya çevrilir ve OpenAI uyumlu `audio/transcriptions` ile yazıya dökülür (mevcut `openai` istemcisi; `imessage.json` yeni alan `transcribe_backend: Optional[str]`, kurulumda anahtarı olan profillerden seçilir). Canlıdaki eşleşmiş kurulumu bozmamak için alan yoksa `None` okunur ve bu açık bir durumdur: döküm kapalıdır, `/durum` bunu gösterir; yeni CLI eylemi `omniagent-imessage transcription` profili sorup alanı yazar (servis yeniden başlatılır). Döküm burst'e "🎤 …" olarak girer ve kullanıcının kendi sözü olarak arşivlenir (hafızaya kanıt olabilir). Hata olursa sabit metin: "sesini açamadım, yazar mısın?"; hata yapılandırılmış loglanır (içerik yok).
3. **Kişilik derinliği** (`persona.py` RULES ve [DURUM]):
   - [DURUM] yerel saat dilimini ve günün bölümünü (sabah/öğle/akşam/gece) taşır; üslup buna uyar (gece sakin, sabah kısa).
   - Bir yanıtta en çok bir soru; aynı açılışı üst üste kullanmama (son 10 ajan mesajının ilk kelimeleri bağlamda "tekrar etme" listesi olarak verilir — saf fonksiyon).
   - Kanıtlı profildeki bilgiye yeri gelince doğal gönderme ("İzmir nasıldı?"), uydurma yok: gönderme yalnız profilde ya da konuşmada olan bilgiye yapılır.
4. **Gösterir.** Deniz'e "ekranımı at" / "o dosyayı gönder" denince iş devredilir; ana ajan `send_file` ile iMessage'a dosya/görsel gönderir (Faz A'daki teslim kanalı; yeni kod gerekmez, canlı kontrol listesine eklenir).
5. **Kabul:** fotoğraf gönderilince Deniz içeriğe uygun yanıt verir; 10 sn'lik sesli mesaj yazıya dökülüp yanıtlanır ve hafıza hattına girer; gece 02:00'de yazılan mesaja üslup gece üslubudur; ardışık 10 yanıtta aynı açılış yoktur.

## Test stratejisi

Temel spec'teki gibi: gerçek SQLite, gerçek `ImsgClient` + sahte `imsg`, saf fonksiyonlara birim testi; model çağrıları sahte (betikli) istemciyle; canlı duman testleri `OMNI_LIVE_COMPANION=1` ile. Faz D: `sips`/`afconvert` gerçek çağrılır (macOS yerleşik), transkripsiyon ve görsel model çağrıları sahte istemciyle; canlı testte gerçek.
