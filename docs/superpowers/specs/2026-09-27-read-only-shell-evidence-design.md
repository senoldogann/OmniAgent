# Salt okunur Git komutu için eylem kanıtı tasarımı

## Sorun

`has_action_evidence`, bir mutasyon hedefinde başarılı `execute_shell` çağrısını kanıt
saymadan önce yalnız küçük bir salt okunur komut listesini eler. `git status --short`
bu listede olmadığından, “dosyayı sil” hedefinde sadece depo durumuna bakıldıktan sonra
modelin “silindi” yanıtı başarı kabul edilebilir. Son yanıt kapısı host'un başarı
kararına dayandığı için bu yanlış kararı düzeltemez.

## Tasarım

Kabuk komutu segmentleri mevcut `shell_command_words` ayrıştırıcısıyla incelenir.
İlk sözcüğü `git`, ikinci sözcüğü `status` olan segment yalnız **mutasyon kanıtı
değerlendirmesinde** gözlem kabul edilir. Diğer mevcut salt okunur programlar aynı
şekilde kalır. Zincirde herhangi bir başka komut, yönlendirme veya komut ikamesi
varsa tüm çağrı mutasyon ihtimali taşıyan kanıt olarak kalır; böylece gerçek yazma
girişimleri sessizce elenmez. `git` komutunun diğer alt komutları genelleştirilmez.

Bu host kontrolü dosyanın gerçekten silinip silinmediğini evrensel olarak kanıtlamaz.
Ama yalnız `git status` gözlemiyle yanlış başarı kararını önler. Başarısız kararda
mevcut tek kurtarma turu ve `Doğrulanmadı` teslimi kullanılır; yeni model turu veya
farklı araç şeması eklenmez.

## Doğrulama

- Önce bir regresyon testi `git status` sonucu ardından sahte “silindi” finalinin
  eski kodda başarı kabul edildiğini gösterir.
- Birim testleri salt `git status`, `git status; rm ...` ve yönlendirmeyi ayırır.
- Hedefli ve tam test, `compileall`, `git diff --check`, canlı çekirdek/kurtarma
  ölçümleri çalıştırılır. Başarı ve süre önceki ham kayıtlarla karşılaştırılır.
