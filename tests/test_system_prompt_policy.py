from __future__ import annotations

from omniagent import config


def test_system_prompt_matches_host_enforced_policy() -> None:
    prompt = config.SYSTEM_PROMPT.casefold()

    assert "no safety rails" not in prompt
    assert "bypass or elevate permissions" not in prompt
    assert "host-enforced approval" in prompt
    assert "never attempt to bypass" in prompt
