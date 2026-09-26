"""Deterministic benchmark scenario contracts without model/API execution."""
import subprocess
from pathlib import Path

from omniagent.dev import benchmark
from omniagent.memory import user as user_memory


def test_long_research_scenario_requires_full_delivery_contract(tmp_path: Path) -> None:
    run_id = "abc12345"
    scenario = benchmark.build_scenario("long_research", tmp_path, run_id, 8765)

    assert "en az 8" in scenario["goal"].casefold()
    assert "tam 5" in scenario["goal"].casefold()
    assert "TOTAL_STARS" in scenario["goal"]
    assert "AVERAGE_STARS" in scenario["goal"]
    assert "RATIO" in scenario["goal"]
    assert str(tmp_path / "github-typescript-research.txt") in scenario["goal"]
    assert "/verify/0" in scenario["goal"]
    assert "/verify/7" in scenario["goal"]


def test_long_research_checker_rejects_missing_delivery_artifact(tmp_path: Path) -> None:
    scenario = benchmark.build_scenario("long_research", tmp_path, "abc12345", 8765)
    ok, detail = scenario["check"]("LONG_RESEARCH: DONE")
    assert ok is False
    assert "rapor" in detail.casefold()


def test_long_research_expected_selection_is_first_eight_only() -> None:
    selected = benchmark.expected_research_selection()
    assert [item["index"] for item in selected] == [0, 2, 4, 6, 7]
    assert sum(item["stars"] for item in selected) == 145_000
    assert benchmark.research_statistics(selected) == (145_000, 29_000.0, 3.33)


def test_stagnation_scenario_is_expected_bounded_failure(tmp_path: Path) -> None:
    scenario = benchmark.build_scenario("stagnation", tmp_path, "deadbeef", 8123)
    assert scenario["expect_success"] is False
    assert scenario["reason_contains"] == "ilerleme"
    assert "/stagnation/deadbeef" in scenario["goal"]


def test_stress_scenarios_are_not_in_default_core_suite() -> None:
    assert "long_research" in benchmark.STRESS_SCENARIOS
    assert "stagnation" in benchmark.STRESS_SCENARIOS
    assert "long_research" not in benchmark.CORE_SCENARIOS
    assert "stagnation" not in benchmark.CORE_SCENARIOS


def test_learning_scenario_shares_store_only_when_learning(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    learning = benchmark.build_scenario("ogrenme", tmp_path / "a", "abc12345", 8765)
    control = benchmark.build_scenario("ogrenme_bos", tmp_path / "b", "abc12345", 8765)
    assert learning["experience_file"] == str(tmp_path / "ogrenme-deneyim.json")
    assert "experience_file" not in control
    tool = tmp_path / "a" / "veri-araci"
    failed = subprocess.run([str(tool), "ozet", "kuzey"], capture_output=True, text=True)
    fixed = subprocess.run([str(tool), "ozet", "kuzey", "--birim=adet"], capture_output=True, text=True)
    assert failed.returncode == 2 and "E17" in failed.stderr and "birim" not in failed.stderr
    assert fixed.stdout.strip() == "OZET: 42"
    assert "ogrenme" in benchmark.SEQUENTIAL_SCENARIOS and "ogrenme" not in benchmark.CORE_SCENARIOS


def test_memory_scenario_seeds_report_folder_preference(tmp_path: Path) -> None:
    scenario = benchmark.build_scenario("hafiza", tmp_path, "abc12345", 8765)
    records = user_memory.load_memory(str(tmp_path / "user_memory.json"))["preferences"]
    assert records[0]["value"] == str(tmp_path / "raporlar")
    assert "raporlar" not in scenario["goal"]
    assert scenario["check"]("TAMAM")[0] is False


def test_self_repair_scenario_is_sequential_and_shares_experience_store(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = benchmark.build_scenario("self_repair", first_dir, "abc12345", 8765)
    second = benchmark.build_scenario("self_repair", second_dir, "def67890", 8765)

    assert first["experience_file"] == second["experience_file"] == str(tmp_path / "self-repair-experience.json")
    assert "self_repair" in benchmark.STRESS_SCENARIOS
    assert "self_repair" in benchmark.SEQUENTIAL_SCENARIOS
    assert first["check"]("OZET: 42")[0] is True


def test_open_chrome_test_tab_falls_back_to_visible_keyboard(monkeypatch) -> None:
    shell_calls: list[list[str]] = []
    keys: list[str] = []
    typed: list[str] = []

    def timeout_script(script: str, argument: str) -> None:
        raise benchmark.subprocess.TimeoutExpired(["osascript"], timeout=20)

    def fake_run(args, **kwargs):
        shell_calls.append(args)
        return benchmark.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(benchmark, "run_osascript", timeout_script)
    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)
    monkeypatch.setattr(benchmark, "_require_accessibility", lambda: None, raising=False)
    monkeypatch.setattr(benchmark, "press_key_spec", lambda key: keys.append(key) or key, raising=False)
    monkeypatch.setattr(benchmark, "type_unicode_text", lambda text: typed.append(text), raising=False)

    target = "http://127.0.0.1:5555/hazir"
    benchmark.open_chrome_test_tab(target)

    assert shell_calls == [["open", "-a", "Google Chrome"]]
    assert keys == ["cmd+t", "enter"]
    assert typed == [target]


def test_close_chrome_test_tabs_falls_back_to_active_test_tab(monkeypatch) -> None:
    shell_calls: list[list[str]] = []
    keys: list[str] = []

    def timeout_script(script: str, argument: str) -> None:
        raise benchmark.subprocess.TimeoutExpired(["osascript"], timeout=20)

    def fake_run(args, **kwargs):
        shell_calls.append(args)
        return benchmark.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(benchmark, "run_osascript", timeout_script)
    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)
    monkeypatch.setattr(benchmark, "_require_accessibility", lambda: None, raising=False)
    monkeypatch.setattr(benchmark, "press_key_spec", lambda key: keys.append(key) or key, raising=False)

    benchmark.close_chrome_test_tabs("http://127.0.0.1:5555/")

    assert shell_calls == [["open", "-a", "Google Chrome"]]
    assert keys == ["cmd+w"]


def test_command_line_entry_point_validates_and_runs_selected_scenarios(monkeypatch) -> None:
    """`omniagent-benchmark` betiği argümansız çağrılan senkron main'i çalıştırır."""
    import pytest

    runs: list[tuple] = []
    applied: list[bool] = []

    async def fake_run(*arguments) -> None:
        runs.append(arguments)

    monkeypatch.setattr(benchmark, "run_benchmark", fake_run)
    monkeypatch.setattr(benchmark, "apply_stored_api_keys", lambda: applied.append(True))
    benchmark.main(["--runs", "2", "--concurrency", "3", "--only", "gun,json"])
    assert runs == [(2, 3, None, ["gun", "json"], None, benchmark.CHROME_STAGE)] and applied == [True]

    for invalid in (["--runs", "1", "--concurrency", "2", "--only", "chrome_ilan"],
                    ["--runs", "1", "--concurrency", "1", "--only", "yok"]):
        with pytest.raises(SystemExit):
            benchmark.main(invalid)
    assert len(runs) == 1 and applied == [True]  # geçersiz argümanda anahtar deposuna dokunulmaz

    closed: list[bool] = []

    class FakePage:
        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(benchmark, "HeadlessPage", FakePage)
    monkeypatch.setattr(benchmark, "install_headless_screen", lambda page: None)
    monkeypatch.setattr(benchmark, "headless_stage", lambda page: "görünmez sahne")
    benchmark.main(["--runs", "1", "--concurrency", "1", "--only", "chrome_maas", "--headless"])
    assert runs[-1][-1] == "görünmez sahne" and closed == [True]
