# OmniAgent Autonomous Recovery & Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove and harden OmniAgent's autonomous recovery, verified learning, resumability, and visible-Chrome resilience without slowing normal tasks.

**Architecture:** Keep the existing modular monolith. Reuse Fast Loop for bounded execution, `experience.py` for verified failure-conditioned learning, `approval.py` for host approval, and task-local tools for recovery. Add deterministic benchmark contracts rather than a new workflow framework.

**Tech Stack:** Python, asyncio, pytest, existing OpenAI-compatible providers, PyObjC/pyautogui, local HTTP/CLI benchmark fixtures.

**Spec:** `docs/superpowers/specs/2026-09-24-autonomous-learning-design.md`

## Global Constraints

- Wall-clock task latency remains the primary quality attribute.
- Preserve all concurrent/user changes in this worktree.
- No database/workflow-engine dependency.
- No hidden chain-of-thought persistence.
- Financial and unrequested persistent-memory mutations remain host-approved.
- New behavior is implemented RED → GREEN.

## Review Focus

- AppleScript hangs while Chrome itself is alive: visible UI fallback must work and must not repeatedly pay the timeout.
- A failed task must not persist an experience lesson.
- A similar but unrelated tool failure must not receive another task's lesson.
- A resume request must preserve prior FACTS/REMAINING instead of restarting.
- Approval/audit output must not leak secrets.

---

### Task 1: Visible Chrome fallback and benchmark harness

**Files:**
- Modify: `tools.py`
- Modify: `benchmark.py`
- Modify: `tests/test_core.py`
- Modify: `tests/test_benchmark.py`

**Interfaces:**
- Produces: per-`Toolbox` AppleScript health circuit breaker; visible Chrome fallback via `open -a` + keyboard.
- Benchmark setup/cleanup uses the same visible-user-profile principle.

- [ ] Write failing tests for AppleScript timeout fallback and same-run circuit breaker.
- [ ] Run focused tests and confirm RED.
- [ ] Implement short AppleScript timeout + visible UI fallback.
- [ ] Write failing benchmark-harness fallback tests.
- [ ] Implement benchmark open/cleanup fallback without touching non-test apps.
- [ ] Run focused tests, full core tests, then one real `chrome_ilan` run.

### Task 2: Deterministic autonomous self-repair benchmark

**Files:**
- Modify: `benchmark.py`
- Modify: `tests/test_benchmark.py`
- Reuse: `experience.py`, `main.py`

**Interfaces:**
- Produces: `self_repair` stress scenario that executes a local CLI fixture twice using one experience-memory file.
- First run must recover from an intentional E17-style syntax failure and persist one verified lesson.
- Second run must encounter the same initial failure, receive the learned hint, succeed, and use no more tool calls than the first run.

- [ ] Write failing scenario-contract tests.
- [ ] Run them and confirm RED.
- [ ] Add local executable fixture + two-stage benchmark flow.
- [ ] Run contract tests GREEN.
- [ ] Run the live `self_repair` benchmark three times and record turns/tools/hints.

### Task 3: Resume, memory, and approval integration proof

**Files:**
- Modify only if a test exposes a gap: `main.py`, `user_memory.py`, `approval.py`, `state_manager.py`
- Modify: `tests/test_autonomy.py`, `tests/test_approval.py`

**Interfaces:**
- Existing incomplete exchange answer carries STATE ledger.
- Existing history/user-memory rendering supplies durable context to the resumed run.
- Host approval remains the execution gate.

- [ ] Add a failing test that a "devam et" run with an incomplete previous exchange receives the prior STATE ledger in model context and keeps the same remaining work.
- [ ] Add/retain tests proving denied/unavailable approval executes no consequential call and audit summaries are redacted.
- [ ] Implement only gaps exposed by RED tests.
- [ ] Run autonomy/approval/experience test group GREEN.

### Task 4: Whole-branch verification and measurements

**Files:**
- Update: `AGENTS.md`, `docs/HANDOFF.md`, `docs/CAPABILITIES.md` with measured results only.

- [ ] Run full pytest with skip reasons.
- [ ] Run real Tk UI tests.
- [ ] Run live API test.
- [ ] Run core, long_research, stagnation, self_repair benchmarks.
- [ ] Run py_compile/compileall and git diff --check.
- [ ] Perform whole-branch code review; fix Critical/Important findings with RED→GREEN.
- [ ] Record measured numbers and remaining uncertainties.
