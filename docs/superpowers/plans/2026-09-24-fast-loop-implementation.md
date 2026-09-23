# OmniAgent Fast Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn OmniAgent into a performance-first computer-use agent that detects semantic stagnation, preserves durable task facts, reserves budget for delivery, and keeps optional voice/memory features safe without slowing normal runs.

**Architecture:** Keep the existing modular monolith and add one pure `fast_loop.py` controller for execution phase, progress fingerprints, bounded task ledger, and metrics. `main.py` remains the orchestrator and calls this controller at turn boundaries; GUI/tool code remains in `tools.py`. Persistent memory mutation becomes a runtime capability, while voice input gains macOS privacy preflight and explicit on-device recognition policy.

**Tech Stack:** Python 3.11+, asyncio, OpenAI-compatible Chat Completions clients, PyObjC Speech/AVFoundation, pytest/pytest-asyncio, existing CustomTkinter UI and local benchmark harness.

**Spec:** `docs/superpowers/specs/2026-09-24-fast-loop-design.md`

## Global Constraints

- End-to-end task latency is the primary quality attribute.
- Preserve correctness, bounded autonomy, privacy, recoverability, and observability.
- Keep the existing modular-monolith shape; do not add a database, queue, planning DSL, mandatory OCR dependency, or general workflow engine.
- Normal runs must not pay voice or persistent-memory mutation overhead.
- Performance changes are accepted only with matched correctness and latency measurements.
- Keep system/tool prefixes stable where practical for provider prompt caching.
- No hidden chain-of-thought persistence.
- User cancel/stop must remain responsive.
- Preserve existing safety rails and current user work.

## Review Focus

- A technically successful GUI action that produces no task-state change must eventually trigger CONSERVE/REPLAN instead of running to the autonomous ceiling; Task 3 tests this.
- A genuine state change after prior stagnation must reset the stagnation window rather than prematurely forcing DELIVERY; Task 3 tests this.
- A normal goal must not be able to mutate persistent memory merely because the model calls `user_memory`; Task 1 tests runtime denial.
- A Python/macOS runtime without required privacy strings must fail voice startup before calling Speech authorization; Task 2 tests this.
- Provider-specific sticky-session metadata must be injected without mutating shared backend profile dictionaries between concurrent runs; Task 5 tests this.

---

### Task 1: Runtime-enforced persistent-memory capabilities

**Files:**
- Modify: `main.py`
- Modify: `tools.py`
- Modify: `config.py`
- Modify: `tests/test_memory.py`
- Modify: `INTEGRATIONS.md`

**Interfaces:**
- Produces: `memory_mutation_requested(goal: str) -> bool`
- Produces: `Toolbox(memory_file: Optional[str], allow_memory_mutation: bool = False)`
- Consumes later: Task 4 uses the same run-level capability construction.

- [ ] **Step 1: Add failing runtime-boundary tests**

Add tests equivalent to:

```python
def test_memory_mutation_intent_is_explicit() -> None:
    assert main.memory_mutation_requested("Bunu hatırla: editörüm VS Code") is True
    assert main.memory_mutation_requested("Hatırladığın editörümü kullan") is False
    assert main.memory_mutation_requested("GitHub'da araştırma yap") is False

def test_memory_mutation_requires_runtime_capability(tmp_path: Path) -> None:
    box = Toolbox(memory_file=str(tmp_path / "user_memory.json"), allow_memory_mutation=False)
    with pytest.raises(ToolError) as denied:
        box.user_memory(action="remember", key="editor", value="VS Code", category="preference")
    assert denied.value.code == "MEMORY_MUTATION_NOT_ALLOWED"

    allowed = Toolbox(memory_file=str(tmp_path / "user_memory.json"), allow_memory_mutation=True)
    result = json.loads(allowed.user_memory(
        action="remember", key="editor", value="VS Code", category="preference"
    ))
    assert result["ok"] is True

def test_memory_recall_remains_available_without_mutation_capability(tmp_path: Path) -> None:
    memory_file = tmp_path / "user_memory.json"
    seeded = Toolbox(memory_file=str(memory_file), allow_memory_mutation=True)
    seeded.user_memory(action="remember", key="editor", value="VS Code", category="preference")
    readonly = Toolbox(memory_file=str(memory_file), allow_memory_mutation=False)
    assert json.loads(readonly.user_memory(action="recall", query="editor"))["preferences"]
```

- [ ] **Step 2: Run tests and verify RED**

Run:
`.venv/bin/python -m pytest tests/test_memory.py -q`

Expected: new tests fail because `memory_mutation_requested` / `allow_memory_mutation` do not exist.

- [ ] **Step 3: Implement minimal runtime capability**

Implement a conservative lexical intent detector for explicit user mutation requests. It is a host capability gate, not a semantic memory extractor. Construct `Toolbox(..., allow_memory_mutation=memory_mutation_requested(goal))`.

In `Toolbox.user_memory`, allow `recall` regardless of the capability but reject `remember` and `forget` when false:

```python
if normalized_action in {"remember", "forget"} and not self._allow_memory_mutation:
    raise ToolError(
        "Bu görev kalıcı hafıza değiştirme yetkisiyle başlatılmadı.",
        "MEMORY_MUTATION_NOT_ALLOWED", False,
    )
```

Do not dynamically grant the capability based on tool arguments.

- [ ] **Step 4: Tighten documentation language**

Document credential detection as best-effort rather than universal. Keep the model prompt instruction, but state that runtime capability is the enforcing boundary.

- [ ] **Step 5: Run memory tests GREEN**

Run:
`.venv/bin/python -m pytest tests/test_memory.py -q`

Expected: all memory tests pass.

- [ ] **Step 6: Commit**

```bash
git add main.py tools.py config.py tests/test_memory.py INTEGRATIONS.md
git commit -m "fix: enforce persistent memory mutation capability"
```

---

### Task 2: Voice privacy preflight and local-only policy

**Files:**
- Modify: `voice.py`
- Modify: `ui.py`
- Modify: `tests/test_voice.py`
- Modify: `INTEGRATIONS.md`

**Interfaces:**
- Produces: `voice_privacy_preflight(bundle_info: Mapping[str, object]) -> None`
- Produces: `VoiceInput(..., require_on_device: bool = True, max_seconds: int = 55)`
- No hot-loop dependencies.

- [ ] **Step 1: Write failing privacy/on-device tests**

Add tests equivalent to:

```python
def test_voice_preflight_rejects_missing_usage_descriptions() -> None:
    with pytest.raises(voice.VoiceInputError, match="NSSpeechRecognitionUsageDescription"):
        voice.voice_privacy_preflight({})

def test_voice_preflight_requires_microphone_description() -> None:
    with pytest.raises(voice.VoiceInputError, match="NSMicrophoneUsageDescription"):
        voice.voice_privacy_preflight({"NSSpeechRecognitionUsageDescription": "Dictation"})

def test_voice_defaults_to_less_than_apple_one_minute_limit() -> None:
    controller = voice.VoiceInput(lambda text: None, lambda state, message: None)
    assert controller.max_seconds <= 55
```

Add a recognizer/request fake verifying:
- local-only mode rejects a recognizer with `supportsOnDeviceRecognition == False`
- local-only mode sets `requiresOnDeviceRecognition = True` before recognition begins.

- [ ] **Step 2: Run voice tests RED**

Run:
`.venv/bin/python -m pytest tests/test_voice.py -q`

Expected: fails on missing preflight/on-device policy and current 120-second default.

- [ ] **Step 3: Implement privacy preflight**

Before any `SFSpeechRecognizer.requestAuthorization_` or microphone permission call, read `NSBundle.mainBundle().infoDictionary()` and verify non-empty:
- `NSSpeechRecognitionUsageDescription`
- `NSMicrophoneUsageDescription`

Fail with a user-facing `VoiceInputError` before calling the framework when missing.

- [ ] **Step 4: Implement local-only request policy**

Default `require_on_device=True`. After recognizer creation:
- check `supportsOnDeviceRecognition`
- if unavailable, raise a clear `VoiceInputError`
- set `request.requiresOnDeviceRecognition = True`

Do not silently fall back to network recognition while documentation says local-only.

- [ ] **Step 5: Reduce recording limit**

Set default/UI max recording duration to 55 seconds to remain below Apple’s documented approximately one-minute SFSpeechRecognizer task limit.

- [ ] **Step 6: Update docs**

State:
- voice is local-only only when on-device recognition is supported
- missing privacy metadata disables voice safely
- no network fallback occurs in local-only mode
- recording duration is capped at 55 seconds.

- [ ] **Step 7: Run voice tests GREEN**

Run:
`.venv/bin/python -m pytest tests/test_voice.py -q`

Expected: all voice tests pass.

- [ ] **Step 8: Commit**

```bash
git add voice.py ui.py tests/test_voice.py INTEGRATIONS.md
git commit -m "fix: harden macOS voice privacy boundary"
```

---

### Task 3: Pure Fast Loop controller and semantic progress

**Files:**
- Create: `fast_loop.py`
- Create: `tests/test_fast_loop.py`

**Interfaces:**
- Produces: `ExecutionPhase = Literal["fast", "conserve", "delivery"]`
- Produces: `FastLoopState`
- Produces: `TurnSignal`
- Produces: `advance_fast_loop(state, signal) -> FastLoopDecision`
- Produces: `normalize_progress_signature(...)->str`
- Consumed by Task 4.

- [ ] **Step 1: Write failing phase-transition tests**

Create tests for:

```python
def test_stagnation_enters_conserve_then_requests_replan(): ...
def test_meaningful_progress_resets_stagnation_window(): ...
def test_second_stagnation_after_replan_enters_delivery(): ...
def test_delivery_stagnation_eventually_requests_bounded_stop(): ...
def test_pressure_threshold_enters_conserve_without_aborting(): ...
def test_equivalent_signatures_do_not_count_as_progress(): ...
def test_ledger_delta_counts_as_progress_even_when_url_is_same(): ...
```

Use tiny thresholds in tests through a `FastLoopPolicy` value; do not monkeypatch wall-clock time.

- [ ] **Step 2: Run tests RED**

Run:
`.venv/bin/python -m pytest tests/test_fast_loop.py -q`

Expected: import/module failure because `fast_loop.py` does not exist.

- [ ] **Step 3: Implement minimal pure controller**

Use dataclasses or TypedDicts with no I/O and no model calls. The controller should track:
- phase
- consecutive stagnant turns
- replan count
- delivery stagnant turns
- last signature
- semantic progress event count
- transition counters

The decision object should expose:
- next phase
- `request_replan: bool`
- `stop_reason: Optional[str]`
- `notice: Optional[str]`

Policy thresholds must be constants/data, not prompt-only behavior.

- [ ] **Step 4: Implement cheap signature normalization**

Use stable JSON/text normalization and SHA-256 over bounded textual fields. Do not hash raw image bytes in this pure layer.

Inputs include normalized:
- successful tool names/arguments
- current URL/app identity if available
- bounded tool-result summaries
- observation digest supplied by the orchestrator
- ledger digest
- unresolved-deliverable count

- [ ] **Step 5: Run controller tests GREEN**

Run:
`.venv/bin/python -m pytest tests/test_fast_loop.py -q`

Expected: all tests pass quickly without network/GUI.

- [ ] **Step 6: Commit**

```bash
git add fast_loop.py tests/test_fast_loop.py
git commit -m "feat: add semantic Fast Loop controller"
```

---

### Task 4: Integrate Fast Loop, task ledger, and delivery mode into orchestration

**Files:**
- Modify: `main.py`
- Modify: `config.py`
- Modify: `events.py`
- Modify: `state_manager.py`
- Modify: `tests/test_core.py`
- Modify: `tests/test_conversation.py`

**Interfaces:**
- Consumes: Task 3 `FastLoopState`, `TurnSignal`, `advance_fast_loop`
- Produces: run metrics for `phase_transitions`, `stagnation_events`, `replans`, `delivery_entries`
- Produces: bounded host-side `TaskLedger` serialization injected after the stable system prefix.

- [ ] **Step 1: Add failing orchestration tests**

Add deterministic model/tool fakes covering:

```python
@pytest.mark.asyncio
async def test_successful_but_useless_tool_turns_trigger_replan_then_delivery(...): ...

@pytest.mark.asyncio
async def test_progress_after_stagnation_prevents_premature_delivery(...): ...

@pytest.mark.asyncio
async def test_delivery_mode_blocks_optional_rediscovery_but_allows_output_tools(...): ...

def test_task_ledger_survives_old_turn_compaction(): ...

def test_run_metrics_include_fast_loop_counters(): ...
```

The fake scenario must return technically successful tool results with identical semantic signature to prove this is not a failure-counter test.

- [ ] **Step 2: Run focused tests RED**

Run:
`.venv/bin/python -m pytest tests/test_core.py tests/test_conversation.py -q -k 'fast_loop or delivery or task_ledger or stagnation'`

Expected: failures because orchestration does not yet consume Task 3.

- [ ] **Step 3: Add bounded TaskLedger**

Keep ledger data factual and non-reasoning. Host code parses compact `STATE:` content when present and augments it with deterministic tool facts already known to the runtime. Bound each section and total serialized size.

Insert the ledger as a dynamic user/context message after stable system/history prefixes rather than changing the invariant system prefix each turn.

- [ ] **Step 4: Feed TurnSignal after each completed tool turn**

Collect:
- normalized tool-call/result digest
- Chrome URL when available
- observation digest from encoded screenshot metadata / bounded image hash
- ledger digest
- unresolved mandatory count where deterministically available

Call `advance_fast_loop` once per tool turn.

- [ ] **Step 5: Implement phase behavior**

FAST:
- current tool set
- existing automatic observations

CONSERVE:
- send one concise host notice
- suppress duplicate observation/navigation where deterministic
- request one REPLAN when decision says so

DELIVERY:
- do not hard-remove a tool if the unresolved goal may still require it unless the controller can prove it optional
- inject a strict delivery instruction emphasizing unresolved output/calculation/verification/cleanup
- prevent known duplicate rediscovery through existing URL/signature caches

If delivery also stagnates to policy limit, terminate with explicit incomplete reason.

- [ ] **Step 6: Replace one-shot soft warning ownership**

Keep token/tool thresholds as pressure inputs into Fast Loop rather than an independent one-shot mechanism. Preserve user-visible pressure notice once per phase transition, not every turn.

- [ ] **Step 7: Run focused tests GREEN**

Run:
`.venv/bin/python -m pytest tests/test_fast_loop.py tests/test_core.py tests/test_conversation.py -q`

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add main.py config.py events.py state_manager.py tests/test_core.py tests/test_conversation.py
git commit -m "feat: integrate delivery-aware Fast Loop"
```

---

### Task 5: Provider session/cache routing without shared-state mutation

**Files:**
- Modify: `config.py`
- Modify: `main.py`
- Modify: `tests/test_core.py`
- Modify: `AGENTS.md`

**Interfaces:**
- Produces: `model_request_overrides(profile, session_id) -> tuple[headers, extra_body]`
- Consumes: existing `session_id` created per run.
- OpenRouter profiles receive request-local `session_id`; OpenCode continues using its configured session header.

- [ ] **Step 1: Add failing request-construction tests**

Add tests that verify:
- OpenRouter/Claude request body gets `session_id`
- original `BACKENDS["claude"]["extra_body"]` remains unchanged
- two session IDs produce independent bodies
- OpenCode header behavior remains unchanged
- `cache_control` remains present for Claude.

- [ ] **Step 2: Run RED**

Run:
`.venv/bin/python -m pytest tests/test_core.py -q -k 'session or cache or request_overrides'`

Expected: failure because OpenRouter `session_id` is not currently added.

- [ ] **Step 3: Implement request-local provider overrides**

Construct fresh dictionaries per request. For OpenRouter-backed profiles, add top-level `session_id` through `extra_body` without modifying the profile. Keep existing `cache_control`.

Do not introduce provider auto-routing or new model dependencies in this task.

- [ ] **Step 4: Run GREEN**

Run:
`.venv/bin/python -m pytest tests/test_core.py -q -k 'session or cache or request_overrides'`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add config.py main.py tests/test_core.py AGENTS.md
git commit -m "perf: add sticky provider sessions"
```

---

### Task 6: Observation deduplication and performance metrics

**Files:**
- Modify: `main.py`
- Modify: `tools.py`
- Modify: `events.py`
- Modify: `state_manager.py`
- Modify: `tests/test_core.py`

**Interfaces:**
- Produces: bounded observation digest (no raw image persistence)
- Produces metrics: `observations`, `observations_reused`, `duplicate_navigation`, `uncached_prompt_tokens`
- Consumed by Task 7 benchmark reporting.

- [ ] **Step 1: Add failing observation tests**

Test:
- identical auto-observation after no semantic progress is not reinjected repeatedly
- changed observation is injected
- explicit user-required final verification can bypass reuse
- metrics count captured/reused observations separately
- observation hashing never stores raw image bytes in run state.

- [ ] **Step 2: Run RED**

Run:
`.venv/bin/python -m pytest tests/test_core.py -q -k 'observation or duplicate_navigation or uncached'`

Expected: new metric/reuse tests fail.

- [ ] **Step 3: Implement bounded observation digest/reuse**

Compute digest from normalized model image bytes or a cheap downsample hash already available at capture time. Store only digest and minimal metadata in runtime state.

Reuse/suppress only when:
- no intervening meaningful state transition occurred
- the action/result signature is equivalent
- the goal does not explicitly demand a fresh revalidation.

- [ ] **Step 4: Expose metrics**

Add counters to run reports/events without changing existing metric meanings.

- [ ] **Step 5: Run GREEN**

Run:
`.venv/bin/python -m pytest tests/test_fast_loop.py tests/test_core.py -q`

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add main.py tools.py events.py state_manager.py tests/test_core.py
git commit -m "perf: deduplicate redundant GUI observations"
```

---

### Task 7: Deterministic long-task benchmarks, documentation, and final verification

**Files:**
- Modify: `benchmark.py`
- Modify: `AGENTS.md`
- Modify: `HANDOFF.md`
- Modify: `CAPABILITIES.md`
- Modify: `INTEGRATIONS.md`
- Test: `tests/test_core.py`, `tests/test_fast_loop.py`, `tests/test_memory.py`, `tests/test_voice.py`

**Interfaces:**
- Consumes all prior tasks.
- Produces benchmark scenarios `long_research` and `stagnation`.
- Produces final measured before/after report fields.

- [ ] **Step 1: Add deterministic benchmark-unit tests**

Test local scenario construction without running the model:
- `long_research` has at least 8 candidates, exactly 5 required selections, calculations, report creation, revalidation, cleanup.
- expected answer/artifact checker rejects missing delivery steps.
- `stagnation` endpoint accepts technically successful actions but exposes no new required fact.

- [ ] **Step 2: Run RED**

Run:
`.venv/bin/python -m pytest tests/ -q -k 'benchmark or long_research or stagnation'`

Expected: new scenario tests fail.

- [ ] **Step 3: Implement benchmark scenarios and metrics**

Extend benchmark output/JSON with:
- uncached prompt tokens
- observations
- observation reuse
- semantic progress events
- stagnation events
- replans
- phase transitions / delivery entries
- duplicate navigation.

Do not make live internet access part of deterministic pass/fail.

- [ ] **Step 4: Run deterministic benchmark-unit tests GREEN**

Run:
`.venv/bin/python -m pytest tests/ -q -k 'benchmark or long_research or stagnation'`

Expected: pass.

- [ ] **Step 5: Update documentation to match actual behavior**

Document Fast Loop phases, runtime memory mutation boundary, local-only voice constraints, provider sticky-session behavior, and benchmark commands. Remove any stronger privacy/security claim than implementation supports.

- [ ] **Step 6: Run full verification**

Run:
```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m py_compile main.py config.py fast_loop.py tools.py voice.py user_memory.py benchmark.py
git diff --check
```

Expected: zero test failures, compile exit 0, diff check clean.

- [ ] **Step 7: Run matched performance benchmarks**

Run the deterministic core benchmark using the same backend/runs as the saved baseline. If environment/API availability permits, also run:
```bash
.venv/bin/python benchmark.py --runs 3 --concurrency 1 --only chrome_ilan --backend opencode
.venv/bin/python benchmark.py --runs 3 --concurrency 1 --only long_research,stagnation --backend opencode
```

Compare success rate first, then median wall time, model turns, tool calls, and uncached tokens. Do not claim a percentage improvement when a matched baseline cannot be reproduced.

- [ ] **Step 8: Commit**

```bash
git add benchmark.py AGENTS.md HANDOFF.md CAPABILITIES.md INTEGRATIONS.md tests
git commit -m "test: add Fast Loop performance benchmarks"
```

- [ ] **Step 9: Whole-branch review**

Use Superpowers requesting-code-review. If no executable subagent tool is available, perform the required separate self-review pass from a generated review package and explicitly record that limitation.

