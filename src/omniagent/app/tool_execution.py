"""Araç çağrısı doğrulama, onay, yürütme, cache ve sonuç olayları."""
from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime, timezone
import json
import logging
import re
import time
from threading import Lock
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from omniagent import approval
from omniagent.app.tool_schema import (
    TOOL_NAMES,
    _CACHEABLE_TOOLS,
    _SIDE_EFFECT_TOOLS,
    _SCREEN_ACTION_TOOLS,
)
from omniagent.app.types import ToolCallDraft, ToolResult
from omniagent.config import redact
from omniagent.core.evidence import sanitize_presentation_arguments, sanitize_text, sanitize_tool_text
from omniagent.core.events import AgentEvent, EventSink, argument_point, argument_tag, preview_arguments
from omniagent.integrations.capabilities import ToolEntry, validate_arguments
from omniagent.integrations.runtime import (
    CURRENT_RUNTIME,
    CURRENT_SERVICE,
    IntegrationRuntime,
    IntegrationStopped,
    InteractionRequired,
    data_root,
)
from omniagent.paths import APP_NAME, workspace_dir
from omniagent.tools import (
    TOOL_RUNTIME,
    ToolError,
    Toolbox,
    ToolRuntime,
    shell_command_words,
)


CALL_LABEL_ARGS_LIMIT: int = 100
EVENT_RESULT_LIMIT: int = 4000
OUTPUT_PRESENTATION_BUFFER_LIMIT: int = 512 * 1024
# Modele giden araç çıktısının enjeksiyon anındaki baş/kuyruk sınırları: uzun çıktı (sayfa okuma,
# log, JSON) bağlama girmeden kırpılır; kuyruk korunur çünkü en taze bilgi sondadır.
TOOL_MESSAGE_HEAD_LIMIT: int = 6000
TOOL_MESSAGE_TAIL_LIMIT: int = 2000


_DELETION_PATTERN = re.compile(
    r"\b(?:rm|rmdir|unlink|trash)\b|\bgit\s+clean\b|\bfind\b[^\n;]*\s-delete\b|"
    r"\b(?:os\.(?:remove|unlink|rmdir)|shutil\.rmtree)\s*\(|\.(?:unlink|rmdir)\s*\("
)


def deletion_command(code: str) -> bool:
    """Conventional shell/Python deletion patterns; deliberately best effort, not a sandbox."""
    return _DELETION_PATTERN.search(code) is not None


def autonomy_requests_for_call(name: str, arguments: Dict[str, Any],
                               runtime: IntegrationRuntime) -> List[approval.ApprovalRequest]:
    guards = runtime.autonomy
    if guards is None:
        return []
    requests: List[approval.ApprovalRequest] = []
    code = str(arguments.get("command" if name == "execute_shell" else "code", ""))
    if name in ("execute_shell", "execute_python") and deletion_command(code):
        requests.append(approval.deletion_request(name, arguments))
    if name == "capture_photo":
        requests.append(approval.camera_request(arguments))
    if name in ("write_file", "edit_file"):
        typed = Path(str(arguments.get("path", ""))).expanduser()
        candidates = ((workspace_dir() / typed).resolve(), typed.resolve())
        if any(candidate.is_relative_to(root.resolve()) or any(
                _same_filesystem_object(parent, root) for parent in (candidate, *candidate.parents))
               for candidate in candidates for root in guards["guarded_roots"]):
            requests.append(approval.self_modification_request(name, arguments))
    return requests


def _tool_cache_key(name: str, arguments: Dict[str, Any]) -> str:
    """Önbellek anahtarı: araç adı + kararlı (sıralı) argüman JSON'u."""
    return name + ":" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def failed_call_key(call: ToolCallDraft) -> str:
    """Aynı araç ve eşdeğer JSON argümanlarını tek tekrar anahtarına dönüştürür."""
    try:
        arguments: object = json.loads(call["arguments"] or "{}")
    except json.JSONDecodeError:
        return f"{call['name']}:{call['arguments']}"
    if isinstance(arguments, dict):
        return _tool_cache_key(call["name"], arguments)
    return f"{call['name']}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"


# Kabuk komutunda korumalı dosya adı geçiyor, komut yazma/silme/taşıma yapabiliyor VE komut veri kökünü gösteriyorsa onay
# istenir. Salt okuma (cat, less, grep) ve veri kökü dışındaki aynı adlı proje dosyaları ('git add catalog.json',
# 'python build.py > catalog.json', 'ls 2>&1 | tee telegram.json') sorulmaz. Yazma göstergeleri sözcük sınırlıdır: 'add'
# içindeki 'dd' eşleşmez. En iyi çabadır: değişken birleştirme veya kodlama gibi kabuk kaçışları bunu geçebilir.
_SHELL_WRITE_MARKER: re.Pattern[str] = re.compile(
    r">|\b(?:tee|sed|mv|cp|rm|truncate|dd|install|ln|python\d?|node|perl|ruby)\b|\b(?:ba)?sh\s+-c\b"
)
# Veri kökünü gösteren ifadeler ('Application Support/OmniAgent', OMNI_DATA_DIR, göreli '..' çıkışı ve kökün mutlak yolu)
_SHELL_DATA_ROOT_HINTS: Tuple[str, ...] = (f"application support/{APP_NAME.casefold()}", "omni_data_dir", "..")


def _same_filesystem_object(first: Path, second: Path) -> bool:
    """
    İki yol aynı dosya sistemi nesnesi (aygıt + inode) mi? Yazım farkını (APFS büyük/küçük harfe duyarsızdır),
    /System/Volumes/Data firmlink'ini ve sert bağları aşar. Yollardan biri yoksa ya da bir bileşeni dizin değilse
    False (var olmayan hedef başka bir nesnedir). Salt okur.
    """
    try:
        return first.samefile(second)
    except (FileNotFoundError, NotADirectoryError):
        return False


def _protected_name(resolved: Path, root: Path) -> Optional[str]:
    """
    Çözülmüş hedef veri kökündeki korumalı dosyalardan biri mi? Kimlikle denetlenir, yazımla değil: (1) ad korumalı
    (küçük harf karşılaştırılır) ve üst dizin veri köküyle AYNI dizin (farklı harfle yazılmış dizin adı ve firmlink
    dahil; dosya henüz yoksa da: provider_fallback.json ilk yazılışta yoktur) ya da (2) hedef zaten var ve korumalı
    dosyalardan biriyle AYNI dosya (sert bağ). Küçük harfli korumalı adı ya da None döner.
    """
    name: str = resolved.name.casefold()
    if name in approval.PROTECTED_DATA_FILES and _same_filesystem_object(resolved.parent, root):
        return name
    return next(
        (protected for protected in sorted(approval.PROTECTED_DATA_FILES)
         if _same_filesystem_object(resolved, root / protected)),
        None,
    )


def _protected_data_file(path: str) -> Optional[str]:
    """
    write_file/edit_file hedefi veri kökündeki korumalı dosyalardan biri mi (bkz. approval.PROTECTED_DATA_FILES)?
    Göreli yol iki biçimde çözülür (iş alanı ve süreç dizini: araç hangisini seçeceğini görev hedefinden bilir);
    sembolik bağlar çözülür, '..' ile veri köküne çıkış yakalanır; farklı harfli dizin adı, firmlink ve sert bağ da
    kimlikle yakalanır (bkz. _protected_name). Küçük harfli adı ya da None döner.
    """
    if not path.strip():
        return None
    root: Path = data_root().resolve()
    typed: Path = Path(path).expanduser()
    for candidate in (workspace_dir() / typed, typed):
        protected: Optional[str] = _protected_name(candidate.resolve(), root)
        if protected is not None:
            return protected
    return None


def _shell_protected_data_file(command: str, root: Path) -> Optional[str]:
    """
    Kabuk komutu veri kökündeki korumalı bir dosyayı yazabilir mi? En iyi çaba; adı ya da None döner. Komut, sözcük
    sınırlı bir yazma göstergesi taşımalı ve veri kökünü göstermeli (bkz. _SHELL_DATA_ROOT_HINTS ve kökün mutlak yolu,
    çözülmüş biçimi dahil); yalnız dosya adı + gösterge yetmez. Ters eğik çizgiler atılır ('Application\\ Support').
    Dosya sistemine yalnız kökün gerçek yolunu çözmek için bakar.
    """
    lowered: str = command.casefold().replace("\\", "")
    hints: Tuple[str, ...] = (*_SHELL_DATA_ROOT_HINTS, str(root).casefold(), str(root.resolve()).casefold())
    if _SHELL_WRITE_MARKER.search(lowered) is None or not any(hint in lowered for hint in hints):
        return None
    return next((name for name in sorted(approval.PROTECTED_DATA_FILES) if name in lowered), None)


def approval_request_for_call(
    name: str, arguments: Dict[str, Any], dynamic: Optional[ToolEntry], memory_mutation_allowed: bool,
) -> Optional[approval.ApprovalRequest]:
    """
    Çağrının çalışmadan önce kullanıcı onayı gerektirip gerektirmediğine karar verir (yalnız argümanlara
    bakarak): hedefin açıkça istemediği kalıcı hafıza değişikliği, finansal veya geri alınamaz dış iletişim
    yapan entegrasyon araçları, ödeme kartı numarası yazan GUI/tarayıcı araçları, para hareketi yapan
    kabuk/JS çağrıları ve veri kökündeki güvenlik/ilke dosyalarına (yedek sağlayıcı izni, denetim kaydı,
    kalıcı hafıza, zamanlanmış görevler...) dosya/kabuk aracıyla yazma. Ekranda tıklanan hedefin etiketine bağlı kararlar burada verilemez: onları araç
    hedefi çözümledikten sonra ToolRuntime onay kancasıyla ister (bkz. execute_tool). Ayrıştırılamayan kabuk
    komutu için onay istenmez; araç kendisi reddeder. Saf fonksiyon.
    """
    if name == "user_memory":
        action: str = str(arguments.get("action", "")).strip().casefold()
        if action in ("remember", "forget") and not memory_mutation_allowed:
            return approval.memory_request(arguments)
        return None
    if name == "personal_memory":
        # Kanıtlı hafızadan unutma da istenmemiş hafıza değişikliğidir: hedef istemediyse kullanıcıya sorulur.
        if str(arguments.get("action", "")).strip().casefold() == "forget" and not memory_mutation_allowed:
            return {"category": "memory",
                    "title": f"Kanıtlı hafızadan #{arguments.get('fact_id')} numaralı bilgi unutulsun mu?",
                    "summary": approval.call_summary("personal_memory", arguments)}
        return None
    if dynamic is not None:
        if dynamic["readonly"]:
            return None
        label: str = dynamic.get("label", name)
        if dynamic.get("financial", False):
            return approval.financial_request(label, arguments, "finansal entegrasyon işlemi")
        if approval.outbound_tool_name(label):
            return approval.outbound_request(label, arguments, "geri alınamaz dış iletişim")
        return None
    if name in ("write_file", "edit_file"):
        protected_file: Optional[str] = _protected_data_file(str(arguments.get("path", "")))
        if protected_file is not None:
            return approval.config_write_request(name, protected_file)
    if name == "execute_shell":
        protected_by_shell: Optional[str] = _shell_protected_data_file(str(arguments.get("command", "")), data_root())
        if protected_by_shell is not None:
            return approval.config_write_request(name, protected_by_shell)
    masked_card: Optional[str] = approval.typed_card_number(name, arguments)
    if masked_card is not None:
        return approval.card_entry_request(name, masked_card)
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
        "Onay gerektiğini final yanıtında açıkça bildir.", approval.APPROVAL_UNAVAILABLE_CODE,
    ),
    "timeout": (
        f"Kullanıcı onayı {approval.APPROVAL_TIMEOUT_SECONDS / 60:.0f} dakika içinde gelmedi; işlem yapılmadı.",
        approval.APPROVAL_TIMEOUT_CODE,
    ),
    "denied": (
        "Kullanıcı bu işlemi onaylamadı; yapılmadı. Aynı işlemi yeniden deneme.", approval.APPROVAL_DENIED_CODE,
    ),
}


async def require_approval(tool: str, request: approval.ApprovalRequest) -> None:
    """
    Onayı arayüz/Telegram üzerinden sorar ve kararı (onay/ret/zaman aşımı/kanal yok) denetim
    kaydına yazar; kayıt yazılamazsa işlem yürütülmez. Onaylanmayan çağrı ToolError ile
    reddedilir; kullanıcı görevi durdurursa IntegrationStopped yükselir.

    BYPASS MODU: İki yolda kullanıcıya sorulmaz ve denetim kaydına "auto_approved" yazılır: (1) istek
    güvenli host listesine/tutar limitine giriyorsa (approval.should_auto_approve), (2) sürekli modda
    (unattended) AUTO_APPROVE_IN_CONTINUOUS_MODE açıkken. Ajan serbest çalışır; denetim izi kalır.
    """
    runtime: Optional[IntegrationRuntime] = CURRENT_RUNTIME.get()
    decision: str = "unavailable"
    request_key = (request["category"], request["summary"])
    if runtime is not None and request_key in runtime.denied_approval_requests:
        raise ToolError("Kullanıcı bu işlemi reddetti; aynı işlem başka araçla yeniden yürütülmez.",
                        approval.APPROVAL_DENIED_CODE, False)
    draft = " ".join(runtime.gui_draft_text.split()) if runtime is not None else ""
    denied_question = runtime is not None and any(
        draft and draft in question for question in runtime.denied_confirmation_questions
    )

    # BYPASS: (1) güvenli host/tutar, (2) sürekli mod → otomatik onay
    if request["category"] == "communication" and denied_question:
        decision = "denied"
    elif (runtime is None or runtime.autonomy is None) and approval.should_auto_approve(request.get("url", ""), request.get("amount")):
        decision = "auto_approved"
    elif runtime is not None and runtime.autonomy is None and runtime.unattended and approval.AUTO_APPROVE_IN_CONTINUOUS_MODE:
        decision = "auto_approved"
    elif runtime is not None and runtime.answer is not None:
        try:
            answer: Dict[str, Any] = await runtime.ask(
                request["title"], approval.approval_fields(request), approval.APPROVAL_TIMEOUT_SECONDS,
                allow_unattended=True,
            )
        except TimeoutError:
            decision = "timeout"
        else:
            decision = "approved" if approval.approval_granted(answer.get(approval.APPROVAL_FIELD)) else "denied"
    approval.append_audit(
        data_root() / "audit.jsonl",
        approval.audit_record(tool, request, decision, datetime.now(timezone.utc).isoformat()),
    )
    if decision not in ("approved", "auto_approved"):
        if runtime is not None and decision == "denied":
            runtime.denied_approval_requests.add(request_key)
        message, code = _APPROVAL_REFUSALS[decision]
        raise ToolError(message, code, False)


# İş parçacığı aracı onay beklerken durdurma bayrağını bu aralıkla yoklar
APPROVAL_POLL_SECONDS: float = 0.1


def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Bu iş parçacığında çalışan olay döngüsü; yoksa None."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _async_approver(tool: str) -> Callable[[approval.ApprovalRequest], Awaitable[None]]:
    """Olay döngüsündeki (async) araçlar için onay kancası: çözümlenen hedefi require_approval'a taşır."""
    async def request_approval(request: approval.ApprovalRequest) -> None:
        await require_approval(tool, request)
    return request_approval


def _blocking_approver(
    tool: str, loop: asyncio.AbstractEventLoop, should_stop: Callable[[], bool],
) -> Callable[[approval.ApprovalRequest], None]:
    """
    Eşzamanlı (asyncio.to_thread) araçlar için köprü: onayı olay döngüsünde require_approval ile çalıştırır,
    kararı iş parçacığında bekler. ToolError/IntegrationStopped burada yeniden yükselir (denetim kaydı
    require_approval'dadır). Kullanıcı görevi durdurursa bekleme kesilir. Olay döngüsü iş parçacığından
    çağrılırsa (kilitlenirdi) açık hata verir; yalnız araç iş parçacığı içindir.
    """
    def request_approval_blocking(request: approval.ApprovalRequest) -> None:
        if _running_loop() is loop:
            raise RuntimeError(
                f"{tool}: bloklayan onay kancası olay döngüsü iş parçacığından çağrıldı (kilitlenirdi); "
                "async araçlar ToolRuntime.request_approval kullanmalı."
            )
        future: concurrent.futures.Future[None] = asyncio.run_coroutine_threadsafe(
            require_approval(tool, request), loop,
        )
        while True:
            finished, _pending = concurrent.futures.wait([future], timeout=APPROVAL_POLL_SECONDS)
            if finished:
                future.result()
                return
            if should_stop():
                future.cancel()
                raise ToolError("Kullanıcı tarafından durduruldu.", "STOPPED", False)
    return request_approval_blocking


async def execute_tool(
    call: ToolCallDraft, toolbox: Toolbox, cache: Dict[str, ToolResult], emit: EventSink,
    should_stop: Callable[[], bool],
    inflight: Optional[Dict[str, asyncio.Future[ToolResult]]] = None,
) -> ToolResult:
    """
    Tek bir araç çağrısını çalıştırır; başarı/hata durumunu yapılandırılmış şekilde döner.
    Üretilen çıktı (komut satırları) çağrı sonunda süzülüp tool_output olarak yayınlanır;
    kullanıcı durdurursa çalışan komut hemen sonlandırılır.

    `inflight`, aynı turda paralel yürüyen eşdeğer salt-okur çağrıların aynı işi iki kez
    yapmasını önler: önbellek kontrolü ile sonucun yazılması arasında `await` olduğu için iki
    eşdeğer `read_file`/`fetch_raw` çağrısı ikisi de önbelleği boş görüp çalışıyordu. İlk çağrı
    anahtarı rezerve eder, sonuç önbelleğe yazıldıktan sonra bekleyenler o sonucu alır.
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
    pending: Optional[asyncio.Future[ToolResult]] = None
    if name in _CACHEABLE_TOOLS:
        cache_key = _tool_cache_key(name, arguments)
        if cache_key in cache:
            return {**cache[cache_key], "tool_call_id": call["id"]}
        if inflight is not None:
            running = inflight.get(cache_key)
            if running is not None:
                # Eşdeğer çağrı aynı turda paralel yürüyor; onun sonucunu paylaş.
                return {**(await running), "tool_call_id": call["id"]}
            pending = asyncio.get_running_loop().create_future()
            inflight[cache_key] = pending

    method: Callable[..., Any] = dynamic["execute"] if dynamic else getattr(toolbox, name)
    integration_started = time.monotonic()
    # Text-only callbacks cannot distinguish interleaved stdout/stderr. Keep only
    # bounded activity counters; present the canonical returned receipt at the end.
    # tool_started remains live and the raw returned result stays unchanged.
    output_size = 0
    output_overflow = False
    output_lock = Lock()

    def buffer_output(text: str) -> None:
        nonlocal output_size, output_overflow
        # stdout/stderr reader threads share this callback and the byte budget.
        with output_lock:
            if output_overflow:
                return
            output_size += len(text.encode("utf-8"))
            if output_size > OUTPUT_PRESENTATION_BUFFER_LIMIT:
                output_overflow = True

    call_context: ToolRuntime = {
        "emit_output": buffer_output,
        "should_stop": should_stop,
        "approved": False,
        "request_approval": _async_approver(name),
        "request_approval_blocking": _blocking_approver(name, asyncio.get_running_loop(), should_stop),
        "preemptible": runtime is not None and runtime.autonomy is not None,
    }
    if runtime is not None and runtime.autonomy is not None:
        call_context["mark_gui_input"] = runtime.autonomy["mark_gui_input"]
    # İptal gibi bir BaseException dışarı taşınırsa bekleyen eşdeğer çağrı askıda kalmasın diye
    # sonuç önceden güvenli bir başarısızlıkla doldurulur; normal yol bunu ezber.
    outcome: ToolResult = {
        "tool_call_id": call["id"], "ok": False,
        "error_type": "Interrupted", "error": "Araç çağrısı tamamlanmadan kesildi.",
    }
    token = TOOL_RUNTIME.set(call_context)
    autonomous_started = False
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
        if runtime is not None and runtime.autonomy is not None:
            for guard_request in autonomy_requests_for_call(name, arguments, runtime):
                await require_approval(name, guard_request)
            if name in _SCREEN_ACTION_TOOLS:
                await runtime.wait(runtime.autonomy["gui_gate"]())
            runtime.check()
            approval.append_audit(data_root() / "audit.jsonl", approval.audit_record(
                name, {"category": "autonomous_action", "title": "Otonom araç eylemi",
                       "summary": approval.call_summary(name, arguments)}, "started", datetime.now(timezone.utc).isoformat()))
            autonomous_started = True
            if name in _SCREEN_ACTION_TOOLS:
                runtime.autonomy["mark_gui_input"]()
        if asyncio.iscoroutinefunction(method):
            operation = method(**arguments)
            result: Any = await runtime.wait(operation) if runtime is not None and not dynamic else await operation
        else:
            result = await asyncio.to_thread(method, **arguments)
        outcome = {"tool_call_id": call["id"], "ok": True,
                   "result": json.dumps(result, ensure_ascii=False) if dynamic else str(result)}
        if name == "capture_photo" and toolbox.last_capture_path is not None:
            outcome["artifact_path"] = toolbox.last_capture_path
    except (IntegrationStopped, InteractionRequired) as error:
        outcome = {"tool_call_id": call["id"], "ok": False, "error_type": type(error).__name__,
                   "error": str(error), "code": "STOPPED" if isinstance(error, IntegrationStopped) else "INPUT_REQUIRED",
                   "recoverable": False}
    except ToolError as error:
        outcome = {
            "tool_call_id": call["id"], "ok": False,
            "error_type": "ToolError", "error": str(error),
            "code": error.code, "recoverable": error.recoverable,
            "completed_steps": error.completed_steps,
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
        if autonomous_started:
            try:
                approval.append_audit(data_root() / "audit.jsonl", approval.audit_record(
                    name, {"category": "autonomous_action", "title": "Otonom araç eylemi",
                           "summary": approval.call_summary(name, arguments)},
                    "succeeded" if outcome.get("ok") else "failed", datetime.now(timezone.utc).isoformat()))
            except OSError:
                logging.error("Otonom araç sonuç denetimi yazılamadı", extra={"tool": name})
        TOOL_RUNTIME.reset(token)
        if dynamic and service:
            service.record(dynamic["capability"], time.monotonic() - integration_started, bool(outcome.get("ok")))
        if _is_side_effect(name):
            cache.clear()
        if cache_key is not None and outcome.get("ok"):
            cache[cache_key] = outcome
        # Bekleyen eşdeğer çağrı, sonuç önbelleğe yazıldıktan SONRA serbest bırakılır.
        if pending is not None and not pending.done():
            pending.set_result(outcome)
        if output_overflow:
            emit({"kind": "tool_output", "call_id": call["id"],
                  "text": "Araç çıktısı gösterim sınırını aştı; ayrıntı için işlem sonucuna bakın.\n"})
        elif output_size:
            canonical = sanitize_tool_text(name, call["arguments"], raw_result_text(outcome))
            if len(canonical.encode("utf-8")) > OUTPUT_PRESENTATION_BUFFER_LIMIT:
                canonical = "Araç çıktısı gösterim sınırını aştı; ayrıntı için işlem sonucuna bakın.\n"
            emit({"kind": "tool_output", "call_id": call["id"], "text": canonical})
    return outcome


def raw_result_text(result: ToolResult) -> str:
    """Actual receipt text; only source capture and the ledger sanitizer consume it."""
    return str(result.get("result", "")) if result.get("ok") else \
        f"{result.get('error_type')}: {result.get('error')}"


def result_text(result: ToolResult) -> str:
    """
    Araç sonucunun gösterim metni (başarıda çıktı, hatada 'tip: mesaj'). Bilinen sırlar
    ve bilinmeyen parola/token biçimleri tam metinde, kırpma ve olay/model sunumundan
    önce maskelenir. Ham makbuz raw_result_text ile kaynak süzgecine ayrı taşınır.
    Saf fonksiyon değildir (sır deposunu okur).
    """
    return sanitize_text(raw_result_text(result))


async def _run_tool_with_events(
    index: int, call: ToolCallDraft, preview: str, toolbox: Toolbox, cache: Dict[str, ToolResult],
    emit: EventSink, should_stop: Callable[[], bool],
    inflight: Optional[Dict[str, asyncio.Future[ToolResult]]] = None,
) -> ToolResult:
    """Aracı çalıştırır; başlangıcını (önizlemeyle) ve bitişini (süre + sonuç) olay olarak yayınlar."""
    emit({"kind": "tool_started", "call_id": call["id"], "index": index, "name": call["name"],
          "preview": sanitize_tool_text(call["name"], call["arguments"], preview), "argument_tag": argument_tag(call["name"], call["arguments"]),
          "point": argument_point(call["name"], call["arguments"])})
    started: float = time.monotonic()
    result: ToolResult = await execute_tool(call, toolbox, cache, emit, should_stop, inflight)
    finished: AgentEvent = {"kind": "tool_finished", "call_id": call["id"],
                            "ok": bool(result.get("ok")),
                            "text": sanitize_tool_text(call["name"], call["arguments"], result_text(result))[:EVENT_RESULT_LIMIT],
                            "seconds": round(time.monotonic() - started, 2)}
    failure_code = result.get("code") or (result.get("error_type") if not result.get("ok") else None)
    if failure_code:
        finished["code"] = sanitize_tool_text(call["name"], call["arguments"], str(failure_code))[:EVENT_RESULT_LIMIT]
    emit(finished)
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
    # Aynı turda paralel yürüyen eşdeğer salt-okur çağrılar tek kez çalışsın diye paylaşılan
    # rezervasyon tablosu (bkz. execute_tool).
    inflight: Dict[str, asyncio.Future[ToolResult]] = {}
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
            *(_run_tool_with_events(k, calls[k], previews[k], toolbox, cache, emit, should_stop,
                                    inflight)
              for k in range(index, stop))
        ))
        results[index:stop] = group
        index = stop
    return [r for r in results if r is not None]


def _call_label(call: ToolCallDraft) -> str:
    """
    Sonucun hangi çağrıya ait olduğunu gösteren kısa etiket. Paralel toplu sonuçlar yalnızca
    tool_call_id ile eşleşince hızlı model onları karıştırabiliyordu (ölçümde 5 dosyalık
    okumada kodlar yanlış sıralandı/atlandı). Argümanlar gösterim için maskelenir.
    """
    arguments: str = " ".join(sanitize_presentation_arguments(call["name"], call["arguments"]).split())
    return f"[{call['name']} {arguments[:CALL_LABEL_ARGS_LIMIT]}]"


def _clip_tool_message(text: str) -> str:
    """
    Modele giden araç çıktısını baş-kuyruk korumalı kırpar: orta bölüm yerine kısa bir işaret konur.
    Kuyruk korunur çünkü en taze bilgi (log sonu, sayfa sonu) sondadır. Saf fonksiyon.
    """
    if len(text) <= TOOL_MESSAGE_HEAD_LIMIT + TOOL_MESSAGE_TAIL_LIMIT:
        return text
    dropped: int = len(text) - TOOL_MESSAGE_HEAD_LIMIT - TOOL_MESSAGE_TAIL_LIMIT
    return (
        text[:TOOL_MESSAGE_HEAD_LIMIT]
        + f"\n… [orta bölüm kırpıldı: {dropped} karakter] …\n"
        + text[-TOOL_MESSAGE_TAIL_LIMIT:]
    )


def _tool_result_to_message(call: ToolCallDraft, result: ToolResult) -> Dict[str, Any]:
    """
    Araç sonucunu, başında çağrı etiketiyle modele geri gönderilecek 'tool' mesajına çevirir.
    Başarısız sonuca hata kodu ve kurtarılabilirlik kararı eklenir: model artık hatanın geçici mi
    kalıcı mı olduğunu düz metinden tahmin etmek zorunda kalmaz (bkz. tools/types.ToolError.recoverable).
    Uzun çıktı bağlama girmeden kırpılır (bkz. _clip_tool_message).
    """
    if result.get("ok"):
        content: str = sanitize_tool_text(call["name"], call["arguments"], result_text(result))
    else:
        content = f"HATA {sanitize_tool_text(call['name'], call['arguments'], result_text(result))}"
        code: object = result.get("code")
        if isinstance(code, str) and code:
            recoverable: object = result.get("recoverable")
            guidance: str = ""
            # Yönlendirme bilinçli olarak NÖTR tutulur: 'farklı bir yol/yöntem seç' gibi ifadeler
            # erişim engeli kurallarıyla çakışır (host o durumda kendi yönergesini verir; bkz.
            # continuous.WALL_*); model yalnız hatanın geçici/kalıcı olduğunu öğrenir.
            if recoverable is True:
                guidance = " — kurtarılabilir: aynı yolu düzelterek yeniden deneyebilirsin"
            elif recoverable is False:
                guidance = " — kalıcı: aynı çağrıyı yineleme"
            safe_code = sanitize_tool_text(call["name"], call["arguments"], code)
            content = f"{content}\n[hata kodu: {safe_code}{guidance}]"
    return {"role": "tool", "tool_call_id": call["id"],
            "content": f"{_call_label(call)}\n{_clip_tool_message(content)}"}


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
