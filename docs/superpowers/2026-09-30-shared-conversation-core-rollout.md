# Shared conversation core: acceptance and rollout

Status: in progress. This record distinguishes implemented source, independent review,
scripted adapter verification, and observed live delivery. It is not a completion claim.

## Reopened delivery gate

The handoff at `48daba9` claimed Task 3 complete. Independent inspection reproduced:

- A stopped companion model turn could publish its accumulated draft and return
  task, memory and proactive control requests.
- Normal iMessage finals and task reports did not implement the planned 3500-character
  transport chunks. A single-chunk echo could not establish delivery of a future
  multipart final.
- Desktop and Telegram final presentation did not mark their evidence delivered.

These findings reopened Task 3; its repairs have since passed independent specification
and quality reviews. Task 2 repairs have also passed independent specification and quality reviews. Existing reported test
counts from the other agent are historical claims; fresh gates are recorded below.

## Acceptance map

All scenarios require the actual adapter boundary with scripted providers/tools.
Live visibility is a separate observation; an API return does not prove what appears
on a recipient's phone.

| Case | Required behavior | Verification area | Gate |
| --- | --- | --- | --- |
| 1 | Current OpenAI/Anthropic question performs research and retains providers, supported names and sources | Coordinator and all adapter entry points | Task 2 source approved; integrated activity gate pending |
| 2 | Explicit web research produces a real receipt or an honest error | Deterministic routing guard and tool boundary | Task 2 source approved; integrated activity gate pending |
| 3 | Directory answers preserve actual requested names and useful type examples | Structured listing, evidence and presentation | Task 2 source approved; integrated activity gate pending |
| 4 | A names-only follow-up refines the previous authenticated subject | Routing/history and adapter handoff | Task 2 source approved; integrated activity gate pending |
| 5 | Failed or empty research does not invent inspection or an answer | Coordinator failure and deterministic rendering | Task 2 source approved; integrated activity gate pending |
| 6 | Successful task reports retain required facts and the verified execution verdict | Shared presentation and iMessage report delivery | Task 3 approved; integration pending |
| 7 | Natural chat preserves paragraphs without sending every line separately | Companion final assembly and shared style | Task 3 approved; integration pending |
| 8 | Long finals survive transport limits; uncertain delivery does not duplicate them | Chunk reconstruction and persistent delivery groups | Task 3 approved; integration pending |
| 9 | Real activity ends on terminal, stop, replacement and shutdown; old cleanup cannot hide newer work | Shared ownership and adapter lifecycle | Pending Task 4 |
| 10 | Telegram typing renewal respects TTL, approval waiting and optional API failure | Telegram foreground activity worker | Pending Task 4 |
| 11 | Unsupported iMessage typing uses a single real-work acknowledgment | Read-only capability check and fallback | Pending Task 4 |
| 12 | Unauthorized/group input triggers no models, tools or indicators | Authenticated adapter ingress | Task 2 source approved; integrated activity gate pending |
| 13 | User priority, host ownership, masking, fallback permission and approvals survive | Existing execution runtime and regression gates | Task 2 source approved; integrated activity gate pending |
| 14 | Explicit extended/continuous modes preserve their selected limits and completion behavior | Full-agent delegation and shared budget | Task 2 source approved; integrated activity gate pending |
| 15 | Selected providers/models, voice, persona and paired settings survive deployment | Before/after comparison | Baseline captured; rollout pending |
| 16 | Middle-of-large-result names/URLs remain retained or explicitly incomplete; deferred reports survive restart | Evidence artifacts and deferred delivery | Tasks 1/3 approved; integration pending |
| 17 | Overlapping read-only runs keep truthful ownership and indicators | Shared activity snapshots and lifecycle | Pending Task 4 |

## Observed local baseline

Before repairs and rollout on 2026-09-30:

- Feature source: `48daba9790ba616c670d72f77bcff960adeda19c`.
- Working tree was clean. Published main remained `5ef0f232067dd82068bac042a609da49d4a7bee3`.
- Nine existing focused companion/iMessage/deferred-report tests passed; they did not
  cover the three reproduced defects.
- CCM quick indexing refreshed 316 files and 20,023 nodes, with no failed files.
- iMessage selected chat backend: `ollama-cloud`; memory backend: `openai`.
- Voice transcription: `openai`, `gpt-4o-mini-transcribe`.
- Selected models: `ollama-cloud/gemma4:cloud`, `openai/gpt-6-luna`,
  `opencode/qwen3.8-flash`, `opencode-think/qwen3.8-flash`.
- iMessage settings SHA-256: `8e677a6677838959b7a62e2f25c440b7780a8e7d366243e62e6a9bf00abff130`.
- Telegram settings SHA-256: `a553e14b03c24aeacad854a526dbc5e10f385d532daaa010e88028c31bac8f1c`.
- Persona SHA-256: `ef8ae3b1a1d694f70d1d8d8168edcc903657ce0272a79c8386c63fec79c865a3`.
- Read-only `imsg status --json`: version `0.15.9`, SIP enabled,
  `typing_indicators=false`, `advanced_features=false`, `v2_ready=false`.
  No typing command or personal test message was sent.

Telegram documents a typing lifetime of at most five seconds and clearing when the
bot sends a message. The planned renewal interval is approximately four seconds.
[Telegram Bot API: sendChatAction](https://core.telegram.org/bots/api#sendchataction).

The installed iMessage capability result controls support. The presence of a compiled
typing method does not establish readiness. The official documentation describes
read-only status inspection and the platform's typing limitations.
[imsg RPC status](https://github.com/openclaw/imsg/blob/main/docs/rpc.md#status),
[imsg typing and status](https://github.com/openclaw/imsg/blob/main/docs/advanced-imcore.md#typing-indicators).

## Review and verification results

Task 3's first repair checkpoint was `e51ef615fcfe175ea0d4ff780898ebc0a2799ca9`.
The implementer reported 255 focused
tests passed, plus 76 real Tk tests passed with the two known Task 2 fixtures excluded.
No deployment was performed.

Independent Task 3 specification review approved the repair: 28 focused tests,
four explicitly enabled real Tk boundary tests, and 25 focused authorization/control/
persona/source/deferred cases passed. Independent quality review reproduced a further
formatting defect: the legacy textual-call helper collapsed indentation, tabs and
repeated spaces even when no tool marker was present. That finding was repaired in
`92f8d31`. The initial quality review's separate transport selection passed 17 cases.

Task 3 is now independently approved at `92f8d31` after the formatting repair.
The implementer's targeted chat/bubbles/iMessage gate passed 83 tests; independent
specification reconfirmation passed 12 cases and quality reconfirmation passed eight.
These selections overlap with previous gates and their counts must not be added.
Task 2 source repairs are now independently approved; Task 4 and live rollout remain pending.

The chunking contract explicitly handles an unbroken token longer than 3500 characters
by an exact character split. Such a token cannot simultaneously fit the transport chunk
and remain within one message. This is an operational edge-case refinement: ordinary
identifiers should stay intact when a semantic boundary exists; fragments of an oversized
identifier are not presented as independently usable links. Content is never clipped.

Task 2 independent specification review: changes required. The review covered
`585af60..2452019`, checked CCM call paths and reproduced four defects:

- A mistaken classifier could leave `Please read /tmp/report.txt`,
  `List files in ~/Desktop` or `Search for OpenAI release dates` on the chat route.
- Host-lock acquisition was outside the selected deadline and stop polling.
- A stop arriving during final evidence persistence still allowed a successful final.
- Quick chat treated `length` completions as complete success.

At that initial review, the required real Telegram compact/verbose publication tests were also missing; subsequent repairs added and verified those boundaries.
Coordinator tests passed: 29. A broader concurrent run had 120 passed and four
failures: three Task 3 RED reproduction cases and a missing `Callable` test import.
This broad run is not a green integration gate.

The repair's self-review additionally found a cancellation risk: a full engine's
newly persisted receipts could be overwritten by the coordinator's older empty
bundle if cancellation discarded the engine report. A separate root reproduction
confirmed that exhausting only the presentation model budget after a verified
successful engine run changed the reported verdict to failure and recommended
repeating the completed effect. Both cases are in the Task 2 repair scope.

Task 2 repairs were frozen at `4b2bc4942bc95942eee36ded0f5b09c0b13a5eb0`.
The implementer reports 205 focused tests, 78 real Tk conversation tests and four host
priority/preemption controls passed (287 distinct cases). The `8477269` commit message
incorrectly says 91 tests: its captured gate was 125 passed; the history is retained.
Specification re-review passed 73 coordinator/repair tests and 13 conversation
regressions and found a further quick-path
exhaustion regression: the final publication check retained partial receipts but
removed the required same-subject continuation offer. `02bd12b` fixes it with one
shared failure renderer. The implementer's 74-test gate passed; independent seven-case
boundary reconfirmation plus the actual-read deadline case passed (overlapping checks).
Task 2 specification review approved `02bd12b`; the following fresh quality review reopened two concrete boundaries.

The fresh quality review reproduced two further defects at `02bd12b`: canceled full
tasks retained receipts but lost actual tool counts and model/tool duration metrics;
quick synchronous `fetch_raw` workers could continue after the asynchronous wrapper
published a stopped terminal. Actual accounting was repaired at `ca58083`; worker interruption and cleanup were repaired at `30d9f7d`. Independent quality verification passed 27
focused cases, three real Tk boundaries, and a scripted real iMessage ingress through
classification, delegation, actual local read and final presentation. These passes
were the initial review evidence and did not close the defects by themselves.

Task 2 source is now independently approved at `30d9f7d`. The completed repair
uses the existing read tools and search implementation, owns synchronous workers and
child processes through cancellation cleanup, and keeps search protocol capture bounded
in memory. No raw source is spooled to disk. The implementer reported 212 affected
regressions, 32 selected web/fetch regressions and a final 95-test boundary gate passed.
Independent specification reconfirmation passed 95 cases; independent quality
reconfirmation passed 18 selected cases and three separate local pipe/child probes.
These selections overlap and are not additive. The actual frozen application worker
probe remains a required delivery gate. The `8f7be49` subject also claimed a premature
GREEN result: its run had 74 passed and two failed; `ca58083` is the subsequently
validated 76-test GREEN checkpoint. Execution evidence takes precedence over subjects.

Read-only live preflight found the launchd iMessage service running and its persisted
`bridge_status=connected`. A separate RPC probe under this development process lacks
Messages database permission; this does not establish a failure of the launchd service.

Pending: Task 4 implementation, specification then quality approval;
whole-change final review; integrated full suite; focused live timing; desktop
package/signature/launch verification.

## Rollout and recovery

Pending: verified source revision, app backup and installation, paired service
restart/connectivity checks, settings comparison, main merge/push and CI.

Preserve the archived repository's common Git directory, retained unique/dirty
worktrees, user data and existing paired settings. Remove only feature/design
branches proven merged after delivery. Do not automatically replay pending or
uncertain messages during restart.

## Live limits and subsequent work

Actual visible iPhone/Telegram indicators have not been observed in this gate.
No personal test messages are authorized for these checks. The current Mac hosts
the system; no VPS is available. Durable ongoing responsibilities and a future
Windows host are separate roadmap stages after this verified delivery.
