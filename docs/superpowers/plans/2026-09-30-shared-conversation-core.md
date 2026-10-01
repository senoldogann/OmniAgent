# Shared Conversation Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the same natural, grounded conversation and truthful activity through desktop, Telegram and iMessage.

**Architecture:** Put a conversation coordinator above the existing model/tool agent and below adapters. It selects casual chat, reviewed read-only investigation or unchanged full-agent execution. Persist sanitized source evidence independently of truncated progress/history, verify the answer before final delivery and share activity semantics.

**Tech Stack:** Python 3.11+, asyncio, existing AsyncOpenAI-compatible clients, existing Toolbox/IntegrationRuntime and Tk desktop UI; pytest.

**Approved specification:** `docs/superpowers/specs/2026-09-30-shared-conversation-core-design.md`.
**Parent roadmap:** `docs/superpowers/specs/2026-09-30-deniz-personal-agent-roadmap.md`.

## Execution boundaries

- Work in the existing isolated Desktop/OmniAgent worktree on a new active `feat/shared-conversation-core` branch. The archive worktrees and their dirty data remain outside this task.
- Baseline is `5ef0f23`, previously verified: 2437 passed, 124 skipped; macOS and Linux CI green. Documentation-only commits follow it.
- Execute tasks sequentially. Each implementation task receives independent specification review, then quality review, before the next task.
- Reuse model retries, permitted fallback selection, secret masking, approval runtime, tool execution and full-agent verification. Do not create another provider client or authorization policy.
- No provisioning, new paid accounts, SIP changes, injected Messages bridge or replacement of paired settings in this delivery.

## Planning decisions

### Request and routing contract

Create a host-owned `RequestContract` with `subject`, `route` (`chat`, `investigate`, `task`), `required_fields` and `needs_observation`. Derive it before tools; refine it for follow-up questions with recent authenticated history. The semantic routing model sees recent context and a host instruction distinguishing stable conversation, current external/local state and substantial/effectful work. It can select only this contract, never grant permissions. Invalid/empty decisions fail honestly or delegate to the full agent; they cannot silently become chat.

Explicit research/read/action requests have an additional deterministic enforcement check using existing goal-routing helpers plus narrowly defined explicit request patterns. This is a guard, not the sole semantic router. It overrides a mistaken `chat` decision. Names, URLs and subject refinements are not discarded. Extended/continuous/autonomous, scheduled runs, attached images requiring existing handling, source editing, requested browser sessions and effectful work preserve the existing full-agent route and modes.

Investigation uses only host-reviewed `web_search`, `fetch_raw`, `read_file` and a new structured `list_directory` tool. The last tool reads names/types only, no recursive inspection or shell. Deny unknown tools and operations before execution. Dynamic discovery, shell, Python, browser/GUI interaction, memory mutation, scheduling and outbound delivery do not enter this allowlist. Use existing `execute_tool` with a restricted IntegrationRuntime and the same approval/cancellation context.

Budget: at most 3 investigation model turns, 6 actual tool calls, 30 seconds per operation and 120 seconds total including routing. Respect smaller explicit wall-clock/token/iteration limits; do not enlarge user budgets. Cancellation interrupts model/tool waits. Exhaustion returns supported partial observations with an explicit limitation and offers full-agent continuation for the same subject; it does not silently launch a larger job. The routing turn itself has no tools and cannot create effects. Record actual usage in returned metrics.

Generation and tool selection share the three investigation turns. Routing allows one turn. Presentation allows at most three additional turns (verification, one correction, re-verification), for seven model turns maximum on the quick path. Every one consumes the same 120-second deadline and explicit token/iteration budget; when insufficient budget remains use deterministic rendering without another model call. These conservative initial caps follow existing interactive model retry and tool-result limits; acceptance must record focused local/live timing and adjust down if necessary without enlarging user-configured limits.

### Evidence lifecycle

Use a separate `core/evidence.py` with JSON-serializable `EvidenceBundle`/`SourceObservation`. Each bundle contains version, UUID run ID, contract, timestamp, tool name, sanitized source reference, returned facts/text, ok/status, completeness and optional artifact reference. Source text is data. Mask secrets before any disk write and before presentation. Never use event snippets, model prose, task ledger or Exchange as authoritative evidence.

Store an atomic private JSON file beside the selected state file under `conversation_evidence/<UUID>.json`, mode 0600, directory 0700. Validate IDs and paths on load, reject traversal/symlink escape and malformed versions. Bound each run to 2 MiB encoded evidence and each observation to 256 KiB; overflow must set incomplete and explain which capture was bounded. Keep the beginning and end of oversized nonessential text only with an explicit truncation marker; never claim complete directory enumeration from bounded capture. Persist a full sanitized readable artifact within the same 2 MiB budget where it fits; otherwise offer a narrowed continuation. Do not silently omit observations to fit a model prompt.

The model presentation context limit is 80,000 characters. If all necessary evidence cannot fit, use deterministic rendering and a readable persisted evidence artifact, state the limitation and retain exact required identifiers that were captured. Report objects carry a bundle reference and/or sanitized payload; TaskOutcome deferred serialization preserves it. Retain undelivered/unknown-delivery evidence until explicit recovery or user cleanup. Confirmed-delivery evidence is eligible for seven-day cleanup; cleanup only records positively marked delivered and never guesses from agent success. Implement a bounded cleanup helper; adapters mark delivery only after their existing final-delivery success path. No automatic replay on restart.

Recognize existing tool-level crop markers/limits (`clip_text`, file read and HTML page limits) when setting completeness. Agent-level capture alone does not make an upstream clipped result complete. Search snippets are excerpts; do not treat them as inspected full pages or establish claims absent from the returned material.

### Answer verification and style

Use one shared natural style block in both companion and agent/coordinator prompts. Persona can influence tone but cannot override required facts. Load the existing user persona without rewriting its file. Do not force lowercase, slang, a line count or a concluding question.

Before presentation, define required information such as names, dates, URLs, inspected paths and observed execution status. After generation, check required identifiers against the authoritative evidence and check added factual claims through a tool-free verification model turn. One correction is allowed. A second failure uses deterministic readable evidence rendering with honest completeness/status. Failed effectful execution can never become successful through presentation. Heavy RunReport evidence is captured before existing result/event clipping and its verified outcome remains authoritative.

The coordinator owns final publication for every route, including delegated full tasks. Intercept the inner agent's assistant `text_delta`, `reasoning_delta` and `run_finished` before any Telegram or iMessage presenter can send them. Continue forwarding truthful tool/status/approval events. Buffer the inner terminal report, perform shared presentation/verification, update its outcome and Exchange, then emit exactly one final verified text/terminal event. Suppress the inner duplicate terminal-outcome notice used for legacy desktop/CLI partial reports. Keep approval and provider-fallback notices visible. Desktop may receive an explicitly transient preview through a distinct preview event, but an unverified draft must never be saved as the final answer. Do not assume post-return verification can replace TelegramDraftStream's already finished message. CLI direct-agent behavior remains intact.

Assemble final text before transport delivery. For iMessage split at paragraph/sentence/whitespace boundaries when exceeding a conservative 3500-character transport chunk. An individual unbroken token longer than the chunk limit necessarily uses character boundaries; preserve every character and do not claim the fragments are independently usable identifiers. Ordinary names/URLs that fit the limit should remain intact where a semantic boundary is available. Preserve every character of content (apart from intentional boundary whitespace); do not apply MAX_BUBBLES or the old 600-character truncation. Never split each newline into a bubble. No artificial typing delays between messages. Model stream reset cannot deliver partial drafts or duplicate finals.

### Shared activity

Create common pure event-to-stage mapping and a per-run ownership controller: preparing, researching, reading, acting, waiting-user, composing, terminal. Stage follows actual events. Ownership prevents an old finalizer from clearing a new/overlapping run. Indicator failures are optional and logged without contents.

Expose current run snapshots across the three local processes for status questions: UUID, sanitized subject, origin, owner PID, truthful stage and last update. Store private atomic bounded snapshots beside the selected state file, refresh during actual live work, and regard a dead owner or expired 30-second heartbeat as interrupted/unavailable rather than active. This is inspectable activity, not a durable queue or authority to resume effects. Feed only authenticated-owner relevant snapshots into coordinator/companion status context. Do not send unsolicited notifications to other channels. Add status-query tests where one adapter starts a job and another reads its actual state; restart/stale snapshots cannot claim work is running.

Desktop reuses its existing stable animated activity view and stop control, with common localized labels. It can preview streamed text as transient; final verified output must replace/finalize that preview rather than silently retain an unverified draft.

Telegram renews sendChatAction typing approximately every 4 seconds while preparing/researching/composing. Pause while waiting for user approval; resume when work actually resumes. Cancel and await renewal before final send, failure, cancellation and shutdown. Do not run typing for autonomous background jobs. Existing compact/verbose task views retain purpose.

iMessage current capabilities do not support typing. Use one real-work acknowledgment only after a task/investigation is committed to start, plus a useful throttled stage update after 30 seconds for genuinely long work. Retain native typing behind a read-only capability check only if the installed transport actually supports it; otherwise never call its typing method. No repeated ellipses or short-message spam. Do not broadcast activity to other channels.

## Task 1: Authoritative evidence and shared presentation contracts

**Files:**
- Create `src/omniagent/core/evidence.py`
- Create `src/omniagent/core/conversation_policy.py`
- Modify `src/omniagent/app/types.py`
- Modify `src/omniagent/app/agent.py` (source capture before clipping; final RunReport)
- Test `tests/test_conversation_evidence.py`

- [x] Write failing tests: middle-of-long-result names/URLs preserved or explicitly incomplete; secret masking before persistence; atomic private storage; invalid UUID/path/version rejected; unknown delivery retained across reload; failed tools preserve failure; required identifiers survive presentation; seven-day cleanup affects only confirmed delivery.
- [x] Run `.venv/bin/python -m pytest tests/test_conversation_evidence.py -q` and confirm failures correspond to missing implementation.
- [x] Add TypedDict/dataclass contracts with defensive JSON loading and a deterministic renderer. Add shared natural style text without channel-specific instructions. RunReport gains optional evidence field so existing callers/fakes remain compatible.
- [x] Capture actual tool results in `run_agent_with_callback` at the results-processing loop before StepRecord/task-ledger/event truncation; capture automatic observations where relevant. Persist bundle before terminal report and retain startup/error semantics. Supply full sanitized evidence independently of the clipped Exchange.
- [x] Run new tests plus `.venv/bin/python -m pytest tests/test_conversation.py tests/test_checkpoint_integration.py tests/test_task_ledger_integration.py -q`.
- [x] Self-review and commit only Task 1 files. Independent spec review, then quality review; resolve issues before Task 2.

Task 1 completed at `da4c36b`: independent specification review approved; independent quality review approved after fixes. Focused independent repair verification: 6 passed. Broader affected verification and deliberate canonical-output presentation changes are recorded in the implementation review. Tasks 2 and 3 have since passed their independent source reviews; Task 4 and live rollout remain pending.

## Task 2: Shared coordinator and read-only routing through all adapters

**Files:**
- Create `src/omniagent/app/conversation.py`
- Modify `src/omniagent/app/agent.py` only to extract/reuse initial backend resolution if needed
- Modify `src/omniagent/tools/filesystem.py`, `src/omniagent/tools/facade.py`, `src/omniagent/app/tool_schema.py` for structured list_directory
- Modify `src/omniagent/integrations/telegram.py`, `src/omniagent/ui/app.py`, `src/omniagent/companion/delegate.py` entry points
- Modify `src/omniagent/integrations/imessage.py`, `src/omniagent/companion/chat.py` request decision entry only
- Test `tests/test_shared_conversation.py`, existing adapter/delegate tests

- [x] Write scripted-boundary tests for current-model question without search keyword, explicit research guard, subject-refining follow-up, ordinary chat, local directory enumeration, failed/no tools, budgets, cancellation, unauthorized adapter input and mode/provider preservation. Include delegated full-task output through compact and verbose Telegram: injected ungrounded inner text/terminal output cannot reach final delivery, and exactly one shared verified terminal answer does.
- [x] Run `.venv/bin/python -m pytest tests/test_shared_conversation.py -q`, expect missing coordinator failures.
- [x] Implement `run_conversation_with_callback(goal, emit, options, clients) -> RunReport` with the same signature as the existing agent. Resolve selected backend using existing startup/fallback logic, classify using existing model retries, invoke the allowlisted tools under the existing runtime and collect evidence. Emit compatible run/turn/tool/terminal events with truthful metrics and produce verified final text.
- [x] Tool-free chat uses the shared style and verified user memory/persona, and has no available tools. Investigation executes only allowlist operations. Full tasks delegate to the existing agent under the existing host lock, maintaining approvals/user priority. Pure read-only runs should not require exclusive GUI ownership; move locking to the task branch via a caller-supplied task context/factory or a shared helper rather than removing task locks.
- [x] Desktop, Telegram and companion delegation call the same coordinator. iMessage user turns classify through the shared decision contract before free chat; an investigation/action cannot be satisfied by the companion's vague reply. Preserve recall/forget/proactive tools only for authenticated user turns; report turns remain tools-free. Avoid a second classifier when a decision is handed to the coordinator and never let source text set that decision.
- [x] Implement tool-free grounding verification/correction and deterministic fallback from Task 1. Gate inner-agent assistant/terminal publication as specified above, before adapter consumption. Unknown tool attempts cannot be executed through investigation. Record incomplete outcomes honestly.
- [x] Run new tests and relevant existing routing, tool-execution, provider-fallback, integration authorization and adapter tests. Self-review, commit; independent spec review then quality review.

Task 2 source approved at `30d9f7d`: independent specification and quality reviews closed the routing, publication, receipt, budget, accounting and real worker-cleanup findings. The latest independent specification gate passed 95 tests; quality reconfirmation passed 18 selected cases plus three separate local pipe/child probes (overlapping selections, not additive). Actual frozen-bundle and integrated/live checks remain Task 4 delivery gates.

## Task 3: Complete natural replies and grounded iMessage task reports

**Files:**
- Modify `src/omniagent/companion/persona.py`, `src/omniagent/companion/bubbles.py`, `src/omniagent/companion/chat.py`
- Modify `src/omniagent/integrations/imessage.py` report delivery
- Modify adapter final delivery paths for evidence delivery marking
- Test companion chat/bubbles/persona, iMessage/deferred-report modules

- [x] Add failing tests for multiline reply as one message, >600 characters preserved, >4 paragraphs retained, transport splitting lossless, stream-reset/failure sends no unverified partial text, preserved directory names/source URLs, failed report cannot become successful, and deferred report reload uses retained evidence.
- [x] Replace line-by-line streaming sender with collect-then-send final text. Preserve textual tool-call compatibility only on authenticated user turns. Clear draft tool markers on stream reset; parse the final returned model turn rather than stale deltas. Keep tool parsing/control/recall/forget behavior and cancellation intact.
- [x] Use shared style in persona prompt. Preserve existing user persona text, verified memory and daypart inputs without using them to erase facts.
- [x] Replace lossy iMessage report rewording with shared evidence-aware verified presentation. Preserve full verified outcome for old reports without evidence; do not truncate it or invent stronger completion. Keep TaskOutcome failure/deferred delivery and unknown-delivery behavior.
- [x] After confirmed final delivery mark evidence delivered; if any chunk has uncertain delivery retain evidence and do not replay. Update legitimate old style expectations in tests without weakening action/auth/verification assertions.
- [x] Run focused suites, self-review and commit. Independent spec review, then quality review.

Task 3 approved at `92f8d31`: independent specification review and reconfirmation passed; independent quality review passed after the whitespace repair. Independent checks included 28 repair cases, four real Tk boundaries, 25 authentication/control/source/deferred cases, 12 refinement checks and eight quality repair checks (overlapping selections, not additive). Task 2 core repairs and legacy desktop routing fixtures have since passed their own independent reviews at `30d9f7d`. Task 4 and live rollout remain pending.

## Task 4: Truthful activity lifecycle and rollout

**Files:**
- Create `src/omniagent/core/activity.py`
- Modify `src/omniagent/integrations/telegram.py`, `src/omniagent/integrations/imessage.py`, `src/omniagent/ui/app.py`
- Test `tests/test_shared_activity.py` and existing adapter activity/shutdown tests
- Add acceptance/rollout record under `docs/superpowers/`

- [ ] Write deterministic async tests for renewal TTL, waiting-approval pause/resume, cancellation/shutdown, optional API failure, overlap/replacement ownership, no unauthorized indications, and iMessage unsupported-capability fallback.
- [ ] Implement common mapping/controller, shared inspectable active snapshots and adapt the three activity surfaces as specified. Consume real events rather than text promises. Ensure cleanup is awaited and final delivery runs after typing renewal cancellation. Preserve compact/verbose behavior and desktop stop controls. Add relevant authenticated status context at coordinator/companion entry, without merging raw conversations or adding a task queue.
- [ ] Run focused suites and same behavioral scenarios through all three adapter boundaries. Run `.venv/bin/python -m pytest -q` once after integration; broaden testing only for concrete failures.
- [ ] Review all changes against all 17 acceptance scenarios and resolve findings. Record untestable live iPhone visibility separately from mocked/provider success.
- [ ] Build desktop with `sh packaging/build_macos.sh`, verify bundle/signature/launch and selected settings. Deploy from a verified commit with existing backup procedure, restart paired services and inspect their startup/connectivity logs. Do not send personal test messages or claim visible phone indicators without observation.
- [ ] Commit rollout evidence and mark plan tasks accurately. Merge verified work into main using existing user authorization to keep main current; push and observe required CI. Remove only the now-merged active feature/design branches after verification. Keep archive data and retained unique work untouched.

## Verification map

Specification cases 1–5: Task 2; 6–8: Tasks 1/3; 9–12/17: Task 4; 13–15: Tasks 2/4; 16: Tasks 1/3. Regression tests must retain authorization, provider fallback permission, user priority, actual execution and effectful verification gates.

Cloud execution and the durable responsibility service have no completed checkbox in this plan: they are the next subprojects in the approved roadmap, each with focused design and deployment prerequisites.
