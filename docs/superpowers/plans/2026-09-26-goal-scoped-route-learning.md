# Hedef İçi Alternatif Yol Öğrenmesi Uygulama Planı

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Başarısız `fetch_raw` çağrısının aynı URL'yi salt okunur `browse_url` ile okuyarak kurtarıldığı başarılı görevlerden, yalnız aynı sonraki hatada gösterilen gizlilik korumalı ders üretmek.

**Architecture:** Mevcut `experience.py` izleyicisine `fixed_tool` ve çapraz araç dersleri için `target_hash` alanı eklenir. Kaydedilen çapraz ders yalnız sabit araç şablonları ile SHA-256 özetleri taşır. `agent.py` çağrı akışı değişmez: her araç sonucunu zaten `observe_result`'e gönderir.

**Tech Stack:** Python 3.13, `TypedDict`, `pytest`, mevcut atomik JSON deneyim dosyası. Yeni bağımlılık yok.

**Spec:** `docs/superpowers/specs/2026-09-26-goal-scoped-route-learning-design.md`

## Dosya yapısı

| Dosya | Sorumluluk |
| --- | --- |
| `src/omniagent/memory/experience.py` | URL eşlemesi, salt okunur tarayıcı kanıtı, ders adayları, eşleşme, geri bildirim ve eski kayıt göçü. |
| `tests/test_experience.py` | Çapraz araç davranışı ve gizlilik; mevcut aynı araç senaryosunun korunması. |
| `docs/CAPABILITIES.md` | Yeni öğrenme davranışının kısa yetenek açıklaması. |

## Task 1 — Aday eşlemesi ve gizlilik

- [ ] Failing tests: tam URL aynı ve `actions=[]` olan `browse_url` sonucu `URL: <tam URL>` satırı ile boş olmayan sayfa metni içerdiğinde aday oluşur. Farklı URL, yönlendirme, eylem ve boş sayfa metninde aday oluşmaz.
- [ ] `.venv/bin/python -m pytest tests/test_experience.py -q` ile yeni testin kırıldığını doğrula.
- [ ] `pair_route_candidate(failed_arguments, failed_detail, fixed_arguments, fixed_detail)` saf fonksiyonunu ekle. JSON argümanlarını doğrula; HTTP(S) URL'lerini birebir karşılaştır; tarayıcı sonucu `URL`, `Başlık` ve `ÖĞELER` sınırları arasından sayfa metnini ayıkla. Adayda `tool="fetch_raw"`, `fixed_tool="browse_url"`, `target_hash=sha256(url)`, `failed_tokens=[]`, sabit çağrı şablonları olsun. `route_error_key(detail)` tek yardımcı fonksiyon olsun: **kırpılmamış** `normalize_text(detail)` sonucunun SHA-256 özetini `sha256:` önekiyle döndürsün; aday üretimi ve sonraki eşleşme yalnız bu fonksiyonu kullansın. 200 karakterden uzun hata testi ekle.
- [ ] Hedefli testleri çalıştır; yalnız `src/omniagent/memory/experience.py` ve `tests/test_experience.py` dosyalarını commit et.

## Task 2 — Ders yaşam döngüsü

- [ ] Failing tests: `observe_result` başarısız `fetch_raw` ardından geçerli `browse_url` adayını toplar; `finish_task(success=False)` saklamaz, `success=True` saklar. Aynı URL ve hata tekrarlandığında hatırlatma çıkar; farklı sorgulu URL'de çıkmaz. Hatırlatma sonrasında başarılı `browse_url` etki sayacını artırır.
- [ ] Failing tests: eski JSON dersinde `fixed_tool` yoksa eski araç varsayılır; çapraz ders aynı araç dersinin yerini almaz ve sayaçlarını devralmaz.
- [ ] Testleri kırmızı çalıştır.
- [ ] `Lesson` ve `LessonCandidate` tiplerine `fixed_tool`, `target_hash` ekle; eski kayıtları `_lesson` içinde göç ettir. `observe_result` başarılı tarayıcı çağrısında bekleyen `fetch_raw` hatalarını tarasın; aday anahtarına düzeltme aracı ve hedef özetini katsın. `match_lesson` çapraz derste `route_error_key(detail)` ve URL özetini birebir karşılaştırsın, eski derste mevcut benzerliği korusun. `merge_candidates` iki ders türünü ayırsın. **Aynı araç dersinin mevcut `lesson_id(tool,key,tokens)` formülü değişmesin**; yalnız çapraz araç dersine ayrı `route_lesson_id(tool,fixed_tool,error_digest,target_hash)` formülü eklensin. Eski dersin yeniden öğrenildiğinde kimliği ve sayaçları korunarak güncellendiğini test et. Geri bildirim için bekleyen dersin `fixed_tool` değerini kullan.
- [ ] Hedefli testleri yeşil çalıştır; yalnız kendi iki dosyanı commit et.

## Task 3 — Bütünleme ve ölçüm

- [ ] Gerçek `observe_result` sırasını kullanan testte URL query, path ve userinfo içindeki yapay sırların deneyim JSON'unda bulunmadığını doğrula. `browse_url` sonucundaki başlık/araç listesi tek başına içerik sayılmasın.
- [ ] `docs/CAPABILITIES.md` yetenek cümlesini ekle; mevcut aynı araç öğrenme akışını açıklayan satırı koru.
- [ ] `/Users/dogan/Desktop/OmniAgent/.venv/bin/python -m pytest tests/ -q` ve `PYTHONPATH=src /Users/dogan/Desktop/OmniAgent/.venv/bin/python -m compileall -q src/omniagent` çalıştır. Worktree'nin kendi kaynaklarını kullandığını doğrula.
- [ ] `git diff --check` çalıştır. Aynı backend ile `self_repair` benchmark'ında önce/sonra başarı, medyan süre ve araç sayısını karşılaştır; canlı API erişimi yoksa bunu açıkça kaydet.
- [ ] Yalnız kendi dosyalarını commit et; ana çalışma ağacındaki kirli değişikliklere dokunma.
