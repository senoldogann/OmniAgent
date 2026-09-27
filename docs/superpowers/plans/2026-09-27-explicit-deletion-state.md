# Açık yol silme hedefi uygulama planı

**Hedef:** Açıkça belirtilen tek mutlak yol hâlâ mevcutken yanlış silme başarısını
ve erken final iddiasını engellemek.

1. `tests/test_action_evidence.py` içine alakasız başarılı `write_file` çağrısı ile
   hedef yolun durduğu sahte silme finali ekle; eski kodda testin kırmızı olduğunu gör.
   Yol ayrıştırma, kırık sembolik bağ, belirsiz/çoklu yol, erişim hatası ve ortak
   eylem kurtarma bütçesi için hedefli testler ekle.
2. `app/policy.py` içine yalnız bitişik silme fiili + tek açık mutlak yolu çıkaran
   saf fonksiyon ve `lstat` tabanlı son durum kontrolü ekle. `app/agent.py` son
   kararında mevcut eylem kanıtından sonra bu kontrolü uygula; aynı tek kurtarma
   sayacını kullan ve kanıtsız sonucu güvenli final olarak yayımla.
3. Hedefli ve tam test, `compileall`, `git diff --check` çalıştır. Dokuz çekirdek
   senaryoda üçer koşu ve iki kurtarma senaryosunda üçer koşuyla başarı/hızı
   birleşmiş tabanla karşılaştır. AGENTS davranış sözleşmesini, yetenek belgesini
   ve ham ölçümü güncelle. Yalnız kendi dosyalarını commit et, PR ve CI sonrası
   `main`e birleştir, canlı Telegram/UI süreçlerini yeni kodla yeniden başlat.
