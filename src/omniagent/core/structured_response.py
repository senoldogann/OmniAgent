"""Complete provider JSON responses, optionally inside one complete Markdown fence."""
from __future__ import annotations

import json
import re
from typing import Any

_JSON_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", re.I)


def parse_complete_json(content: str) -> Any:
    """Accept JSON or a whole JSON fence; never recover fragments or surrounding prose."""
    payload = content.strip()
    if payload.startswith("```"):
        matched = _JSON_FENCE.fullmatch(payload)
        if matched is None:
            raise ValueError("Incomplete or unsupported JSON fence")
        payload = matched[1]
    return json.loads(payload)
