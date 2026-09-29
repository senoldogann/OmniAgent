"""API anahtarına dayanan model profillerinin yapılandırma regresyonları."""
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Set

import pytest

from omniagent import config
from omniagent.app import agent as main

def test_missing_key_warning_state_is_injectable() -> None:
    """Uyarı kaydı dışarıdan verilebilir; süreç düzeyi kaydı kirletilmez (test izolasyonu)."""
    profiles: Dict[str, Any] = {
        "openai": {
            "provider": "openai", "base_url": "https://api.openai.com/v1", "api_key": None,
            "model": "gpt-test", "max_tokens": 8, "session_header": None,
            "extra_headers": {}, "extra_body": {},
        },
    }
    main.model_runtime.reset_missing_key_warnings()
    warned: Set[str] = set()
    clients = main.model_runtime.create_model_clients(
        backends=profiles, api_key_variables={"openai": "OPENAI_API_KEY"},
        ollama_ready=lambda: False, warned=warned,
    )
    assert clients == {}
    assert warned == {"openai"}
    assert main.model_runtime._MISSING_KEY_WARNED == set()


PROBE: str = (
    "import json; from omniagent import config;"
    "print(json.dumps({"
    "'backends': sorted(config.BACKENDS),"
    "'ladder': list(config.QUALITY_LADDER),"
    "'default': config.DEFAULT_BACKEND,"
    "'keys': {name: profile['api_key'] for name, profile in config.BACKENDS.items()},"
    "'key_env': config.API_KEY_VARIABLES,"
    "}))"
)
KEY_VARIABLES: tuple[str, ...] = ("OPENAI_API_KEY", "OPENCODE_API_KEY", "OPENROUTER_API_KEY")


def _probe(overrides: Dict[str, str]) -> Dict[str, Any]:
    """config'i anahtarlardan arındırılmış temiz bir alt süreçte yükleyip haritasını döner."""
    environment: Dict[str, str] = {**os.environ, **overrides}
    for variable in KEY_VARIABLES:
        if variable not in overrides:
            environment.pop(variable, None)
    result = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=Path(__file__).parents[1],
        env=environment, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_profiles_are_api_key_only_and_ladder_matches() -> None:
    """Oturum tabanlı CLI profilleri kalmadı; merdiven yalnız kalan API profillerini içerir."""
    probe = _probe({})
    assert probe["backends"] == ["ollama-cloud", "openai", "opencode", "opencode-think", "openrouter"]
    assert probe["ladder"] == ["ollama-cloud", "openai", "openrouter"]
    assert probe["default"] == "ollama-cloud"
    assert set(probe["ladder"]) <= set(probe["backends"])
    assert probe["key_env"] == {
        "openai": "OPENAI_API_KEY",
        "opencode": "OPENCODE_API_KEY",
        "opencode-think": "OPENCODE_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    # Anahtar tanımlı değilse profil kullanılamaz sayılır; Ollama yerel sunucudan doğrulanır.
    assert probe["keys"]["ollama-cloud"] == "ollama"
    for name in ("openai", "opencode", "opencode-think", "openrouter"):
        assert probe["keys"][name] is None


def test_environment_variables_populate_matching_profiles() -> None:
    """Her ortam değişkeni yalnız kendi profillerine anahtar olarak işlenir."""
    probe = _probe({
        "OPENAI_API_KEY": " openai-key ",
        "OPENCODE_API_KEY": "opencode-key",
        "OPENROUTER_API_KEY": "openrouter-key",
    })
    assert probe["keys"]["openai"] == "openai-key"
    assert probe["keys"]["opencode"] == "opencode-key"
    assert probe["keys"]["opencode-think"] == "opencode-key"
    assert probe["keys"]["openrouter"] == "openrouter-key"
    assert probe["keys"]["ollama-cloud"] == "ollama"


def test_load_api_key_rejects_blank_and_missing_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boş veya yalnız boşluk içeren değer anahtar sayılmaz; çevresel boşluk kırpılır."""
    monkeypatch.setenv("OMNI_TEST_KEY", "  gizli  ")
    assert config.load_api_key("OMNI_TEST_KEY") == "gizli"
    monkeypatch.setenv("OMNI_TEST_KEY", "   ")
    assert config.load_api_key("OMNI_TEST_KEY") is None
    monkeypatch.delenv("OMNI_TEST_KEY", raising=False)
    assert config.load_api_key("OMNI_TEST_KEY") is None


@pytest.mark.asyncio
async def test_keyless_profile_is_skipped_with_clear_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Anahtarı eksik profil istemci kurmaz; hangi değişkenin gerektiği uyarıyla bildirilir."""
    monkeypatch.setattr(main, "ollama_cloud_ready", lambda: False)
    monkeypatch.setattr(main, "BACKENDS", {
        "openai": {**main.BACKENDS["openai"], "api_key": "test-key"},
        "openrouter": {**main.BACKENDS["openrouter"], "api_key": None},
    })
    with caplog.at_level(logging.WARNING):
        clients = main.create_model_clients()
    try:
        assert list(clients) == ["openai"]
        assert clients["openai"].base_url.host == "api.openai.com"
    finally:
        await main.close_model_clients(clients)
    assert any(
        record.levelno == logging.WARNING
        and getattr(record, "backend", "") == "openrouter"
        and getattr(record, "variable", "") == "OPENROUTER_API_KEY"
        for record in caplog.records
    )


FALLBACK_PROBE: str = (
    "import json; from omniagent import fallback_policy as policy;"
    "print(json.dumps(policy.load_fallback_policy()))"
)


def test_default_fallback_policy_is_strict_in_clean_process(tmp_path: Path) -> None:
    """Ortam ve kayıt yokken (temiz süreç) yedek sağlayıcı izni kapalıdır: istek yalnız seçili sağlayıcıya gider."""
    environment: Dict[str, str] = {**os.environ, "OMNI_DATA_DIR": str(tmp_path)}
    for variable in ("OMNI_FALLBACK_BACKENDS", "OMNI_FALLBACK_IMAGES"):
        environment.pop(variable, None)
    result = subprocess.run(
        [sys.executable, "-c", FALLBACK_PROBE], cwd=Path(__file__).parents[1],
        env=environment, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"backends": [], "allow_images": False}


def test_session_based_cli_connector_is_removed() -> None:
    """Bilgisayardaki oturumu kullanan CLI bağlayıcısı geri sızmasın."""
    assert not (Path(__file__).parents[1] / "cli_backends.py").exists()
