# Deniz Phase C: Heartbeat and Autonomy Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan task by task. The existing feature checkout and approved companion spec authorize execution; the parent coordinates bridge and memory integration.

**Goal:** Enable evidence checked proactive conversation and guarded autonomous work, with reliable user preemption and morning reports.

**Architecture:** The bridge owns lifecycle, message delivery and tasks; heartbeat owns scheduling, snapshots, deterministic permissions and model decision validation. Autonomous runs use the existing agent with typed guards and the host lock. All state remains in the existing SQLite store.

**Tech Stack:** Python asyncio, SQLite, flock, PyObjC Quartz/AppKit, existing model runtime and approval audit.

---

### Task 1: Host priority and presence

**Files:** `src/omniagent/platform/macos/host_lock.py`, new `src/omniagent/platform/macos/presence.py`, `tests/test_companion_autonomy.py`.

- [x] Implement typed owner metadata and targeted preemption requests, retaining ordinary nonblocking locks.
- [x] Add sync user preemption and an async wrapper that acquires in a worker thread; release always happens after acquisition even if cancelled.
- [x] Read HID idle, session lock and foreground app only; fail closed on missing presence signals. GUI gate polls every 30 seconds through a stop sensitive runtime.
- [x] Verify real subprocess preemption, stale request handling, user owner refusal and cancellation during GUI waiting.

### Task 2: Agent guards

**Files:** `src/omniagent/app/types.py`, `src/omniagent/integrations/runtime.py`, `src/omniagent/app/agent.py`, `src/omniagent/app/tool_execution.py`, `src/omniagent/approval.py`.

- [x] Define AutonomyGuards with GUI gate/input marker, guarded roots, quiet hour callback and deferred approvals.
- [x] Pass guards into runtime; reject unattended combined with autonomy; autonomous tasks have no wall/iteration/token limit while retaining normal finish behavior.
- [x] Require approvals before conventional deletion commands, camera capture, source tree writes and explicit deletion goals.
- [x] Ensure autonomous approvals never use continuous bypass, quiet hours defer without sending, and every autonomous tool result is audited.
- [x] Verify denial prevents side effects; ordinary user work remains compatible.

### Task 3: Delegate lifecycle

**Files:** `src/omniagent/companion/delegate.py`, `tests/test_companion_delegate.py`, `tests/test_companion_autonomy.py`.

- [x] Add origin and mandatory rationale; automatic jobs use preemptible lock and a stop callback incorporating request checks.
- [x] Preserve existing user call signatures and never set unattended.
- [x] Carry deferred approvals and origin into outcomes, and add a report only lesson helper using memory backend.
- [x] Verify failed, stopped and successful jobs can all yield lessons and reports through bridge integration.

### Task 4: Heartbeat

**Files:** new `src/omniagent/companion/heartbeat.py`, `tests/test_companion_heartbeat.py`.

- [x] Implement quiet hours, scheduling with follow up deadlines, 30 day rhythms, allowed actions and snapshot collection.
- [x] Build one decision call with filtered tools; reject missing/multiple/malformed calls and missing rationale.
- [x] Verify grounds deterministically against active facts, then use memory backend to reject unsupported claims. Revalidate hard rules after every model wait.
- [x] Persist wake time, unanswered count, and FIFO reports. Flush reports outside quiet hours or after direct user input; remove before delivery to avoid uncertain retries.
- [x] Provide async tick/run/flush_reports and queue_report APIs to bridge; check consent, conversational silence and active work before every send/start.

### Task 5: Verification and handoff

- [x] Run `uv run python -m pytest tests/test_companion_autonomy.py tests/test_companion_heartbeat.py tests/test_companion_delegate.py -q`.
- [ ] Parent runs entire suite after integrating bridge and store APIs and verifies live service without sending unsolicited user messages during development.
- [ ] Commit only owned code, tests and this plan with a focused feature commit; report API and material limits to parent.

## Verification evidence

- 2026-09-30: 59 new autonomy/heartbeat cases pass; expanded guard, delegate, approval and input regressions pass (359 cases).
- Parent handles final bridge/store integration, complete suite and live service rollout. No unsolicited real message was sent during development.
