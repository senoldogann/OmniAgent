"""Araç çağrısı doğrulama, onay, yürütme, cache ve sonuç olayları."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from omniagent import approval
from omniagent.app.tool_schema import (
    TOOL_NAMES,
    _CACHEABLE_TOOLS,
    _SIDE_EFFECT_TOOLS,
)
from omniagent.app.types import ToolCallDraft, ToolResult
from omniagent.config import redact
from omniagent.core.events import EventSink, preview_arguments
from omniagent.integrations.capabilities import ToolEntry, validate_arguments
from omniagent.integrations.runtime import (
    CURRENT_RUNTIME,
    CURRENT_SERVICE,
    IntegrationRuntime,
    IntegrationStopped,
    InteractionRequired,
    data_root,
)
from omniagent.tools import (
    TOOL_RUNTIME,
    ToolError,
    Toolbox,
    ToolRuntime,
    shell_command_words,
)


CALL_LABEL_ARGS_LIMIT: int = 100
EVENT_RESULT_LIMIT: int = 4000


def _tool_cache_key(name: str, arguments: Dict[str, Any]) -> str:
    """Önbellek anahtarı: araç adı + kararlı (sıralı) argüman JSON'u."""
    return name + ":" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def approval_request_for_call(
    name: str, arguments: Dict[str, Any], dynamic: Optional[ToolEntry], memory_mutation_allowed: bool,
) -> Optional[approval.ApprovalRequest]:
    """
    Çağrının çalışmadan önce kullanıcı onayı gerektirip gerektirmediğine karar verir: hedefin
    açıkça istemediği kalıcı hafıza değişikliği, finansal işaretli entegrasyon araçları ve para
    hareketi yapan kabuk/JS çağrıları. Ayrıştırılamayan kabuk komutu için onay istenmez; araç
    kendisi reddeder. Saf fonksiyon.
    """
    if name == "user_memory":
        action: str = str(arguments.get("action", "")).strip().casefold()
        if action in ("remember", "forget") and not memory_mutation_allowed:
            return approval.memory_request(arguments)
        return None
    if dynamic is not None:
        if dynamic.get("financial", False) and not dynamic["readonly"]:
            return approval.financial_request(dynamic.get("label", name), arguments, "finansal entegrasyon işlemi")
        return None
    reason: Optional[str] = None
    if name == "execute_shell":
        command: str = str(arguments.get("command", ""))
        try:
            reason = approval.shell_financial_reason(shell_command_words(command), command)
        except ToolError:
            return None
    elif name == "execute_js":
        reason = approval.script_financial_reason(str(arguments.get("code", "")))
    return approval.financial_request(name, arguments, reason) if reason is not None else None


_APPROVAL_REFUSALS: Dict[str, Tuple[str, str]] = {
    "unavailable": (
        "Bu işlem kullanıcı onayı gerektiriyor ama etkileşimli kanal (arayüz/Telegram) yok; yapılmadı. "
        "Onay gerektiğini final yanıtında açıkça bildir.", "APPROVAL_UNAVAILABLE",
    ),
    "timeout": (
        f"Kullanıcı onayı {approval.APPROVAL_TIMEOUT_SECONDS / 60:.0f} dakika içinde gelmedi; işlem yapılmadı.",
        "APPROVAL_TIMEOUT",
    ),
    "denied": ("Kullanıcı bu işlemi onaylamadı; yapılmadı. Aynı işlemi yeniden deneme.", "APPROVAL_DENIED"),
}


async def require_approval(tool: str, request: approval.ApprovalRequest) -> None:
    """
    Onayı arayüz/Telegram üzerinden sorar ve kararı (onay/ret/zaman aşımı/kanal yok) denetim
    kaydına yazar; kayıt yazılamazsa işlem yürütülmez. Onaylanmayan çağrı ToolError ile
    reddedilir; kullanıcı görevi durdurursa IntegrationStopped yükselir.
    """
    runtime: Optional[IntegrationRuntime] = CURRENT_RUNTIME.get()
    decision: str = "unavailable"
    if runtime is not None and runtime.answer is not None:
        try:
            answer: Dict[str, Any] = await runtime.ask(
                request["title"], approval.approval_fields(request), approval.APPROVAL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            decision = "timeout"
        else:
            decision = "approved" if approval.approval_granted(answer.get(approval.APPROVAL_FIELD)) else "denied"
    approval.append_audit(
        data_root() / "audit.jsonl",
        approval.audit_record(tool, request, decision, datetime.now(timezone.utc).isoformat()),
    )
    if decision != "approved":
        message, code = _APPROVAL_REFUSALS[decision]
        raise ToolError(message, code, False)


async def execute_tool(
    call: ToolCallDraft, toolbox: Toolbox, cache: Dict[str, ToolResult], emit: EventSink,
    should_stop: Callable[[], bool],
) -> ToolResult:
    """
    Tek bir araç çağrısını çalıştırır; başarı/hata durumunu yapılandırılmış şekilde döner.
    Çalışırken üretilen canlı çıktı (komut satırları) tool_output olayı olarak yayınlanır;
    kullanıcı durdurursa çalışan komut hemen sonlandırılır.
    """
    name: str = call["name"]
    try:
        arguments: Dict[str, Any] = json.loads(call["arguments"] or "{}")
    except json.JSONDecodeError as error:
        return {
            "tool_call_id": call["id"], "ok": False,
            "error_type": "JSONDecodeError", "error": f"Araç argümanları çözümlenemedi: {error}",
        }
    runtime = CURRENT_RUNTIME.get()
    service = CURRENT_SERVICE.get()
    dynamic = runtime.published.get(name) if runtime is not None else None
    if name == "discover_capabilities" and dynamic is None:
        return {"tool_call_id": call["id"], "ok": False, "error_type": "IntegrationUnavailable",
                "error": "Keşif yalnızca görev bağlamında kullanılabilir."}
    if runtime is not None and runtime.allowed_tools is not None and name not in runtime.allowed_tools:
        return {
            "tool_call_id": call["id"], "ok": False,
            "error_type": "ToolUnavailable",
            "error": f"Bu görevde {name} aracı kullanılamaz; seçilen oturum yolunu koru.",
        }
    if name not in TOOL_NAMES and dynamic is None:
        return {
            "tool_call_id": call["id"], "ok": False,
            "error_type": "UnknownTool", "error": f"Bilinmeyen araç: {name}. Geçerli araçlar: {', '.join(sorted(TOOL_NAMES))}",
        }

    cache_key: Optional[str] = None
    if name in _CACHEABLE_TOOLS:
        cache_key = _tool_cache_key(name, arguments)
        if cache_key in cache:
            return {**cache[cache_key], "tool_call_id": call["id"]}

    method: Callable[..., Any] = dynamic["execute"] if dynamic else getattr(toolbox, name)
    integration_started = time.monotonic()
    call_context: ToolRuntime = {
        "emit_output": lambda text: emit({"kind": "tool_output", "call_id": call["id"], "text": text}),
        "should_stop": should_stop,
        "approved": False,
    }
    token = TOOL_RUNTIME.set(call_context)
    try:
        if runtime is not None:
            runtime.check()
        if dynamic:
            validate_arguments(dynamic, arguments)
        request: Optional[approval.ApprovalRequest] = approval_request_for_call(
            name, arguments, dynamic, toolbox.memory_mutation_allowed,
        )
        if request is not None:
            await require_approval(name, request)
            # Onay yalnız bu çağrının bağlamına işlenir; araç (ör. user_memory) bunu doğrular.
            TOOL_RUNTIME.set({**call_context, "approved": True})
        if asyncio.iscoroutinefunction(method):
            operation = method(**arguments)
            result: Any = await runtime.wait(operation) if runtime is not None and not dynamic else await operation
        else:
            result = await asyncio.to_thread(method, **arguments)
        outcome: ToolResult = {"tool_call_id": call["id"], "ok": True,
                               "result": json.dumps(result, ensure_ascii=False) if dynamic else str(result)}
    except (IntegrationStopped, InteractionRequired) as error:
        outcome = {"tool_call_id": call["id"], "ok": False, "error_type": type(error).__name__,
                   "error": str(error), "code": "STOPPED" if isinstance(error, IntegrationStopped) else "INPUT_REQUIRED",
                   "recoverable": False}
    except ToolError as error:
        outcome = {
            "tool_call_id": call["id"], "ok": False,
            "error_type": "ToolError", "error": str(error),
            "code": error.code, "recoverable": error.recoverable,
        }
    except TypeError as error:
        outcome = {
            "tool_call_id": call["id"], "ok": False,
            "error_type": "TypeError", "error": f"Geçersiz argümanlar ({name}): {error}",
        }
    except Exception as error:  # Araç sınırı: üçüncü taraf hataları döngüyü çökertmeden modele raporlanır.
        logging.warning("Araç beklenmeyen hata verdi", extra={"tool": name, "error_type": type(error).__name__})
        outcome = {
            "tool_call_id": call["id"], "ok": False,
            "error_type": type(error).__name__, "error": str(error),
        }
    finally:
        TOOL_RUNTIME.reset(token)

    if dynamic and service:
        service.record(dynamic["capability"], time.monotonic() - integration_started, bool(outcome.get("ok")))
    if _is_side_effect(name):
        cache.clear()
    if cache_key is not None and outcome.get("ok"):
        cache[cache_key] = outcome
    return outcome


def result_text(result: ToolResult) -> str:
    """
    Araç sonucunun gösterim/kayıt metni (başarıda çıktı, hatada 'tip: mesaj'). Metin modele,
    transkripte ve Telegram'a gitmeden önce bilinen sır değerleri maskelenir (redact):
    execute_shell ile okunan bir API anahtarı süreç ağacına ve sohbet geçmişine yayılmaz.
    Anahtarın kendisi hiçbir durumda log'a yazılmaz. Saf fonksiyon değildir (sır deposunu okur).
    """
    text: str = str(result.get("result", "")) if result.get("ok") else \
        f"{result.get('error_type')}: {result.get('error')}"
    return redact(text)


async def _run_tool_with_events(
    index: int, call: ToolCallDraft, preview: str, toolbox: Toolbox, cache: Dict[str, ToolResult],
    emit: EventSink, should_stop: Callable[[], bool],
) -> ToolResult:
    """Aracı çalıştırır; başlangıcını (önizlemeyle) ve bitişini (süre + sonuç) olay olarak yayınlar."""
    emit({"kind": "tool_started", "call_id": call["id"], "index": index, "name": call["name"], "preview": preview})
    started: float = time.monotonic()
    result: ToolResult = await execute_tool(call, toolbox, cache, emit, should_stop)
    emit({"kind": "tool_finished", "call_id": call["id"], "ok": bool(result.get("ok")),
          "text": result_text(result)[:EVENT_RESULT_LIMIT], "seconds": round(time.monotonic() - started, 2)})
    return result


def _is_side_effect(name: str) -> bool:
    runtime = CURRENT_RUNTIME.get()
    entry = runtime.published.get(name) if runtime else None
    return (not entry["readonly"]) if entry else name in _SIDE_EFFECT_TOOLS


async def _execute_tool_calls(
    calls: List[ToolCallDraft], toolbox: Toolbox, cache: Dict[str, ToolResult], emit: EventSink,
    should_stop: Callable[[], bool],
) -> List[ToolResult]:
    """
    Bir turdaki tüm araç çağrılarını MODELİN DÖNDÜRDÜĞÜ SIRAYI koruyarak çalıştırır:
    yan etkili çağrılar seri, aralarındaki bağımsız salt okunur bloklar paralel. Böylece
    'tıkla -> ekran görüntüsü al' gibi eylem-gözlem çiftlerinde gözlem her zaman
    eylemden SONRA gelir (yarış yok).
    """
    results: List[Optional[ToolResult]] = [None] * len(calls)
    previews: List[str] = [preview_arguments(call["name"], call["arguments"]) for call in calls]
    index: int = 0
    while index < len(calls):
        if _is_side_effect(calls[index]["name"]):
            results[index] = await _run_tool_with_events(
                index, calls[index], previews[index], toolbox, cache, emit, should_stop)
            index += 1
            continue
        stop: int = index
        while stop < len(calls) and not _is_side_effect(calls[stop]["name"]):
            stop += 1
        group: List[ToolResult] = list(await asyncio.gather(
            *(_run_tool_with_events(k, calls[k], previews[k], toolbox, cache, emit, should_stop)
              for k in range(index, stop))
        ))
        results[index:stop] = group
        index = stop
    return [r for r in results if r is not None]


def _call_label(call: ToolCallDraft) -> str:
    """
    Sonucun hangi çağrıya ait olduğunu gösteren kısa etiket. Paralel toplu sonuçlar yalnızca
    tool_call_id ile eşleşince hızlı model onları karıştırabiliyordu (ölçümde 5 dosyalık
    okumada kodlar yanlış sıralandı/atlandı). Saf fonksiyon.
    """
    arguments: str = " ".join(call["arguments"].split())
    return f"[{call['name']} {arguments[:CALL_LABEL_ARGS_LIMIT]}]"


def _tool_result_to_message(call: ToolCallDraft, result: ToolResult) -> Dict[str, Any]:
    """Araç sonucunu, başında çağrı etiketiyle modele geri gönderilecek 'tool' mesajına çevirir."""
    if result.get("ok"):
        content: str = result_text(result)
    else:
        content = f"HATA {result_text(result)}"
    return {"role": "tool", "tool_call_id": call["id"], "content": f"{_call_label(call)}\n{content}"}


def update_chrome_visits(
    visits: Dict[str, int], call: ToolCallDraft, result: ToolResult,
) -> Tuple[Dict[str, int], Optional[str]]:
    """
    Başarılı chrome_active_tab URL ziyaretlerini yan etkisiz biçimde sayar. Aynı tam URL ikinci
    kez açıldığında modele kısa bir durum uyarısı üretir; çağrıyı engellemez çünkü hedef final
    revalidation isteyebilir. Böylece meşru doğrulama mümkün kalırken kör tekrar görünür olur.
    """
    updated: Dict[str, int] = dict(visits)
    if call["name"] != "chrome_active_tab" or not result.get("ok"):
        return updated, None
    try:
        arguments: object = json.loads(call["arguments"] or "{}")
    except json.JSONDecodeError:
        return updated, None
    if not isinstance(arguments, dict):
        return updated, None
    url: object = arguments.get("url")
    if not isinstance(url, str) or not url:
        return updated, None
    count: int = updated.get(url, 0) + 1
    updated[url] = count
    if count == 1:
        return updated, None
    return updated, (
        f"STATE uyarısı: {url} bu görevde {count}. kez başarıyla açıldı. "
        "Hedef açıkça yeniden doğrulama istemiyorsa ve gereken bilgi STATE içinde kayıtlıysa "
        "bu sayfayı tekrar dolaşma; eksik zorunlu adıma geç."
    )
