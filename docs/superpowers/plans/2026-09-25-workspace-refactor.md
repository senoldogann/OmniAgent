# OmniAgent Workspace Refactor Implementation Plan

> **For agentic workers:** Execute inline in this session. Every behavior change follows RED→GREEN TDD; structural moves are gated by import/smoke tests before and after each batch.

**Goal:** OmniAgent'i okunabilir bir `src/omniagent` paketine dönüştürmek, gerçek mantık sorunlarını düzeltmek ve çalışma dizinini kaynak/runtime ayrımı net olacak şekilde temizlemek.

**Architecture:** Önce paket ve path sözleşmesini kur, sonra düşük-bağımlı core modüllerinden dışa doğru taşı. Agent döngüsü en son bileşenlere ayrılır; böylece her aşamada test edilebilir bir sistem korunur. Telegram servis yolu yalnız final paket doğrulamasından sonra değiştirilir.

**Tech Stack:** Python 3.11, uv, pytest, OpenAI Python SDK, macOS PyObjC, launchd.

**Spec:** `docs/superpowers/specs/2026-09-25-workspace-refactor.md`

## Global Constraints
- Dirty worktree resetlenmeyecek; ilgisiz kullanıcı değişiklikleri korunacak.
- Python kaynakları shell heredoc/cat/tee ile yazılmayacak.
- `.omni_backups` ve kullanıcı verileri silinmeyecek.
- Her mantık düzeltmesi önce başarısız testle gösterilecek.
- Refactor sonunda Telegram launchd servisi yeniden kurulup doğrulanacak.

## Review Focus
- Paket taşındıktan sonra monkeypatch/import semantiğinin testlerde ve runtime'da aynı kalması.
- Varsayılan veri yollarının kaynak dizine geri düşmemesi ve eski veriyi ezmemesi.
- Model retry/fallback ile tool dispatch'in ContextVar temizliğini her hata yolunda koruması.
- GUI otomatik gözlem/bitiş doğrulamasının taşıma sırasında araç sırasını değiştirmemesi.
- Telegram install-service'in çalışan eski servisi güncel paket koduyla gerçekten yeniden yüklemesi.

---

### Task 1: Paketleme ve giriş noktası sözleşmesi
**Files:** `pyproject.toml`, `src/omniagent/__init__.py`, `tests/test_package_layout.py`

- [ ] RED: Paket giriş noktalarının ve kökte üretim modülü bırakılmaması hedefinin testini ekle.
- [ ] GREEN: build backend, src package ve script entry point tanımlarını ekle.
- [ ] `uv sync` + package import smoke testi.

### Task 2: Canonical path ve kalıcı veri katmanı
**Files:** `src/omniagent/paths.py`, core state/checkpoint, memory modülleri, ilgili testler.

- [ ] RED: state writer eksik parent dizini oluşturmalı; varsayılan state/checkpoint yolları Application Support altında olmalı.
- [ ] GREEN: canonical path API ve güvenli legacy migration helper'ı.
- [ ] Core state, checkpoint, conversation, events, fast-loop, task-ledger modüllerini taşı.
- [ ] Hedefli test + tam suite.

### Task 3: Config, bellek ve host policy
**Files:** `src/omniagent/config.py`, `platform/macos/api_keys.py`, `memory/*`, approval modülü ve testler.

- [ ] RED: sistem promptunun gerçek host approval davranışıyla çelişmediğini test et.
- [ ] GREEN: prompt/policy tutarsızlığını gider ve import döngülerini azalt.
- [ ] Deneyim/user-memory/config modüllerini sorumluluklarına taşı.
- [ ] Hedefli test + tam suite.

### Task 4: Entegrasyon katmanı
**Files:** `src/omniagent/integrations/*`, Telegram testleri.

- [ ] RED: Telegram launchd plist ProgramArguments kaynak dosyasına değil paket modülüne/entry point'e işaret etmeli.
- [ ] GREEN: capabilities, MCP, Outlook ve Telegram'ı integrations altında taşı; launchd kurulumunu idempotent güncelleme yapacak hale getir.
- [ ] Telegram/Outlook/MCP testleri + suite.

### Task 5: Araç katmanı
**Files:** `src/omniagent/tools/*`, araç testleri.

- [ ] RED: bulunan somut araç edge-case'leri için testler (exception chaining davranışı, strict coordinate/input uzunluğu, process cleanup).
- [ ] GREEN: Toolbox'ı `facade.py`ye ayır; `__init__.py` sadece re-export.
- [ ] Kullanılmayan importlar ve üretim bare-except temizliği.
- [ ] Araç testleri + suite.

### Task 6: Agent runtime'ını bileşenlere ayır
**Files:** `src/omniagent/app/{agent,model_runtime,routing,tool_execution,verification}.py`, agent testleri.

- [ ] RED: public agent API ve önemli monkeypatch davranışlarını package yolunda sabitle.
- [ ] Tool schema/routing, tool execution, model retry ve GUI verification'ı ayrı modüllere çıkar.
- [ ] Üretim `assert` kontrollerini explicit exception yollarına çevir.
- [ ] `run_agent_with_callback` yalnız orchestration/state-machine sorumluluğuna indir.
- [ ] Core/action/fallback/verification testleri + suite.

### Task 7: macOS platform, UI ve geliştirme araçları
**Files:** `platform/macos/*`, `ui/*`, `dev/benchmark.py`, UI/voice/benchmark testleri.

- [ ] Platforma özgü screen/OCR/voice/permissions/desktop/headless kodunu taşı.
- [ ] UI ve markdown modüllerini `ui/` altında topla; büyük settings/event bölümlerini davranış korunarak helper'lara ayır.
- [ ] Benchmark'ı `dev/` altına taşı ve script giriş noktasını doğrula.
- [ ] Hedefli test + suite.

### Task 8: Workspace temizliği, dokümantasyon ve final deployment
**Files:** README, AGENTS, docs, gitignore ve legacy runtime dosyaları.

- [ ] Root kaynak dosyalarını kaldır; canlı runtime kalıntılarını canonical data root'a migrate et veya kayıpsız legacy alana taşı.
- [ ] README dizin haritasını ve komutları yeni entry point'lerle güncelle.
- [ ] AST + Ruff odaklı audit, compileall, diff-check, tam pytest.
- [ ] Benchmark smoke.
- [ ] Telegram launchd servis bootout → reinstall → kickstart; PID/log/Bot API doğrulaması.
