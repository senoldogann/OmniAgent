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

## 🎯 Hedefler
- [x] `@Chatgpt-System` yeteneklerini `tools.py` içerisine gömmek. (`process_list`, `get_pointer_position`, `session_authority_status/end`)
- [x] Ajanın kendi yetki seviyesini yönetebildiği bir güvenlik katmanı eklemek. (bkz. 🛡️ Güvenlik Rayları — yol koruması, yıkıcı komut engeli, sözdizimi doğrulaması, yedekleme)
- [x] Vizyon ve Koordinat sistemini hibrit hale getirerek tıklama başarısını %100'e yaklaştırmak. (`smart_click`: AX → görsel şablon → koordinat)
- [ ] `main.py` içindeki ajan döngüsünü canlı hedeflerle daha fazla test edip iterasyon/maliyet sınırlarını ayarlamak.
