# Verified File Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Açık yerel dosya silme, taşıma ve düzenleme hedeflerinde yanlış başarıyı önlemek.

**Architecture:** `app/file_delivery.py` hedef ayrıştırma, başlangıç/son durum ve kısa işlem makbuzunu tutar. `app/agent.py` bu sözleşmeyi görev başında kurar, tam araç çağrısı/sonucuyla günceller ve finalde tek ortak kurtarma hakkıyla değerlendirir. Telegram ve Tk mevcut olay akışını tüketir.

**Tech Stack:** Python 3.11, pytest, `pathlib`, `hashlib`, mevcut `ToolCallDraft`/`ToolResult` ve benchmark CLI.

---

## Dosya haritası

- Oluştur: `src/omniagent/app/file_delivery.py` — dar hedef dilbilgisi, `FileContract`, dosya durum özeti, araç makbuzu ve saf/yerel final kararı.
- Değiştir: `src/omniagent/app/agent.py` — görev başlangıcı, şema seçimi, tam çağrı makbuzu, final kapısı ve ortak teslim kurtarma hakkı.
- Değiştir: `src/omniagent/app/tool_schema.py` yalnız gerekirse — `edit_file` şemasının sıradan metin dosyası düzenlemesinde açılması.
- Oluştur: `tests/test_file_delivery.py` — ayrıştırma, durum ve sahte modelle final karşıtları.
- Değiştir: `tests/test_telegram_bridge.py`, `tests/test_ui_conversation.py` — gerçek son kararın sahte kanal uçtan uca teslimi.
- Değiştir: `src/omniagent/dev/benchmark.py` — ayrı `file_delete`, `file_move`, `file_edit` canlı senaryoları ve son durum denetimi.
- Oluştur: `benchmarks/2026-09-27-verified-file-delivery/` — ham ölçüm ve önceki sürüm karşılaştırması.

### Task 1: Yanlış başarı ve doğru başarı matrisi

**Files:** Create `tests/test_file_delivery.py`; read `tests/test_explicit_deletion.py`, `tests/test_action_evidence.py`.

- [ ] Başlangıçta var olmayan dosyada `rm -f`, alakasız `write_file`, yalnız bir hedefin silinmesi, `mv` ile silmiş gibi yapma, yanlış hedefe taşıma, önceden mevcut hedefe karşı kaynak silme ve değişmeyen içerik karşıtlarını yaz.
- [ ] Gerçek silme, çoklu/bağıl silme, dosyayı dizine taşıma, sembolik bağı bağ olarak taşıma ve farklı içerikle düzenleme olumlu testlerini yaz.
- [ ] Yöntem sorusu, soru ardından emir, README içi yol referansı ve sembolik bağ düzenlemesini sınır testlerine ekle.
- [ ] `pytest tests/test_file_delivery.py -q` çalıştır; yeni yanlış başarı testlerinin eski kodda gerçekten başarısız olduğunu kaydet.

### Task 2: Dosya sözleşmesi ve durum özeti

**Files:** Create `src/omniagent/app/file_delivery.py`; test `tests/test_file_delivery.py`.

- [ ] `FileContract` içinde `kind: Literal["delete", "move", "edit"]`, çözülmüş `sources`, isteğe bağlı `destination`, başlangıç durumlarını tanımla. Ayrı `UnsupportedFileIntent` işareti tanımla; dosya işlemi niyeti açık olup hedefi çözülemeyen görev `None` (normal görev) gibi işlem görmesin. Yol ayrıştırması yalnız `_action_scope` içindeki açık emirde çalışsın; yöntem sorusunun yolu yalnız sonraki `it` emrine bağlansın.
- [ ] Yerel yol için `os.path.abspath` ile bağıl yolu leksik olarak çöz; `http(s)` ve belirsiz token'ı reddet. `lstat` ile linki takip etmeden varlığı ölç; düzenli dosyada düzenleme için mevcut `FILE_READ_MAX_BYTES` (8 MiB), taşıma için 64 MiB sınırında akışlı SHA-256 al. Sembolik bağ için `os.readlink` kimliği al; özel/okunamayan dosyayı belirsiz say.
- [ ] `capture_file_contract(goal, cwd)` ve `file_delivery_gap(contract, receipts)` fonksiyonlarını ekle. Silmede hedeflerin **hepsi başlangıçta mevcut** ve bitişte yok olmalı; her hedefe `delete` makbuzu bağlı olsun. Taşımada ilk kaynak özeti son hedef özetiyle eşleşsin, kaynak yok olsun; hedef bir dizinse `destination/source.name` etkin hedefini kontrol et ve etkin hedefin başlangıçta yokluğunu ara. Düzenlemede hedefin ilk/son içerik özeti farklı olsun ve o hedefe `edit` makbuzu bulunsun.
- [ ] Hedefli testleri yeşile getir; `python -m compileall -q src` çalıştır.

### Task 3: Tam araç makbuzu ve host finali

**Files:** Modify `src/omniagent/app/agent.py`; test `tests/test_file_delivery.py`.

- [ ] Tam `ToolCallDraft` ile başarılı `ToolResult` çiftinden `FileReceipt(kind, source, destination)` üret. Kabukta yalnız doğrudan ayrıştırılan `rm`/`unlink`/`mv`; `write_file`/`edit_file` için yalnız kendi `path` argümanı makbuz oluştursun. Kırpılmış `StepRecord`'a dayanma.
- [ ] Sözleşmeyi veya `UnsupportedFileIntent` işaretini görev başında kur; `guarded_final_output` ve metin dosyası düzenleme için `allow_edit` kararına kat. Kalite basamağı sonrası şema yenilemesinde de aynı kararı kullan.
- [ ] Dosya sözleşmesi varsa finalde `file_delivery_gap` uygula; desteklenmeyen dosya niyeti de mevcut genel eylem kanıtını geçip başarı sayılmasın. Kaynak/eylem/dosya kapıları tek ortak kurtarma hakkı kullansın; başarısız final `Doğrulanmadı:` ile başlasın.
- [ ] Bütünleşik sahte model testlerini yeşile getir; mevcut `test_action_evidence.py`, `test_explicit_deletion.py`, `test_delete_verb_routing.py` testlerini çalıştır.

### Task 4: Kanal teslimi

**Files:** Modify `tests/test_telegram_bridge.py`, `tests/test_ui_conversation.py`.

- [ ] Sahte Telegram API üzerinde yanlış “silindi” model metninin dışarı akmadığını ve tek nihai `Doğrulanmadı` balonunu doğrula; gerçek `run_agent_with_callback` kullan.
- [ ] `OMNI_UI_TEST=1` ile gerçek Tk bileşeninin `_send_goal` → `_run_exclusive` worker yolundan gerçek runner'ı çalıştır; sahte model ve yalnız yerel geçici dosya kullanarak `_tick` sonrası ekranda yalnız doğrulanmış sonucu ara.
- [ ] Kanal testlerini, sonra tüm `tests/` paketini çalıştır.

### Task 5: Canlı değerlendirme ve hız

**Files:** Modify `src/omniagent/dev/benchmark.py`; create `benchmarks/2026-09-27-verified-file-delivery/README.md`, `core.json`, `recovery.json`, `file.json`.

- [ ] Üç geçerli yerel dosya senaryosu için `--only file_delete,file_move,file_edit --runs 3 --concurrency 3` çalıştır; her koşuda gerçek dosya son durumunu kontrol et. Negatif yanlış başarı sayısı test matrisinden ayrı raporlansın.
- [ ] Görünmez çekirdek 27 koşu ve kurtarma 6 koşuyu tekrarla. Önceki `benchmarks/2026-09-27-deletion-verb-routing/` kaydıyla başarı, medyan model/araç/duvar sürelerini karşılaştır.
- [ ] Hız farkı ölçülürse yalnız somut yerel darboğazı düzelt; model servis gecikmesini kod kazancı diye sunma.
- [ ] `compileall`, `git diff --check`, tam test ve `OMNI_UI_TEST=1` testini tamamla; belgeleri ve ham ölçümleri commit et.

### Task 6: Birleştirme ve canlı doğrulama

**Files:** Update `AGENTS.md`, `docs/CAPABILITIES.md`, benchmark README.

- [ ] Ayrı daldan PR aç ve bu oturuma bağla; Linux/macOS CI sonucu geçince birleştir.
- [ ] Temiz `/Users/dogan/Desktop/OmniAgent-live` ana dalını ileri sar; görev kilidi boşken Telegram ve UI hizmetlerini yeniden başlat.
- [ ] Birleştirme sonrası tam testi çalıştır; hizmet PID'lerini, köprü kilidini ve OmniAgent penceresini doğrula. Gerçek Telegram ağına mesaj gönderilmediyse bunu açıkça bildir.
