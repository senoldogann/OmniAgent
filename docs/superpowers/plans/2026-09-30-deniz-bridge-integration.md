# Deniz Phase D and C Bridge Integration Plan

**Goal:** Complete the approved companion roadmap in the existing feature worktree, preserving the paired installation and evidence guarantees.

**Architecture:** The bridge archives accepted input before acting, retains attachment paths atomically with the cursor, and converts media only for the current model/task lifetime. Heartbeat shares the archive and delivery channel while autonomous runs carry explicit origin, rationale and guards.

## Tasks

- [x] Establish the baseline and verify Phase B+ commits. Baseline: 2298 passed, 125 skipped, one stale prompt assertion.
- [x] Review Phase D media and Phase C autonomy plans against the approved design and add explicit transcription model selection.
- [x] Test and implement attachment recovery, secret filtered transcript evidence, actual image message parts and image fallback permission/audit.
- [x] Wire heartbeat lifecycle, commands and chat tools, quiet approvals, same process preemption, queued morning reports and lessons.
- [x] Test multi step integration with real SQLite and scripted models; real conversion is tested in the media suite.
- [x] Review the completed changes for source authorization, side effects, shutdown and uncertain delivery handling.
- [x] Run the complete suite and required GUI tests and document live acceptance limits. Final full suite: 2436 passed, 125 skipped; actual Tk selection: 85 passed. The final full run includes partial report delivery, exact archive quote preservation and Telegram restart/polling regressions. Tk tests: 85 passed; live chat/memory provider tests: 4 passed.
- [x] Commit and restart the installed paired iMessage and Telegram services; verify actual service connection state. Both launchd services are running, iMessage is connected through the actual service interpreter, heartbeat is scheduled and database version remains 2.

## Review findings resolved

Media review: delayed transcription cannot pass the learning cursor; filenames are excluded from evidence and quotes still match the unmodified archive; threaded conversion is joined/cleaned after cancellation; audio failures produce a single fixed reply; restart reuses completed speech.

Autonomy review: cancelled lock request workers stop; HID activity during tool settling remains human input; definite report non-delivery retains FIFO while partial/unknown delivery is not replayed; curl and Chrome subprocesses stop and are reaped before host ownership is released. Report/model tasks remain tracked during bridge shutdown. Commands do not wait for the morning report model. Concurrent fatal Telegram polling errors allow an already accepted restart a bounded shutdown grace period; stalled maintenance still exits and completed poll errors are consumed.

Live model checks: existing selected chat profile correctly recognized a synthetic red/blue image; actual heartbeat returned a valid decision; four existing live chat/memory tests passed. No iPhone delivery, p50/p95 or 15-second cross-channel user acceptance claim is inferred from these checks.

Paired installation: chat profile remains the configured `ollama-cloud`, memory profile `openai`. The user explicitly selected `openai` / `gpt-4o-mini-transcribe`; the paired installation was configured through `omniagent-imessage transcription` without re-pairing and the iMessage service restarted. A real synthetic CAF audio conversion/upload returned the expected transcript through that model; the service is running and connected. Actual iPhone voice delivery remains a separate acceptance check. Existing persona, paired address and quiet hours are preserved.

## Required behavior

Unpaired and group messages never reach conversion or a model. Image content never becomes memory evidence. Transcripts are the user's own words, retain the original message time and use the shared secret filter. Report turns cannot execute chat tools. Autonomous approval never uses unattended mode. User work preempts automatic work. Proactive messaging obeys quiet time, mute and the unanswered limit, with state checked again after model waits.
