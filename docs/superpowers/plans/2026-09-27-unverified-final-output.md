# Kanıtsız Final İddiasını Gizleme Uygulama Planı

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Host kanıt kapısının reddettiği final iddiasını UI, CLI ve Telegram'a göstermeden doğru son durumu teslim etmek.

**Architecture:** Korunan görevde model metnini kullanıcı yüzeylerine erken aktarma; araç sonuçları akmaya devam etsin. Final kararından sonra yalnız kabul edilen tamamlanmış metni veya host'un doğrulanamama sonucunu `model_finished` öncesi yayınla. Başarısız görev geçmişine modelin doğrulanmamış `STATE` bloğunu alma.

**Tech Stack:** Python 3.11, asyncio, tipli `AgentEvent`, pytest, mevcut canlı benchmark.

---

### Task 1: Kanıtsız sonucun kullanıcıya sızdığını test et

**Files:**
- Add: `tests/test_final_output_guard.py`
- Test: `tests/test_final_output_guard.py`

- [ ] Sahte modelin `emit({"kind": "text_delta", "text": ...})` çağırmasını sağla; böylece eski kodun metni erken yayınladığı gerçekten gözlenir. `test_no_tool_action_claim_gets_one_recovery_then_fails` testinde `Dosya silindi.` hiçbir `text_delta`/`run_finished.outcome` içinde bulunmasın; final `Doğrulanmadı` desin.
- [ ] Başarısız kaynak yazımı ve yanlış `status='ready'` finali için aynı doğrulamayı ekle. `stream_reset` sonrasında eski parçanın atıldığını; korunan araç turu anlatımı ve `reasoning_delta` iddiasının görünmediğini; kabul edilmiş finalin `model_finished` öncesinde aktığını; modelin açık başarısızlık metninin korunduğunu sınayan ayrı testler ekle. Başarısız görev geçmişindeki sahte `STATE` metnini de ara.
- [ ] `PYTHONPATH=src /Users/dogan/Desktop/OmniAgent/.venv/bin/python -m pytest tests/test_final_output_guard.py -q` çalıştır; yeni doğrulamaların mevcut kodda başarısız olduğunu gör.

### Task 2: Son karar öncesi metin kapısı

**Files:**
- Modify: `src/omniagent/app/agent.py`
- Test: `tests/test_final_output_guard.py`

- [ ] `must_change_source`, `must_execute_action` veya `unmet_wait_status(goal, [])` eşleşiyorsa metin kapısını aç.
- [ ] Model çağrısına verilen olay yayıcısını sar: korunan görevde `text_delta` ve `reasoning_delta` parçalarını kullanıcıya verme; sağlayıcı sıfırlaması ve araç olaylarını geçir. Araçsız finalde tamamlanmış `turn["content"]` metnini yalnız host kabul ederse yayınla.
- [ ] `model_finished` olayını araçsız korunan turda karar sonuna taşı. Kabul edilen metni veya host'un güvenli finalini önce `text_delta` ile, ardından `model_finished` ile yayınla; kurtarma turunda metni atıp istatistiği yayınla.
- [ ] Kaynak yazımı, eylem ve bekleme durumu terminal retlerinde `outcome = 'Doğrulanmadı: ...'` üret; gerçek `tool_calls` gelmeyen korunan görevde de modelin sahte çağrı metnini sonuçta tutma.
- [ ] Korunan başarısız görev geçmişinde `format_ledger_prompt({**host_task_ledger, "model_state": ""})` ile yalnız araç kaynaklı gerçekleri kullan.
- [ ] Hedefli testler geçene kadar düzelt; mevcut bilgi yanıtlarının canlı `text_delta` akışının korunduğunu doğrula. `model_finished` her turda bir kez yayınlansın.

### Task 3: Tam doğrulama ve ölçüm

**Files:**
- Modify: `docs/CAPABILITIES.md`
- Add: `benchmarks/2026-09-27-final-output-guard/README.md` ve ölçüm JSON'ları

- [ ] `PYTHONPATH=src /Users/dogan/Desktop/OmniAgent/.venv/bin/python -m pytest tests/ -q`, `PYTHONPATH=src /Users/dogan/Desktop/OmniAgent/.venv/bin/python -m compileall -q src/omniagent`, `git diff --check` çalıştır.
- [ ] GUI'ye dokunmayan 9 temel senaryoyu `PYTHONPATH=src /Users/dogan/Desktop/OmniAgent/.venv/bin/omniagent-benchmark --runs 3 --concurrency 3 --json /tmp/omni-final-output-core.json` ile ölç. Aynı `ollama-cloud` backend'li önceki [ölçüm](../../../benchmarks/2026-09-26-route-learning/README.md) ve `/Users/dogan/Library/Application Support/OmniAgent/benchmark-results/2026-09-27-merged-main/core.json` ile karşılaştır; en az 27/27 başarı ve yaklaşık 3,5 sn medyan ara.
- [ ] `PYTHONPATH=src /Users/dogan/Desktop/OmniAgent/.venv/bin/omniagent-benchmark --runs 3 --concurrency 1 --only self_repair,stagnation --json /tmp/omni-final-output-recovery.json` çalıştır. Önceki `recovery.json` ile karşılaştır; `stagnation` için 3/3 beklenen başarısızlık olduğunu açıkça kaydet.
- [ ] Sonuçları ve sınırları belgeye yaz, yalnız kendi dosyalarını commit et, PR aç ve CI'yi kontrol et.
