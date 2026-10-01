# Shared conversation core: acceptance and rollout

Status: reported live-failure repairs approved at `cf608b18`; full macOS suite GREEN
(2,838 passed, 26 skipped). Native installation, paired-service startup and main
publication are recorded in the local delivery receipt listed below. Source review
and scripted adapter verification do not establish visible delivery on a recipient’s phone.

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
| 1 | Current OpenAI/Anthropic question performs research and retains providers, supported names and sources | Coordinator and all adapter entry points | Source and integrated activity independently approved |
| 2 | Explicit web research produces a real receipt or an honest error | Deterministic routing guard and tool boundary | Source and integrated activity independently approved |
| 3 | Directory answers preserve actual requested names and useful type examples | Structured listing, evidence and presentation | Source and integrated activity independently approved |
| 4 | A names-only follow-up refines the previous authenticated subject | Routing/history and adapter handoff | Source and integrated activity independently approved |
| 5 | Failed or empty research does not invent inspection or an answer | Coordinator failure and deterministic rendering | Source and integrated activity independently approved |
| 6 | Successful task reports retain required facts and the verified execution verdict | Shared presentation and iMessage report delivery | Source and final integration independently approved |
| 7 | Natural chat preserves paragraphs without sending every line separately | Companion final assembly and shared style | Source and final integration independently approved |
| 8 | Long finals survive transport limits; uncertain delivery does not duplicate them | Chunk reconstruction and persistent delivery groups | Source and final integration independently approved |
| 9 | Real activity ends on terminal, stop, replacement and shutdown; old cleanup cannot hide newer work | Shared ownership and adapter lifecycle | Task 4 independently approved; lifecycle boundary regressions passed |
| 10 | Telegram typing renewal respects TTL, approval waiting and optional API failure | Telegram foreground activity worker | Task 4 independently approved; lifecycle boundary regressions passed |
| 11 | Unsupported iMessage typing uses a single real-work acknowledgment | Read-only capability check and fallback | Task 4 independently approved; lifecycle boundary regressions passed |
| 12 | Unauthorized/group input triggers no models, tools or indicators | Authenticated adapter ingress | Source and integrated activity independently approved |
| 13 | User priority, host ownership, masking, fallback permission and approvals survive | Existing execution runtime and regression gates | Source and integrated activity independently approved |
| 14 | Explicit extended/continuous modes preserve their selected limits and completion behavior | Full-agent delegation and shared budget | Source and integrated activity independently approved |
| 15 | Selected providers/models, voice, persona and paired settings survive deployment | Before/after comparison | Before-install hashes unchanged; final comparison in delivery receipt |
| 16 | Middle-of-large-result names/URLs remain retained or explicitly incomplete; deferred reports survive restart | Evidence artifacts and deferred delivery | Full artifact recovery and delivery integration approved |
| 17 | Overlapping read-only runs keep truthful ownership and indicators | Shared activity snapshots and lifecycle | Task 4 independently approved; lifecycle boundary regressions passed |

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
Task 2 source repairs were independently approved at this checkpoint; Task 4 and live rollout were still pending then.

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

These source gates are complete; the following integration history records their
actual failures, repairs and final passing evidence. Live recipient visibility remains
a separate observation.

## Rollout and recovery

Verified package source: `c7145c0bb190cadb003578bf23e2ba40cfd00c1e`.
Native build, signature, Tk bundle check and three packaged-worker protocol probes
passed. The installed executable SHA-256 is
`1cd3d7fff1b1aebb23d049b331b014ba3c24583bad85099fc5dfc15f553d2f69`,
matching the freshly built package. The previous application is retained at
`/Users/dogan/Library/Application Support/OmniAgent/app-backups/OmniAgent-20261001-170621.app`.
The subsequent paired-service restart, main/remote identity, CI and merged-branch
cleanup are recorded separately after actual verification in the delivery receipt.

Preserve the archived repository's common Git directory, retained unique/dirty
worktrees, user data and existing paired settings. Remove only feature/design
branches proven merged after delivery. Do not automatically replay pending or
uncertain messages during restart.

## Live limits and subsequent work

Actual visible iPhone/Telegram indicators have not been observed in this gate.
No personal test messages are authorized for these checks. The current Mac hosts
the system; no VPS is available. Durable ongoing responsibilities and a future
Windows host are separate roadmap stages after this verified delivery.

## Final integration reopened on 2026-10-01

Task 4 source was independently approved at `d370b8e`. The implementer reported
191 passed and 65 UI opt-in skips, plus one concurrent-status case. Independent
specification review first reproduced three lifecycle gaps: premature iMessage
acknowledgment before actual host ownership, cross-process activity admission
exceeding 64 live records, and repeated cancellation interrupting bridge cleanup.
Commit `d370b8e` repaired them. Independent specification reconfirmation passed
110 cases with two excluded internal-worker selectors and three real boundary
probes. Fresh quality review passed 49 cases and two private lifecycle probes;
no actionable quality blocker remained in that component. Counts overlap and
are not additive.

The root then ran the full suite with real Tk cases enabled:
`OMNI_UI_TEST=1 .venv/bin/python -m pytest -q`.
Result: **3 failed, 2,755 passed, 26 skipped in 216.83 seconds**. One masking
fixture expected the legacy literal placeholder despite actual secret absence;
two cross-channel memory fixtures bypassed the old model entry point and did
not supply the shared classifier boundary. These are recorded as failures,
not a green suite or a reason to weaken memory/privacy acceptance.

The root's actual paired-model probe used `ollama-cloud/gemma4:cloud`, temporary
state and a temporary directory, and public research queries. Natural chat
completed in 6.566 seconds. A plain Turkish directory request misrouted to the
full-task boundary and used no inspection tools. Current model research made
two real searches using obsolete 2024/2025 query years, fetched no official
page, and returned a failed 15,361-character raw JSON fallback in 14.891 seconds.
No messages were sent to personal Telegram or iMessage recipients.

The final independent whole-change review rejected integration with four
Important findings: valid tool-free full-engine results were erased; ordinary
pure Turkish read requests could enter full task execution; quick research
lacked trusted host-date and requested primary-page verification; literal source
checks demanded unrelated discovery links/dates and then exposed raw search JSON.
The first finding was independently reproduced through the real full engine,
scripted only at the model boundary. Integration repairs were reopened before
packaging, deployment or main merge.


### Consolidated integration repair rounds

`47329ef` added real-engine tool-free completion, Turkish read routing, trusted
UTC date and primary fetch guidance, requested-field identifiers and readable
search failures. Its targeted gate passed 156 tests in 38.32 seconds. Independent
specification review then reproduced remaining evidence scope and primary-source
validation gaps; the actual selected provider returned complete fenced JSON that
was rejected by the host. Both live probes entered task with zero tools. Delivery
remained closed.

`c9fd9c0` added strict complete optional fenced JSON parsing, truthful external
state requirements, factual quotation/numeric safeguards and relevant source-page
checks. Its affected gate passed 172 tests in 37.32 seconds. Independent review
passed 58 cases but reproduced an actual coordinator certification of an unsupported
search-snippet model name beside a different official page. Root's real directory
probe succeeded with one actual list_directory in 4.919 seconds and exact temporary
names/types. Research used the existing search default, read a failed OpenAI news
page and an Anthropic news index, then exhausted three generation turns; it returned
an incomplete 11,455-character fallback. The directory success was retained and was
not repeated during later research-only verification.

`4cf8f266` repaired same-source official model/date attribution, bounded research
planning and a compact partial primary-source fallback. The affected gate passed
180 tests in 37.88 seconds. Independent specification reconfirmation passed 63 cases
in 3.21 seconds and confirmed the mixed-source blocker repaired. It then reproduced
a complete 448,088-byte official source retained intact in a private artifact whose
bounded inline preview caused primary rejection before artifact loading. Exact middle
model/date and the artifact recovery path disappeared from the fallback. SPEC remained NO.

Root's actual paired research-only v3 performed four model calls and six real tools
in 15.258 seconds: two provider searches and four page fetches. OpenAI's selected
product-release page returned a real 403 access wall. Three specific Anthropic release
pages were successfully fetched, with complete plain text lengths 17,286, 13,743 and
10,479 characters. The shared generic HTML cleaner prematurely reduced each returned
receipt to 5,037 characters including its explicit truncation marker. Capture correctly
marked those tool receipts incomplete, so no primary factual answer could be certified.
The 1,797-character partial fallback reported the gaps honestly. The successful fetches
must reach canonical capture intact; truncation honesty must not be weakened. Actual
search schema categories are auto/text/news, so the new category=web instruction must
be corrected to text. These concrete defects reopened the sole source writer before
packaging, deployment or main integration.

At this checkpoint, iMessage/Telegram/persona settings hashes still matched the
preflight baseline. Voice remained openai/gpt-4o-mini-transcribe. Launchd services
continued running the older source revision; the installed application had not been
updated. Actual phone-visible typing has not been observed and no personal transport
test messages were sent.

`c3c81976` repaired full canonical fetch capture and store-authoritative artifact
views before primary gaps. New actual HTTP transport/tool regressions failed first
(2 failed, 7 passed in 4.21 seconds), then the affected gate passed 186 tests in
40.78 seconds; the selected browser compatibility gate passed nine cases. Independent
specification verification passed 69 cases in 6.36 seconds. Its own exact 448,088-byte
probe retained the middle model/date and artifact path in a 970-character truthful
context-limited fallback without mutating persisted evidence. Failed, tool-truncated,
legacy capture-flag absence, bounded-without-artifact and missing-artifact controls
remained unverified. This capture repair was approved; overall delivery stayed closed.

The root's v4 selected-model probe confirmed complete receipts now reached capture,
but generic discovery returned an OpenAI prompting cookbook and Anthropic's Model
Hardware Standard preview. The relevance check incorrectly accepted these unrelated
provider pages as model announcements, then the full-source model context limit was
exceeded. The 9.812-second run used three model calls and five tools. Independent
review inspected the saved actual result and confirmed the false primary status.
This remaining relevance boundary was reopened before the quality handoff.

`15b03adc` restricts known-provider model-release primary status to specific main-site
announcement pages with a nearby release statement and named provider model. Developer
cookbooks, support pages and hardware standards cannot clear model-release gaps. The
same URL eligibility rule filters discovery candidates; trusted planning supplies
concrete current-year article-site/model-release queries without rewriting model tool
arguments. The new relevance controls failed first (2 failed, 14 passed in 6.36 seconds),
then passed 16 cases. The consolidated affected gate passed 191 tests in 41.87 seconds.
Source is clean and frozen for independent specification and quality review. Root v5
research-only verification is pending at this checkpoint.

Independent specification verification at `15b03adc` passed 74 affected cases in
6.66 seconds. Its replay of the three actual v4 URLs retained both provider gaps
and no false primary announcement claim. Ordinary named GPT/Claude announcement
positives, complete artifact recovery, incomplete source controls and mixed-source
attribution remained passing.

Root's actual paired v5 research-only probe used four model calls and six real tools
in 14.858 seconds. Three specific Anthropic model announcement pages were fully
captured (19,651, 19,835 and 16,506 characters). OpenAI's selected page returned a
real 403 wall. The published result was an honest partial with actual read model
headlines/dates/source links and an explicit OpenAI primary verification gap. The
model's draft also included uninspected Sonnet/Haiku snippet claims, which were not
published as verified. This is a successful truthful partial boundary, not a claim
that both providers were verified or that the quick report had success=true.
The only remaining observed formatter defect was that the discovery section called
one already successfully read article unread; a narrow deterministic fix was requested.
No further live repeat is needed for this label-only change.

`b8ac21ca` corrected unread candidate labels only (one regression RED, then 55
affected cases passed). Independent final SPEC at that snapshot passed 17 official
research cases in 5.68 seconds and approved the consolidated scope.

The fresh final QUALITY review passed 132 affected cases in 40.14 seconds but
confirmed one Important regression with matched actual-coordinator local probes:
adding the word official to a generic Python asyncio documentation request produced
a permanent unknown-authority gap despite a complete successful actual fetch. Without
that word the same actual receipt succeeded; both used four models and one tool.
The newly added strict provider model-release gate must not block generic source
research. A bounded scope correction was assigned to the sole writer.

Root's complete frozen b8 suite with real Tk enabled finished with **1 failed,
2,812 passed, 26 skipped in 311.06 seconds**. The failed legacy real imsg control
listener fixture waits only for three send requests to appear in the child log, then
cancels listening while the third RPC response may still be pending. It observed
truthful unknown delivery rather than the expected pending delivery. The writer is
examining a real notification-handler completion barrier while preserving the first
two transport errors, successful third pending state, cursor 9 and listener survival
assertions. This run is recorded as failing; deployment remains closed pending both
narrow fixes, independent delta reviews and a fresh full-suite gate.

`c7145c0b` closes the two final bounded gaps. The generic official Python probe
failed first (one failed, one explicit-authority control passed), then the final
102-case official/integration/iMessage selection passed in 12.51 seconds. The real
notification-handler completion observer preserves rejection propagation and waits
for all three actual handlers before listener cancellation; runtime delivery behavior
and the original exact assertions are unchanged.

Independent final SPEC passed 64 official-research/iMessage cases in 9.94 seconds
and confirmed both generic-source and explicit-authority controls. Final QUALITY
reconfirmation passed 83 official-research/review/iMessage cases in 10.17 seconds.
Its paired actual coordinator/fetch/tool/evidence probes now both succeed with four
model calls and one tool, including the authoritative verifier. No remaining confirmed
Critical or Important issue exists in the reviewed source delta. These selections
overlap and their counts are not additive.

Root CCM quick index refreshed 327 files, zero failures and 21,503 nodes. Bounded
find_usages and actual source search reconfirmed the two production direct callers
of execute_tool: quick _investigate in app/conversation.py and _run_tool_with_events
in app/tool_execution.py. The final full suite and whole-change reconfirmation are
running at the frozen c714 source snapshot; packaging and live delivery remain pending.

Final independent whole-change reconfirmation at clean c714 approved source
integration. Its bounded actual regression selection passed 26 cases in 5.09 seconds,
closing the original four Important findings and reconfirming the approved material
acceptance deltas. No new Critical, Important or Minor finding remained in that scope.

The final frozen-source full suite with real Tk enabled is GREEN:
**2,815 passed, 26 skipped in 301.20 seconds**. The prior failing suite remains in
this history; it has not been relabeled as passing. Source, packaging configuration,
project dependency manifest and lockfile identities were saved for comparison after
the final documentation commit. Native packaging is now running at c714.

## Delivery receipt

Final operational observations are written after service startup and remote CI finish to
[local delivery receipt](</Users/dogan/Library/Application Support/OmniAgent/shared-conversation-core-delivery-2026-10-01.json>).
It records the tested code commit separately from the final documentation commit,
matching source/dependency trees, installed application hash, actual service PIDs and
connectivity, preserved settings, remote main identity, CI result and branch cleanup.
This avoids treating a documentation-only commit as a different application build.

The current Mac hosts all three channels. The next product phase is durable ongoing
responsibilities with resume, cancellation, progress and guarded effects. A Windows
host can be added later; no VPS or paid infrastructure was provisioned for this delivery.

Installed native application verification: a new empty chat using the preserved
ollama-cloud/gemma4:cloud profile answered the public arithmetic probe with `4`
in 1.9 seconds, two model turns and zero tools. The user-visible final was marked
completed; startup keys were ready and no new Keychain interaction was needed.


## Reported live failures: 2026-10-01 macOS repair

The user supplied iMessage and Telegram screenshots showing repeated busy failures,
missing observations for screen questions, and literal Markdown in Messages. Actual
private receipts confirmed no-tool screen endings and user-task contention. The
desktop X task was still awaiting user approval during the earlier busy failures;
it subsequently captured screens successfully and ended on user cancellation.
A stale lock was not assumed or forcibly removed.

Repairs at `198da07a`, with the final permission/report compatibility correction
at `cf608b18`, received independent specification and quality approval:

- User host contention waits for up to the existing 15-second interval, remains
  cancellable, and never steals another user task’s ownership. Singleton locks
  remain immediate; autonomous preemption retains its existing scope. Longer
  contention gives one actionable explanation about the running channel/on-screen
  approval, rather than repeated generic missing-source summaries.
- Failed execution retains its verdict and completed source receipts. Empty-source
  exceptions report their actual cause once. Existing failed-task captions remain
  compatible with desktop and Telegram delivery.
- Ollama tool-only assistant messages send an empty content string in place of
  null, without changing canonical history or other provider requests. An actual
  `gemma4:cloud` protocol fixture completed in 0.727 seconds without the recorded
  HTTP 400 nil-content error; this was a synthetic tool history, not a user action.
- iMessage converts the complete reply to native readable text before splitting.
  Code contents, indentation, tabs, final code newlines, technical filenames and
  URL destinations remain intact. Displayed text is also the archived/echo-matched
  text. Unknown-delivery groups still are not replayed automatically.
- Explicit current-screen questions, including the supplied Turkish typos, require
  real capture. The same run’s private image checks the answer; a failed or newer
  unreadable capture cannot reuse an older image. Images are not persisted into
  evidence/history. The 80,000-character text guard remains; only identity-matched
  host-generated JPEG parts receive a separately bounded visual allowance.
- Local permission/capability questions use read-only calling-process checks and
  the registered tool catalog without acquiring the effectful host lock. Catalog
  membership does not establish access to every target. No permission is requested
  or changed, and read-only help no longer claims a prompt was displayed.

Observed gates (overlapping counts are not additive):

- Implementer focused gate: 304 passed in 21.60 seconds.
- Initial full macOS run: 2,828 passed, 26 skipped, 8 failed in 303.39 seconds.
  Six old chat test doubles rejected the new optional presentation parameter; two
  host-rejection tests expected the previous caption. Original voice, memory,
  archive and failed-effect assertions were retained when correcting these cases.
- Final followup real-Tk gate: 47 passed in 4.37 seconds.
- Independent final specification gate: 31 passed in 3.36 seconds.
- Independent quality gate: 42 passed in 3.47 seconds; no Critical or Important
  findings remain.
- Final full suite at `cf608b18`, with `OMNI_UI_TEST=1`: 2,838 passed, 26 skipped,
  zero failures in 301.44 seconds.
- CCM refreshed 329 files, zero failures, 21,708 graph nodes; the actual capability
  implementation and its coordinator path were checked.

The user explicitly selected macOS as the delivery gate; Linux CI is informative
and does not delay this Mac-only delivery. Remote workflows are retained. Only
the merged, unattached `feat/shared-conversation-core` and
`design/shared-conversation-core` local branches were deleted after an all-ref
bundle backup. Unique work and attached worktrees were preserved. Final binary,
rollback copy, service checks and published main SHA belong to the delivery receipt.
