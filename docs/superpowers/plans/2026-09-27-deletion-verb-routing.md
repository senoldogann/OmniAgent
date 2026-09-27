# Silme fiili yönlendirmesi uygulama planı

1. `remove`, `kaldır` ve kibar `How about you remove` uygulama isteği
   regresyonlarını yaz; eski kodda yanlış başarı verdiğini gör. İngilizce
   yöntem sorusu, yöntem sorusu ardından silme ve farklı eylem karşıtlarını ekle.
2. `app/policy.py` eylem ve mutasyon fiillerine `remove`/`kaldır` ekle; açık yol
   kontrolünü `kaldır` için aç. İngilizce yöntem sorusu ile sonraki açık emri
   ayır; eylem kanıtı ve yol kontrolünü aynı kapsamla değerlendir.
3. Hedefli ve tam test, `compileall`, `git diff --check`, üçer koşulu görünmez
   çekirdek/kurtarma benchmark'ları yap. Davranış ve ölçüm kayıtlarını güncelle.
   Kendi dalını PR ile birleştir; canlı Telegram ve UI hizmetlerini temiz `main`
   koduyla yeniden başlatıp doğrula.
