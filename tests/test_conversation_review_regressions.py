"""Concrete independent-review and real paired-provider format regressions."""
import json

import pytest

from omniagent.app.conversation_routing import contract_from_decision
from omniagent.app.conversation_grounding import semantic_check
from omniagent.core.conversation_policy import derive_request_contract, primary_source_gaps
from omniagent.core.evidence import EvidenceStore
from tests.test_shared_conversation import coordinator, scripted, options, run, route, turn, call, verified
from tests.test_conversation_integration_repairs import task_lock


def fenced(text):
    return "```json\n" + text + "\n```"


@pytest.mark.asyncio
async def test_real_coordinator_complete_fenced_route_and_verifier_inspect_directory(coordinator, scripted, tmp_path):
    directory = tmp_path / "DesktopProbe"
    directory.mkdir()
    (directory / "Linux Dersleri").mkdir()
    decision, verification = route("investigate", ["directory_names"]), verified()
    decision["content"], verification["content"] = fenced(decision["content"]), fenced(verification["content"])
    scripts, requests = scripted
    scripts.extend([decision, turn(calls=[call("list_directory", path=str(directory))]), turn("Linux Dersleri: klasör."), verification])
    report, _ = await run(coordinator, tmp_path, f"{directory} klasöründeki dosya ve klasör adlarını listele. Türlerini de belirt, içeriklerini tahmin etme.")
    assert report["success"] and report["metrics"]["tool_calls"] == 1 and len(requests) == 4
    assert report["evidence"]["contract"]["route"] == "investigate"


@pytest.mark.parametrize("wrapped", [
    lambda value: "Here is the answer:\n" + fenced(value),
    lambda value: fenced(value) + "\nThis is confirmed.",
    lambda value: "```json\n" + value,
    lambda value: "```python\n" + value + "\n```",
])
def test_incomplete_or_prose_wrapped_json_is_not_recovered(tmp_path, wrapped):
    assert contract_from_decision("Have OpenAI released new models?", options(tmp_path), wrapped(route("chat")["content"]))["route"] == "task"
    store = EvidenceStore(tmp_path / "state.json")
    bundle = store.create(derive_request_contract("List directory names", route="investigate"))
    store.capture(bundle, "list_directory", {"ok": True, "result": '{"entries":[],"complete":true}'})
    assert not semantic_check("Dizin boş; 0 klasör var.", bundle, wrapped(verified()["content"]), [bundle["observations"][0]["text"]])[0]


def test_invalid_classifier_cannot_bypass_authenticated_pure_read_guard(tmp_path):
    goal = "/tmp/DesktopProbe klasöründeki dosya ve klasör adlarını listele. Türlerini de belirt, içeriklerini tahmin etme."
    contract = contract_from_decision(goal, options(tmp_path), "not JSON")
    assert contract["route"] == "investigate" and contract["needs_observation"] and contract["subject"] == goal


@pytest.mark.asyncio
@pytest.mark.parametrize("goal,answer", [
    ("What is 2 + 2?", "2 + 2 = 4."),
    ("Bana bir ejderha hikâyesi anlat.", "Gümüş ejderha şafağı izledi."),
])
async def test_real_extended_engine_stable_and_turkish_creative_keep_zero_tool_success(coordinator, scripted, tmp_path, goal, answer):
    scripts, requests = scripted
    scripts.append(turn(answer))
    report, _ = await run(coordinator, tmp_path, goal, {"run_mode": "extended", "task_context": task_lock})
    assert report["success"] and report["outcome"] == answer and report["metrics"]["tool_calls"] == 0
    assert report["evidence"]["contract"]["needs_observation"] is False and len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("goal", ["Explain who Microsoft’s CEO is.", "Explain OpenAI's current model releases.", "List folders in ~/Desktop."])
async def test_real_extended_engine_current_and_local_claims_need_observation(coordinator, scripted, tmp_path, goal):
    scripts, _ = scripted
    scripts.append(turn("Microsoft CEO is Fictional Person."))
    report, events = await run(coordinator, tmp_path, goal, {"run_mode": "extended", "task_context": task_lock})
    assert not report["success"] and report["evidence"]["contract"]["needs_observation"] is True
    assert report["metrics"]["tool_calls"] == 0 and "Fictional Person" not in json.dumps(events)


def release_bundle(tmp_path, url="https://openai.com/index/exact/", text=None):
    store = EvidenceStore(tmp_path / "state.json")
    bundle = store.create(derive_request_contract("Verify OpenAI official model names, release dates and source links", route="investigate",
        required_fields=["model_names", "release_dates", "source_urls"]))
    text = text or "model: GPT Exact\nOpenAI released GPT Exact on 2026-09-29.\nCareers https://openai.com/careers/"
    store.capture(bundle, "fetch_raw", {"ok": True, "result": text}, arguments=json.dumps({"url": url}))
    return bundle, text


def verified_release():
    return json.dumps({"ok": True, "facts": [
        {"field": "model_names", "value": "GPT Exact", "source": 0, "quote": "model: GPT Exact"},
        {"field": "release_dates", "value": "2026-09-29", "source": 0, "quote": "OpenAI released GPT Exact on 2026-09-29."}],
        "missing_fields": [], "unsupported_claims": []})


def test_primary_page_footer_url_cannot_be_supported_by_approving_verifier(tmp_path):
    bundle, text = release_bundle(tmp_path)
    answer = "GPT Exact — 2026-09-29. https://openai.com/index/exact/ https://openai.com/careers/"
    assert not semantic_check(answer, bundle, verified_release(), [text])[0]


def test_official_careers_page_is_not_a_model_release_page(tmp_path):
    bundle, _ = release_bundle(tmp_path, "https://openai.com/careers/", "Careers at OpenAI. Work on GPT models. Latest model research opportunities.")
    assert primary_source_gaps(bundle)


@pytest.mark.parametrize("certification", [verified()["content"], verified_release()])
def test_unverified_numeric_claim_is_not_certified_by_model_ok(tmp_path, certification):
    bundle, text = release_bundle(tmp_path)
    answer = "GPT Exact — 2026-09-29. https://openai.com/index/exact/ It has 900 billion parameters."
    assert not semantic_check(answer, bundle, certification, [text])[0]


def test_factual_release_answer_needs_quoted_facts_and_accepts_complete_fenced_certification(tmp_path):
    bundle, text = release_bundle(tmp_path)
    answer = "GPT Exact — 2026-09-29. https://openai.com/index/exact/"
    assert not semantic_check(answer, bundle, verified()["content"], [text])[0]
    assert semantic_check(answer, bundle, fenced(verified_release()), [text])[0]


def test_structured_directory_counts_need_no_invented_numeric_quotes(tmp_path):
    store = EvidenceStore(tmp_path / "state.json")
    bundle = store.create(derive_request_contract("List files and directory names", route="investigate"))
    store.capture(bundle, "list_directory", {"ok": True, "result": json.dumps({"entries": [
        {"name": "Linux", "type": "directory"}, {"name": "Draft", "type": "directory"},
        {"name": "notes.txt", "type": "file"}], "complete": True})})
    assert semantic_check("Linux ve Draft: 2 klasör; notes.txt: 1 dosya. Toplam 3 giriş.", bundle,
        verified()["content"], [bundle["observations"][0]["text"]])[0]


def test_trusted_host_date_qualification_is_allowed_but_other_dates_need_quotes(tmp_path):
    from datetime import datetime, timezone
    bundle, text = release_bundle(tmp_path)
    answer = "GPT Exact — 2026-09-29. https://openai.com/index/exact/"
    today = datetime.now(timezone.utc).date().isoformat()
    assert semantic_check("As of " + today + ": " + answer, bundle, verified_release(), [text])[0]
    assert not semantic_check("As of 2099-01-01: " + answer, bundle, verified_release(), [text])[0]


def test_release_page_leading_navigation_does_not_hide_actual_release_body(tmp_path):
    bundle, _ = release_bundle(tmp_path, text="Careers https://openai.com/careers/\nmodel: GPT Exact\nOpenAI released GPT Exact on 2026-09-29.\nFooter updated 2024-01-01")
    assert not primary_source_gaps(bundle)
