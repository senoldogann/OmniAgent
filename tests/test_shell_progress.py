"""Yeni komut çıktısı Fast Loop ilerlemesidir; tekrar ve boş çıktı değildir."""
import main


def _call(identifier: str) -> main.ToolCallDraft:
    return {"id": identifier, "name": "execute_shell", "arguments": "{}"}


def _result(text: str, ok: bool = True) -> main.ToolResult:
    return {"tool_call_id": "x", "ok": ok, "result": f"STDOUT: {text}\nSTDERR: \nÇıkış Kodu: 0"}


def test_novel_successful_shell_output_counts_once() -> None:
    seen: frozenset[str] = frozenset()
    seen, first = main.novel_shell_output_progress([_call("a")], [_result("OZET: 42")], seen)
    assert first
    seen, repeated = main.novel_shell_output_progress([_call("b")], [_result("OZET: 42")], seen)
    assert not repeated
    seen, changed = main.novel_shell_output_progress([_call("c")], [_result("OZET: 43")], seen)
    assert changed


def test_failed_and_empty_shell_outputs_do_not_count() -> None:
    seen: frozenset[str] = frozenset()
    after_failure, failed = main.novel_shell_output_progress([_call("a")], [_result("OZET: 42", False)], seen)
    after_empty, empty = main.novel_shell_output_progress([_call("b")], [_result("")], seen)
    assert not failed and not empty
    assert after_failure == after_empty == seen
