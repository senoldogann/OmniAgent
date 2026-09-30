# Deniz Phase D Senses Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Give the paired iMessage companion bounded photo input, explicitly configured voice transcription and context aware conversational style.

**Architecture:** A focused media module owns temporary conversion files and calls the existing OpenAI compatible client without provider substitution. Settings preserve paired legacy installations by interpreting the absent transcription field as disabled. Persona helpers are pure; the bridge owns archive, evidence, fallback routing and turn wiring.

**Tech Stack:** Python, pytest, OpenAI SDK, macOS sips and afconvert, existing atomic JSON settings and launchd setup.

---

### Task 1: Bounded photo preparation

**Files:** Create `src/omniagent/companion/media.py`; create `tests/test_companion_media.py`.

- [x] Write tests for a real 2400 px PNG and HEIC fixture converted by sips, maximum edge 1600, JPEG output, input/output 5 MB limits, four input candidates, conversion failure and cleanup when the caller raises.
- [x] Run `.venv/bin/python -m pytest tests/test_companion_media.py -q`; expect missing module failure.
- [x] Implement `PreparedImages` as a context manager and `prepare_images(paths)` with a private temporary directory, bounded subprocess timeout, no shell and content free errors. Input source files stay intact; output files are deleted when the context exits.
- [x] Rerun the media tests and inspect real JPEG dimensions.

### Task 2: Explicit transcription and conversion

**Files:** Modify `src/omniagent/companion/media.py`; test `tests/test_companion_media.py`.

- [x] Write async tests with a scripted existing client; absent/unknown/unavailable profiles must make zero network calls. Assert the selected model is used exactly once and no fallback occurs on errors.
- [x] Generate real CAF from a PCM WAV fixture using afconvert; transcribe the converted M4A through a fake client and inspect upload magic plus cleanup. Cover direct M4A, corrupt input, empty transcription, size limits and cancellation cleanup.
- [x] Implement `transcribe_audio(path, backend, clients, model)` using an explicitly configured model, temporary CAF/AMR conversion, existing `client.audio.transcriptions.create` and sanitized exception types. No new client credentials or guessed model defaults.
- [x] Run the media suite and commit the focused module and tests.

### Task 3: Backwards compatible settings and CLI configuration

**Files:** Modify `src/omniagent/integrations/imessage_settings.py`, `src/omniagent/integrations/imessage_setup.py`; test `tests/test_imessage_settings.py`, `tests/test_imessage_setup.py`. Root integration task modifies `src/omniagent/integrations/imessage.py` action dispatch.

- [x] Add tests for missing and null transcription profile -> None, unknown/empty/wrong type -> configuration error, selected profile preserved through pairing/save/load.
- [x] Add `transcribe_backend: Optional[str]` and explicit `transcribe_model: Optional[str]` to the validated settings. Add keyed profile selector with explicit disable choice, and `transcription()` to update an existing paired config and reinstall/restart its service without re-pairing.
- [x] Cover selector filtering, disabled selection, settings preservation, permissions and service restart in tests; run `.venv/bin/python -m pytest tests/test_imessage_settings.py tests/test_imessage_setup.py -q`.
- [x] Root wires the `transcription` CLI action and disabled state in `/durum`.

### Task 4: Persona context and rules

**Files:** Modify `src/omniagent/companion/persona.py`; test `tests/test_companion_persona.py`. Root integration task supplies archived assistant text.

- [x] Add daypart boundary tests, aware Helsinki timezone including summer/winter, and recent opening extraction from the last ten assistant messages without mutation.
- [x] Extend `[DURUM]` with timezone/daypart and a bounded repeat prevention list. Add rules for at most one question, quieter night/shorter morning style, natural evidence based references, no invented profile context.
- [x] Run `.venv/bin/python -m pytest tests/test_companion_persona.py -q`; commit owned files after review.

### Acceptance and integration

- [ ] Run all focused tests together; root runs full integration suite after bridge wiring.
- [ ] Live checklist: photo content appropriate response; 10 s voice with configured supporting model appears as user evidence; 02:00 night style and ten distinct openings; request screenshot/file through existing delegation delivery channel.
- [ ] Keep live voice explicitly disabled until a supporting profile is selected; model support and live iPhone behavior are not inferred from fake clients.

## Verification result

2026-09-30: `.venv/bin/python -m pytest tests/test_companion_media.py tests/test_companion_persona.py tests/test_imessage_settings.py tests/test_imessage_setup.py tests/test_companion_media_integration.py -q` — **68 passed**. Real macOS HEIC/JPEG and CAF/M4A conversion covered; real OpenAI SDK request exercised through a mock HTTP transport. Actual provider audio/model support and iPhone acceptance remain live checks. Root reviewed this plan before commits.
