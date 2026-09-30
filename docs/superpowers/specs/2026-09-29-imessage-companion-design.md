# OmniAgent iMessage yol arkadaşı: insan gibi sohbet, kanıta dayalı hafıza, göz gezdiren kalp atışı

**Tarih:** 29 Eylül 2026
**Durum:** Taslak — kullanıcı incelemesi bekliyor.
**Kapsam:** Yeni iMessage kanalı (`integrations/imsg.py`, `integrations/imessage.py`), yeni `companion/` paketi, yeni `memory/personal.py` ve `platform/macos/presence.py`. Mevcut çekirdekte yalnızca "Mevcut koda dokunuşlar" bölümünde listelenen değişiklikler yapılır.

## Amaç

Kullanıcı iPhone'undan iMessage ile OmniAgent'a bir arkadaşa yazar gibi yazar: kısa, küçük harfli, art arda balonlar. Ajan hızlı cevap verir, gerektiğinde bilgisayarda iş yapar, kullanıcı hakkında yalnızca kanıtlanabilir şeyleri hatırlar ve canı istediğinde — kullanıcı ortalıkta yokken "hey nerdesin" diye — kendiliğinden yazar ya da kendi başına iş yapar. Karakter: yakın arkadaş.

## Kullanıcıyla alınan kararlar

| Konu | Karar |
| --- | --- |
| Kimlik | Ajana ayrı Apple ID. Bu Mac'in ana macOS kullanıcısındaki Messages.app o hesapla oturum açar; kullanıcının kendi iMessage'ı bu Mac'ten çıkar, iPhone'da sürer. |
| Mimari | İki katmanlı beyin: hızlı sohbet katmanı + gerektiğinde mevcut ajana arka planda iş devri. |
| Taşıma | `imsg rpc` (openclaw/imsg). |
| Karakter | Yakın arkadaş; isim ve fotoğraf kurulumda. |
| Hafıza | Yalnızca kullanıcının kendi iMessage mesajlarından, birebir alıntıyla kanıtlanan bilgiler. |
| Proaktiflik | OpenClaw tarzı kalp atışı: uyan → göz gezdir → sessiz kal / mesaj at / iş yap. |
| Otonomi | Kendi başına iş için süre ve sayı sınırı yok; kendi fikriyle iş başlatabilir; tek fren, geri alınamaz adımlar için iMessage onayı. |
| Mesaj sınırları | Yalnızca sessiz saatler (23:30–09:00) ve üst üste 2 cevapsız kendiliğinden mesajdan sonra kullanıcı yazana dek susma. Günlük sınır yok. |

## Başarı ölçütleri

1. **Hız:** İş devri gerektirmeyen mesajlarda burst bitişinden (son balondan sonra `burst_quiet_seconds` sessizlik) ilk yanıt balonunun gönderilmesine kadar p50 ≤ 3 sn, p95 ≤ 6 sn (30 mesajlık canlı ölçüm).
2. **İzolasyon:** Eşleşmiş handle dışından ya da grup sohbetinden gelen hiçbir mesaj model istemine, arşive veya log içeriğine girmez.
3. **Kanıt değişmezi:** `facts` tablosundaki her kayıt yönü `in` olan bir arşiv mesajına bağlıdır ve alıntısı o mesajın normalize metninde birebir geçer.
4. **Kayıpsızlık:** Köprü yeniden başladığında hiçbir mesaj kaybolmaz, hiçbir burst iki kez yanıtlanmaz.
5. **Öncelik:** Otonom iş sürerken kullanıcı masaüstünden, Telegram'dan veya iMessage'dan iş başlatırsa otonom iş en çok 15 sn içinde durur ve kullanıcının işi başlar.
6. **Odak:** Mesaj gönderimi Messages.app'i öne getirip kullanıcının odağını çalmaz.
7. Mevcut test paketi (`uv run python -m pytest tests/ -q`) yeşil kalır.

## Kapsam dışı

- "Yazıyor…" göstergesi, tapback, düzenleme ve geri alma: SIP kapatmayı gerektiren IMCore köprüsüne bağlı. `imsg`'nin `typing` yöntemi Messages.app'i öne getirebildiği için o da kullanılmaz.
- Grup sohbetleri, kullanıcı dışındaki kişilerle konuşma, birden fazla kullanıcı.
- Masaüstü/Telegram sohbetlerinden ve Mac kişisel verilerinden (Rehber, Takvim, Notlar) hafızaya otomatik öğrenme. Kalp atışı bunlara bir iş olarak bakabilir; sonuç etkinlik günlüğüne girer, `facts`'e girmez.
- Sesli mesaj çevirisi (sonraki adım; `integrations/transcription.py` yeniden kullanılabilir).
- Zamanlanmış görevler (`schedules.json`) Telegram köprüsünde kalır; `can_schedule` Telegram'a bağlı kalır.
- Embedding/vektör arama: FTS5 ve istemde her zaman bulunan çekirdek profil ile başlanır.
- Kullanıcının başlattığı işlerin onay politikası değişmez; yeni kapılar yalnızca otonom koşularda çalışır.

## Alternatifler ve neden bu yaklaşım

1. **İki katmanlı beyin (seçildi).** "Selam"a tek küçük model çağrısıyla cevap verilir, iş sürerken sohbet sürer, mevcut ajan döngüsü neredeyse değişmeden kullanılır.
2. **Her mesaj tam ajan koşusu (Telegram modeli).** En az kod; ama büyük İngilizce görev istemi, araç şemaları ve çok turlu döngü her mesajı yavaşlatır, iş sürerken sohbet edilemez, görev odaklı istem kişilikle çatışır.
3. **OpenClaw ve OmniAgent'ı araç olarak bağlamak.** Kanal ve heartbeat hazır; ama ikinci bir ajan çerçevesi, iki ayrı hafıza/kişilik oluşur ve OmniAgent'ın onay/denetim katmanı devre dışı kalır.

Taşıma katmanı olarak `imsg rpc` seçildi. BlueBubbles'ın son sunucu sürümü Mayıs 2025 tarihli ve Tahoe'da sorunlu. Kendi `chat.db` + AppleScript kodumuzu yazmak, `imsg`'nin zaten çözdüğü `attributedBody` çözme ve `is_from_me` düzeltme tuzaklarını yeniden çözmek demek. OpenClaw da 2026.5.12'den beri yalnızca `imsg rpc` kullanıyor.

## Gerçekçi kısıtlar (dürüstlük bölümü)

- Messages.app bir macOS kullanıcısında tek iMessage hesabıyla oturum açar; bu yüzden kullanıcının kendi iMessage'ı bu Mac'ten çıkar. "Kendine mesaj" modeli reddedildi: iOS kendi hesabından gelen mesajlar için bildirim göstermiyor ve her mesaj iki kez (farklı `is_from_me` ile) kaydedildiği için yankı döngüsü riski var.
- Ajan daha önce yazışmadığı bir handle'a ilk mesajı güvenilir biçimde başlatamıyor. Eşleştirme kullanıcının iPhone'dan göndereceği ilk mesajla yapılır. iOS 26 bilinmeyen gönderenleri süzdüğü için kullanıcı ajanı rehbere kaydetmelidir.
- `imsg` `send` teslimatı doğrulamaz. `-32001` "sonuç bilinmiyor" demektir; otomatik yeniden gönderim yapılmaz (çift mesaj riski). Teslim, gönderilen metnin izlemede `is_from_me` satırı olarak görülmesiyle doğrulanır.
- AppleScript gönderimi Messages.app çalışmıyorsa onu açabilir. Köprü açılışta Messages.app'i `open -g -a Messages` ile arka planda başlatır. Odak çalmama (ölçüt 6) canlı kabul testinde doğrulanır; tutmazsa bu bir engeldir ve uygulama o noktada durup kullanıcıya danışır.
- **TCC izinleri sorumlu sürece verilir.** Mevcut `platform/macos/permissions.py` bunu zaten belgeliyor: Terminal'den çalışan süreçte izin Terminal'e, arka plan servisinde python ikilisine gider. Bu yüzden Full Disk Access ve Messages için Automation izni, LaunchAgent altında çalışan gerçek python ikilisine (`realpath(sys.executable)`) verilmelidir. Kurulum eşleştirmeyi ve ilk gönderimi terminalde değil servisin kendisinde yaptırır. Bu, canlı kurulumda doğrulanacak ilk risktir.
- Mac uykudayken mesaj alınmaz. Köprü Telegram'daki gibi `caffeinate` kullanır; kapağı kapalı MacBook'ta çalışmaz.
- **Kodla doğrulama sırasında düzeltilen varsayım:** Tasarım konuşmasında "silme, sen iş verdiğinde de sorulur" dendi; kod bunu desteklemiyor. `explicit_deletion_target` ve `unmet_explicit_deletion` (`app/policy.py:376-398`) bir izin kapısı değil, görev sonu doğrulamasıdır (`app/agent.py:1717`). Mevcut onay kapıları finansal işlem, dışa gönderim, kart bilgisi, korumalı veri dosyası ve istenmemiş hafıza değişikliğini kapsar; dosya silmeyi kapsamaz. Bu spec otonom koşular için yeni bir silme kapısı ekler; kullanıcının başlattığı işlerde davranış değişmez.
- Silme ve kendi-kod kapıları sezgiseldir: kabuk/Python kodundaki olağan silme kalıplarını ve OmniAgent kaynak ağacına `write_file`/`edit_file` yazımlarını yakalar. Finder üzerinden GUI ile silme ya da kasıtlı olarak gizlenmiş komutlar yakalanmaz. Her otonom eylem zaten `audit.jsonl`'e yazıldığı için iz kalır.
- Öncelik (ölçüt 5), çalışan aracın `should_stop`'a duyarlılığına bağlıdır: uzun süren bir kabuk komutu bitmeden durmayabilir. Ölçüt 5 canlı ölçülür; süre dolarsa kullanıcı işi bugünkü `HostBusyError` mesajını alır.
- Kullanıcının algıladığı cevap süresi, burst beklemesi (`burst_quiet_seconds`) ile ölçüt 1'deki sürenin toplamıdır. Hız hedefi modelin ilk token süresine bağlıdır; sohbet profili kurulumda ölçümle seçilir.
- Otonom işlerde süre ve sayı sınırı olmadığından maliyet sınırlanmaz, görünür kılınır: her iş etkinlik günlüğüne süre ve token ile yazılır, `/durum` bugünün toplamını gösterir. Kalp atışı günde yaklaşık 40–50 küçük model çağrısıdır.

## Mimari

```
iPhone ─iMessage─▶ Messages.app (ajanın Apple ID'si) ─chat.db─▶ imsg rpc ─stdio JSON-RPC─▶ ImsgClient
                                                                                              │
                                            iMessage köprüsü (integrations/imessage.py) ◀─────┘
                                            süzgeç · burst · komutlar · onay/dosya sink'leri · arşiv+imleç
                                                   │                          │
                              companion/chat.py ◀──┘                          └──▶ companion/heartbeat.py
                              (sohbet katmanı)                                     (kalp atışı)
                                     │ iş devri                                          │ iş kararı
                                     └──────────────▶ companion/delegate.py ◀────────────┘
                                                        └─▶ run_agent_with_callback (mevcut ajan)
                              memory/personal.py (companion.db): arşiv · facts · activity · state
```

Tek süreç, tek asyncio döngüsü (Telegram köprüsüyle aynı model). Köprü, sohbet katmanı, kalp atışı ve öğrenme hattı aynı döngüde görevlerdir. Ajan koşusu `run_agent_with_callback` ile aynı döngüde çalışır; araçlar kendi iş parçacıklarındadır.

## Bileşenler

### 1. `integrations/imsg.py` — `ImsgClient`

Dış sistem bağlayıcısı olduğu için sınıftır. `imsg rpc` alt sürecini yönetir (satır başına bir JSON-RPC 2.0 nesnesi).

- `start()`: süreci başlatır, `initialize` çağırır. `status.database.ready` yanlışsa `ImsgUnavailable` yükseltir; mesaj, Full Disk Access verilecek ikili yolunu içerir.
- `subscribe(since_rowid)`: `watch.subscribe` (`attachments: true`, `debounce_ms: 500`) ile gelen `message` bildirimlerini `IncomingMessage` olarak verir. `watch.overflow` gelirse `resume_after_rowid`'den `messages.after` ile sayfalayarak devam eder.
- `catch_up(since_rowid)`: `messages.after` ile `has_more` bitene dek sayfalar; yetkili imleç `next_rowid`'dir.
- `send_text(handle, text)`, `send_file(handle, path)`: `send` (`to`, `service: "imessage"`, `allow_sms_fallback: false`). Sonuç `SendResult` (`ok`, varsa `id`, `guid`).
- Hata politikası:
  - Süreç çöktü veya stdout kapandı: 3 deneme (1, 2, 4 sn bekleme), her denemede yapılandırılmış uyarı logu; sonra `ImsgProcessError` yükselir, köprü süreci çıkar, launchd yeniden başlatır, imleçten devam edilir.
  - `-32002` (veritabanı yok): `ImsgUnavailable`.
  - `-32001` (teslim sonucu bilinmiyor): `DeliveryUnknown`; yeniden gönderim yok.
  - `-32004` (mutasyon şeridi bloke): alt süreç yeniden başlatılır, belirsiz gönderim `DeliveryUnknown` olarak raporlanır.
  - Diğer JSON-RPC hataları: kod, mesaj ve kısaltılmış istek parametreleriyle `ImsgRpcError`.
- `IncomingMessage` (TypedDict): `rowid` (imsg `id`), `guid`, `chat_id`, `sender`, `is_from_me`, `is_group`, `text`, `created_at`, `attachments` (`path`, `mime_type`).

### 2. `integrations/imessage.py` — köprü ve komut satırı

`omniagent-imessage setup | run | install-service` (pyproject betiği).

- **Süzgeç (saf `accepted`):** `is_from_me` yanlış, `is_group` yanlış ve `normalize_handle(sender) == handle`. Normalizasyon: telefonda boşluk, tire ve parantez atılır, `+` korunur; e-postada `casefold`. Reddedilen mesajın içeriği hiçbir yere yazılmaz, yalnızca sayacı loglanır.
- **Arşiv ve imleç tek işlemde:** Kabul edilen her mesaj `messages` tablosuna yazılır ve imleç (`state.imsg_cursor`) aynı SQLite işleminde ilerletilir; işleme bundan sonra başlar (Telegram'ın önce-imleç deseni, `telegram.py:1362`). Açılışta arşivdeki son mesaj kullanıcıdansa, ardından ajan mesajı yoksa ve 1 saatten yeniyse o burst yanıtlanır. Böylece çökme ne kayıp ne çift yanıt üretir.
- **Burst birleştirme:** Kabul edilen son mesajdan `burst_quiet_seconds` sonra birikmiş metinler tek girdi olur.
- **Kontrol komutları** (burst beklemeden, anında):
  - `dur`, `/dur`: çalışan işi durdurur.
  - `/sessiz <saat>`, `/proaktif aç|kapat`: kalp atışını susturur veya açıp kapatır.
  - `/hafıza`: aktif bilgileri kimlik ve tarihle listeler.
  - `/durum`: çalışan iş, bugünkü otonom işler ve token toplamı, son 50 yanıtın gecikme p50'si, son hafıza hatası.
- **Gönderim doğrulaması:** Giden her balon arşive `pending` yazılır. İzlemede aynı metinli `is_from_me` satırı 60 sn içinde görülürse `sent`, görülmezse `unconfirmed` olur ve uyarı loglanır. Yeniden gönderilmez.
- **`AnswerSink` (onay/soru):** Soruyu kişilik tonunda kısa metne çevirip gönderir.
  - Tek alanlı sorularda sonraki mesaj cevaptır. `approval.approval_granted` evet/hayır olarak çözemediği mesaj normal sohbete gider ve bekleyen soru sohbet istemine eklenir.
  - Çok alanlı sorularda alanlar sırayla ayrı balon olarak sorulur, gelen mesajlar sırayla eşlenir.
  - Otonom koşularda sessiz saat içindeyse sink mesaj göndermeden `TimeoutError` yükseltir; mevcut `require_approval` bunu `timeout` olarak kaydeder ve adım yapılmaz.
- **`DeliverSink`:** `send_file`; hata `DeliveryFailed`.
- **Süreç:** Açılışta `open -g -a Messages`; tek örnek kilidi (`imessage-bridge.lock`); `caffeinate`; LaunchAgent `com.omniagent.imessage` (RunAtLoad, KeepAlive). Telegram'daki genel launchd kodu (`telegram.py:1464-1530`: plist yazma, `launchctl`, yükleme bekleme) `platform/macos/launch_agent.py`'ye taşınır ve iki köprü onu kullanır.
- **Kurulum (`setup`):** TCC izinleri servis ikilisine gittiği için eşleştirme ve ilk gönderim servisin içinde yapılır.
  1. `imsg` yoksa `brew install steipete/tap/imsg` talimatıyla durur.
  2. Karakter adını alır, `persona.md`'yi yakın arkadaş şablonundan üretir.
  3. Kullanılabilir profillerin ilk token süresini sabit bir Türkçe istemle ölçer ve tablo gösterir; kullanıcı `chat_backend` ile `memory_backend`'i seçer.
  4. Eşleştirmesiz `imessage.json` taslağını yazar, servisi kurar ve başlatır. Servis `ImsgUnavailable` bildirirse kurulum, Full Disk Access verilecek ikili yolunu yazar ve ilgili Sistem Ayarları bölmesini açar.
  5. 6 haneli tek kullanımlık kod gösterir. Kullanıcı kodu iPhone'dan ajana gönderir; servis, kodu 180 sn içinde birebir sohbette gönderen handle'ı kaydeder ve eşleşme mesajını gönderir (Automation izni bu gönderimde servis ikilisi için sorulur).
  6. Kurulum eşleşmenin ve gönderimin `state`'e yazılmasını bekler; süre dolarsa neyin eksik olduğunu yazıp durur.

### 3. `companion/persona.py`

- `persona.md` (veri kökünde, kullanıcı düzenleyebilir): isim, karakter, konuşma tarzı.
- Değişmez kurallar (kodda, Türkçe):
  - 1–4 kısa balon; küçük harf; markdown yok; emoji az.
  - Kullanıcı hakkında yalnızca çekirdek profildeki ve bu konuşmadaki bilgiyi kullanır; bilmiyorsa sorar.
  - **Duygular serbest, olaylar gerçek:** ruh hâli gösterebilir ama yaşamadığı olayı anlatmaz; yaptığını söylediği her şey etkinlik günlüğünde olmalıdır.
- `system_prompt(persona_text, core_profile, situation)`: saf. Sabit önek (kurallar, persona, çekirdek profil) başta, değişen durum (saat, çalışan iş, bekleyen soru) sonda durur; sağlayıcı önek önbelleği için.

### 4. `companion/chat.py` — sohbet katmanı

- **Girdi:** birleşik burst metni (fotoğraf ekleri `[fotoğraf: dosya adı]` olarak), arşivden son 40 mesaj, çekirdek profil, mevcut `user_memory` bloğu (`memory_prompt_block`), durum (çalışan iş ve son ilerleme olayları, bekleyen soru).
- **Model:** `chat_backend` profili. Mevcut `model_runtime.stream_completion` ve `model_retry` sınıflandırması kullanılır; yeni istemci yazılmaz.
- **Çıktı:** Yanıt metni balonlara bölünür: her boş olmayan satır bir balondur, 4'ü aşan satırlar sonuncuda birleşir. Akış sırasında ilk satır tamamlanır tamamlanmaz gönderilir.
- **Araçlar:** `start_task(goal)`, `recall(query)` (sonucuyla ikinci tur), `forget(fact_id)`, `mute(hours)`, `set_proactive(enabled)`. "Bugün yazma bana" gibi cümleler `mute`/`set_proactive`'e çevrilir.
- **Balon aralığı (saf):** `min(1.5, 0.4 + 0.02 × karakter)` sn.
- Her yanıt için yapılandırılmış gecikme logu (burst bitişinden ilk balona, modelin ilk token süresi, gönderim süresi); son 50 değer `state`'te tutulur.

### 5. `companion/delegate.py` — iş devri

- **`RunOptions`:** `answer` ve `deliver` iMessage sink'leri; `history` olarak `imessage-history.json`'daki son 8 görev `Exchange`'i (Telegram deseni); `model_retry_seconds` = `REMOTE_MODEL_RETRY_SECONDS`; `images` = burst'teki fotoğraflar. **`unattended` hiçbir zaman verilmez**; böylece `AUTO_APPROVE_IN_CONTINUOUS_MODE` yolu devreye girmez.
- **Kökene göre:** Kullanıcı işi `host_task_lock_preempting()` ile, otonom iş `preemptible_host_task_lock()` ve `RunOptions["autonomy"]` ile çalışır (Bölüm 8 ve 9).
- **İlerleme:** Olaylar kullanıcıya akıtılmaz; son durum sohbet katmanına verilir ve "ne durumda?" sorusu gerçek olaylardan cevaplanır. Kullanıcı işi 5 dakikayı geçerse bir kez kısa ara bilgi gider; otonom işler yalnızca bitişte raporlanır.
- **Bitiş:** `RunReport` sohbet katmanına "iş raporu" girdisi olarak verilir; sonuç yalnızca rapordan anlatılır, başarısızlık açıkça söylenir. Etkinlik günlüğüne köken, hedef, gerekçe, sonuç, süre ve token yazılır.
- iMessage'dan gelen kullanıcı işi, çalışan otonom işi süreç içinden durdurur ve yerini alır.

### 6. `memory/personal.py` — kanıta dayalı hafıza (`companion.db`, 0600, WAL)

**Tablolar** (tüm zaman damgaları ISO 8601, UTC — `memory/user.py` `utc_timestamp` deseni; `imsg`'nin `created_at` değeri UTC'ye çevrilerek saklanır):
- `messages`: `id`, `imsg_rowid` (benzersiz; giden mesajda doğrulanınca dolar), `guid`, `direction` (`in`/`out`), `kind` (`chat`/`proactive`/`task_report`/`question`), `text`, `created_at`, `delivery` (`pending`/`sent`/`unconfirmed`, yalnızca `out`). FTS5 `messages_fts` (`unicode61 remove_diacritics 2`).
- `facts`: `id`, `statement`, `quote`, `message_id` (→ `messages.id`), `category` (`kisi`/`tercih`/`plan`/`durum`/`olay`), `status` (`active`/`superseded`/`forgotten`), `superseded_by`, `follow_up_at`, `created_at`, `updated_at`. FTS5.
- `activity`: `id`, `kind` (`task`/`message`/`lesson`/`dropped_message`), `origin` (`user`/`autonomous`), `goal`, `rationale`, `outcome`, `success`, `started_at`, `finished_at`, `tokens`.
- `state`: anahtar-değer (imsg imleci, hafıza imleci, cevapsız proaktif sayacı, `muted_until`, `proactive_enabled`, sonraki uyanış, bekleyen sabah raporları, son hafıza hatası, son gecikmeler, eşleştirme durumu).

**Öğrenme hattı.** Tetik: son kullanıcı mesajından 180 sn sessizlik ya da 20 işlenmemiş kullanıcı mesajı. Girdi: `memory_cursor`'dan sonraki `in` mesajları.
1. **Çıkarım (`memory_backend`):** JSON `{"facts":[{statement, quote, message_id, category, supersedes?, follow_up_at?}]}`. Mevcut aktif bilgiler kimlikleriyle bağlam olarak verilir.
2. **Kapı 1 (deterministik, saf `quote_supported`):** `message_id` bir `in` mesajıdır; alıntı en az 3 kelime ve 12 karakterdir; iki taraf aynı biçimde normalize edildikten sonra (NFKD, birleşik işaretler atılır, `casefold`, boşluk sıkıştırma) alıntı mesaj metninde geçer.
3. **Kapı 2 (doğrulayıcı model, `memory_backend`):** Her aday için "alıntı, ifadeyi ek çıkarım olmadan destekliyor mu?" sorulur; yalnızca kesin `evet` kabul edilir.
4. **Gizli bilgi süzgeci:** `memory/user.py`'deki kurallar (`_SENSITIVE_TERMS`, `_SECRET_VALUE_PATTERN`) ortak bir genel fonksiyona çıkarılır ve iki modülde kullanılır; kopya kural yazılmaz.
5. **`supersedes` ve `follow_up_at`:** Hedef aktif ve aynı kategorideyse eski kayıt `superseded` olur. `follow_up_at` mesaj zamanından sonra ve en çok 1 yıl içindeyse tutulur; değilse yalnızca bu alan düşürülür.
6. **Başarısızlık** (geçersiz JSON, model hatası): imleç ilerlemez, yapılandırılmış hata loglanır, `/durum`'da görünür; sonraki tetikte yeniden denenir. Kapı retleri yalnızca sayı olarak loglanır.

**Kullanım:**
- **Çekirdek profil:** Aktif bilgiler sabit sırayla (kategori, oluşturma zamanı), satır başına `[#id] ifade — "alıntı" (gg.aa.yyyy)` biçiminde verilir; alıntı 80 karakterde kesilir. Bütçe 6000 karakterdir; aşılırsa en son güncellenenler kalır ve "(+N kayıt: recall ile ara)" notu eklenir (`memory_prompt_block` deseni).
- **`recall(query)`:** FTS5 ile `messages` ve `facts` içinde arar; en çok 8 birebir parçayı tarihiyle döndürür. Sonuçlar yönle etiketlenir; ajanın kendi mesajları kullanıcı hakkında kanıt sayılmaz.
- **`forget(fact_id)`:** Kaydı `forgotten` yapar; istemden ve aramadan çıkar. Düzeltmeler ("yanlış biliyorsun, doğrusu…") kullanıcının kendi mesajı olduğundan öğrenme hattından `supersedes` ile geçer; o ana kadar konuşma bağlamı düzeltmeyi zaten taşır.

### 7. `companion/heartbeat.py` ve `platform/macos/presence.py` — kalp atışı

- **Uyanış (saf `next_wake`):** `şimdi + clamp(modelin önerdiği dakika ya da heartbeat.base, heartbeat.min, heartbeat.max) + uniform(−jitter, +jitter)`. Uyanış, en erken vadeli `follow_up_at`'ten sonraya kurulmaz; hatırlatmalar zamanında gider.
- **Atlama:** Proaktif kapalıysa, `muted_until` içindeyse ya da iki yönden herhangi birindeki son mesaj 15 dakikadan yeniyse (sohbet zaten sürüyor) karar verilmez, yalnızca uyanış yeniden kurulur.
- **Göz gezdirme — `Snapshot` (saf toplama, model yok):**
  - zaman: yerel saat, gün, sessiz saat mi;
  - konuşma: son kullanıcı/ajan mesajı zamanı, cevapsız proaktif sayısı, bekleyen soru;
  - varlık (`presence.py`): HID boşta süresi (`CGEventSourceSecondsSinceLastEventType`), ekran kilidi (`CGSessionCopyCurrentDictionary`), öndeki uygulama adı (`NSWorkspace`; pencere başlığı alınmaz);
  - ritim (saf, son 30 günün arşivinden): saat dilimlerine göre yazma dağılımı, bugün yazdı mı, sessizlik her zamankinden uzun mu;
  - hafıza: vadesi gelmiş `follow_up_at` kayıtları;
  - kendi hayatı: son 5 etkinlik ve ders, çalışan iş;
  - `kalp_atisi.md`: kullanıcının kontrol listesi (örneğin "takvimime bak").
- **Karar:** Aynı persona öneki ve snapshot ile tek model çağrısı yapılır (`chat_backend`). Araçlar: `stay_quiet(next_check_minutes)`, `send_message(bubbles, grounds, next_check_minutes)`, `start_task(goal, rationale, next_check_minutes)`. Salt-okunur bir kontrol (takvime bakmak gibi) de bir `start_task`'tır; sınırlar kalktığı için ayrı bir "kontrol" eylemi gereksizdir. Araç çağrısı yoksa bu bir hatadır: loglanır, uyanış yeniden kurulur.
- **Sert kurallar (saf `allowed_actions`):** Model çağrısından önce araç listesini daraltır. Sessiz saatte `send_message` yok; cevapsız proaktif sayısı 2 veya üstündeyse `send_message` yok (kullanıcı yazınca sayaç sıfırlanır); iş çalışıyorsa `start_task` yok.
- **Dayanak denetimi (yalnızca proaktif mesaj):** `grounds` içindeki her kimlik var ve aktif olmalıdır (deterministik). Ardından doğrulayıcı model (`memory_backend`) "taslakta, dayanaklarda olmayan kullanıcı/geçmiş iddiası var mı?" diye bakar. Varsa mesaj gönderilmez, `dropped_message` etkinliği yazılır.
- Gönderilen proaktif mesaj `kind=proactive` olarak arşivlenir, cevapsız sayacı bir artar.
- Sessiz saatte biten otonom işin raporu `state`'te bekler; sessiz saat bitince ilk uyanışta ya da kullanıcı yazınca hemen gider ("günaydın, gece şunu hallettim; şunun için onayın lazım").

### 8. Otonom iş korumaları — `RunOptions["autonomy"]`

Yalnızca kalp atışının başlattığı işlerde verilir; kullanıcının başlattığı işlerde yoktur (bugünkü davranış korunur). `AutonomyGuards` (TypedDict) şu kapıları taşır ve `tool_execution` bunları uygular:

- **`gui_gate`:** `app/tool_schema.py`'deki `_SCREEN_ACTION_TOOLS` kümesinin tamamından önce çağrılır (bu araçlar fare/klavye girdisi üretir ya da kullanıcının Chrome sekmesini değiştirir). Ekran kilitliyse ya da kullanıcı son `gui_idle_seconds` içinde Mac'e dokunduysa 30 sn aralıkla yoklayarak bekler; `should_stop`'a duyarlıdır, işi iptal etmez. Kapı ajanın kendi son girdi zamanını bilir: HID boşta süresi bundan kısaysa girdi kullanıcıdandır.
- **Silme kapısı:** `execute_shell` veya `execute_python` kodu olağan silme kalıpları içeriyorsa (`rm`, `rmdir`, `unlink`, `find … -delete`, `trash`, `git clean`, `os.remove`, `os.unlink`, `shutil.rmtree`, `Path.unlink`, `Path.rmdir`) çalıştırılmadan önce yeni `deletion_request` ile `require_approval` üzerinden iMessage onayı istenir. Modelin yazdığı görev metni `explicit_deletion_target` ile bir silme hedefi içeriyorsa görev başlamadan önce onay sorulur.
- **Kamera kapısı:** `capture_photo` otonom koşuda onaya bağlıdır (kullanıcının haberi olmadan fotoğraf çekilmez).
- **Kendi-kod kapısı (`guarded_roots`):** OmniAgent kaynak ağacı (`Path(omniagent.__file__).parent`). `write_file`/`edit_file` bu köke yazmak isterse `self_modification_request` ile onay istenir. Bu, SELF-MODIFICATION kuralındaki "onaylanmış öneri" koşulunun uygulamasıdır.
- **Gerekçe zorunlu:** `start_task.rationale` boşsa iş başlamaz; gerekçe etkinlik günlüğüne ve rapora girer.
- **Gece:** Sessiz saatte otonom koşunun `AnswerSink`'i mesaj göndermeden `TimeoutError` yükseltir; onay gerektiren adım yapılmaz ve sabah raporunda listelenir. Kullanıcı "evet" derse sohbet katmanı o adımı yeni bir kullanıcı işi olarak başlatır.
- **Ders:** Her otonom iş bitince `memory_backend` yalnızca `RunReport` içeriğinden "ne denendi, ne oldu, sonraki sefer ne farklı" dersini yazar (`activity.kind=lesson`); son dersler snapshot'a girer. Araç düzeyindeki mevcut `experience` hafızası aynen çalışır.
- Süre ve sayı sınırı yoktur. Aynı anda tek iş `host_task_lock` ile sağlanır.

### 9. Host kilidi önceliği — `platform/macos/host_lock.py`

- Kilidi alan her fonksiyon sahip türünü (`user`/`autonomous`) kilit dosyasına yazar.
- `preemptible_host_task_lock()`: otonom işler içindir; kilidi alır ve türü `autonomous` yazar.
- `host_task_lock_preempting(timeout_seconds)`: kullanıcı işleri içindir. Kilit meşgulse ve sahibi `autonomous` ise `host-preempt.request` dosyasını yazar ve kilidi `timeout_seconds` (15) boyunca yoklar; alırsa isteği siler. Sahip bir kullanıcı işiyse ya da süre dolarsa bugünkü `HostBusyError` yükselir.
- `preemption_requested(since)`: otonom koşunun `should_stop`'u bunu en çok 500 ms aralıkla yoklar. İş durur, "yarıda bıraktım" dersini yazar ve kilidi bırakır.
- Kullanıcı işi başlatan yerler bu fonksiyona geçer: `ui/app.py:3009`, `integrations/telegram.py:917`, `integrations/telegram.py:1075` (zamanlanmış görev kullanıcı tanımlıdır) ve iMessage kullanıcı işi.

## Yapılandırma ve dosyalar

`imessage.json` (0600, korumalı). Tüm alanlar zorunludur, kodda varsayılan yoktur. Kurulum onaylanan değerleri yazar; doğrulama eksik veya geçersiz alanda açık hata verir. Sentetik örnek (`handle` eşleştirmede dolar; `quiet_hours` yerel saatle `HH:MM`):

```json
{
  "handle": "+905551112233",
  "persona_name": "Deniz",
  "chat_backend": "openai",
  "memory_backend": "opencode",
  "quiet_hours": {"start": "23:30", "end": "09:00"},
  "burst_quiet_seconds": 2.0,
  "gui_idle_seconds": 180,
  "heartbeat_minutes": {"base": 30, "jitter": 10, "min": 20, "max": 240}
}
```

Veri kökündeki yeni dosyalar: `imessage.json`, `companion.db` (ve `companion.db-wal`, `companion.db-shm`), `persona.md`, `kalp_atisi.md`, `imessage-history.json`, `imessage-bridge.lock`, `host-preempt.request`. `imessage.json`, `companion.db` ve WAL/SHM dosyaları, `persona.md`, `kalp_atisi.md` ve `imessage-history.json` `approval.PROTECTED_DATA_FILES`'a eklenir: ajan kendi kişiliğini, kontrol listesini ya da hafıza veritabanını onaysız değiştiremez. Yol yardımcıları `paths.py`'ye eklenir.

## Mevcut koda dokunuşlar

| Dosya | Değişiklik |
| --- | --- |
| `app/types.py` | `AutonomyGuards` tipi ve `RunOptions["autonomy"]: NotRequired[AutonomyGuards]` |
| `integrations/runtime.py`, `app/agent.py` | `autonomy` alanı `IntegrationRuntime`'a aktarılır (`unattended` ile aynı yerde) |
| `app/tool_execution.py` | `autonomy` varsa GUI, silme, kamera ve kendi-kod kapıları |
| `approval.py` | `deletion_request`, `self_modification_request`, `camera_request`; `PROTECTED_DATA_FILES` genişler |
| `platform/macos/host_lock.py` | öncelik mekanizması (Bölüm 9) |
| `ui/app.py`, `integrations/telegram.py` | kullanıcı işleri için `host_task_lock_preempting`; Telegram'ın launchd kodu `platform/macos/launch_agent.py`'ye taşınır |
| `memory/user.py` | gizli bilgi kuralı ortak genel fonksiyona çıkarılır |
| `app/tool_schema.py:205, 486-499`, `config.py:393, 466, 471`, `tools/facade.py:506, 518` | "Telegram" geçen metinler "kullanıcının mesaj kanalı (Telegram/iMessage)" olarak genelleşir |
| `platform/macos/permissions.py` | Full Disk Access ve Messages Automation durumu da raporlanır |
| `paths.py`, `pyproject.toml` | yeni yollar; `omniagent-imessage` betiği |

## Hata yönetimi (özet)

| Durum | Davranış |
| --- | --- |
| `imsg` süreci çöktü | 3 deneme + uyarı → `ImsgProcessError` → launchd yeniden başlatır → imleçten devam |
| Full Disk Access yok (`-32002`) | `ImsgUnavailable`: hangi ikiliye izin verileceği yazılır |
| Teslim bilinmiyor (`-32001`) | Yeniden gönderim yok; `unconfirmed` + uyarı |
| Model geçici hatası | `model_retry`; kalıcıysa kullanıcıya kısa hata balonu + profil, durum kodu ve gövde özetiyle yapılandırılmış log |
| Hafıza çıkarım hatası | İmleç ilerlemez, log, `/durum` |
| Kalp atışında araç çağrısı yok | Hata logu, uyanış yeniden kurulur |
| Otonom iş kilidi alamadı | Atlanır, sonraki uyanışta yeniden değerlendirilir |
| Kullanıcı işi, başka kullanıcı işi çalışırken | Bugünkü `HostBusyError` mesajı |

Loglar yapılandırılmış alanlarla yazılır (`extra={...}`). Mesaj ve hafıza içerikleri loglanmaz; yalnızca kimlikler ve sayılar.

## Güvenlik

- Yalnızca eşleşmiş handle; grup yok; eşleştirme tek kullanımlık kodla.
- Hiçbir iMessage koşusunda `unattended` verilmez; otomatik onay yolu kullanılmaz.
- Web içeriğinden gelebilecek istem enjeksiyonu otonom işlerde de mevcut onay kapılarına ve yeni silme, kamera ve kendi-kod kapılarına takılır.
- Yeni yapılandırma ve hafıza dosyaları korumalıdır; veriler yerel ve 0600'dür. Model sağlayıcısına yalnızca istem içeriği gider: persona, çekirdek profil, son mesajlar ve snapshot (öndeki uygulama adı dahil, pencere başlığı hariç).

## Test stratejisi

Repo deseni korunur: dış sistemler sınırda sahtelenir, gerçek ajan döngüsü ve gerçek SQLite kullanılır. Modele bağlı davranış için gerçek çağrılar tercih edilir.

- **Saf mantık (küçük veri testleri):** `accepted` ve handle normalizasyonu, burst birleştirme, balon bölme ve aralığı, `quote_supported` (Türkçe İ/ı, aksan, boşluk, noktalama, kısa alıntı reddi), çekirdek profil sırası ve bütçesi, ritim hesabı, `next_wake`, `allowed_actions`, gece yarısını aşan sessiz saat hesabı, silme kalıbı sezgisi.
- **`ImsgClient`:** stdio üzerinden JSON-RPC konuşan sahte `imsg` betiği (`tests/fixtures/fake_imsg.py`) ile gerçek alt süreç yolu: `initialize`, bildirim, overflow → `messages.after`, `send`, `-32001`/`-32002`/`-32004`, çökme → yeniden başlatma.
- **Köprü akışı:** `test_telegram_bridge.py` tarzında sahte imsg ve gerçek ajan döngüsü (model çağrısı mevcut desenle yamalanır): iş devri, evet/hayır onayı, `dur`, açılışta yanıtsız burst.
- **Hafıza:** `tmp_path`'te gerçek `companion.db`; FTS5 araması; `superseded`/`forgotten` geçişleri; kanıt değişmezi.
- **Host kilidi önceliği:** iki gerçek süreçle `flock` testi.
- **Canlı duman testleri** (API anahtarı yoksa `skipif` ile atlanır): sohbet katmanı çıktısı (Türkçe, en çok 4 balon, markdown yok), çıkarım + Kapı 2 (bilinen mesaj kümesinden bilinen bilgiler, uydurma aday reddi), dayanaksız proaktif taslağın düşürülmesi.
- **Canlı uçtan uca kontrol listesi** (kurulum kılavuzuyla birlikte `docs/IMESSAGE.md`'de): eşleştirme ve izinler, 30 mesajlık gecikme ölçümü (ölçüt 1), odak çalmama (ölçüt 6), iş devri + onay, `dur`, yeniden başlatma kayıpsızlığı (ölçüt 4), öncelik (ölçüt 5), bir günlük kalp atışı gözlemi, gece işi ve sabah raporu.

## Fazlar

Her faz tek başına kullanılabilir. Her fazın sonunda tam test paketi yeşil olmalı ve o fazın canlı kontrolleri yapılmış olmalıdır.

- **Faz A — kanal, sohbet, iş devri:** `imsg.py`, `imessage.py`, `launch_agent.py`, `persona.py`, `chat.py`, `delegate.py`; `companion.db`'nin `messages`, `activity` ve `state` tabloları; kurulum; Telegram metinlerinin genelleşmesi; izin raporu. Hafıza olarak yalnızca mevcut `user_memory` bloğu kullanılır. Kabul: ölçüt 1, 2, 4, 6.
- **Faz B — kanıta dayalı hafıza:** `facts`, öğrenme hattı, iki kapı, çekirdek profil, `recall`/`forget`, `/hafıza`. Kabul: ölçüt 3.
- **Faz C — kalp atışı ve otonomi:** `heartbeat.py`, `presence.py`, `allowed_actions`, dayanak denetimi, `AutonomyGuards` ve kapıları, host kilidi önceliği, dersler, sabah raporu. Kabul: ölçüt 5.
