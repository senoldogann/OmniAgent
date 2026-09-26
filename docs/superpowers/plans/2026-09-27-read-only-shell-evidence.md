# Salt okunur Git eylem kanıtı uygulama planı

**Hedef:** `git status` gözlemiyle mutasyon hedefinin yanlış başarıya ulaşmasını önlemek.

1. `tests/test_action_evidence.py` içinde başarılı `execute_shell(git status --short)`
   ardından gelen sahte “dosya silindi” finalini uçtan uca yeniden üret. Eski kodda
   testin başarısız olduğunu gör. Saf `git status`, `git status; rm ...` ve
   yönlendirme için sınıflandırma testlerini ekle.
2. `app/policy.py` içindeki `_obviously_read_only_shell` kontrolüne yalnız `git status`
   segmentini dahil et. Zincirdeki başka komut veya yönlendirme varsa mevcut
   ihtiyatlı davranışı koru.
3. Hedefli test, tam test, `compileall` ve `git diff --check` çalıştır. Dokuz çekirdek
   senaryoyu üçer kez ve `self_repair,stagnation` senaryolarını üçer kez canlı ölç;
   önceki ham kayıtlarla başarı ve süreyi karşılaştır. Davranışı ve ölçümü belgeye
   kaydet, kendi dalını PR ile birleştir, canlı hizmetleri temiz `main` koduyla
   yeniden başlatıp süreçleri doğrula.
