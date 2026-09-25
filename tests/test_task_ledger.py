"""
task_ledger.py için deterministik ve alan-bağımsız (domain-agnostic) testler:
Genel anahtar-değer çiftleri, sistem durumları, JSON ağaçları, çok dilli metrikler ve prompt formatlama.
"""
from typing import Any, Dict, List
from omniagent.core import task_ledger as tl


def test_empty_task_ledger_initializes_empty() -> None:
    ledger = tl.empty_task_ledger()
    assert ledger["facts"] == {}
    assert ledger["model_state"] == ""
    assert ledger["turn"] == 0


def test_extract_facts_from_system_and_devops_output() -> None:
    output = (
        "Server status report:\n"
        "Status: Running\n"
        "IP Address: 192.168.1.50\n"
        "Exit code: 0\n"
        "CPU Usage: 14.5%\n"
        "Active Workers: 8\n"
    )
    facts = tl.extract_facts_from_text(output, "execute_shell", 1)
    keys = {f["key"]: f["value"] for f in facts}
    assert keys["status"] == "Running"
    assert keys["ip_address"] == "192.168.1.50"
    assert keys["exit_code"] == "0"
    assert keys["cpu_usage"] == "14.5%"
    assert keys["active_workers"] == "8"


def test_extract_facts_from_markdown_labels() -> None:
    md_text = (
        "- **Docker Container:** redis-cluster\n"
        "- **Port:** 6379\n"
        "- Total Stars: 1250\n"
        "- Average Latency = 12ms\n"
    )
    facts = tl.extract_facts_from_text(md_text, "execute_shell", 1)
    keys = {f["key"]: f["value"] for f in facts}
    assert keys["docker_container"] == "redis-cluster"
    assert keys["port"] == "6379"
    assert keys["total_stars"] == "1250"
    assert keys["average_latency"] == "12ms"


def test_extract_facts_from_multilingual_and_turkish_text() -> None:
    text = (
        "Lead Full Stack Developer · Remote\n"
        "Aylık maaş: 6300 €\n"
        "İlan kodu: IL-AUR991\n"
        "Toplam Başvuru: 42\n"
    )
    facts = tl.extract_facts_from_text(text, "cua_read_scrollable", 1)
    keys = {f["key"]: f["value"] for f in facts}
    assert keys["aylik_maas"] == "6300 €"
    assert keys["ilan_kodu"] == "IL-AUR991"
    assert keys["toplam_basvuru"] == "42"


def test_extract_facts_from_json_content() -> None:
    json_text = '{"database": {"host": "db.internal", "port": 5432}, "count": 42}'
    facts = tl.extract_facts_from_text(json_text, "fetch_raw", 2)
    keys = {f["key"]: f["value"] for f in facts}
    assert keys["database.host"] == "db.internal"
    assert keys["database.port"] == "5432"
    assert keys["count"] == "42"


def test_extract_facts_from_uppercase_labels() -> None:
    shell_output = "GUN: Tuesday\nSATIR: 2000\nTOPLAM: 31\nKODLAR: KOD-1, KOD-2"
    facts = tl.extract_facts_from_text(shell_output, "execute_shell", 1)
    keys = {f["key"]: f["value"] for f in facts}
    assert keys["gun"] == "Tuesday"
    assert keys["satir"] == "2000"
    assert keys["toplam"] == "31"
    assert keys["kodlar"] == "KOD-1, KOD-2"


def test_record_tool_result_is_pure_and_merges() -> None:
    ledger0 = tl.empty_task_ledger()
    ledger1 = tl.record_tool_result(ledger0, "execute_shell", "Memory: 8GB", 1)
    assert ledger0["facts"] == {}  # Saf fonksiyon: girdi değişmez
    assert ledger1["facts"]["memory"]["value"] == "8GB"

    ledger2 = tl.record_tool_result(ledger1, "execute_shell", "Memory: 16GB\nCPU: 4 cores", 2)
    assert ledger2["facts"]["memory"]["value"] == "16GB"
    assert ledger2["facts"]["cpu"]["value"] == "4 cores"
    assert ledger2["turn"] == 2


def test_record_model_state_captures_state_block() -> None:
    ledger = tl.empty_task_ledger()
    assistant_reply = "Sunucuyu kontrol ediyorum.\n\nSTATE:\nFACTS: cpu=4, memory=16GB\nREMAINING: disk kontrolü yap"
    updated = tl.record_model_state(ledger, assistant_reply)
    assert updated["model_state"].startswith("STATE:")
    assert "memory=16GB" in updated["model_state"]


def test_format_ledger_prompt_lists_alphabetically() -> None:
    ledger = tl.empty_task_ledger()
    ledger = tl.record_tool_result(ledger, "execute_shell", "Status: Active\nCPU: 10%", 1)
    prompt = tl.format_ledger_prompt(ledger)
    assert "### TASK SCRATCHPAD (Host-Verified Facts)" in prompt
    assert "- cpu: 10% (via execute_shell)" in prompt
    assert "- status: Active (via execute_shell)" in prompt


def test_inject_task_ledger_into_messages_empty() -> None:
    ledger = tl.empty_task_ledger()
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user goal"},
    ]
    injected = tl.inject_task_ledger_into_messages(messages, ledger)
    assert injected == messages


def test_inject_task_ledger_into_messages_with_user_msg() -> None:
    ledger = tl.empty_task_ledger()
    ledger = tl.record_tool_result(ledger, "execute_shell", "Status: Active", 1)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user goal"},
    ]
    injected = tl.inject_task_ledger_into_messages(messages, ledger)
    assert len(injected) == 2
    assert "user goal" in injected[-1]["content"]
    assert "### TASK SCRATCHPAD (Host-Verified Facts)" in injected[-1]["content"]
    assert "Active" in injected[-1]["content"]


def test_inject_task_ledger_into_messages_with_tool_msg() -> None:
    ledger = tl.empty_task_ledger()
    ledger = tl.record_tool_result(ledger, "execute_shell", "GUN: Tuesday", 1)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user goal"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "name": "execute_shell", "arguments": "{}"}]},
        {"role": "tool", "content": "GUN: Tuesday"},
    ]
    injected = tl.inject_task_ledger_into_messages(messages, ledger)
    assert len(injected) == 5
    assert injected[-1]["role"] == "user"
    assert "Tuesday" in injected[-1]["content"]


def test_inject_task_ledger_into_multimodal_message() -> None:
    ledger = tl.empty_task_ledger()
    ledger = tl.record_tool_result(ledger, "execute_shell", "PID: 12345", 1)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": [{"type": "image_url", "image_url": "data:..."}, {"type": "text", "text": "obs"}]},
    ]
    injected = tl.inject_task_ledger_into_messages(messages, ledger)
    assert len(injected) == 2
    content = injected[-1]["content"]
    assert isinstance(content, list)
    text_parts = [p["text"] for p in content if p.get("type") == "text"]
    joined = "\n".join(text_parts)
    assert "12345" in joined
