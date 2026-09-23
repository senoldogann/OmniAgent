# OmniAgent Fast Loop — Performance-First Agent Architecture

**Date:** 2026-09-24  
**Status:** Proposed / approved conversationally; written spec pending user review  
**Primary quality attribute:** End-to-end task latency  
**Secondary attributes:** correctness, bounded autonomy, privacy, recoverability, observability

## 1. Intent

OmniAgent should complete real computer-use tasks with the fewest practical model turns, tool calls, screenshots, uncached input tokens, and wall-clock seconds.

The project already has useful primitives: batched GUI actions, automatic post-action observation, provider fallback, prompt compaction, Chrome-session control, bounded run modes, and deterministic benchmarks. The next architecture should turn those primitives into one explicit controller that optimizes for progress per model turn instead of merely allowing longer runs.

The design must preserve user-visible correctness and tool safety. "Fast" does not mean skipping required validation, hiding failures, weakening protected-path checks, or returning early with incomplete deliverables.

## 2. Measurable success

### 2.1 Primary performance metrics

Every benchmark run records at least:

- wall-clock task duration
- model seconds
- tool seconds
- model turns
- tool calls
- prompt tokens
- cached prompt tokens
- uncached prompt tokens
- completion tokens
- screenshot / automatic-observation count
- duplicate navigation count
- semantic-stagnation recoveries
- backend escalations

Performance changes are judged on matched scenarios and the same backend configuration.

### 2.2 Acceptance targets

For the current deterministic core benchmark:

- no regression in pass rate
- median wall-clock must not worsen by more than 5%
- median model turns must not worsen
- uncached prompt tokens must not materially increase

For GUI / Chrome benchmarks:

- target at least 30% reduction in median wall-clock from the pre-Fast-Loop baseline where the scenario is unchanged
- routine GUI flows should normally finish within 6–8 model turns
- repeated observations with no semantic state change should be near zero
- duplicate navigation should occur only for an explicit revalidation requirement or when navigation state changed externally

For adversarial long-task benchmarks:

- the agent must leave open-ended discovery before the run-mode maximum when no semantic progress is being made
- required delivery steps must retain enough budget to finish
- repeated successful-but-useless GUI activity must not bypass progress controls

These are architecture targets, not claims about results before implementation.

## 3. Non-goals

This iteration will not:

- replace the application with a distributed workflow engine
- add a database solely for task state
- add a general planning DSL
- add background workers or queues
- add speculative autonomous self-improvement
- add OCR as a mandatory dependency
- weaken shell/file/GUI safety rails for speed
- force expensive reasoning models on ordinary tool-heavy tasks

The existing modular-monolith shape remains appropriate.

## 4. Fast Loop state machine

Each run has one explicit execution phase:

```
FAST -> CONSERVE -> DELIVERY
          |           |
          +-> REPLAN -+
```

A run begins in **FAST**.

### FAST

Purpose: maximize useful progress per second.

Behavior:

- use the fastest suitable backend
- batch independent reads and compatible GUI actions
- use automatic observation instead of a separate screenshot turn
- permit bounded discovery
- retain normal tool set

Transition to CONSERVE occurs when any pressure signal crosses a threshold, for example:

- semantic progress stalls
- uncached prompt budget reaches the soft threshold
- tool-call budget reaches the soft threshold
- repeated equivalent observations or navigation are detected

### CONSERVE

Purpose: stop spending budget on low-value exploration.

Behavior:

- prohibit optional rediscovery already represented in task state
- aggressively deduplicate equivalent observations
- prefer task-state facts over reopening a page or file
- restrict new discovery unless a required deliverable has an unresolved field
- keep the fastest backend unless the problem is demonstrably reasoning-bound
- request a compact replan focused on unresolved mandatory work

CONSERVE does not end the task merely because a threshold was reached.

### DELIVERY

Purpose: spend remaining budget only on mandatory completion.

Behavior:

- expose or strongly prioritize only tools needed for unresolved deliverables
- permit explicit final verification requested by the goal
- permit report/file creation, calculations, save operations, and cleanup
- reject optional candidate expansion or repeated browsing
- require the final completion check before a tool-free answer

The controller may enter DELIVERY when:

- discovery/selection quotas are satisfied
- required facts are complete
- remaining task work is purely calculation/output/verification/cleanup
- a second stagnation cycle occurs after a CONSERVE replan

### REPLAN

REPLAN is a single model intervention, not a new long-lived phase.

The model receives:

- compact task ledger
- last semantic-progress signature
- unresolved deliverables
- reason progress was considered stagnant
- instruction to choose the shortest remaining path

After REPLAN, the run resumes in CONSERVE or DELIVERY.

## 5. Structured task ledger

The current free-form `STATE:` instruction becomes a small structured ledger controlled by host code.

Conceptual schema:

```text
DONE
- completed mandatory actions

FACTS
- verified durable facts needed later

REJECTED
- candidate -> reason

REMAINING
- unresolved mandatory clauses

DELIVERABLES
- output/file/calculation/cleanup status
```

The ledger is not chain-of-thought. It stores only task facts and completion state that may safely be replayed to the model.

### Ledger invariants

- bounded size
- deterministic serialization order
- no raw screenshots
- no full tool transcripts
- no hidden model reasoning
- verified facts survive context compaction
- rejected candidates survive long enough to avoid reopening them
- optional discoveries can be evicted before mandatory facts
- final answer cannot be declared complete while mandatory ledger items remain unresolved

For the first implementation, the host may derive ledger updates from explicit assistant `STATE:` output plus tool results rather than introducing a complex new protocol. The design should leave room for a typed model-output field later.

## 6. Semantic progress

Tool success is not equivalent to task progress.

A turn receives a normalized progress signature derived from available low-cost signals:

- normalized tool names and arguments
- current URL / application identity where relevant
- normalized tool-result digest
- observation fingerprint
- task-ledger delta
- unresolved-deliverable count

The exact signature implementation must avoid expensive vision inference. Cheap hashing / normalized textual summaries are preferred.

A turn counts as semantic progress when at least one meaningful condition changes, such as:

- a required fact is newly verified
- a candidate moves to VALID/REJECTED/SELECTED
- a deliverable changes state
- a navigation reaches a new required target
- an output artifact is created or corrected
- a mandatory verification completes

A screenshot that looks different only because of animation, cursor movement, clock changes, or loading shimmer does not by itself count as task progress.

### Stagnation policy

- first stagnation window: enter CONSERVE and perform one REPLAN
- second stagnation window with unresolved mandatory work: enter DELIVERY if possible
- if DELIVERY also makes no semantic progress for a bounded window, stop with an explicit incomplete reason instead of consuming the full autonomous budget

The thresholds must be benchmarked. They are configuration constants, not magic behavior hidden in prompts.

## 7. GUI fast path

### 7.1 Action batching

Prefer one model turn that performs a logical unit of work:

- click + select-all + type + submit
- multiple deterministic key presses
- a short action sequence on a stable screen

Do not merge actions whose intermediate state must be observed to choose the next action.

### 7.2 Observation policy

An observation is taken when:

- an action changes screen state and the model needs the result
- the goal explicitly requires visual verification
- the controller cannot determine progress from the tool result alone

Observation is skipped or reused when:

- no screen-changing action occurred
- the previous observation is still valid for the next deterministic action
- the tool result itself proves the requested state
- an equivalent observation fingerprint has already been consumed without intervening semantic progress

### 7.3 Screenshot cost

Screenshot capture and context injection should be measured separately.

Where possible:

- keep capture in-process
- use one common 1000x1000 coordinate space
- avoid file rereads
- delete temporary observations after encoding
- compact old images while retaining durable text facts

## 8. Provider-aware latency routing

The default path remains the fastest validated backend.

Escalation occurs because of evidence, not because a task is merely long.

### Fast backend remains selected when

- tools are succeeding
- semantic progress is occurring
- the task is primarily navigation/extraction/GUI interaction
- errors are argument or UI-state errors the model can cheaply repair

### Escalate reasoning when

- repeated semantic stagnation occurs after a host-level replan
- the model repeatedly produces invalid structured arguments
- a decision requires synthesis not represented in the current state
- a lower-capability backend fails the same recoverable problem beyond the configured threshold

Transient provider/network fallback remains turn-local. It must not permanently move a run onto a slower or lower-output backend.

### Cache stability

System prompt and stable tool prefixes remain byte-stable wherever practical. Dynamic task state should be placed after stable prefix material. Provider-supported session/cache routing may be used where documented and benchmarked, but no provider-specific optimization is accepted without matched latency and correctness measurements.

## 9. Context compaction

Compaction is optimized for **information retained per token**, not raw transcript fidelity.

Retain:

1. system policy
2. current user goal
3. structured task ledger
4. recent turns necessary for current action
5. unresolved error context

Evict or compact:

- old screenshots
- completed tool payloads
- repeated search results
- old successful action arguments
- verbose assistant narration
- tool outputs already represented in the ledger

Compaction must never remove the latest unconsumed tool result.

## 10. Persistent user memory boundary

Persistent memory is separate from task state.

### Default capabilities

Ordinary runs:

- may use task-local ledger
- may receive persistent-memory recall only when the goal genuinely depends on a remembered preference/path/decision
- cannot mutate persistent memory

### Mutation capability

`remember` / `forget` is enabled only when the user explicitly asks to remember, forget, update, or persist a stable preference/path/decision.

The restriction must exist in host/runtime code, not only in the system prompt.

Stored memory remains:

- bounded
- atomic
- local
- excluded from automatic prompt injection
- protected against likely credentials/secrets with documented best-effort detection

Documentation must not claim universal secret detection unless the implementation can support that guarantee.

## 11. Voice input boundary

Voice input is an optional composer input mechanism, not part of the agent execution loop. It must not add latency to non-voice tasks.

Before enabling production use:

- preflight required macOS privacy-description keys
- fail gracefully before requesting permissions if packaging/runtime metadata is insufficient
- if product documentation promises local-only recognition, require on-device recognition support and set the recognition request accordingly
- otherwise document that Apple speech services may process audio remotely
- use a recording-duration limit compatible with the chosen Speech API path
- keep temporary recordings short-lived and delete them after completion/cancel/error

Voice lifecycle tests must cover permission/preflight and recognition-policy decisions without requiring real microphone interaction.

## 12. Benchmark strategy

### Core deterministic suite

Continue existing non-GUI scenarios for correctness and latency regression.

### GUI benchmark

Keep `chrome_ilan` and record:

- success
- wall time
- model time
- tool time
- turns
- screenshots/observations
- uncached tokens
- progress transitions

### Long-research benchmark

Add a deterministic local web benchmark shaped like the failed GitHub research task:

- at least N candidates
- cheap rejection criteria
- exactly K selections
- required calculations
- required file/report output
- explicit revalidation
- cleanup

The local server should expose enough data to verify whether the agent:

- revisits rejected candidates
- performs extra discovery after K valid selections
- omits delivery steps
- exceeds reasonable turns/tool calls

This benchmark avoids relying on live GitHub variability while preserving the decision structure that caused the previous failure.

### Stagnation benchmark

Provide a GUI/page state where actions succeed technically but do not advance the task. Verify:

- FAST -> CONSERVE transition
- one REPLAN
- DELIVERY or bounded failure
- no run to the full autonomous ceiling

## 13. Test strategy

Implementation follows TDD.

Required regression areas:

- phase transitions
- semantic-progress classification
- progress reset on meaningful state change
- successful-but-useless action stagnation
- delivery-mode tool restriction / prioritization
- ledger preservation through compaction
- duplicate navigation and observation suppression
- transient fallback not sticky
- persistent-memory mutation denied without explicit capability
- memory recall not automatically injected
- voice privacy preflight
- local-only voice policy when selected
- benchmark metric accounting

Fast unit tests should use fake clocks and deterministic fingerprints.

Live tests remain opt-in:

- real API
- real Chrome
- real Tk
- real microphone/Speech permission

## 14. Observability

Each run report should make performance diagnosis possible without reading the full transcript.

Add or retain counters for:

- phase transitions
- semantic progress events
- stagnation events
- replans
- observations reused/skipped
- duplicate navigation suppressed/warned
- backend transitions and reasons
- uncached-token pressure
- delivery-mode entry reason

Logging must avoid storing secrets or raw persistent-memory contents.

## 15. Rollout order

### Phase A — correctness boundaries

1. persistent-memory mutation capability
2. voice privacy/runtime preflight
3. voice duration / local-recognition policy

These changes prevent correctness/privacy defects but should not touch the hot agent loop.

### Phase B — semantic controller

1. progress signature
2. structured ledger adapter
3. FAST / CONSERVE / DELIVERY transitions
4. bounded REPLAN behavior
5. stagnation regression tests

### Phase C — GUI latency

1. observation deduplication
2. batch-action opportunities
3. screenshot metrics
4. Chrome benchmark tuning

### Phase D — provider/context latency

1. routing based on semantic evidence
2. compaction improvements
3. provider cache/session optimizations only after measured validation

### Phase E — adversarial benchmark and final review

1. local long-research benchmark
2. stagnation benchmark
3. full deterministic suite
4. opt-in live Chrome/UI runs
5. matched before/after performance report
6. final whole-branch code review

## 16. Design constraints

- preserve existing user changes in the dirty worktree
- implementation should occur in an isolated worktree if practical
- no performance optimization is accepted solely because it reduces code or feels faster
- benchmark correctness and latency together
- do not add heavyweight infrastructure unless measurement shows it is necessary
- no hidden chain-of-thought persistence
- no security/privacy guarantees stronger than the implementation
- user stop/cancel must remain responsive during waits and tool execution

## 17. Decision

Adopt a **performance-first Fast Loop controller inside the existing modular monolith**, rather than adding a general workflow engine or continuing to accumulate independent prompt guards.

The controller makes task progress, budget pressure, delivery state, and provider routing explicit host-level concepts while keeping the model responsible for high-level action selection.

This gives OmniAgent a short path for normal tasks and a bounded path for difficult tasks without paying orchestration overhead on every turn.
