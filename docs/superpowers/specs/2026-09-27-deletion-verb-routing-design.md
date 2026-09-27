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
- İngilizce yöntem sorusu (`How to ...?`, `How do/can I ...?`) eylem emri
  değildir; bilgi önekine yalnız bu dar kalıplar eklenir. `How about you
  remove ...?` gibi kibar eylem isteği kanıt kapısını açmaya devam eder.
- Kaynak kodu silme yönlendirmesi bu tasarımın dışında tutulur: mevcut kaynak
  kapısı yalnız başarılı `write_file`/`edit_file` yazımını kanıt sayar; tüm dosya
  kaldırma için ayrı araç kanıtı sözleşmesi gerekir.

## Doğrulama

Sahte modelin `remove` ve `kaldır` hedeflerinde araçsız “silindi” demesi eski
kodda başarı, yeni kodda güvenli başarısızlık vermelidir. `git status` gözlemi
silme kanıtı olmamalı; gerçek hedef kaldırıldığında mevcut yol kapısı başarıya
izin vermelidir. Türkçe/İngilizce bilgi soruları canlı metin akışını korumalıdır.
`How about you remove ...?` ile `How to delete ...?` karşıt testleri de
çalıştırılır. Tam test ve görünmez canlı hız/doğruluk ölçümü yapılır.
