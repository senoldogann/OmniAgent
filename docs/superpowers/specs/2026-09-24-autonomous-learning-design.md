# OmniAgent Autonomous Recovery & Learning Design

**Date:** 2026-09-24
**Status:** Approved by user for autonomous implementation
**Primary quality attribute:** End-to-end task completion with low wall-clock latency
**Secondary attributes:** bounded self-recovery, durable learning, user memory, human approval for consequential actions

## Goal

OmniAgent should execute user goals autonomously, diagnose recoverable failures, try a corrected approach, verify the correction, remember only verified lessons, reuse those lessons when the same failure recurs, and preserve enough state to continue an interrupted task. It may execute local task code/tools as part of recovery. Persistent user memory and financial actions remain explicit host-controlled boundaries.

## Existing foundations

- Fast Loop phases and bounded stagnation.
- Real shell/JS/file/web/GUI tools.
- Structured STATE ledger carried through failed runs.
- Persistent user memory with secret filtering.
- Experience memory that pairs failed tool calls with verified successful corrections.
- Host-side approval classification and audit log for financial actions and unrequested memory mutation.

## Design rules

1. **Verified learning only.** A lesson is persisted only when a task contains a failed call, a related corrected call succeeds, and the overall task succeeds.
2. **Failure-conditioned recall.** Experience hints are injected only after a matching failure recurs, never globally at task start.
3. **No blind self-modification.** Recovery code is task-local shell/JS or explicit file edits requested by the goal. OmniAgent does not silently rewrite its own source while running a user task.
4. **Human approval boundary.** Financial transfers/orders/payments and unrequested persistent-memory mutation require host approval before execution. The model cannot waive this.
5. **Resume from durable state.** Incomplete runs return their ledger; a later "devam et"/resume goal should reuse confirmed FACTS and REMAINING work rather than restart discovery.
6. **Visible Chrome resilience.** If Chrome Apple Events are unavailable or hang, OmniAgent falls back to the user's visible Chrome UI using accessibility keyboard input. It must not open a separate browser profile.
7. **Speed first.** Recovery must be bounded by Fast Loop; learning should reduce future turns/tool calls. No heavy workflow engine/database is added.

## Acceptance evidence

- Existing autonomy/approval/experience tests remain green.
- A deterministic self-repair benchmark succeeds on the first run through exploration and on the second matching run with an experience hint; the second run must not use more tool calls than the first.
- A failure cannot create an experience lesson.
- A repeated matching failure can surface a learned corrected call while unrelated tasks receive no lesson.
- Chrome AppleScript timeout falls back within a short timeout and subsequent Chrome calls in the same run skip AppleScript.
- Real Chrome benchmark can start even when AppleScript is unresponsive.
- Full suite, real Tk tests, live model test, and stress benchmarks remain green.
