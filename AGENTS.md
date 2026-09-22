# OmniAgent Geliştirme Kuralları ve Yol Haritası

Bu dosya, OmniAgent projesinin geliştirilme sürecinde uyulacak katı kuralları, davranışsal rehberleri ve öncelikli geliştirme hedeflerini içerir.

## 🛡️ Güvenlik Rayları (Safety Rails)
Ajan, kendi kaynak kodunu ve host sistemini değiştirebildiği için `tools.py` içinde kod
seviyesinde koruma katmanı bulunur (sandbox değildir, en iyi çaba korumasıdır):
- `write_file` / `self_modify`: hassas sistem/kimlik dosyalarına (`~/.ssh`, `/etc`, kabuk
  profil dosyaları vb.) yazmayı reddeder; `.py` hedeflerinde yazmadan önce `compile()` ile
  sözdizimini doğrular; üzerine yazmadan önce `.omni_backups/` içine zaman damgalı yedek alır.
- `execute_shell`: bilinen yıkıcı komut kalıplarını (`rm -rf /`, `mkfs`, fork bomb, disk
  biçimlendirme vb.) çalıştırmadan önce engeller; `sudo` çağrıları yapılandırılmış log ile
  kaydedilir ve zaten parolasız (`-n`) olmayan sudo oturumlarında güvenli şekilde başarısız olur.
- Bu raylar; iyi niyetli hatalara (örn. bozuk bir `self_modify` yazımı) karşı geri dönüş
  sağlar, kötü niyetli kullanıma karşı bir güvenlik sınırı değildir.

## 🚨 Kritik Yazım ve Modifikasyon Kuralları

### Dosya Yazma Kuralları (FILE WRITING RULES)
- **Yasaklar:** Python dosyaları yazmak için asla kabuk komutları (`cat`, `echo`, `tee`, `heredoc`) kullanılmayacaktır.
- **Zorunluluk:** Dosya içerikleri her zaman `write_file` aracı ile yazılmalıdır. Bu araç kodlama ve tamlığı garanti eder.
- **Büyük Dosyalar:** Büyük dosyalar için `write_file` tek seferde tam içerik yazacak şekilde kullanılmalıdır.

### Kendi Kendini Modifikasyon Kuralları (SELF-MODIFICATION RULES)
- **Önce Oku:** `self_modify` kullanmadan önce her zaman `read_file` ile mevcut dosya okunmalıdır.
- **Tam İçerik:** Dosyaların tamamı yazılmalıdır. Asla kırpma yapılmamalı ve "// rest of code" gibi yer tutucu yorumlar kullanılmamalıdır.
- **Doğrulama:** `self_modify` işleminden hemen sonra, dosya tekrar okunarak doğru yazıldığı doğrulanmalıdır.
- **Bölümleme:** Dosya tek seferde yazılamayacak kadar büyükse, mantıksal bölümlere ayrılmalı ve `write_file` "append" modu ile yazılmalıdır.

### Detaylı Yazım ve Modifikasyon Kuralları (Detailed Rules)
### SELF-MODIFICATION RULES:
- Before using self_modify, ALWAYS read the current file with read_file first.
- Write the COMPLETE file content. Never truncate. Never use placeholder comments like "// rest of code".
- After self_modify, immediately read the file back to verify it was written correctly.
- If the file is longer than what you can write in one shot, split it into logical sections and use write_file with append mode.

### FILE WRITING RULES:
- NEVER use shell commands (cat, echo, tee, heredoc) to write Python files.
- ALWAYS use the write_file tool to write file content. It handles encoding and completeness.
- For large files, use write_file once with the full content.

## 🛠️ @Chatgpt-System Plugin Entegrasyonu
Projenin "God-tier" seviyesine çıkarılması için `@Chatgpt-System` pluginindeki yetenekler `Toolbox` yapısına entegre edilecektir:

1.  **Süreç Yönetimi:** `_process_list` kullanılarak sistem süreçleri üzerinde tam hakimiyet sağlanacaktır.
2.  **Hassas Koordinat Takibi:** `_computer_pointer_position` ile fare konumları gerçek zamanlı ve yüksek hassasiyetle takip edilecektir.
3.  **Oturum ve Yetki Yönetimi:** `_session_authority_status` ve `_session_authority_end` ile yetki döngüleri güvenli hale getirilecektir.

## 🎨 Kodlama Standartları
- **Dil:** Tüm yorumlar ve dokümantasyonlar Türkçe olacaktır.
- **Paradigma:** Fonksiyonel programlama öncelikli olacaktır. OOP sadece dış sistem konnektörleri için kullanılacaktır.
- **Saf Fonksiyonlar:** Fonksiyonlar giriş parametrelerini veya global durumu değiştirmeyecek, sadece yeni değerler dönecektir.
- **Tipleme:** `TypedDict`, `Optional`, `Union` gibi yapılarla katı tipleme (strict typing) uygulanacaktır.
- **Sadelik:** DRY, KISS ve YAGNI prensiplerine sadık kalınacaktır.

## ⚡ Performans Notları
- Bağımsız araç çağrıları `main.py` içinde gerçek paralellikte çalışır (`asyncio.gather`);
  fiziksel fare/klavye eylemleri (`mouse_click`, `keyboard_type`, `cua_click`, `smart_click`,
  `run_action_sequence`) yarış durumunu önlemek için model hangi sırayla döndürdüyse o
  sırayla seri çalışır — aynı anda iki tıklama/yazma asla çakışmaz.
- `run_action_sequence`, çok adımlı fare/klavye zincirlerini ("tıkla → yaz → enter") TEK
  model turunda bitirir; her adım için ayrı LLM round-trip'i gerekmez.
- `pyautogui.PAUSE` 0.1sn'den 0.02sn'ye düşürüldü — uzun `keyboard_type` çağrılarında
  karakter başına binen gizli gecikmeyi azaltır.
- Uzun görevlerde eski araç çıktıları otomatik kısaltılır (`_trim_old_tool_messages`) ve
  10 dakikalık bir zaman bütçesi vardır (`MAX_WALL_CLOCK_SECONDS`) — tam otonom/gözetimsiz
  çalışırken bağlamın sınırsız büyümesini ve döngünün sınırsız sürmesini önler.
- Bu yaklaşımlar `~/Desktop/chatgpt-system`'daki native computer-use runtime'ının ölçülmüş
  tasarım derslerinden (tek seferlik fiziksel eylem hattı, toplu `computer_run`, opt-in
  doğrulama) esinlenildi — kod taşınmadı, sadece prensipler uygulandı.

## 🔀 Çoklu Model Backend'i (config.py: BACKENDS)
- Üç backend tanımlı: `opencode` (varsayılan, qwen3.8-flash, ~2.7sn), `claude`
  (openrouter üzerinden claude-sonnet-5, ~4.2sn, ESCALATION_BACKEND), `openai`
  (gpt-5-mini, doğrudan api.openai.com). Üçü de opencode'un auth.json'ındaki mevcut
  anahtarları kullanır, ayrı bir kimlik bilgisi saklama eklenmedi.
- **Otomatik yükseltme**: aynı API çağrısı başarısız olursa (`_call_model_with_retries`)
  veya aynı görevde art arda `CONSECUTIVE_FAILURE_ESCALATION_THRESHOLD` (2) araç hatası
  olursa, kalan çalışma otomatik olarak `claude`'a geçer.
- **Manuel seçim**: `OMNI_BACKEND=claude` veya `OMNI_BACKEND=openai` ortam değişkeniyle
  bir görev o backend'le başlatılabilir (anahtar yoksa varsayılana düşer, uyarı verir).
- **Bilinen kısıt (2026-09-22 itibarıyla)**: `openai` backend'inin anahtarı var ama
  hesapta kredi yok (429 "no credits remaining") — platform.openai.com'dan kredi
  eklenmeden çalışmaz. `claude` backend'inin openrouter bakiyesi de düşük; bu yüzden
  `max_tokens` 2048 ile sınırlandı (65536 varsayılanı bakiyeyi aşıp 402 hatası
  veriyordu). Bakiye tükenirse aynı 402 hatası tekrar görülebilir.

## 🎯 Hedefler
- [x] `@Chatgpt-System` yeteneklerini `tools.py` içerisine gömmek. (`process_list`, `get_pointer_position`, `session_authority_status/end`)
- [x] Ajanın kendi yetki seviyesini yönetebildiği bir güvenlik katmanı eklemek. (bkz. 🛡️ Güvenlik Rayları — yol koruması, yıkıcı komut engeli, sözdizimi doğrulaması, yedekleme)
- [x] Vizyon ve Koordinat sistemini hibrit hale getirerek tıklama başarısını %100'e yaklaştırmak. (`smart_click`: AX → görsel şablon → koordinat)
- [ ] `main.py` içindeki ajan döngüsünü canlı hedeflerle daha fazla test edip iterasyon/maliyet sınırlarını ayarlamak.
