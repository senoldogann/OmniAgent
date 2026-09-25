"""Task-ledger, bütçe ve host ilerleme sinyallerini hesaplayan yardımcılar."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from omniagent.app.constants import (
    MAX_NOVEL_READ_OUTPUTS,
    MAX_NOVEL_SHELL_OUTPUTS,
    SOFT_TOOL_CALL_BUDGET,
    SOFT_UNCACHED_PROMPT_TOKEN_BUDGET,
    TASK_LEDGER_LIMIT,
    TRIMMED_ARGS_LIMIT,
    TRIMMED_CONTENT_LIMIT,
)
from omniagent.app.tool_execution import result_text
from omniagent.app.tool_schema import (
    _DETERMINISTIC_PROGRESS_TOOLS,
    _READ_PROGRESS_TOOLS,
    _SCREEN_ACTION_TOOLS,
)
from omniagent.app.types import ToolCallDraft, ToolResult
from omniagent.config import redact
from omniagent.core.events import TokenUsage
from omniagent.core.fast_loop import normalize_progress_signature


def add_usage(total: TokenUsage, turn: TokenUsage) -> TokenUsage:
    """İki token kullanımını toplar. Saf."""
    return {
        "prompt_tokens": total["prompt_tokens"] + turn["prompt_tokens"],
        "cached_tokens": total["cached_tokens"] + turn["cached_tokens"],
        "completion_tokens": total["completion_tokens"] + turn["completion_tokens"],
    }


def budget_pressure_message(usage: TokenUsage, tool_call_count: int) -> Optional[str]:
    """Uzun görevin bütçe baskısını kısa, eyleme dönük bir model mesajına çevirir. Saf fonksiyon."""
    uncached: int = max(0, usage["prompt_tokens"] - usage["cached_tokens"])
    reasons: List[str] = []
    if uncached >= SOFT_UNCACHED_PROMPT_TOKEN_BUDGET:
        reasons.append(f"{uncached} önbelleksiz giriş tokenı")
    if tool_call_count >= SOFT_TOOL_CALL_BUDGET:
        reasons.append(f"{tool_call_count} araç çağrısı")
    if not reasons:
        return None
    return (
        "ÇALIŞMA BÜTÇESİ UYARISI: " + ", ".join(reasons) + ". "
        "Görevi bırakma; yeni/opsiyonel keşfi ve gereksiz tekrar kontrollerini durdur. "
        "STATE içindeki doğrulanmış bilgileri yeniden kullan ve kalan zorunlu hesaplama, yazma, "
        "final doğrulama ve cleanup adımlarını en kısa yoldan tamamla."
    )


def extract_task_ledger(current: str, content: str) -> str:
    """Assistant metnindeki STATE bloğunu dayanıklı, sınırlı çalışma kaydına dönüştürür."""
    match = re.search(r"(?im)^\s*STATE\s*:", content)
    if match is None:
        return current
    return content[match.start():].strip()[:TASK_LEDGER_LIMIT]


def _ledger_delivery_ready(ledger: str) -> bool:
    """Model açıkça kalan zorunlu iş olmadığını kaydetti mi? Saf ve muhafazakâr."""
    return bool(re.search(
        r"(?im)^\s*(?:REMAINING|KALAN)\s*:\s*(?:0|none|nothing|yok|tamamlandı|complete)\s*$",
        ledger,
    ))


def _call_signature_arguments(call: ToolCallDraft) -> Dict[str, Any]:
    try:
        value: object = json.loads(call["arguments"] or "{}")
    except json.JSONDecodeError:
        return {"raw": call["arguments"][:TRIMMED_ARGS_LIMIT]}
    return value if isinstance(value, dict) else {"value": value}


def turn_progress_signature(
    calls: List[ToolCallDraft], results: List[ToolResult], observation_digest: Optional[str],
    ledger_digest: str, unresolved_deliverables: int,
) -> str:
    """GUI'de eylem koordinatını değil gözlenen sonucu; diğer araçlarda hedef/sonucu imzalar."""
    visual: bool = observation_digest is not None
    tool_facts: List[Tuple[str, Dict[str, Any]]] = []
    result_facts: List[str] = []
    for call, result in zip(calls, results, strict=True):
        is_screen_action: bool = visual and call["name"] in _SCREEN_ACTION_TOOLS
        tool_facts.append((
            call["name"],
            {} if is_screen_action else _call_signature_arguments(call),
        ))
        if is_screen_action and result.get("ok"):
            result_facts.append("ok")
        else:
            result_facts.append(result_text(result)[:TRIMMED_CONTENT_LIMIT])
    return normalize_progress_signature(
        tool_facts=tool_facts,
        result_facts=result_facts,
        observation_digest=observation_digest,
        ledger_digest=ledger_digest,
        unresolved_deliverables=unresolved_deliverables,
    )


def novel_shell_output_progress(
    calls: List[ToolCallDraft], results: List[ToolResult], seen: frozenset[str],
) -> Tuple[frozenset[str], bool]:
    """Yeni başarılı komut çıktısını sınırlı sayıda ilerleme sayar; tekrarları saymaz."""
    updated = set(seen)
    progressed = False
    for call, result in zip(calls, results, strict=True):
        if call["name"] != "execute_shell" or not result.get("ok"):
            continue
        rendered = redact(str(result.get("result", "")))
        if not rendered.startswith("STDOUT:"):
            continue
        stdout = rendered[len("STDOUT:"):].split("\nSTDERR:", 1)[0].strip()
        if not stdout:
            continue
        digest = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
        if digest not in updated and len(updated) < MAX_NOVEL_SHELL_OUTPUTS:
            updated.add(digest)
            progressed = True
    return frozenset(updated), progressed


def novel_read_output_progress(
    calls: List[ToolCallDraft], results: List[ToolResult], seen: frozenset[str],
) -> Tuple[frozenset[str], bool]:
    """Yeni başarılı okuma/web çıktısını sınırlı sayıda ilerleme sayar; aynı içeriği tekrar saymaz."""
    updated = set(seen)
    progressed = False
    for call, result in zip(calls, results, strict=True):
        if call["name"] not in _READ_PROGRESS_TOOLS or not result.get("ok"):
            continue
        rendered = redact(str(result.get("result", ""))).strip()
        if not rendered:
            continue
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        if digest not in updated and len(updated) < MAX_NOVEL_READ_OUTPUTS:
            updated.add(digest)
            progressed = True
    return frozenset(updated), progressed


def host_turn_progress(
    calls: List[ToolCallDraft], results: List[ToolResult], observation_digest: Optional[str],
    signature: str, previous_signature: Optional[str],
) -> bool:
    """
    Model STATE yazmasa bile host'un güvenle doğrulayabildiği ilerleme.
    - Settled görsel gerçekten değiştiyse GUI ilerlemiştir.
    - Görsel yoksa yalnız deterministik teslim araçları yeni başarılı sonuçla ilerleme sayılır.
    - Aynı imza (aynı ekran dahil) sırf yeni click/fetch hedefi yüzünden ilerleme sayılmaz.
    """
    if previous_signature is None or signature == previous_signature:
        return False
    if observation_digest is not None:
        return True
    return any(
        call["name"] in _DETERMINISTIC_PROGRESS_TOOLS and bool(result.get("ok"))
        for call, result in zip(calls, results, strict=True)
    )


def _fast_loop_prompt(kind: str, ledger: str) -> str:
    ledger_text: str = ledger or "STATE: henüz kalıcı görev kaydı yok"
    if kind == "replan":
        instruction = (
            "HOST FAST LOOP — YENİDEN PLAN: Anlamlı ilerleme durdu. Aynı başarısız çağrıyı "
            "tekrarlama. STATE ve hata sonucundan nedeni çıkar; görevde kullanılabilen araçlarla "
            "tek somut alternatif dene. Hazır yol yoksa ve hizmet gerektiriyorsa en çok bir hedefli "
            "yetenek keşfi yap. Zorunlu işi en az turla tamamla."
        )
    else:
        instruction = (
            "HOST FAST LOOP — TESLİM MODU: Opsiyonel keşfi bırak. Yalnız kalan zorunlu hesaplama, "
            "dosya/rapor yazma, açıkça istenen final doğrulama ve cleanup adımlarını tamamla."
        )
    return f"{instruction}\n\n{ledger_text}"
