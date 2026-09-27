# Silme fiili yönlendirmesi

## Sorun

`delete /tmp/hedef` eylem kanıtı kapısından geçerken eş anlamlı `remove /tmp/hedef`
geçmiyor; Türkçe `kaldır: /tmp/hedef` de aynı şekilde atlanıyor. Açık yol son durum
kontrolü `remove` için yolu çıkarabilse bile `must_execute_action=False` olduğundan
çalışmıyor. Bu ifadelerle gelen sahte tamamlanma yanıtı başarı sayılabilir.

## Tasarım

- `remove` ve `kaldır`, eylem ve mutasyon niyeti için mevcut fiil listelerine eklenir.
  Böylece salt okunur araçlar bu hedeflerde silme kanıtı sayılmaz ve mevcut güvenli
  final akışı açılır.
- Açık yol silme ayrıştırıcısı `kaldır` fiilini de `sil/delete/remove` ile aynı
  dar bitişiklik kurallarıyla tanır. Yolun son durum kontrolü ve ortak tek kurtarma
  turu değiştirilmez.
- Türkçe bilgi sorusu öneki korunur. `How to ...?` ve `How can/do I ...?`
  biçimindeki İngilizce yöntem sorusu tek başına eylem sayılmaz. Soru sonrası
  `Please remove it`, `Could you remove it` veya `Please do it` gibi açık emir
  varsa eylem sayılır. Mutasyon ve açık yol kontrolü de bu emrin kapsamında
  kalır: `How to remove /tmp/a? Please open the terminal` dosya silme isteği
  değildir. Soru ve emirde tekrarlanan aynı yol tek hedef sayılır; sonraki emir
  “öteki dosya” derse sorudaki yol ona bağlanmaz. `remove /tmp/a from README`
  dosyanın kendisini silme sayılmaz; `remove the file at /tmp/a` sayılır.
  `How about you remove ...?` doğrudan uygulama isteğidir.
- Kaynak kodu silme yönlendirmesi bu tasarımın dışında tutulur: mevcut kaynak
  kapısı yalnız başarılı `write_file`/`edit_file` yazımını kanıt sayar; tüm dosya
  kaldırma için ayrı araç kanıtı sözleşmesi gerekir.

## Doğrulama

Sahte modelin `remove` ve `kaldır` hedeflerinde araçsız “silindi” demesi eski
kodda başarı, yeni kodda güvenli başarısızlık vermelidir. `git status` gözlemi
silme kanıtı olmamalı; gerçek hedef kaldırıldığında mevcut yol kapısı başarıya
izin vermelidir. Türkçe/İngilizce bilgi soruları canlı metin akışını korumalıdır.
Kibar uygulama isteği ve gerçek dosya yolu için karşıt testler çalıştırılır.
Tam test ve görünmez canlı hız/doğruluk ölçümü yapılır.
