# Hedef İçi Alternatif Yol Öğrenmesi

**Tarih:** 2026-09-26  
**Durum:** Kullanıcı, keşfin yalnız verilen hedef içinde yapılmasını seçti.  
**Amaç:** Bir araç yolu başarısız olduğunda aynı hedefe ulaşan farklı ve doğrulanmış bir araç yolunu sonraki benzer hatada hatırlatmak.

## Mevcut durum ve sınır

`memory/experience.py` aynı araçta başarısız çağrı ile değiştirilmiş başarılı çağrıyı eşler. Başarıyla biten görevde dersi saklar ve yalnız aynı hata tekrarlandığında gösterir. Farklı araca geçişler kaydedilmez. `feat/completion-contract` dalındaki zorunluluk defteri ve kurtarma merdiveni görev içi yönlendirmeyi sağlar; bu tasarım o dalın dosyalarını değiştirmez.

İlk kapsam web okumasıdır: `fetch_raw` başarısız olup **aynı tam HTTP(S) URL** `browse_url` ile okunursa ve görev başarılı biterse alternatif yol öğrenilir. Tarayıcı çağrısı `actions=[]` olmalı, dönen son URL istenen URL ile birebir eşleşmeli ve dönen temiz sayfa metni boş olmamalıdır. `browse_url` çıktısı başlıkla içeriği `SAYFA METNİ:` sınırıyla ayırır; çok satırlı başlık içerik sayılmaz. Bu koşullar sayfanın gerçekten hedef bilgiye sahip olduğunu tek başına kanıtlamaz; ders yalnız o yolu denemenin işe yaradığına dair bir ipucudur. Hedef URL'si farklıysa, yönlendirme varsa, görev başarısızsa veya tarayıcı çağrısı da başarısızsa ders oluşmaz.

Tam URL, sorgu ve fragment dahil, yalnız çalışma sırasında karşılaştırılır. Kalıcı `target_hash` SHA-256 özetidir; sonraki hatırlatma aynı tam URL'nin özetini gerektirir. Çapraz araç dersinin `failed_call` ve `fixed_call` alanları yalnız `fetch_raw(<aynı hedef URL>)` ve `browse_url(<aynı hedef URL>, actions=[])` şablonlarını taşır. `error_key` normalize edilmiş hatanın SHA-256 özetidir; `failed_tokens` boş listedir ve çapraz araç eşleşmesinde kullanılmaz. Böylece hata metni, URL'nin hiçbir parçası ve sırlar kalıcı dersin hiçbir alanına yazılmaz.

## Veri akışı

1. `observe_result` başarısız `fetch_raw` çağrısını görev içi izleyicide tutar.
2. Başarılı `browse_url`, son başarısız `fetch_raw` çağrılarından URL'si birebir eşleşeni arar ve ders adayı üretir.
3. `finish_task(success=True)` adayı mevcut atomik deneyim dosyasına ekler. Mevcut eski dersler yüklenirken `fixed_tool` alanı yoksa başarısız aracın adı varsayılır; `target_hash` olmayan eski dersler eskisi gibi eşleşir.
4. Sonraki görevde aynı araç, hata özeti ve **tam hedef URL özeti** eşleşirse ders hata sonucuna eklenir: hangi araca geçileceği açıkça yazılır. Aynı araç düzeltmesi ile farklı araç yolu ayrı derslerdir; aday anahtarı, birleştirme ve ders kimliği `fixed_tool` ile `target_hash` alanlarını da kullanır. Sayaçlar farklı dersler arasında taşınmaz.
5. Hatırlatılan alternatif aracın sonraki turdaki **ilk çağrısı** ancak aynı URL'de `actions=[]` ile dolu sayfa metni dönerse `helped` sayılır. İlk çağrı farklı URL'ye gider veya eylem içerirse `helped=False` kaydedilir; daha sonraki çağrı bunu başarıya çeviremez. Aynı araç derslerinde mevcut geri bildirim davranışı korunur. Mevcut budama eşiği korunur.

Bu genişleme yeni model çağrısı, çevrimiçi keşif veya otomatik yan etkili işlem başlatmaz. Model mevcut izinli araç listesinden karar verir. Tarayıcı yolu kullanılamıyorsa ipucu bunu aşamaz; mevcut host araç kapısı geçerlidir.

## Kabul ölçütleri

- Aynı URL'de başarısız `fetch_raw` → salt okunur, aynı son URL ve dolu metin döndüren başarılı `browse_url` → başarılı görev bir ders üretir.
- Farklı URL, yönlendirme, boş metin, tarayıcı eylemi, başarısız görev ve hatalı ikinci çağrı ders üretmez.
- Eski deneyim dosyası yüklenir; aynı araç düzeltme davranışı korunur.
- Kalıcı ders URL sorgusunu, tam hata metnini ve sırları içermez; farklı sorgulu aynı endpoint'e hatırlatma çıkmaz.
- Çapraz araç dersi aynı araç dersinin yerini almaz ve onun etki sayacını devralmaz.
- Hatırlatma yalnız eşleşen hata sonrasında görünür; alternatif araç başarılıysa etki sayacı artar.
- Tam test paketi, `git diff --check` ve `compileall` temizdir; `self_repair` ölçümünde doğruluk ve medyan süre regresyonu olmaz.
