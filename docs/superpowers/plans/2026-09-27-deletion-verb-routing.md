# Silme fiili yönlendirmesi uygulama planı

1. `remove`, `kaldır`, `How to delete` ve `How about you remove` karşıt
   regresyonlarını yaz; eski kodda yanlış başarı/yanlış bilgi ayrımı verdiğini gör.
2. `app/policy.py` eylem ve mutasyon fiillerine `remove`/`kaldır` ekle; açık yol
   kontrolünü `kaldır` için aç. İngilizce yöntem sorusunu `How to`, `How do/can I`
   gibi dar öneklerle ayır; `How about you` eylem kalsın.
3. Hedefli ve tam test, `compileall`, `git diff --check`, üçer koşulu görünmez
   çekirdek/kurtarma benchmark'ları yap. Davranış ve ölçüm kayıtlarını güncelle.
   Kendi dalını PR ile birleştir; canlı Telegram ve UI hizmetlerini temiz `main`
   koduyla yeniden başlatıp doğrula.
