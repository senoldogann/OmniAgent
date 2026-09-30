# Shared conversation, evidence and activity across channels

Date: 2026-09-30
Status: Proposed design for user review; implementation has not started.

## Objective

Desktop, Telegram and iMessage should share the same decisions about answering, researching, inspecting local state and performing a task. Replies should answer the actual request, preserve useful evidence and use natural length. Each channel should show honest activity while work is happening.

This extends the user's approved companion work. It addresses the user's observed failures: a specific current-model question received vague conversation, a desktop listing became generic personal commentary, and normal replies were split into several small messages.

The user selected shared natural conversation style and capabilities across all three channels. Layout adapts to each surface; conversation quality, evidence and activity behavior remain shared.

## Observed causes

- The companion prompt requires 1–4 short lines, each a separate message. Its streaming sender dispatches completed lines before the turn is complete.
- The companion can delegate to the existing agent but does not enforce research for current information questions. A broad AI research job ran a real `web_search`; the later narrower OpenAI/Claude question did not trigger another research job.
- The desktop-file request delegated successfully and ran `execute_shell ls -F ~/Desktop`. Its detailed result contained directory names and categories. The companion report presentation replaced that detail with four informal comments and a personal follow-up question.
- Telegram and desktop currently enter the agent directly, while iMessage adds a separate conversational and report presentation layer. Changing only that layer would leave behavior inconsistent across channels.

## Chosen approach and alternatives

Recommended: one shared conversation coordinator with a quick conversational path, a bounded read-only research path and delegation of substantial/effectful tasks to the existing agent. All channel adapters consume the same evidence and activity events.

Alternative: always use the full agent loop for every message. This gives a simple routing model but adds latency and cost to casual conversation.

Alternative: adjust companion prompts and message splitting only. This is small but leaves research decisions and evidence preservation dependent on free-form model compliance.

## Shared request and execution contract

The coordinator belongs below channel adapters and above the existing model/tool runtime. It receives an authenticated user turn, recent conversation context, verified shared memory, current job state, selected profile and run-mode settings, and attachment references prepared through existing channel authorization.

It selects a conversational response, bounded read-only investigation, or an agent task. Routing combines explicit user instructions with a semantic decision for implicit current-state questions; a keyword list alone is insufficient. Questions such as “Have OpenAI and Claude released new models?” require current sources even without “search” in the wording. Questions about current desktop directories require a real local read.

Explicit search/read/action requests cannot be satisfied by a conversational response alone. Their result requires a tool receipt or an honest unavailable/error result. The selected route, requested subject and observed result are recorded. Model promises do not count as execution.

Follow-up turns resolve their subject from conversation context. “No, give me their names” refines the preceding model question. It must not create a new general AI-news question. An ambiguous target receives one necessary clarification.

The coordinator reuses existing tool authorization, approval, model retry/fallback, audit and cancellation behavior. A bounded read-only path exposes only an explicit host-maintained allowlist of reviewed operations, with operation-level classification when a tool can both read and mutate. Cacheability flags, model declarations and shell completion heuristics do not establish read-only status. It does not execute arbitrary shell or GUI actions through this path. Unknown or effectful operations delegate to the existing agent.

Quick investigation has bounded model/tool iterations, per-tool timeouts and a total deadline, configured centrally. Exhaustion reports what was obtained. It may continue through the full agent for the same requested subject under unchanged authorization and the user's selected mode/budget limits; otherwise it returns the partial result and offers continuation. All adapters use this same rule; they cannot silently extend budgets, expand the subject or invent results. Implementation planning must select those budgets based on existing runtime constants and focused live measurements.

Explicit extended/continuous/autonomous modes retain their full-agent execution and completion contracts. They are not silently converted to a quick conversational route. Effectful host work retains the cross-process host lock and user priority. Read-only investigation must not generate GUI input or bypass a tool's approval requirements.

Existing provider/model selections are preserved per channel. Shared behavior does not silently assign a different backend. Missing capabilities or denied fallback produce an explicit error.

## Evidence and answer presentation

Derive the answer contract before collecting or discarding evidence, then refine it when the user clarifies the request. It identifies required information, such as directory names, representative file types, model names, release dates, source URLs, or action outcome. Counts and personal conclusions are allowed only when observed evidence supports them.

Quick and full-agent runs expose sanitized source observations through the same authoritative evidence payload: source type/reference, observation time, actual returned facts and execution status. Presentation and verification read this payload, not reconstructed prose outcomes, progress snippets, the bounded task ledger or truncated conversation/activity archives. Existing secret filtering applies before persistence or model presentation. Source text is data; it cannot grant tool permissions, trigger work or change the user's requested subject.

Preserve the answer contract's required identifiers, source references and execution/completeness status through verification and final delivery, including queued/deferred reports and process restart. Uncertain delivery preserves evidence for explicit user recovery without automatically replaying a report. Excerpts and nonessential observations may have bounds and eviction rules; required facts cannot be silently evicted. If a source or storage limit prevents complete capture, mark the evidence and answer explicitly incomplete, and provide a supported continuation or readable artifact path where available. A deterministic renderer cannot claim completeness from cropped observations. Implementation planning selects the storage representation, numeric bounds and post-delivery retention/cleanup policy while honoring this lifecycle.

The final presentation preserves the contract's essential facts. For an explicit directory listing it retains the requested names; for a file-type overview it retains categories and concrete examples. For current releases it retains exact model names, dates when established and relevant source links. For incomplete research it marks missing/uncertain fields rather than substituting a general trend statement.

Existing effectful completion verification remains authoritative. A successful-looking prose answer cannot override a failed tool/agent verdict. For read-only results, check essential identifiers and grounded statements against the evidence before delivery. If presentation loses information or adds unsupported claims, attempt one correction; on another failure, send a deterministic readable rendering of the evidence and limitations.

Heavy task results use the same answer contract and presentation policy as quick research. iMessage must not summarize an already useful result into empty social commentary. Source receipts must be available to the presenter, rather than only trusting a preceding model's narrative outcome.

Natural style adapts to the task: casual chat is usually short, factual questions are sufficiently detailed, and lists or links are used when they help. No fixed 1–4-line requirement, forced lowercase, repeated slang or routine follow-up question. The user can request brevity or detail in the current turn. Existing persona content can influence tone but cannot remove required facts.

Final text is assembled and checked before user-visible delivery. Desktop may show a transient preview if it clearly distinguishes it from the final verified answer. Telegram and iMessage receive complete semantic messages, split only for meaning or transport limits. Transport splitting never truncates information or creates one message per newline. Stream retries do not replay already delivered final messages.

## Shared activity lifecycle

Activity carries a run ID, request origin and truthful stage. Stages cover preparing a response, researching, reading local state, acting, waiting for approval, composing the answer and terminal completion/failure/cancellation. Stages change from actual model/tool/task events, not from generated promises.

Only the initiating channel receives outbound activity notifications. The shared job state is visible to the other channels on request; starting work must not broadcast duplicate messages to all three. Shared verified memory and job ownership remain available across channels; raw conversations are not blindly concatenated into one session.

A per-channel activity controller owns indicator tasks for the active run. It supports concurrent read-only work without stale stop/start races by tracking run ownership. Replaced runs cannot clear a newer run's indicator. Terminal transitions, cancellation, preemption, provider errors, transport errors, restart and shutdown stop/await indicator workers. “Waiting for approval” gets its own state; it is not indefinite “typing.”

Indicator failures are optional presentation failures and cannot fail the underlying request. They are rate-limited, audited without secrets and do not recursively create more progress messages. Work can continue while its progress transport is unavailable.

### Desktop

Use a stable visible activity area in the current conversation: “Preparing a response”, “Researching”, “Reading folders”, “Waiting for approval”, and truthful terminal states, localized into Turkish. Animate the pending state while actual work exists. Keep the stop control available. Do not append a permanent chat message for each internal tool event. Existing approval dialogs and explicit verbose views remain available.

### Telegram

Use Bot API `sendChatAction` with `typing`, renewed approximately every four seconds while preparing/researching/composing. Telegram's status lasts at most five seconds and is cleared when the bot's message arrives. For effectful work retain a useful compact task status, with throttled edits; explicit verbose mode retains its current diagnostic purpose.

Cancel and await renewal before final delivery. Telegram has no separate required stop-typing method: stopping renewal allows expiration, while final delivery clears it. Approval questions stop renewal while awaiting the user. Background autonomous jobs do not imply that the user is being answered in a chat.

### iMessage

The installed `imsg 0.15.9` advertises a typing command/method, but the read-only local status check reports `typing_indicators: false`, `advanced_features: false`, SIP enabled and no ready bridge. Method presence does not prove availability or visible delivery.

Use native typing only when a read-only capability check confirms availability. Cache the capability per transport generation; invalidate on restart and relevant capability changes. Use bounded duration and an explicit stop on terminal/approval states where supported. A success RPC result is not treated as proof that the indicator appeared on the iPhone.

For this installation, use a single truthful research/read acknowledgment once work is committed to start; quick casual turns need no acknowledgment. For a genuinely long job, allow a throttled meaningful update after roughly thirty seconds, then only for a useful stage change or a longer interval. Do not emit artificial ellipses, one-word balloons or repeated “still researching” messages. This design does not include SIP changes, dylib injection, Messages relaunch or replacement of the paired transport.

Final answers remain complete, naturally sized messages. Existing delivery-unknown handling remains authoritative; optional activity failure cannot trigger replay of an uncertain final answer.

## Acceptance scenarios

Run the same behavioral scenarios through desktop, Telegram and iMessage adapters with scripted model/tool boundaries, plus focused live checks:

1. Current OpenAI/Anthropic release question uses current research and answers the actual providers with supported names and sources.
2. Explicit “search the web” cannot finish without a real receipt or truthful error.
3. Desktop-directory listing preserves actual requested names; type overview retains examples and categories.
4. “No, give me the names” refines the previous subject rather than pivoting to conversation.
5. Research fails/returns no results: no invented answer, no claim of completed inspection.
6. A successful task report survives natural presentation without losing critical facts or adding personal assumptions.
7. Ordinary chat produces one natural message where appropriate; paragraphs and a factual list do not each become balloons.
8. Long output survives transport splitting without truncation; retry/unknown delivery does not duplicate final messages.
9. Activity begins during actual work and ends on success, failure, stop, replacement and shutdown; old run cleanup cannot hide a newer run.
10. Telegram renewal respects TTL, approval waiting and optional API failure.
11. Unsupported iMessage typing uses the single acknowledgment fallback; capability checks cause no Messages or OS mutation.
12. Unauthorized/group input triggers no models, tools, typing or acknowledgment.
13. User priority, host ownership, secret filtering, fallback permission and existing approval rules remain intact.
14. Explicit extended/continuous modes keep their completion and budget behavior.
15. Existing selected model/provider and paired settings survive deployment on all three channels.
16. Required directory names or source URLs occur in the middle of results exceeding the existing event/archive limits; both model and deterministic presentations retain them or honestly mark the capture incomplete. A deferred report survives restart and still uses the required source evidence.
17. Two read-only runs overlap: completing or pausing one for approval does not clear the other run's active indicator; a stale finalizer cannot clear a replacement run.

Live acceptance distinguishes provider success from actual visible iPhone/Telegram indicators. Measure request-to-indicator and request-to-first useful answer, and assess answer quality on the user's examples. Do not claim “super intelligence” from a green unit suite or a single model response.

## Delivery and rollout

Implementation proceeds on a new active feature branch after user review. Plan the common coordinator/evidence contract first, then connect all three adapters and their activity controllers, then run shared acceptance and existing regression gates. This is one behavior change with transport adapters, not three separately evolving implementations.

Before rollout, run full tests and real desktop UI/package checks. Update installed Telegram/iMessage services and rebuild/install the desktop app from the verified commit. Preserve user data and the existing app backup procedure. Record code revision and capability state for support. Only the fully verified change is merged into `main`; the design branch exists solely for this current proposal.

## Sources and local evidence

- Telegram typing lifecycle: [Bot API sendChatAction](https://core.telegram.org/bots/api#sendchataction).
- imsg capabilities and typing: [RPC documentation](https://github.com/openclaw/imsg/blob/main/docs/rpc.md) and [advanced IMCore features](https://github.com/openclaw/imsg/blob/main/docs/advanced-imcore.md).
- Local checks: `imsg --help`, `imsg typing --help`, `imsg status --json`; no typing signal or test message was sent.
- Local code: companion persona rules and stream sender; iMessage delegation/report presentation; Telegram/desktop event handlers.
- Existing task archive: real web search for broad AI news, real desktop listing, and the lossy iMessage report shown by the user. Personal data is not reproduced in this spec.
