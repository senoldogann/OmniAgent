"""Official facts must belong to the actual primary receipt and fit quick research."""
import json
from datetime import datetime, timezone

import pytest

from omniagent.app import agent
from omniagent.core.conversation_policy import derive_request_contract, primary_source_gaps, render_evidence
from omniagent.core.evidence import EvidenceStore
from tests.test_shared_conversation import coordinator, scripted, run, route, turn, call
from tests.test_conversation_integration_repairs import RESEARCH


def checked(facts):
    return turn(json.dumps({"ok": True, "facts": facts, "missing_fields": [], "unsupported_claims": []}))


def fact(field, value, source, quote):
    return {"field": field, "value": value, "source": source, "quote": quote}


@pytest.mark.asyncio
async def test_real_coordinator_cannot_borrow_primary_status_for_a_search_snippet(coordinator, scripted, tmp_path, monkeypatch):
    url = "https://openai.com/index/gpt-real/"
    snippet = "model: GPT Phantom\nOpenAI released GPT Phantom on 2026-09-28. https://openai.com/index/gpt-phantom/"
    primary = "model: GPT Real\nOpenAI released GPT Real on 2026-09-29."
    monkeypatch.setattr(agent.Toolbox, "web_search", lambda self, **kwargs: snippet)
    monkeypatch.setattr(agent.Toolbox, "fetch_raw", lambda self, **kwargs: primary)
    scripts, requests = scripted
    real_facts = [fact("model_names", "GPT Real", 1, "model: GPT Real"),
                  fact("release_dates", "2026-09-29", 1, "OpenAI released GPT Real on 2026-09-29.")]
    scripts.extend([route("investigate", ["model_names", "release_dates", "source_urls"]),
        turn(calls=[call("web_search", query="site:openai.com/index/ latest model", category="text")]),
        turn(calls=[call("fetch_raw", url=url)]),
        turn("Official models: GPT Phantom — 2026-09-28; GPT Real — 2026-09-29. " + url + " Arama kapsamı eksik."),
        checked([fact("model_names", "GPT Phantom", 0, "model: GPT Phantom"),
                 fact("release_dates", "2026-09-28", 0, "OpenAI released GPT Phantom on 2026-09-28."), *real_facts]),
        turn("GPT Real — 2026-09-29. " + url + " Arama kapsamı eksik; yalnız okunan birincil sayfa doğrulandı."),
        checked(real_facts)])
    report, events = await run(coordinator, tmp_path, "Verify OpenAI official model names, release dates and source links")
    assert report["success"] and "Official models: GPT Phantom" not in report["outcome"]
    assert "GPT Phantom" not in report["outcome"] and "2026-09-28" not in report["outcome"]
    assert report["metrics"]["turns"] == len(requests) == 7 and report["metrics"]["tool_calls"] == 2
    assert "Official models: GPT Phantom" not in json.dumps(events)
    assert report["evidence"]["observations"][0]["text"] == snippet
    assert report["evidence"]["observations"][1]["text"] == primary


@pytest.mark.asyncio
async def test_official_planning_precedes_search_and_post_search_generation(coordinator, scripted, tmp_path, monkeypatch):
    urls = ["https://openai.com/index/gpt-real/", "https://www.anthropic.com/news/claude-real"]
    observations = []
    def search(self, query, category="auto", **kwargs):
        observations.append((query, category))
        return json.dumps([{ "title": "OpenAI model release" if "openai" in query else "Anthropic model release",
                             "url": urls[0] if "openai" in query else urls[1], "body": "Specific model announcement."}])
    monkeypatch.setattr(agent.Toolbox, "web_search", search)
    monkeypatch.setattr(agent.Toolbox, "fetch_raw", lambda self, url: "model: " + ("GPT Real" if "openai" in url else "Claude Real"))
    scripts, requests = scripted
    snapshots = []
    def capture(item):
        async def boundary(emit, stop):
            snapshots.append(json.loads(json.dumps(requests[-1][0])))
            return item
        return boundary
    scripts.extend([route("investigate", ["model_names", "source_urls"]),
        capture(turn(calls=[call("web_search", query="site:openai.com/index/ latest model release", category="text"),
                    call("web_search", query="site:anthropic.com/news/ latest model release", category="text")])),
        capture(turn(calls=[call("fetch_raw", url=url) for url in urls])),
        turn("GPT Real; Claude Real. " + " ".join(urls) + " Arama kapsamı eksik; resmi duyuru sayfaları okundu."),
        checked([fact("model_names", "GPT Real", 2, "model: GPT Real"), fact("model_names", "Claude Real", 3, "model: Claude Real")])])
    report, _ = await run(coordinator, tmp_path, RESEARCH)
    assert report["success"] and report["metrics"]["turns"] == 5 and report["metrics"]["tool_calls"] == 4
    assert all(category == "text" for _, category in observations)
    first = "\n".join(message["content"] for message in snapshots[0] if message["role"] == "system")
    second = "\n".join(message["content"] for message in snapshots[1] if message["role"] == "system")
    assert 'category="text"' in first and "site:openai.com/index/" in first and "site:anthropic.com/news/" in first
    assert "READ SPECIFIC PRIMARY PAGES NOW" in second and urls[0] in second and urls[1] in second
    assert "not news indexes" in second and datetime.now(timezone.utc).date().isoformat() in second


def test_successful_official_news_index_is_not_specific_primary_model_evidence(tmp_path):
    store = EvidenceStore(tmp_path / "state.json")
    bundle = store.create(derive_request_contract(RESEARCH, route="investigate", required_fields=["model_names", "release_dates", "source_urls"]))
    store.capture(bundle, "fetch_raw", {"ok": True, "result": "model: Claude Feed\nAnthropic introduced Claude Feed on 2026-09-29."},
                  arguments=json.dumps({"url": "https://www.anthropic.com/news"}))
    assert any("anthropic.com" in gap for gap in primary_source_gaps(bundle))


def test_official_partial_fallback_is_readable_with_full_private_receipts(tmp_path):
    store = EvidenceStore(tmp_path / "state.json")
    bundle = store.create(derive_request_contract(RESEARCH, route="investigate", required_fields=["model_names", "release_dates", "source_urls"]))
    news = json.dumps([{"title": "OpenAI model third-party report", "url": f"https://news.example/{index}",
                        "body": "UNRELATED_NEWS_TEXT " * 80, "date": "2026-09-28"} for index in range(20)])
    store.capture(bundle, "web_search", {"ok": True, "result": news})
    store.capture(bundle, "fetch_raw", {"ok": False, "error_type": "FetchFailed", "error": "HTTP 404"}, arguments='{"url":"https://openai.com/news/"}')
    index_body = "model: Claude Feed\nAnthropic introduced Claude Feed on 2026-09-29.\n" + "RAW_FEED_MARKER " * 800
    store.capture(bundle, "fetch_raw", {"ok": True, "result": index_body}, arguments='{"url":"https://www.anthropic.com/news"}')
    rendered = render_evidence(bundle)
    assert len(rendered) < 1800
    assert "openai.com" in rendered and "anthropic.com" in rendered and "eksik" in rendered.casefold()
    assert "HTTP 404" in rendered and "birincil" in rendered.casefold()
    assert "UNRELATED_NEWS_TEXT" not in rendered and "RAW_FEED_MARKER" not in rendered
    saved = store.load(bundle["run_id"])
    assert saved["observations"][0]["text"] == news and saved["observations"][2]["text"] == index_body


def test_official_fallback_preserves_actual_primary_essential_identifiers(tmp_path):
    store = EvidenceStore(tmp_path / "state.json")
    bundle = store.create(derive_request_contract(RESEARCH, route="investigate", required_fields=["model_names", "release_dates", "source_urls"]))
    text = "model: GPT Real\nOpenAI released GPT Real on 2026-09-29.\n" + "Incidental page plumbing\n" * 900
    store.capture(bundle, "fetch_raw", {"ok": True, "result": text}, arguments='{"url":"https://openai.com/index/gpt-real/"}')
    rendered = render_evidence(bundle)
    assert "GPT Real" in rendered and "2026-09-29" in rendered and "https://openai.com/index/gpt-real/" in rendered
    assert "anthropic.com" in rendered and "eksik" in rendered.casefold()
    assert len(rendered) < 1800 and store.load(bundle["run_id"])["observations"][0]["text"] == text


@pytest.mark.asyncio
async def test_complete_retained_middle_facts_survive_model_context_exhaustion(tmp_path):
    from omniagent.app.conversation_grounding import ground_answer
    store = EvidenceStore(tmp_path / "state.json")
    bundle = store.create(derive_request_contract("Verify OpenAI official model names, release dates and source links", route="investigate", required_fields=["model_names", "release_dates", "source_urls"]))
    text = "Preface prose.\n" + "page plumbing\n" * 18000 + "\nmodel: GPT Exact Middle\nOpenAI released GPT Exact Middle on 2026-09-29.\n" + "page plumbing\n" * 14000
    source = store.capture(bundle, "fetch_raw", {"ok": True, "result": text}, arguments='{"url":"https://openai.com/index/middle/"}')
    assert not source["complete"] and "GPT Exact Middle" not in source["text"]
    assert store.observation_text(bundle, 0) == text
    async def no_model(messages):
        pytest.fail("full artifact exceeds the existing 80k model context")
    answer, success = await ground_answer("Draft", bundle, store, no_model)
    assert not success
    assert "GPT Exact Middle" in answer and "2026-09-29" in answer
    assert source["artifact_path"] in answer and "bağlam" in answer
    assert "sayfası okunamadı" not in answer
    assert len(answer) < 2500


@pytest.mark.asyncio
@pytest.mark.parametrize("failed,truncated", [(False, True), (True, False)])
async def test_retained_artifact_does_not_upgrade_incomplete_tool_capture(tmp_path, failed, truncated):
    from omniagent.app.conversation_grounding import ground_answer
    store = EvidenceStore(tmp_path / "state.json")
    bundle = store.create(derive_request_contract("Verify OpenAI official model names", route="investigate", required_fields=["model_names"]))
    text = "model: GPT Incomplete\n" + "page plumbing\n" * 32000
    source = store.capture(bundle, "fetch_raw", {"ok": not failed, "result": text, "truncated": truncated, "error": "HTTP 403"}, arguments='{"url":"https://openai.com/index/incomplete/"}')
    assert source["artifact_path"]
    async def no_model(messages):
        pytest.fail("incomplete or failed captures cannot certify primary facts")
    answer, success = await ground_answer("Draft", bundle, store, no_model)
    assert not success and "sayfası okunamadı" in answer
    assert "Kaynakta bulunan istenen bilgiler: GPT Incomplete" not in answer


@pytest.mark.asyncio
async def test_real_fetch_execution_retains_full_middle_facts_and_masks_presentation(coordinator, scripted, tmp_path, monkeypatch):
    import subprocess
    from omniagent import config
    from omniagent.tools import browser
    from omniagent.tools.filesystem import clean_html
    secret = "sk-retained-fetch-secret-123456789"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    url = "https://www.anthropic.com/news/claude-middle"
    primary = "page plumbing " * 700 + "model: Claude Exact Middle\nAnthropic released Claude Exact Middle on 2026-09-29.\n" + "page plumbing " * 700 + secret
    html = "<html><body><article>" + primary + "</article></body></html>"
    monkeypatch.setattr(browser, "_fetch_with_retries", lambda request: subprocess.CompletedProcess([], 0, html, ""))
    scripts, requests = scripted
    scripts.extend([route("investigate", ["model_names", "release_dates", "source_urls"]),
        turn(calls=[call("fetch_raw", url=url)]),
        turn("Claude Exact Middle — 2026-09-29. " + url),
        checked([fact("model_names", "Claude Exact Middle", 0, "model: Claude Exact Middle"), fact("release_dates", "2026-09-29", 0, "Anthropic released Claude Exact Middle on 2026-09-29.")])])
    report, events = await run(coordinator, tmp_path, "Verify Anthropic official model names, release dates and source links")
    assert report["success"] and report["metrics"]["tool_calls"] == 1
    source = report["evidence"]["observations"][0]
    assert source["complete"] and len(source["text"]) > 19000
    assert any("Claude Exact Middle" in message["content"] for message in requests[2][0] if message["role"] == "tool")
    assert "Claude Exact Middle" in source["text"] and "2026-09-29" in source["text"]
    assert secret not in source["text"] and secret not in json.dumps(events) and secret not in json.dumps(requests)
    assert all(len(event.get("text", "")) <= 4000 for event in events if event["kind"] == "tool_finished")
    assert len(clean_html(html)) < 5100  # generic UI cleaner remains bounded


def test_legacy_bounded_artifact_has_no_implicit_capture_upgrade(tmp_path):
    store = EvidenceStore(tmp_path / "state.json")
    bundle = store.create(derive_request_contract("Verify OpenAI official model names", route="investigate", required_fields=["model_names"]))
    source = store.capture(bundle, "fetch_raw", {"ok": True, "result": "model: GPT Legacy\n" + "page plumbing\n" * 32000}, arguments='{"url":"https://openai.com/index/legacy/"}')
    source.pop("captured_complete")
    assert not store.authoritative_bundle(bundle)["observations"][0]["complete"]
    assert primary_source_gaps(store.authoritative_bundle(bundle))
    source["captured_complete"] = "true"
    with pytest.raises(ValueError, match="Invalid source observation"):
        store.authoritative_bundle(bundle)


def test_missing_retained_artifact_is_honest_and_does_not_certify(tmp_path):
    from pathlib import Path
    store = EvidenceStore(tmp_path / "state.json")
    bundle = store.create(derive_request_contract("Verify OpenAI official model names", route="investigate", required_fields=["model_names"]))
    source = store.capture(bundle, "fetch_raw", {"ok": True, "result": "model: GPT Missing\n" + "page plumbing\n" * 32000}, arguments='{"url":"https://openai.com/index/missing/"}')
    Path(source["artifact_path"]).unlink()
    rendered = render_evidence(bundle, store)
    assert "Tam kaynak dosyası okunamadı" in rendered
    assert "Kaynakta bulunan istenen bilgiler" not in rendered
