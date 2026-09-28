import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import ssl
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, TypedDict, NotRequired
from urllib.request import urlopen

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from openai.types import CompletionUsage
from PIL import Image, ImageOps

from omniagent.config import (
    API_KEY_VARIABLES, BACKENDS, CONTINUOUS_GUIDANCE, DEFAULT_BACKEND, ESCALATION_BACKEND, QUALITY_LADDER,
    SYSTEM_PROMPT, BackendProfile, apply_stored_api_keys, redact,
)
from omniagent.core.events import (
    AgentEvent, ArtifactReady, EventSink, TokenUsage, argument_point, argument_tag,
    compact_count, preview_arguments, tool_label,
)
from omniagent.core.fast_loop import (
    FastLoopPolicy, FastLoopState, TurnSignal, advance_fast_loop, classify_semantic_progress,
    normalize_progress_signature,
)
from omniagent.tools import (
    MODEL_SCREEN_SIZE, SCREENSHOT_MAX_EDGE, TOOL_RUNTIME, ToolRuntime, Toolbox, ToolError,
    shell_command_words,
)
from omniagent.app.continuous import (
    CONTEXT_KEEP_TURNS, CONTEXT_MAX_TURNS, CONTINUE_PROMPT, CONTINUOUS_MODE, MAX_GOAL_REPORTS,
    MAX_IDLE_REPORTS, continuous_limits_path, goal_confirmation_question,
    goal_report_problem, goal_report_repeat_problem, load_continuous_limits, window_messages,
)
from omniagent.approval import approval_granted
from omniagent.app.tool_schema import (
    AUTO_OBSERVATION_PREVIEW,
    GOAL_REPORT_SCHEMA,
    GOAL_REPORT_TOOL,
    POINT_SCHEMA,
    TOOL_NAMES,
    VERIFICATION_OBSERVATION_PREVIEW,
    _ACTION_RECEIPT_TOOLS,
    _CACHEABLE_TOOLS,
    _DETERMINISTIC_PROGRESS_TOOLS,
    _GUI_VERIFICATION_TOOLS,
    _READ_PROGRESS_TOOLS,
    _SCREEN_ACTION_TOOLS,
    _SIDE_EFFECT_TOOLS,
    _function_schema,
    active_chrome_session_goal,
    build_tool_schemas,
    camera_photo_goal,
    chrome_session_route,
    continues_chrome_session,
    memory_mutation_requested,
    route_tool_schemas,
    scheduling_goal,
    screen_reading_schemas,
    skills_sh_goal,
)
from omniagent.app.types import (
    ModelTurn,
    RunModeProfile,
    RunOptions,
    RunReport,
    ToolCallDraft,
    ToolResult,
)
from omniagent.app.constants import (
    ENGLISH_WEEKDAYS,
    FULL_DETAIL_TURNS,
    MAX_ACTION_EVIDENCE_RECOVERIES,
    MAX_EMPTY_ANSWER_RECOVERIES,
    MAX_FINAL_LENGTH_RECOVERIES,
    MAX_ITERATIONS,
    MAX_NOVEL_READ_OUTPUTS,
    MAX_NOVEL_SHELL_OUTPUTS,
    MAX_UNEXECUTED_TOOL_RECOVERIES,
    MAX_WALL_CLOCK_SECONDS,
    MODEL_IMAGE_QUALITY,
    NO_PROGRESS_LIMIT,
    READ_TRIMMED_TAIL_LIMIT,
    RUN_MODE_PROFILES,
    TASK_LEDGER_LIMIT,
    TRIMMED_ARGS_LIMIT,
    TRIMMED_CONTENT_LIMIT,
    TURKISH_WEEKDAYS,
    ZERO_USAGE,
)
from omniagent.app.file_delivery import (
    FileReceipt, capture_file_contract, file_delivery_gap, receipt_for_call,
)
from omniagent.app import model_runtime
from omniagent.app.progress import (
    _fast_loop_prompt,
    _ledger_delivery_ready,
    add_usage,
    extract_task_ledger,
    failed_tool_recovery_message,
    host_turn_progress,
    novel_read_output_progress,
    novel_shell_output_progress,
    turn_progress_signature,
)
from omniagent.app.policy import (
    _obviously_read_only_shell,
    action_execution_expected,
    attempt_plan,
    contains_unexecuted_tool_call,
    final_verdict,
    has_action_evidence,
    next_quality_backend,
    retry_after_seconds,
    screenshot_requested,
    source_change_expected,
    unmet_explicit_deletion,
    unmet_wait_status,
)
from omniagent.app.tool_execution import (
    _call_label,
    _execute_tool_calls,
    _is_side_effect,
    _run_tool_with_events,
    _tool_cache_key,
    _tool_result_to_message,
    approval_request_for_call,
    execute_tool,
    failed_call_key,
    require_approval,
    result_text,
    update_chrome_visits,
)
from omniagent.app.verification import (
    COMMIT_UNVERIFIED_MESSAGE,
    GUI_VERIFICATION_PROMPT,
    REPEATED_NO_EFFECT_ACTION_MESSAGE,
    commit_action_call,
    commit_navigation_call,
    commit_then_navigation_in_sequence,
    executed_call_prefix,
    gui_evidence_summary,
    gui_verification_needed,
    needs_action_observation,
    no_effect_action_key,
    requested_chrome_navigation_gap,
    should_reuse_observation,
    text_entry_call,
    unchanged_screen_note,
    verification_message,
    with_observation_note,
)
from omniagent import approval
from omniagent.memory import experience
from omniagent.core import state as sm
from omniagent.memory import user as user_memory
from omniagent.core.conversation import Exchange, make_exchange, to_messages
from omniagent.core.task_ledger import (
    TaskLedger, empty_task_ledger, record_tool_receipt, record_tool_result,
    format_ledger_prompt, record_model_state, inject_task_ledger_into_messages,
)
from omniagent.core.checkpoint import (
    clear_checkpoint, find_resume_checkpoint,
    format_checkpoint_scratchpad, save_checkpoint,
)
from omniagent.integrations.capabilities import CapabilityService, ToolEntry, discovery_entry, validate_arguments
from omniagent.paths import migrate_legacy_runtime_data, state_file, telegram_settings_file
from omniagent.integrations.runtime import (
    AnswerSink, CURRENT_RUNTIME, CURRENT_SERVICE, IntegrationRuntime,
    IntegrationStopped, InteractionRequired, data_root,
)

STATE_FILE: str = str(state_file())

# Aynı (araç + argüman) çağrısı bir tur önce başarısız olduysa yürütülmez.
REPEATED_FAILED_CALL_MESSAGE: str = (
    "Aynı başarısız araç çağrısı aynı argümanlarla tekrarlandı; farklı bir adım dene."
)
# Gönderim koruması bu kadar tur sürer: yazılı taslak gönderildikten sonra ilk turda sayfadan
# ayrılma engellenir, ajan ekrana bakıp gönderimi doğrular ya da gönder düğmesine yeniden basar.
COMMIT_GUARD_TURNS: int = 3



def encode_image(path: str) -> str:
    """
    Kaydedilen (gerçek en-boy oranlı) ekran görüntüsünü modele gidecek MODEL_SCREEN_SIZE karesine
    ölçekleyip JPEG q90, renk alt örneklemesiz (4:4:4) base64 metnine çevirir. Karede görüntü
    pikseli ile normalize koordinat aynı sayıdır. Ölçüm (gerçek sayfalarda 48 hedef, gemma4):
    eski JPEG q70 (4:2:0) 20 isabet ve hedefin üstüne kayma (medyan -13 px); PNG 36-39; q90 4:4:4
    38. PNG yükü 649 KB idi ve her görüntülü isteğe ~0,8 sn ekliyordu; q90 4:4:4 ~200 KB'tır.
    """
    with Image.open(path) as source:
        frame: Image.Image = source.convert("RGB")
    square: Image.Image = frame.resize((MODEL_SCREEN_SIZE, MODEL_SCREEN_SIZE), Image.Resampling.LANCZOS)
    buffer: BytesIO = BytesIO()
    square.save(buffer, format="JPEG", quality=MODEL_IMAGE_QUALITY, subsampling=0)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def encode_screen_observation(path: str, detail_requested: bool = True) -> Tuple[str, Optional[str], Tuple[int, int]]:
    """Tıklama haritasını ve gerekirse oranı korunmuş ayrıntı görüntüsünü hazırlar."""
    with Image.open(path) as source:
        frame: Image.Image = source.convert("RGB")
    width, height = frame.size
    square = frame.resize((MODEL_SCREEN_SIZE, MODEL_SCREEN_SIZE), Image.Resampling.LANCZOS)
    square_buffer = BytesIO()
    square.save(square_buffer, format="JPEG", quality=MODEL_IMAGE_QUALITY, subsampling=0)
    coordinate_image = base64.b64encode(square_buffer.getvalue()).decode("ascii")

    if not detail_requested or (width == height and width <= MODEL_SCREEN_SIZE):
        return coordinate_image, None, (width, height)

    detail = frame.copy()
    detail.thumbnail((SCREENSHOT_MAX_EDGE, SCREENSHOT_MAX_EDGE), Image.Resampling.LANCZOS)
    detail_buffer = BytesIO()
    detail.save(detail_buffer, format="JPEG", quality=MODEL_IMAGE_QUALITY, subsampling=0)
    reference_image = base64.b64encode(detail_buffer.getvalue()).decode("ascii")
    return coordinate_image, reference_image, (width, height)


def encode_attachment_image(path: str) -> str:
    """
    Kullanıcının gönderdiği görseli (Telegram fotoğrafı) en-boy oranını koruyarak en uzun kenarı
    MODEL_SCREEN_SIZE olacak biçimde JPEG q90 4:4:4 base64 metnine çevirir. Ekran görüntüsü gibi
    kareye sündürülmez: fotoğrafın tıklama koordinat uzayı yoktur, bozulan oran okunaklılığı düşürür.
    """
    with Image.open(path) as source:
        frame: Image.Image = ImageOps.exif_transpose(source).convert("RGB")
    frame.thumbnail((MODEL_SCREEN_SIZE, MODEL_SCREEN_SIZE), Image.Resampling.LANCZOS)
    buffer: BytesIO = BytesIO()
    frame.save(buffer, format="JPEG", quality=MODEL_IMAGE_QUALITY, subsampling=0)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def user_message_with_images(text: str, image_paths: List[str]) -> Dict[str, Any]:
    """
    Görevin ilk kullanıcı mesajı. Ekli görseller modele görüntü olarak verilir; açılamayan görsel
    sessizce düşmez, yolu ve hatası metne yazılır (model dosyayı yine araçla işleyebilir).
    """
    if not image_paths:
        return {"role": "user", "content": text}
    images: List[Dict[str, Any]] = []
    failures: List[str] = []
    for path in image_paths:
        try:
            encoded: str = encode_attachment_image(path)
        except (OSError, ValueError) as error:
            failures.append(f"Ek görsel modele verilemedi ({path}): {type(error).__name__}")
            continue
        images.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
    note: str = "\n".join(failures)
    parts: List[Dict[str, Any]] = [{"type": "text", "text": f"{text}\n\n{note}" if note else text}]
    return {"role": "user", "content": parts + images} if images else {"role": "user", "content": parts[0]["text"]}





def _assistant_entry(turn: ModelTurn) -> Dict[str, Any]:
    """
    Model turunu geçmiş için {role, content, tool_calls} biçiminde kurar. Düşünme metni
    (reasoning_content) her turda tekrar gönderilmesin diye geçmişe eklenmez. Saf.
    """
    entry: Dict[str, Any] = {"role": "assistant"}
    if turn["content"]:
        entry["content"] = turn["content"]
    if turn["tool_calls"]:
        entry["tool_calls"] = [
            {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": call["arguments"]}}
            for call in turn["tool_calls"]
        ]
    return entry


def _trim_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Eski bir mesajın büyük parçalarını (araç çıktısı, görsel, uzun argüman) budar. Saf."""
    content: Any = entry.get("content")
    if entry.get("role") == "tool" and isinstance(content, str) and content.startswith("[cua_read_scrollable"):
        # Okunan panelin sonucu (maaş, kod, fiyat) çoğu zaman sondadır: baş ve son korunur. Yalnız baş
        # korunurken model maaş satırlarını kaybedip ilanları yeniden okuyordu.
        if len(content) <= TRIMMED_CONTENT_LIMIT + READ_TRIMMED_TAIL_LIMIT:
            return entry
        return {**entry, "content": (content[:TRIMMED_CONTENT_LIMIT] + " …[eski okuma kısaltıldı]… "
                                     + content[-READ_TRIMMED_TAIL_LIMIT:])}
    if entry.get("role") == "tool" and isinstance(content, str) and len(content) > TRIMMED_CONTENT_LIMIT:
        return {**entry, "content": content[:TRIMMED_CONTENT_LIMIT] + " …[eski çıktı kısaltıldı]"}
    if isinstance(content, list) and any(isinstance(part, dict) and part.get("type") == "image_url" for part in content):
        # Görseli atarken aynı mesajdaki metinsel gözlem/STATE bilgisini koru. Önceki davranış
        # image_url gördüğü anda bütün multimodal mesajı tek placeholder'a çeviriyor, böylece
        # görselle birlikte yazılmış dayanıklı gerçekleri de siliyordu.
        text_parts: List[str] = [
            str(part.get("text"))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        ]
        preserved: str = "\n".join(text_parts)
        marker: str = "[eski ekran görüntüsü bağlamdan çıkarıldı]"
        return {**entry, "content": f"{preserved}\n{marker}" if preserved else marker}
    if entry.get("role") == "assistant" and entry.get("tool_calls"):
        return {**entry, "tool_calls": [
            {**call, "function": {**call["function"], "arguments": "{}"}}
            if len(str(call["function"].get("arguments", ""))) > TRIMMED_ARGS_LIMIT
            else call
            for call in entry["tool_calls"]
        ]}
    return entry


def _trim_old_turns(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Son FULL_DETAIL_TURNS assistant turundan (ve sonrasındaki araç sonuçlarından) önceki
    mesajları budar. Yaş tur ile ölçülür: en son turun sonuçları model onları görmeden
    kırpılmaz (eski araç-mesajı sayacı paralel 5'li okumada ilk 2 sonucu kesiyordu).
    Pencere tur tur kaydığı için önceki önek bayt bayt aynı kalır (önek önbelleği bozulmaz).
    Saf fonksiyon: yeni liste döner.
    """
    assistant_indices: List[int] = [i for i, entry in enumerate(messages) if entry.get("role") == "assistant"]
    if len(assistant_indices) <= FULL_DETAIL_TURNS:
        return messages
    cutoff: int = assistant_indices[-FULL_DETAIL_TURNS]
    return [_trim_entry(entry) if index < cutoff else entry for index, entry in enumerate(messages)]


def build_system_prompt(today: date, goal: Optional[str], memory_block: str) -> str:
    """Yalnız hedef metnine bakan sistem istemi; Chrome oturumu açıkça istendiyse o yolun rehberi. Saf."""
    return route_system_prompt(today, goal, memory_block, active_chrome_session_goal(goal))


def route_system_prompt(today: date, goal: Optional[str], memory_block: str, chrome_session: bool) -> str:
    """
    Sabit istemin sonuna tarih, kullanıcının kayıtlı hafıza bloğu ve yalnız ilgili görevde kısa
    yöntem bilgisi ekler. Hafıza bloğu görevden bağımsız aynı sırada olduğundan genel görevlerde
    önek aynı kalır; ev dizini modele verilmez. chrome_session: bkz. chrome_session_route. Saf.
    """
    weekday: int = today.weekday()
    camera_guidance: str = (
        "\n### CAMERA PHOTO\n- Call capture_photo with {} directly. It chooses the real "
        "~/Desktop path, validates the image and returns its filename. Skip camera/ffmpeg/"
        "Photo Booth probes; use Photo Booth only if the tool fails.\n"
        if goal is not None and camera_photo_goal(goal) else ""
    )
    chrome_guidance: str = (
        "\n### USER'S OPEN CHROME SESSION\n"
        "- Use chrome_active_tab and the visible Chrome GUI. Never use browse_url, "
        "API/MCP discovery, shell, Node or CDP for this goal.\n"
        "- If the user asks for a new tab, call chrome_active_tab with new_tab=true and the target URL; "
        "otherwise it reuses a matching open tab. For a LinkedIn home feed, use "
        "https://www.linkedin.com/feed/ and verify the visible page before reporting its contents.\n"
        "- Click any target that shows text (link, button, tab, job/list title, menu item, dropdown "
        "option, checkbox label) with cua_click_text: OCR finds its exact spot. Use cua_click_point "
        "only for targets without text (icons, empty fields) and aim at the element's CENTER, not "
        "empty whitespace.\n"
        "- Editable text inside an input is not a button: click_text then type only inserts at the "
        "caret. For a replacement, use cua_fill_field(point,text), which selects the old value first; "
        "check the resulting field/account before reading or changing its data.\n"
        "- Search box: ONE cua_submit_text call. Multi-field form: fill each field with cua_fill_field "
        "(no Enter; Enter inside a form submits it half-filled), open a dropdown with a click and pick "
        "the option with cua_click_text (if no option list appears, type the option text with "
        "cua_type_text and press enter), tick a checkbox by clicking its label text, and submit with "
        "the form's own button only after every required field is set. Put every step you can already "
        "locate in ONE turn.\n"
        "- Content outside the visible area does not exist for you until you scroll. Scroll the pane "
        "you need (list or detail) with cua_scroll; read a long description, article or list completely "
        "with ONE cua_read_scrollable call instead of scrolling screenshot by screenshot.\n"
        "- A lone cua_scroll 'KAYMADI' can mean either the end or a non-scrollable/wrong target. "
        "For complete coverage use cua_read_scrollable and require 'sona ulaşıldı' without a "
        "truncation warning. Never claim you checked all items without that evidence.\n"
        "- In a list/detail layout open each item from the list pane. Once you know their labels, "
        "use run_action_sequence with 2-3 click_text/read_scrollable pairs per call: "
        "{action:click_text,text:<label>,near:[x,y]} then "
        "{action:read_scrollable,point:[detail_x,detail_y],max_pages:15}. "
        "The steps run in order and each detail is read after its click. Keep measured values and "
        "inspected labels in STATE; do not wait for separate screenshots between items.\n"
        "- After a turn with actions you automatically receive a screenshot taken once the screen "
        "settles. Do not call take_screenshot after actions and never wait.\n"
        "- If the automatic screenshot is unchanged after an action (the host reports it), the action "
        "missed. Do not repeat the same point; use cua_click_text or another target, or record the "
        "obstacle in STATE.\n"
        f"- Screenshots and long read results leave the context after {FULL_DETAIL_TURNS} turns: in the "
        "turn you read a needed value (code, name, number, salary), also write it in STATE.\n"
        "- A click alone is not proof: finish only when a screenshot shows the result (for a form, "
        "the confirmation after submitting).\n"
        if chrome_session else ""
    )
    return (
        SYSTEM_PROMPT
        + f"\n### TODAY\n- Date: {today.isoformat()} ({TURKISH_WEEKDAYS[weekday]} / {ENGLISH_WEEKDAYS[weekday]}).\n"
        + memory_block + camera_guidance + chrome_guidance
    )


def resolve_run_limits(options: RunOptions) -> Tuple[str, int, float]:
    """Görev profilini ve geçersiz override'ları güvenli biçimde çözer. Saf fonksiyon."""
    mode: str = options.get("run_mode", "normal")
    if mode not in RUN_MODE_PROFILES:
        raise ValueError(f"Bilinmeyen görev modu: {mode}")
    profile: RunModeProfile = RUN_MODE_PROFILES[mode]
    # Testlerin ve mevcut çağıranların MAX_ITERATIONS monkeypatch davranışını koruyoruz.
    default_iterations: int = MAX_ITERATIONS if mode == "normal" else profile["max_iterations"]
    default_wall_clock: float = MAX_WALL_CLOCK_SECONDS if mode == "normal" else profile["max_wall_clock_seconds"]
    max_iterations: int = int(options.get("max_iterations", default_iterations))
    max_wall_clock: float = float(options.get("max_wall_clock_seconds", default_wall_clock))
    if max_iterations < 1 or max_wall_clock <= 0:
        raise ValueError("Görev bütçesi pozitif olmalı")
    return mode, max_iterations, max_wall_clock


merge_tool_call_delta = model_runtime.merge_tool_call_delta
token_usage = model_runtime.token_usage


def ollama_cloud_ready() -> bool:
    """Facade: testlerde override edilebilen yerel Ollama hazırlık kontrolü."""
    return model_runtime.ollama_cloud_ready(backends=BACKENDS, opener=urlopen)


def create_model_clients() -> Dict[str, AsyncOpenAI]:
    """Facade: model istemcilerini güncel agent readiness kontrolüyle kurar."""
    return model_runtime.create_model_clients(
        backends=BACKENDS,
        api_key_variables=API_KEY_VARIABLES,
        ollama_ready=ollama_cloud_ready,
    )


async def close_model_clients(
    clients: Optional[Dict[str, AsyncOpenAI]],
) -> None:
    """Facade: model bağlantı havuzlarını kapatır."""
    await model_runtime.close_model_clients(clients)


model_request_overrides = model_runtime.model_request_overrides


async def _stream_completion(
    client: AsyncOpenAI,
    profile: BackendProfile,
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]],
    session_id: str,
    emit: EventSink,
    should_stop: Callable[[], bool],
) -> ModelTurn:
    """Facade: streaming model çağrısını ayrık runtime katmanına yönlendirir."""
    return await model_runtime.stream_completion(
        client,
        profile,
        messages,
        tool_schemas,
        session_id,
        emit,
        should_stop,
    )


async def _call_model_with_retries(
    clients: Dict[str, AsyncOpenAI],
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]],
    session_id: str,
    backend: str,
    emit: EventSink,
    should_stop: Callable[[], bool],
) -> Tuple[ModelTurn, str]:
    """
    Model çağrısını attempt_plan'a göre yapar ve yanıt veren backend'i de döner. Kalıcı
    istemci hataları (400/404 vb.) yeniden denenmez. Kimlik/bakiye hatası 401/402/403
    aynı backend'de beklemeden farklı sağlayıcıya geçer (görev boyunca karantinaya alınır).
    Zaman aşımında da doğrudan son denemeye atlanır. Yarıda
    kesilen bir akış yeniden denenirse önce stream_reset yayınlanır (arayüz o turun akmış
    içeriğini siler, metin iki kez görünmez).
    """
    runtime = CURRENT_RUNTIME.get()
    available = frozenset(clients) - (runtime.blocked_backends if runtime is not None else set())
    plan: Tuple[str, ...] = attempt_plan(backend, available)
    emitted: List[bool] = [False]

    def tracking_emit(event: AgentEvent) -> None:
        emitted[0] = True
        emit(event)

    last_error: Optional[Exception] = None
    attempt: int = 0
    while attempt < len(plan):
        active: str = plan[attempt]
        try:
            turn: ModelTurn = await _stream_completion(
                clients[active], BACKENDS[active], messages, tool_schemas, session_id, tracking_emit, should_stop,
            )
            return turn, active
        # ssl.SSLError (ör. SSLV3_ALERT_BAD_RECORD_MAC) SDK tarafından sarılmadan akış okumasından
        # yükselebiliyor; geçici bağlantı hatası gibi yeniden denenir (canlı ölçümde görevi bitirdi).
        except (APIStatusError, APIConnectionError, ssl.SSLError) as error:
            status: Optional[int] = error.status_code if isinstance(error, APIStatusError) else None
            if status is not None and status < 500 and status not in (401, 402, 403, 429):
                raise
            has_alternative = plan[-1] != active
            if status in (401, 402, 403, 429) and has_alternative and runtime is not None:
                runtime.blocked_backends.add(active)
            if status in (401, 402, 403) and not has_alternative:
                raise
            last_error = error
            logging.warning(
                "Model çağrısı başarısız, yeniden deneniyor",
                extra={"attempt": attempt + 1, "max_attempts": len(plan), "backend": active,
                       "status_code": status, "error_type": type(error).__name__},
            )
            if emitted[0]:
                emit({"kind": "stream_reset", "reason": f"{type(error).__name__} ({active}), yeniden deneniyor"})
                emitted[0] = False
            timed_out: bool = isinstance(error, APITimeoutError)
            access_failed: bool = status in (401, 402, 403)
            throttled: bool = status == 429
            if throttled and not has_alternative:
                if attempt >= len(plan) - 1:
                    raise
                delay = retry_after_seconds(error)
                if delay > 30:
                    raise
                if runtime is not None:
                    await runtime.delay(delay)
                else:
                    await asyncio.sleep(delay)
            if timed_out or access_failed or (throttled and has_alternative):
                attempt = next(
                    (index for index in range(attempt + 1, len(plan)) if plan[index] != active),
                    len(plan),
                )
            else:
                attempt += 1
            if attempt < len(plan):
                if access_failed or throttled:
                    continue
                if runtime is not None:
                    await runtime.delay(0.5 * attempt)
                else:
                    await asyncio.sleep(0.5 * attempt)
    if last_error is None:
        raise RuntimeError("Model retry planı sonuç veya hata üretmeden tükendi.")
    raise last_error


async def _screenshot_observation_with_digest(
    call: ToolCallDraft,
) -> Tuple[Dict[str, Any], str]:
    """Görsel gözlem mesajını ve tekrar tespiti için ucuz içerik digest'ini döner."""
    arguments: Dict[str, Any] = json.loads(call["arguments"] or "{}")
    coordinate_image, reference_image, (width, height) = await asyncio.to_thread(
        encode_screen_observation, str(Path(arguments["filename"]).expanduser()),
        arguments.get("detail", True),
    )
    digest: str = hashlib.sha256(
        (coordinate_image + (reference_image or "")).encode("ascii")
    ).hexdigest()
    reference_note = (
        f"İkinci görüntü aynı ekranın oranı korunmuş {width}×{height} görünümüdür; "
        "küçük metin ve görsel ayrıntıları oradan incele. İkinci görüntüdeki piksel sayılarını "
        "doğrudan tıklama noktası olarak kullanma. "
        if reference_image is not None else ""
    )
    message: Dict[str, Any] = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Gözlem: ilk görüntü 1000×1000 tıklama koordinat haritasıdır. "
                                     + reference_note
                                     + "Emin olunmayan yazıyı OCR ile doğrula; görünmeyen içeriği görmek için kaydır. "
                                     f"Görüntü {FULL_DETAIL_TURNS} tur sonra bağlamdan silinir: gereken değerleri "
                                     "(kod, ad, sayı) bu turdaki yanıt metnine yaz."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{coordinate_image}"}},
        ],
    }
    if reference_image is not None:
        message["content"].append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{reference_image}"}}
        )
    return message, digest


async def _screenshot_observation(call: ToolCallDraft) -> Dict[str, Any]:
    """Geriye uyumlu görsel gözlem helper'ı."""
    message, _ = await _screenshot_observation_with_digest(call)
    return message


async def _observe_after_actions(
    call_id: str, index: int, preview: str, toolbox: Toolbox, cache: Dict[str, ToolResult], emit: EventSink,
    should_stop: Callable[[], bool],
) -> Tuple[Dict[str, Any], sm.StepRecord, Optional[str]]:
    """
    Eylem turunun sonunda take_screenshot'ı model yerine çalıştırır (ekran durulunca) ve
    görüntüyü gözlem mesajı olarak döner; geçici dosya modele eklendikten sonra silinir.
    Başarısız gözlem modele açık metinle bildirilir. preview arayüzdeki çağrı etiketidir.
    """
    path: Path = Path(tempfile.gettempdir()) / f"omni-{call_id}.png"
    call: ToolCallDraft = {
        "id": call_id,
        "name": "take_screenshot",
        "arguments": json.dumps({"filename": str(path), "detail": False}),
    }
    result: ToolResult = await _run_tool_with_events(
        index, call, preview, toolbox, cache, emit, should_stop)
    step: sm.StepRecord = sm.make_step_record(call["name"], call["arguments"], bool(result.get("ok")), result_text(result))
    if not result.get("ok"):
        return {"role": "user", "content": f"Otomatik gözlem alınamadı: {result_text(result)}"}, step, None
    try:
        observation, digest = await _screenshot_observation_with_digest(call)
        return observation, step, digest
    except (OSError, ValueError) as error:
        logging.warning("Otomatik gözlem modele eklenemedi", extra={"error_type": type(error).__name__})
        return {"role": "user", "content": f"Otomatik gözlem modele eklenemedi: {type(error).__name__}: {error}"}, step, None
    finally:
        path.unlink(missing_ok=True)
def is_resume_goal(goal: str) -> bool:
    """Kullanıcının önceki bir görevi kaldığı yerden sürdürmek isteyip istemediğini tespit eder."""
    lowered: str = goal.casefold().strip()
    if re.match(
        r"^(?:lütfen\s+)?(?:"
        r"devam(?:\s+et)?|"
        r"kald[ıi]ğ[ıi]n\s+yerden(?:\s+devam\s+et)?|"
        r"s[uü]rd[uü]r|"
        r"(?:önceki\s+görev|onceki\s+gorev)(?:e|i)?\s+(?:devam\s+et|s[uü]rd[uü]r)"
        r")\b",
        lowered,
    ):
        return True
    return bool(re.fullmatch(
        r"(?:please\s+)?(?:resume|continue)"
        r"(?:\s+(?:please|the\s+previous\s+task|previous\s+task))?"
        r"[.!?]*",
        lowered,
    ))


_ARTIFACT_TITLES: Dict[str, str] = {
    "take_screenshot": "Ekran görüntüsü", "capture_photo": "Fotoğraf",
    "write_file": "Oluşturulan dosya", "edit_file": "Düzenlenen dosya",
    "send_file": "Gönderilen dosya",
}
_IMAGE_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


def artifact_event_for_call(
    call: ToolCallDraft, result: ToolResult, cwd: Path, goal: str,
) -> Optional[ArtifactReady]:
    """
    Başarılı çıktı aracını var olan yerel dosyanın sohbet kartına çevirir. Hedef ekran
    görüntüsü istemediyse modelin kendi gözlem görüntüleri kart olmaz. Dosya varlığını okur.
    """
    name: str = call["name"]
    if not result.get("ok") or name not in _ARTIFACT_TITLES:
        return None
    if name == "take_screenshot" and not screenshot_requested(goal):
        return None
    if name == "capture_photo":
        raw_path: object = result.get("artifact_path")
    else:
        # Başarılı çağrının argümanları execute_tool'da zaten JSON nesnesi olarak çözüldü.
        arguments: Dict[str, Any] = json.loads(call["arguments"] or "{}")
        raw_path = arguments.get("filename" if name == "take_screenshot" else "path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    expanded: Path = Path(raw_path).expanduser()
    path: Path = (expanded if expanded.is_absolute() else cwd / expanded).resolve()
    if not path.is_file():
        return None
    media_type: Literal["image", "file"] = "image" if path.suffix.casefold() in _IMAGE_SUFFIXES else "file"
    return {"kind": "artifact_ready", "call_id": call["id"], "tool": name, "path": str(path),
            "title": _ARTIFACT_TITLES[name], "media_type": media_type}


def merge_artifact(
    artifacts: Tuple[ArtifactReady, ...], artifact: ArtifactReady,
) -> Tuple[ArtifactReady, ...]:
    """
    Görev sonu kart listesine yeni çıktıyı ekler. Aynı yolun eski kaydı düşer; yeni kayıt
    ekran görüntüsüyse Telegram'daki gibi yalnız son istenen görüntü kalır. Saf.
    """
    replaces_screenshot: bool = artifact["tool"] == "take_screenshot"
    kept: Tuple[ArtifactReady, ...] = tuple(
        item for item in artifacts
        if item["path"] != artifact["path"]
        and not (replaces_screenshot and item["tool"] == "take_screenshot")
    )
    return kept + (artifact,)


def emit_existing_artifacts(cards: Tuple[ArtifactReady, ...], emit: EventSink) -> None:
    """Birikmiş çıktı kartlarını yayınlar; bu arada silinen geçici dosya teslim sayılmaz."""
    for card in cards:
        if Path(card["path"]).is_file():
            emit(card)


def record_goal_evidence(
    evidence: Dict[str, str], calls: List[ToolCallDraft], results: List[ToolResult],
) -> Dict[str, str]:
    """Başarılı araç çağrılarını report_goal_met kanıtı olarak id → kısa özetle ekler. Saf."""
    return {**evidence, **{
        call["id"]: f"{call['name']}: {result_text(result)[:160]}"
        for call, result in zip(calls, results, strict=True) if result.get("ok")
    }}


def merge_call_results(
    hosted_mask: List[bool], regular: List[ToolResult], hosted: List[ToolResult],
) -> List[ToolResult]:
    """Araç ve host sonuçlarını modelin çağrı sırasına geri dizer (tool mesajları sırayla gider). Saf."""
    regular_results = iter(regular)
    hosted_results = iter(hosted)
    return [next(hosted_results) if is_hosted else next(regular_results) for is_hosted in hosted_mask]


async def resolve_goal_report(
    call: ToolCallDraft, index: int, evidence: Dict[str, str], runtime: IntegrationRuntime, emit: EventSink,
    seen_evidence: frozenset[str], report_count: int, pending_commit: bool = False,
) -> Tuple[ToolResult, Optional[str], frozenset[str], bool]:
    """
    report_goal_met çağrısını host'ta işler: kanıt id'lerini görevin başarılı çağrılarıyla
    karşılaştırır, aynı kanıtla yinelenen bildirimi ve bildirim tavanını reddeder, geçerliyse
    kullanıcıya onaylatır. Onaylanan özeti ve o ana dek değerlendirilmiş kanıt id'lerini döner.
    """
    started: float = time.monotonic()
    emit({"kind": "tool_started", "call_id": call["id"], "index": index, "name": call["name"],
          "preview": preview_arguments(call["name"], call["arguments"]),
          "argument_tag": argument_tag(call["name"], call["arguments"]),
          "point": argument_point(call["name"], call["arguments"])})
    confirmed: Optional[str] = None
    try:
        arguments: object = json.loads(call["arguments"] or "{}")
    except json.JSONDecodeError as error:
        arguments = f"Argümanlar çözümlenemedi: {error}"
    if not isinstance(arguments, dict):
        problem: Optional[str] = str(arguments) if isinstance(arguments, str) else "Argümanlar JSON nesnesi olmalı."
        summary: str = ""
        evidence_ids: List[str] = []
    else:
        summary = str(arguments.get("summary", ""))
        raw_ids: object = arguments.get("evidence_call_ids")
        problem = goal_report_problem(summary, raw_ids, evidence)
        evidence_ids = [item for item in raw_ids if isinstance(item, str)] if isinstance(raw_ids, list) else []
        if problem is None and pending_commit:
            # Gönderim koruması aktifken hedef kapandı denemez: canlı kayıtta ajan gönderiyi
            # yayınlamadan "başarıyla paylaşıldı" diye bildiriyordu.
            problem = (
                "Son gönder/paylaş eyleminin tamamlandığı henüz doğrulanmadı; ekrandan gönderim "
                "onayını (kapanan ileti kutusu, 'gönderildi' bildirimi) görmeden hedefi bildirme."
            )
        if problem is None and report_count >= MAX_GOAL_REPORTS:
            problem = (
                f"Bu görevde kullanıcıya {report_count} hedef bildirimi soruldu; daha fazlası "
                "sorulmaz. Hedefi kapatmak için yeni kanıtla tek ve net bir bildirim kur ya da "
                "eksik işi bitirmeye devam et."
            )
        if problem is None:
            problem = goal_report_repeat_problem(evidence_ids, seen_evidence)
    # Yalnız gerçekten var olan kanıt id'leri "değerlendirildi" sayılır: modelin uydurduğu bir
    # id, sonradan gerçek bir çağrıya denk gelirse o çağrıyı haksız yere reddettirmemeli.
    evaluated: frozenset[str] = seen_evidence | frozenset(
        item for item in evidence_ids if item in evidence
    )
    asked: bool = False
    if problem is not None:
        result: ToolResult = {"tool_call_id": call["id"], "ok": False, "error_type": "GoalNotProven",
                              "error": problem, "code": "GOAL_NOT_PROVEN", "recoverable": True}
    else:
        asked = True
        try:
            # Tek metin alanı: "evet" onaylar; başka yanıt eksik olanı anlatan nottur (Telegram'da düz metin).
            answer: Dict[str, Any] = await runtime.ask(
                redact(goal_confirmation_question(summary, evidence_ids, evidence)),
                {"yanit": {"type": "string", "label": "'evet' ya da eksik olan", "default": ""}},
                None,
            )
        except IntegrationStopped as error:
            asked = False
            result = {"tool_call_id": call["id"], "ok": False, "error_type": "IntegrationStopped",
                      "error": str(error), "code": "STOPPED", "recoverable": False}
        else:
            reply: str = str(answer.get("yanit", "")).strip()
            if approval_granted(reply):
                confirmed = summary.strip()
                result = {"tool_call_id": call["id"], "ok": True,
                          "result": "Kullanıcı hedefin gerçekleştiğini ONAYLADI; görev tamamlandı."}
            else:
                result = {"tool_call_id": call["id"], "ok": True, "result": (
                    f"Kullanıcı hedefin gerçekleştiğini ONAYLAMADI: {reply or '(açıklama yok)'}\n"
                    "Eksik olanı tamamla, gerekirse ask_user ile sor ve göreve devam et."
                )}
    hosted_finished: AgentEvent = {
        "kind": "tool_finished", "call_id": call["id"], "ok": bool(result.get("ok")),
        "text": result_text(result), "seconds": round(time.monotonic() - started, 2),
    }
    if result.get("code"):
        hosted_finished["code"] = str(result["code"])
    emit(hosted_finished)
    return result, confirmed, evaluated, asked


async def run_agent_with_callback(
    goal: str, emit: EventSink, options: RunOptions, clients: Dict[str, AsyncOpenAI],
) -> RunReport:
    """
    Hedefi planla-yürüt-gözlemle-onar döngüsüyle çalıştırır; model yanıtını, araç
    çağrılarını ve komut çıktılarını yapılandırılmış olaylar olarak AKIŞ hâlinde yayınlar.
    İstemciler çağırana aittir (kapatılmaz), böylece arayüz görevler arasında sıcak
    bağlantıları yeniden kullanır.
    """
    def startup_failure(error: Exception, backend: str) -> RunReport:
        """Başlangıç hatasında bile arayüze terminal olayını teslim eder."""
        failure = f"Kritik hata: {error}"
        initial_metrics: sm.EpisodeMetrics = {
            "turns": 0, "tool_calls": 0, "elapsed_seconds": 0.0, "backend": backend,
            "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0,
            "model_seconds": 0.0, "tool_seconds": 0.0,
        }
        emit({"kind": "notice", "level": "error", "text": failure})
        emit({"kind": "run_finished", "success": False, "outcome": failure,
              "reason": failure, "metrics": initial_metrics})
        return {"outcome": failure, "success": False, "reason": failure,
                "metrics": initial_metrics, "exchange": make_exchange(goal, failure, [])}

    try:
        if options.get("run_mode") == CONTINUOUS_MODE:
            # Ayarlar'daki kullanıcı sınırları varsayılandır; çağıranın açık değerleri önceliklidir.
            limits = load_continuous_limits(continuous_limits_path())
            options = {"max_wall_clock_seconds": limits["max_hours"] * 3600,
                       "max_total_tokens": limits["max_total_tokens"], **options}
        run_mode, max_iterations, max_wall_clock = resolve_run_limits(options)
    except (OSError, ValueError) as error:
        return startup_failure(error, DEFAULT_BACKEND)
    continuous: bool = run_mode == CONTINUOUS_MODE
    max_total_tokens: Optional[int] = options.get("max_total_tokens")
    if continuous and options.get("answer") is None:
        return startup_failure(ValueError(
            "Sürekli mod, soru sorup yanıt bekleyebileceği bir kanal ister (masaüstü uygulaması veya Telegram)."
        ), DEFAULT_BACKEND)
    if not clients:
        return startup_failure(RuntimeError(
            "Kullanılabilir model yok: Ollama Cloud modeli veya bir API anahtarı "
            "(OPENAI_API_KEY / OPENCODE_API_KEY / OPENROUTER_API_KEY) gerekli."
        ), DEFAULT_BACKEND)
    available: frozenset[str] = frozenset(clients)
    backend_override: Optional[str] = options["requested_backend"] or os.environ.get("OMNI_BACKEND")
    preferred: str = next(
        (name for name in QUALITY_LADDER if name in available), next(iter(clients)),
    )
    current_backend: str = backend_override if backend_override else preferred
    if current_backend not in available:
        replacement: str = preferred
        emit({"kind": "notice", "level": "warning",
              "text": f"'{current_backend}' backend'i kullanılamıyor; '{replacement}' kullanılacak."})
        current_backend = replacement
    emit({"kind": "run_started", "goal": goal, "backend": current_backend, "model": BACKENDS[current_backend]["model"],
          "run_mode": run_mode, "max_turns": max_iterations, "max_wall_clock_seconds": max_wall_clock})

    service: Optional[CapabilityService] = None
    chrome_session: bool = chrome_session_route(goal, options["history"])
    try:
        if Path(options["state_file"]).expanduser() == state_file():
            migrate_legacy_runtime_data()
        memory_file: str = options.get("memory_file") or str(
            Path(options["state_file"]).with_name("user_memory.json")
        )
        experience_file: str = options.get("experience_file") or str(
            Path(options["state_file"]).with_name("experience_memory.json")
        )
        must_change_source: bool = source_change_expected(goal, options["history"])
        toolbox: Toolbox = Toolbox(
            memory_file=memory_file,
            allow_memory_mutation=memory_mutation_requested(goal),
            history_file=options["state_file"],
            allow_source_relative_writes=must_change_source,
        )
        session_id: str = str(uuid.uuid4())
        user_content: str = goal
        if is_resume_goal(goal):
            resume_hint = options["history"][-1]["goal"] if options["history"] else None
            latest_cp = find_resume_checkpoint(resume_hint)
            if latest_cp is not None:
                session_id = latest_cp.get("session_id", session_id)
                scratchpad = format_checkpoint_scratchpad(latest_cp)
                emit({"kind": "notice", "level": "info",
                      "text": f"Önceki oturum kontrol noktası yüklendi ({latest_cp.get('goal', '')[:50]}). Kaldığı yerden devam ediliyor."})
                user_content = f"{goal}\n\n{scratchpad}"
        state: sm.StateDict = sm.load_state(options["state_file"])
        experience_state: experience.ExperienceState = experience.load_experience(experience_file)
        memory_block: str = user_memory.memory_prompt_block(user_memory.load_memory(memory_file))
        first_message: Dict[str, Any] = await asyncio.to_thread(
            user_message_with_images, user_content, options.get("images", []),
        )
        messages: List[Dict[str, Any]] = (
            [{"role": "system", "content": route_system_prompt(date.today(), goal, memory_block, chrome_session)
              + (CONTINUOUS_GUIDANCE if continuous else "")}]
            + to_messages(options["history"])
            + [first_message]
        )
        service = options.get("integrations") or CapabilityService()
        runtime = IntegrationRuntime(emit, options["should_stop"], options.get("answer"), options.get("deliver"))
        if continuous:
            # Sürekli görev kullanıcının yanıtını süresiz bekler; bekleme bütçeye sayılmaz.
            runtime.user_input_timeout = None
        can_send_files: bool = runtime.deliver is not None
        # Planı Telegram köprüsü çalıştırır; zamanlanmış görevin kendisi yeniden plan kuramaz
        can_schedule: bool = (
            not options.get("scheduled_run", False) and scheduling_goal(goal) and telegram_settings_file().is_file()
        )
        if not chrome_session or skills_sh_goal(goal):
            runtime.selected["discover_capabilities"] = discovery_entry(service, runtime)
        file_cwd: Path = Path.cwd()
        file_contract = capture_file_contract(goal, file_cwd)
        allow_edit: bool = must_change_source or (
            file_contract is not None and file_contract["kind"] == "edit"
        )
        tool_schemas: List[Dict[str, Any]] = route_tool_schemas(
            goal, allow_edit, chrome_session, can_send_files, can_schedule,
        )
    except Exception as error:
        if service is not None and "integrations" not in options:
            try:
                await service.close()
            except Exception:
                logging.exception("Başlangıç hatasından sonra entegrasyon kapanışı başarısız")
        return startup_failure(error, current_backend)
    runtime_token = CURRENT_RUNTIME.set(runtime)
    service_token = CURRENT_SERVICE.set(service)
    steps: List[sm.StepRecord] = []
    outcome: str = ""
    reason: str = ""
    success: bool = False
    start_time: float = time.monotonic()
    no_progress_turns: int = 0
    tool_cache: Dict[str, ToolResult] = {}
    chrome_visits: Dict[str, int] = {}
    final_length_recoveries: int = 0
    empty_answer_recoveries: int = 0
    unexecuted_tool_recoveries: int = 0
    awaiting_real_tool_call: bool = False
    delivery_recoveries: int = 0
    file_receipts: Tuple[FileReceipt, ...] = ()
    artifacts: Tuple[ArtifactReady, ...] = ()
    head_len: int = len(messages)
    dropped_turns: int = 0
    idle_reports: int = 0
    goal_evidence: Dict[str, str] = {}
    must_execute_action: bool = action_execution_expected(goal) or file_contract is not None
    # Sürekli modda son yanıt "bitti" beyanı değil ilerleme raporudur; metin canlı akar.
    guarded_final_output: bool = not continuous and (
        must_change_source or must_execute_action or unmet_wait_status(goal, []) is not None
    )
    gui_verified: bool = False
    gui_verification_failures: int = 0
    task_ledger: str = ""
    host_task_ledger: TaskLedger = empty_task_ledger()
    combined_ledger: str = ""
    fast_loop_policy = FastLoopPolicy()
    fast_loop_state = FastLoopState()
    fast_loop_delivery_entries: int = 0
    fast_loop_stagnation_events: int = 0
    seen_shell_outputs: frozenset[str] = frozenset()
    seen_read_outputs: frozenset[str] = frozenset()
    seen_observations: frozenset[str] = frozenset()
    observations: int = 0
    observations_reused: int = 0
    duplicate_navigation_count: int = 0
    last_observation_digest: Optional[str] = None
    last_observation_injected_turn: Optional[int] = None
    turns: int = 0
    tool_call_count: int = 0
    usage: TokenUsage = ZERO_USAGE
    model_seconds: float = 0.0
    tool_seconds: float = 0.0
    experience_tracker: experience.TaskTracker = experience.new_tracker()
    experience_hints: int = 0
    # Bu turda başarısız olan/yürütülmeyen çağrılar: anahtar → modele verilecek gerekçe.
    recent_blocked_keys: Dict[str, str] = {}
    # Aynı görsel state'te daha önce no-op olduğu kanıtlanan doğrudan GUI girdileri. Ekran
    # değiştiğinde temizlenir; böylece form düzeltildikten sonra aynı submit yeniden denenebilir.
    no_effect_screen_digest: Optional[str] = None
    no_effect_action_keys: frozenset[str] = frozenset()
    goal_reports_seen: frozenset[str] = frozenset()
    goal_report_count: int = 0
    # Gönderim koruması: yazılmış bir taslak gönderildikten sonra doğrulanmadan sayfadan
    # ayrılmayı engeller (canlı kayıtta taslak bu yüzden hiç gönderilmedi).
    composed_text_seen: bool = False
    commit_pending_turn: Optional[int] = None
    metrics: sm.EpisodeMetrics

    def record_visual_action_outcome(
        digest: Optional[str], previous_digest: Optional[str],
        calls: List[ToolCallDraft], results: List[ToolResult],
    ) -> None:
        """Ekran değişmediyse son doğrudan GUI girdisini mevcut state için no-op olarak kaydeder."""
        nonlocal no_effect_screen_digest, no_effect_action_keys
        if digest is None:
            return
        if digest != previous_digest:
            no_effect_screen_digest = digest
            no_effect_action_keys = frozenset()
            return
        guarded_keys: List[str] = []
        for call, result in zip(calls, results, strict=True):
            if not result.get("ok"):
                continue
            key = no_effect_action_key(call)
            if key is not None:
                guarded_keys.append(key)
        if not guarded_keys:
            return
        if no_effect_screen_digest != digest:
            no_effect_screen_digest = digest
            no_effect_action_keys = frozenset()
        # Bir turda birden çok eylem varsa settled ekranı en doğrudan son girdi açıklar.
        # Önceki adımları yanlışlıkla no-op sayıp ileride meşru kullanımlarını engelleme.
        no_effect_action_keys = no_effect_action_keys | frozenset({guarded_keys[-1]})

    def recover_continuous(stall: str) -> None:
        """Takılan sürekli görev için alternatif adımı isteyip sayaçları sıfırlar."""
        nonlocal no_progress_turns, fast_loop_state, idle_reports, recent_blocked_keys
        messages.append({"role": "user", "content": (
            f"HOST — YENİDEN PLANLA: {stall}. Aynı başarısız çağrıyı aynı argümanlarla yineleme. "
            "Hatanın nedenini kullanarak farklı bir araç, argüman veya yöntem seç ve somut bir adım uygula. "
            "Gerçekten eksik kullanıcı bilgisi varsa ask_user çağır."
        )})
        no_progress_turns, fast_loop_state, idle_reports = 0, FastLoopState(), 0
        # Tekrar engeli kalkar: hata geçici olabilir (ağ, açılmayan uygulama) ve host zaten yeni
        # bir deneme istiyor. Aksi hâlde bir kez düşen çağrı görev boyunca bir daha denenemiyordu.
        recent_blocked_keys = {}

    try:
        for iteration in range(1, max_iterations + 1):
            if options["should_stop"]():
                outcome, reason = "Kullanıcı tarafından durduruldu.", "durduruldu"
                break
            if time.monotonic() - start_time - runtime.metrics["user_wait_seconds"] > max_wall_clock:
                outcome, reason = "", (
                    f"sınır doldu: süre ({max_wall_clock / 3600:g} saat)" if continuous
                    else f"zaman bütçesi ({max_wall_clock:.0f}sn) aşıldı"
                )
                break
            if max_total_tokens is not None and usage["prompt_tokens"] + usage["completion_tokens"] >= max_total_tokens:
                outcome, reason = "", f"sınır doldu: token ({max_total_tokens} giriş + çıkış)"
                break

            runtime.published = dict(runtime.selected)
            tool_schemas = route_tool_schemas(goal, allow_edit, chrome_session, can_send_files, can_schedule) + [
                entry["schema"] for name, entry in runtime.published.items() if name != "discover_capabilities"
            ] + ([GOAL_REPORT_SCHEMA] if continuous else [])
            runtime.allowed_tools = frozenset(entry["function"]["name"] for entry in tool_schemas)
            messages = _trim_old_turns(messages)
            if continuous:
                messages, dropped_turns = window_messages(
                    messages, head_len, CONTEXT_MAX_TURNS, CONTEXT_KEEP_TURNS, dropped_turns,
                )
            messages_for_model = inject_task_ledger_into_messages(messages, host_task_ledger)
            emit({"kind": "turn_started", "turn": iteration, "max_turns": max_iterations,
                  "backend": current_backend, "model": BACKENDS[current_backend]["model"]})

            def emit_model_event(event: AgentEvent) -> None:
                # Kanıt kapısına tabi görevde modelin erken "yaptım" metnini hiçbir yüzeye
                # aktarma. Kabul edilen nihai metin aşağıda host kararıyla yayınlanır.
                if guarded_final_output and event["kind"] in ("text_delta", "reasoning_delta"):
                    return
                emit(event)

            model_started: float = time.monotonic()
            try:
                turn, used_backend = await _call_model_with_retries(
                    clients, messages_for_model, tool_schemas, session_id, current_backend,
                    emit_model_event, options["should_stop"],
                )
            finally:
                model_elapsed: float = time.monotonic() - model_started
                model_seconds += model_elapsed
            turns += 1
            usage = add_usage(usage, turn["usage"])
            model_finished_event: AgentEvent = {
                "kind": "model_finished", "turn": iteration,
                "seconds": round(model_elapsed, 2), "usage": turn["usage"],
                "finish_reason": turn["finish_reason"],
                "tool_call_count": len(turn["tool_calls"]),
                "empty_content": not bool(turn["content"].strip()),
            }
            if not guarded_final_output:
                emit(model_finished_event)

            def finish_guarded_turn(visible_text: Optional[str] = None) -> None:
                """Kabul edilen metni model turunu kapatmadan önce göster; her turu bir kez kapat."""
                if not guarded_final_output:
                    return
                if visible_text:
                    emit({"kind": "text_delta", "text": visible_text})
                emit(model_finished_event)
            if used_backend != current_backend:
                permanent_failure = current_backend in runtime.blocked_backends
                emit({"kind": "backend_changed", "backend": used_backend, "model": BACKENDS[used_backend]["model"],
                      "reason": (
                          f"erişim/bakiye/hız sınırı; {current_backend} bu görevde yeniden denenmeyecek"
                          if permanent_failure else
                          f"geçici hata; yalnız bu tur için fallback, sonraki tur {current_backend} yeniden denenecek"
                      )})
                if permanent_failure:
                    current_backend = used_backend
            if turn["finish_reason"] == "stopped":
                finish_guarded_turn()
                outcome, reason = "Kullanıcı tarafından durduruldu.", "durduruldu"
                break
            messages.append(_assistant_entry(turn))

            if not turn["tool_calls"]:
                if contains_unexecuted_tool_call(turn["content"]):
                    if unexecuted_tool_recoveries < MAX_UNEXECUTED_TOOL_RECOVERIES:
                        unexecuted_tool_recoveries += 1
                        awaiting_real_tool_call = True
                        emit({"kind": "notice", "level": "warning",
                              "text": "Model araç çağrısını metin olarak yazdı; hiçbir araç çalışmadı. Gerçek araç çağrısı isteniyor."})
                        messages.append({
                            "role": "user",
                            "content": (
                                "Önceki yanıtta araç çağrısı yalnız metin olarak yazıldı; hiçbir araç çalışmadı. "
                                "Gerekli işlemi API'nin gerçek tool_calls alanıyla çağır. "
                                "Bir işlem yapamadıysan tamamlandı deme; somut engeli belirt."
                            ),
                        })
                        finish_guarded_turn()
                        continue
                    reason = "model araç çağrısını tekrar yalnız metin olarak yazdı; hiçbir araç çalışmadı"
                    outcome = f"Doğrulanmadı: {reason}" if guarded_final_output else turn["content"]
                    finish_guarded_turn(outcome)
                    break
                if awaiting_real_tool_call:
                    reason = "metinsel araç çağrısından sonra gerçek araç çağrısı yapılmadı"
                    outcome = f"Doğrulanmadı: {reason}" if guarded_final_output else turn["content"]
                    finish_guarded_turn(outcome)
                    break
                if continuous:
                    # Son yanıt ilerleme raporudur: görev sürer, bu dönemin çıktı kartları şimdi gelir.
                    emit_existing_artifacts(artifacts, emit)
                    artifacts = ()
                    idle_reports += 1
                    if idle_reports < MAX_IDLE_REPORTS:
                        messages.append({"role": "user", "content": CONTINUE_PROMPT})
                        continue
                    recover_continuous(f"{idle_reports} ardışık yanıtta hiçbir araç çalışmadı")
                    continue
                outcome = turn["content"]
                success, reason = final_verdict(outcome, turn["finish_reason"])
                if (
                    not success
                    and not outcome.strip()
                    and turn["finish_reason"] not in ("length", "content_filter")
                    and empty_answer_recoveries < MAX_EMPTY_ANSWER_RECOVERIES
                ):
                    # Model araç çağrısı olmadan tamamen boş bir yanıt döndürebiliyor (sağlayıcı
                    # aksaklığı veya kesilmiş akış); bu tek başına görevi bitirmemeli. Bir kez
                    # gerçek yanıt veya araç çağrısı istenir; boş yanıtın kaynağı raporda ayrıca
                    # `empty_model_answers` olarak görünür.
                    empty_answer_recoveries += 1
                    emit({"kind": "notice", "level": "warning",
                          "text": "Model boş yanıt döndü; gerçek yanıt veya araç çağrısı isteniyor."})
                    messages.append({
                        "role": "user",
                        "content": (
                            "Önceki yanıt tamamen boştu; ne metin ne araç çağrısı ürettin. Görevi "
                            "sürdür: gerekiyorsa gerekli işlemi API'nin gerçek tool_calls alanıyla "
                            "çağır, aksi hâlde STATE ve mevcut bulgularla kısa, tam bir final yanıt ver."
                        ),
                    })
                    outcome, reason, success = "", "", False
                    finish_guarded_turn()
                    continue
                navigation_gap = requested_chrome_navigation_gap(goal, steps) if success else None
                if navigation_gap is not None:
                    if delivery_recoveries < MAX_ACTION_EVIDENCE_RECOVERIES:
                        delivery_recoveries += 1
                        emit({"kind": "notice", "level": "warning", "text": navigation_gap})
                        feed_goal = "linkedin" in goal.casefold() and (
                            "akış" in goal.casefold() or "feed" in goal.casefold()
                        )
                        destination = (
                            "https://www.linkedin.com/feed/"
                            if feed_goal else "kullanıcının istediği URL"
                        )
                        new_tab_arg = (
                            "new_tab=true ve "
                            if "yeni Chrome sekmesi" in navigation_gap or "yeni sekmede" in navigation_gap
                            else ""
                        )
                        messages.append({
                            "role": "user",
                            "content": (
                                f"HOST: {navigation_gap}. chrome_active_tab aracını {new_tab_arg}"
                                f"hedef URL olarak {destination} ile çağır. Güncel sayfayı okuyup "
                                "doğrula; tamamlanmadan bitirme."
                            ),
                        })
                        outcome, reason, success = "", "", False
                        finish_guarded_turn()
                        continue
                    success, reason = False, navigation_gap
                status_gap = unmet_wait_status(goal, steps)
                if success and status_gap is not None:
                    success, reason = False, status_gap
                    if guarded_final_output:
                        outcome = f"Doğrulanmadı: {reason}"
                if success and must_change_source and not any(
                    step["tool"] in {"write_file", "edit_file"} and step["ok"] for step in steps
                ):
                    if delivery_recoveries < MAX_ACTION_EVIDENCE_RECOVERIES:
                        delivery_recoveries += 1
                        emit({"kind": "notice", "level": "warning",
                              "text": "Kod değişikliği istenmişti; henüz doğrulanmış dosya yazımı yok. Bir kez daha gerçek uygulama isteniyor."})
                        messages.append({
                            "role": "user",
                            "content": (
                                "Bu görevde kod değişikliği istendi fakat hiçbir write_file veya edit_file çağrısı başarıyla çalışmadı. "
                                "Planı tekrar anlatma veya yeniden onay isteme. Yetkili değişikliği gerçek araç çağrısıyla "
                                "uygula ve test et; teknik bir engel varsa tam nedenini bildir."
                            ),
                        })
                        outcome, reason, success = "", "", False
                        finish_guarded_turn()
                        continue
                    success = False
                    reason = "kod değişikliği istendi fakat hiçbir dosya başarıyla değiştirilmedi"
                    if guarded_final_output:
                        outcome = f"Doğrulanmadı: {reason}"
                action_gap: Optional[str] = None
                action_evidence_missing: bool = False
                if success and must_execute_action:
                    if file_contract is not None and file_contract["kind"] == "unsupported":
                        action_gap = file_contract["reason"]
                    else:
                        action_evidence_missing = not has_action_evidence(goal, steps)
                        if action_evidence_missing:
                            action_gap = "eylem istendi fakat başarılı işlem kanıtı yok"
                        if action_gap is None and file_contract is not None:
                            action_gap = file_delivery_gap(file_contract, file_receipts)
                    if action_gap is None and file_contract is None:
                        action_gap = unmet_explicit_deletion(goal)
                if success and action_gap is not None:
                    recoverable_file_target: bool = (
                        file_contract is None or file_contract["kind"] != "unsupported"
                    )
                    if recoverable_file_target and delivery_recoveries < MAX_ACTION_EVIDENCE_RECOVERIES:
                        delivery_recoveries += 1
                        emit({"kind": "notice", "level": "warning",
                              "text": (
                                  "Eylem istendi; başarılı yürütme kanıtı yok. Bir gerçek işlem denemesi isteniyor."
                                  if action_evidence_missing else
                                  f"{action_gap}. Bir gerçek işlem denemesi isteniyor."
                              )})
                        recovery_instruction = (
                            "Bu eylem için başarılı bir yürütme aracı sonucu yok. Okuma veya keşif, "
                            "işlemin tamamlandığını kanıtlamaz. İstenen eylemi gerçek araçla uygula "
                            "ve sonucu gözlemle; yapamıyorsan somut engeli bildir."
                            if action_evidence_missing else
                            f"{action_gap}. Hedef dosyayı gerçekten taşı ve hedefte kaynakla aynı "
                            "içeriğin bulunduğunu kontrol et; yapamıyorsan somut engeli bildir."
                            if file_contract is not None and file_contract["kind"] == "move" else
                            f"{action_gap}. Hedef dosyanın içeriğini gerçekten değiştir ve son "
                            "durumunu kontrol et; yapamıyorsan somut engeli bildir."
                            if file_contract is not None and file_contract["kind"] == "edit" else
                            f"{action_gap}. Başka dosyayı yazma bu hedefin silindiğini kanıtlamaz. "
                            "Hedefi gerçekten kaldırıp son durumunu kontrol et; yapamıyorsan somut engeli bildir."
                        )
                        messages.append({
                            "role": "user",
                            "content": recovery_instruction,
                        })
                        outcome, reason, success = "", "", False
                        finish_guarded_turn()
                        continue
                    success = False
                    reason = action_gap
                    if guarded_final_output:
                        outcome = f"Doğrulanmadı: {reason}"
                if success and not gui_verified and gui_verification_needed(steps):
                    # Ekranda iş yapan görev bir kez güncel ekranla doğrulanmadan bitmez: canlı kayıtta
                    # model formun yarısını doldurup "gönderdim", paneli kaydırmadan "tüm ilanlara
                    # baktım" dedi. Bitiş anındaki gözlem tahmini değil, gerçek son durumu gösterir.
                    emit({"kind": "notice", "level": "info",
                          "text": "Bitiş doğrulaması: güncel ekranla her zorunlu madde kontrol ediliyor."})
                    finish_guarded_turn()
                    verification_started: float = time.monotonic()
                    try:
                        observation, observation_step, _digest = await _observe_after_actions(
                            f"dogrulama-{session_id[:8]}-{iteration}", 0, VERIFICATION_OBSERVATION_PREVIEW,
                            toolbox, tool_cache, emit, options["should_stop"],
                        )
                    finally:
                        tool_seconds += time.monotonic() - verification_started
                    steps.append(observation_step)
                    observations += 1
                    verification_ok = bool(observation_step["ok"] and _digest is not None)
                    messages.append(verification_message(observation, gui_evidence_summary(steps)))
                    if verification_ok:
                        gui_verified = True
                        gui_verification_failures = 0
                    else:
                        gui_verification_failures += 1
                        if gui_verification_failures >= 2:
                            outcome = ""
                            success = False
                            reason = "bitiş doğrulaması iki denemede alınamadı"
                            break
                        emit({"kind": "notice", "level": "warning",
                              "text": "Bitiş doğrulaması alınamadı; bir kez daha güncel ekran denenecek."})
                    outcome, reason, success = "", "", False
                    continue
                if (
                    not success
                    and turn["finish_reason"] == "length"
                    and final_length_recoveries < MAX_FINAL_LENGTH_RECOVERIES
                ):
                    final_length_recoveries += 1
                    messages.append({
                        "role": "user",
                        "content": (
                            "Önceki araçsız yanıt max_tokens sınırında kesildi. STATE'i ve mevcut "
                            "sonuçları kullanarak eksik zorunlu kısmı tamamla; yöntem anlatma ve "
                            "gereksiz yeni araştırma yapma. Final yanıtı kısa tut."
                        ),
                    })
                    outcome, reason = "", ""
                    finish_guarded_turn()
                    continue
                if guarded_final_output and not success and turn["finish_reason"] in ("length", "content_filter"):
                    outcome = f"Doğrulanmadı: {reason}"
                finish_guarded_turn(outcome)
                break

            finish_guarded_turn()
            awaiting_real_tool_call = False
            idle_reports = 0
            if turn["finish_reason"] == "length":
                emit({"kind": "notice", "level": "warning",
                      "text": "Yanıt max_tokens sınırında kesildi; araç argümanları eksik olabilir."})
            tool_call_count += len(turn["tool_calls"])
            tools_started: float = time.monotonic()
            # report_goal_met host'a aittir: kanıtı görev kaydıyla denetler ve kullanıcıya onaylatır.
            hosted_mask: List[bool] = [
                continuous and call["name"] == GOAL_REPORT_TOOL for call in turn["tool_calls"]
            ]
            regular_calls: List[ToolCallDraft] = [
                call for call, hosted in zip(turn["tool_calls"], hosted_mask, strict=True) if not hosted
            ]
            confirmed_goal: Optional[str] = None
            try:
                commit_guard: bool = (
                    commit_pending_turn is not None
                    and iteration <= commit_pending_turn + COMMIT_GUARD_TURNS
                )

                def no_effect_blocked(candidate: ToolCallDraft) -> bool:
                    """Aynı GUI girdisi aynı son gözlem state'inde daha önce no-op oldu mu."""
                    key = no_effect_action_key(candidate)
                    return bool(
                        key is not None
                        and no_effect_screen_digest is not None
                        and no_effect_screen_digest == last_observation_digest
                        and key in no_effect_action_keys
                    )

                # Aynı turdaki yaz → gönder → gezin dizisini yürütmeden önce denetle.
                # Araçlar sırayla çalışsa da sonuçlar ancak tur sonunda geldiğinden,
                # önceki çağrıların niyetini ihtiyatlı biçimde korumaya dahil et.
                block_reasons: List[Optional[str]] = []
                batch_composed: bool = composed_text_seen
                batch_commit_guard: bool = commit_guard
                for candidate in regular_calls:
                    if (
                        (batch_commit_guard and commit_navigation_call(candidate))
                        or commit_then_navigation_in_sequence(candidate)
                    ):
                        blocked = COMMIT_UNVERIFIED_MESSAGE
                    elif no_effect_blocked(candidate):
                        blocked = REPEATED_NO_EFFECT_ACTION_MESSAGE
                    else:
                        blocked = recent_blocked_keys.get(failed_call_key(candidate))
                    block_reasons.append(blocked)
                    if blocked is None:
                        batch_composed = batch_composed or text_entry_call(candidate)
                        batch_commit_guard = batch_commit_guard or (
                            batch_composed and commit_action_call(candidate)
                        )
                blocked_mask: List[bool] = [reason is not None for reason in block_reasons]
                runnable_calls: List[ToolCallDraft] = [
                    call for call, blocked in zip(regular_calls, blocked_mask, strict=True) if not blocked
                ]
                runnable_results: List[ToolResult] = await _execute_tool_calls(
                    runnable_calls, toolbox, tool_cache, emit, options["should_stop"],
                ) if runnable_calls else []
                blocked_results: List[ToolResult] = []
                for index, (call, blocked) in enumerate(zip(regular_calls, blocked_mask, strict=True)):
                    if not blocked:
                        continue
                    message: str = block_reasons[index] or REPEATED_FAILED_CALL_MESSAGE
                    committed: bool = message == COMMIT_UNVERIFIED_MESSAGE
                    no_effect: bool = no_effect_blocked(call)
                    if committed:
                        code, error_type = "COMMIT_UNVERIFIED", "CommitNotVerified"
                    elif no_effect:
                        code, error_type = "REPEATED_NO_EFFECT_ACTION", "RepeatedNoEffectAction"
                    else:
                        code, error_type = "REPEATED_FAILED_CALL", "RepeatedFailedCall"
                    emit({"kind": "tool_started", "call_id": call["id"], "index": index,
                          "name": call["name"], "preview": preview_arguments(call["name"], call["arguments"]),
                          "argument_tag": argument_tag(call["name"], call["arguments"]),
                          "point": argument_point(call["name"], call["arguments"])})
                    emit({"kind": "tool_finished", "call_id": call["id"], "ok": False,
                          "text": message, "seconds": 0.0, "code": code})
                    blocked_results.append({"tool_call_id": call["id"], "ok": False,
                                            "error_type": error_type,
                                            "error": message, "code": code, "recoverable": True})
                regular_results: List[ToolResult] = merge_call_results(
                    blocked_mask, runnable_results, blocked_results,
                )
                attempted_keys: frozenset[str] = frozenset(
                    failed_call_key(call) for call, blocked in zip(regular_calls, blocked_mask, strict=True)
                    if not blocked
                )
                if attempted_keys:
                    recent_blocked_keys = {
                        failed_call_key(call): REPEATED_FAILED_CALL_MESSAGE
                        for call, result, blocked in zip(regular_calls, regular_results, blocked_mask, strict=True)
                        if not blocked and not result.get("ok")
                    }
                goal_evidence = record_goal_evidence(goal_evidence, regular_calls, regular_results)
                # Host araçları aynı turdaki GUI sonuçlarından sonra işlenir. Gönderim
                # doğrulanmadan report_goal_met ile kullanıcıya başarı sorulmasını engelle.
                executed_regular_calls: List[Optional[ToolCallDraft]] = [
                    executed_call_prefix(call, result)
                    for call, result in zip(regular_calls, regular_results, strict=True)
                ]
                turn_composed: bool = composed_text_seen or any(
                    call is not None and text_entry_call(call) for call in executed_regular_calls
                )
                turn_commit_pending: bool = commit_guard or (
                    turn_composed and any(
                        call is not None and commit_action_call(call)
                        for call in executed_regular_calls
                    )
                )
                hosted_results: List[ToolResult] = []
                for index, (call, hosted) in enumerate(zip(turn["tool_calls"], hosted_mask, strict=True)):
                    if hosted and confirmed_goal is not None:
                        # Aynı turdaki ikinci bildirim kullanıcıya ikinci kez sorulmaz.
                        hosted_results.append({"tool_call_id": call["id"], "ok": True,
                                               "result": "Hedef bu turda zaten onaylandı."})
                    elif hosted:
                        hosted_result, confirmed, goal_reports_seen, asked = await resolve_goal_report(
                            call, index, goal_evidence, runtime, emit,
                            goal_reports_seen, goal_report_count, turn_commit_pending,
                        )
                        # Tavan yalnız kullanıcıya gerçekten sorulan bildirimleri sayar: geçersiz
                        # kanıtla yapılan denemeler sonraki meşru bildirimi engellememeli.
                        goal_report_count += int(asked)
                        hosted_results.append(hosted_result)
                        confirmed_goal = confirmed
                results: List[ToolResult] = merge_call_results(hosted_mask, regular_results, hosted_results)
            finally:
                tool_seconds += time.monotonic() - tools_started

            failures_in_turn: int = 0
            facts_before_turn = dict(host_task_ledger["facts"])
            pending_shots: List[ToolCallDraft] = []
            turn_observation_digests: List[str] = []
            duplicate_navigation_notes: List[str] = []
            for call, result in zip(turn["tool_calls"], results, strict=True):
                ok: bool = bool(result.get("ok"))
                detail: str = result_text(result)
                steps.append(sm.make_step_record(
                    call["name"], call["arguments"], ok, detail,
                    partial_steps=int(result.get("completed_steps") or 0),
                ))
                host_task_ledger = record_tool_receipt(
                    host_task_ledger, call["id"], call["name"], ok, detail, iteration,
                )
                if file_contract is not None:
                    file_receipts += receipt_for_call(call, result, file_cwd)
                if ok and call["name"] == "take_screenshot":
                    pending_shots.append(call)
                artifact: Optional[ArtifactReady] = artifact_event_for_call(call, result, file_cwd, goal)
                if artifact is not None:
                    artifacts = merge_artifact(artifacts, artifact)
                if not ok:
                    failures_in_turn += 1
                if ok and call["name"] not in _ACTION_RECEIPT_TOOLS:
                    host_task_ledger = record_tool_result(host_task_ledger, call["name"], detail, iteration)
                # Deneyim belleği yalnız hata anında konuşur: aynı hata imzası için doğrulanmış
                # önceki çözüm ve görev içi tekrar uyarısı araç sonucunun altına eklenir.
                if result.get("code") in (
                    "REPEATED_FAILED_CALL", "REPEATED_NO_EFFECT_ACTION", "COMMIT_UNVERIFIED",
                ):
                    # Host'un yürütmediği çağrı gerçek bir araç hatası değildir; deneyim belleğine
                    # yazılırsa aynı imza için sahte "doğrulanmış çözüm" dersleri birikir.
                    observed = {"notes": [], "lesson_id": None}
                else:
                    experience_tracker, observed = experience.observe_result(
                        experience_state, experience_tracker, call["name"], call["arguments"], ok, detail, iteration,
                    )
                tool_message: Dict[str, Any] = _tool_result_to_message(call, result)
                if observed["notes"]:
                    tool_message = {**tool_message, "content": tool_message["content"] + "\n\n" + "\n\n".join(observed["notes"])}
                if observed["lesson_id"] is not None:
                    experience_hints += 1
                    emit({"kind": "notice", "level": "info",
                          "text": "Deneyim belleği: bu hata için daha önce doğrulanmış çözüm hatırlatıldı."})
                messages.append(tool_message)
                chrome_visits, duplicate_note = update_chrome_visits(chrome_visits, call, result)
                if duplicate_note is not None:
                    duplicate_navigation_notes.append(duplicate_note)
                    duplicate_navigation_count += 1

            if confirmed_goal is not None:
                # Sürekli görevin tek başarı yolu: kanıtlı hedefi kullanıcı onayladı.
                outcome, reason, success = confirmed_goal, "", True
                emit({"kind": "text_delta", "text": confirmed_goal})
                break

            # Ekran gözlemleri TÜM araç mesajlarından SONRA eklenir: tool sonuçları assistant
            # tool_calls'ı kesintisiz izlemeli; araya user mesajı 400'e yol açar.
            turn_acted: bool = any(
                call["name"] in _GUI_VERIFICATION_TOOLS
                and (result.get("ok") or int(result.get("completed_steps") or 0) > 0)
                for call, result in zip(turn["tool_calls"], results, strict=True)
            )
            for shot_call in pending_shots:
                try:
                    observation, digest = await _screenshot_observation_with_digest(shot_call)
                    observations += 1
                    turn_observation_digests.append(digest)
                    previous_observation_digest = last_observation_digest
                    shot_note: Optional[str] = (
                        unchanged_screen_note(digest, previous_observation_digest) if turn_acted else None
                    )
                    record_visual_action_outcome(
                        digest, previous_observation_digest, turn["tool_calls"], results,
                    )
                    messages.append(with_observation_note(observation, shot_note))
                    last_observation_digest = digest
                    last_observation_injected_turn = iteration
                except (OSError, KeyError, ValueError) as error:
                    logging.warning("Ekran görüntüsü modele eklenemedi", extra={"error_type": type(error).__name__})
                    emit({"kind": "notice", "level": "warning",
                          "text": f"Ekran görüntüsü modele iliştirilemedi ({type(error).__name__}: {error})."})

            # Eylem turu kendi gözlemiyle biter: model sonucu görmek için ayrı bir "ekran
            # görüntüsü al" turu harcamaz. Genel GUI yolunda da geçerlidir.
            if needs_action_observation(turn["tool_calls"], results):
                observation_started: float = time.monotonic()
                try:
                    observation, observation_step, observation_digest = await _observe_after_actions(
                        f"otomatik-gozlem-{session_id[:8]}-{iteration}", len(turn["tool_calls"]),
                        AUTO_OBSERVATION_PREVIEW, toolbox, tool_cache, emit, options["should_stop"],
                    )
                finally:
                    tool_seconds += time.monotonic() - observation_started
                steps.append(observation_step)
                observations += 1
                if observation_digest is not None:
                    turn_observation_digests.append(observation_digest)
                previous_observation_digest = last_observation_digest
                action_note: Optional[str] = (
                    unchanged_screen_note(observation_digest, previous_observation_digest) if turn_acted else None
                )
                record_visual_action_outcome(
                    observation_digest, previous_observation_digest, turn["tool_calls"], results,
                )
                if should_reuse_observation(
                    observation_digest, previous_observation_digest,
                    current_turn=iteration, last_injected_turn=last_observation_injected_turn,
                ):
                    observations_reused += 1
                    messages.append(with_observation_note({
                        "role": "user",
                        "content": (
                            "Otomatik gözlem önceki yakın görselle aynı; image payload yeniden "
                            "gönderilmedi. Önceki görsel hâlâ full-detail context içinde."
                        ),
                    }, action_note))
                else:
                    messages.append(with_observation_note(observation, action_note))
                    if observation_digest is not None:
                        last_observation_digest = observation_digest
                        last_observation_injected_turn = iteration

            if duplicate_navigation_notes:
                messages.append({"role": "user", "content": "\n".join(duplicate_navigation_notes)})
            failure_recovery: Optional[str] = failed_tool_recovery_message(turn["tool_calls"], results)
            if failure_recovery is not None:
                messages.append({"role": "user", "content": failure_recovery})
            if turn["finish_reason"] == "length":
                messages.append({
                    "role": "user",
                    "content": (
                        "Bu araç çağrısı turu max_tokens sınırında kesildi. Başarılı çağrıları STATE'e "
                        "işlenmiş kabul et; eksik/başarısız çağrıları yalnız zorunluysa yeniden oluştur. "
                        "Aynı hedefleri baştan dolaşma ve kalan teslim adımlarına öncelik ver."
                    ),
                })

            # Gönderim koruması: yazılmış bir taslak gönderildiyse sonraki turlarda sayfadan ayrılma
            # kilitlenir; ajan ekrandan gönderimi doğrulayınca (ya da gönder düğmesine yeniden
            # basınca) koruma kalkar. Canlı kayıtta ajan gönderiyi yayınlamadan sayfayı
            # değiştirdi ve taslak hiç gönderilmedi.
            turn_pairs: List[Tuple[ToolCallDraft, ToolResult]] = list(
                zip(turn["tool_calls"], results, strict=True)
            )
            executed_turn_calls: List[Optional[ToolCallDraft]] = [
                executed_call_prefix(call, result) for call, result in turn_pairs
            ]
            if any(call is not None and text_entry_call(call) for call in executed_turn_calls):
                composed_text_seen = True
            if composed_text_seen and any(
                call is not None and commit_action_call(call) for call in executed_turn_calls
            ):
                commit_pending_turn = iteration
            # Başka bir GUI tıklaması gönderim onayı değildir; koruma süre penceresi boyunca sürer.
            if any(result.get("ok") and commit_navigation_call(call) for call, result in turn_pairs):
                # Sayfa gerçekten değiştiyse önceki taslak bağlamı bitti.
                composed_text_seen = False

            all_failed: bool = bool(results) and all(
                not result.get("ok") and int(result.get("completed_steps") or 0) == 0
                for result in results
            )
            task_ledger = extract_task_ledger(task_ledger, turn["content"])
            if turn["content"]:
                host_task_ledger = record_model_state(host_task_ledger, turn["content"])
            combined_ledger = format_ledger_prompt(host_task_ledger) or task_ledger
            ledger_changed: bool = host_task_ledger["facts"] != facts_before_turn
            unresolved_deliverables: int = 0 if (_ledger_delivery_ready(task_ledger) or _ledger_delivery_ready(combined_ledger)) else 1
            observation_digest: Optional[str] = ":".join(turn_observation_digests)[:512] or None
            signature: str = turn_progress_signature(
                turn["tool_calls"], results,
                observation_digest,
                combined_ledger, unresolved_deliverables,
            )
            seen_shell_outputs, shell_output_progress = novel_shell_output_progress(
                turn["tool_calls"], results, seen_shell_outputs,
            )
            seen_read_outputs, read_output_progress = novel_read_output_progress(
                turn["tool_calls"], results, seen_read_outputs,
            )
            # Görevde daha önce görülmüş ekrana dönüş (A→B→A) ilerleme değildir: ölçümde model iki ilan
            # arasında 25 tur gidip geldi; her farklı görüntü ilerleme sayıldığı için döngü durmadı.
            revisited_screen: bool = bool(turn_observation_digests) and all(
                digest in seen_observations for digest in turn_observation_digests
            )
            seen_observations = seen_observations | frozenset(turn_observation_digests)
            deterministic_progress: bool = host_turn_progress(
                turn["tool_calls"], results, None if revisited_screen else observation_digest,
                signature, fast_loop_state.last_signature,
            ) or shell_output_progress or read_output_progress
            semantic_progress: bool = classify_semantic_progress(
                ledger_changed=ledger_changed,
                deterministic_progress=deterministic_progress,
                has_ledger=bool(task_ledger),
                previous_signature=fast_loop_state.last_signature,
                signature=signature,
                all_failed=all_failed,
            )
            if not semantic_progress:
                fast_loop_stagnation_events += 1
            if not continuous:
                previous_phase = fast_loop_state.phase
                decision = advance_fast_loop(
                    fast_loop_state,
                    TurnSignal(
                        signature=signature,
                        semantic_progress=semantic_progress,
                        unresolved_deliverables=unresolved_deliverables,
                        delivery_ready=_ledger_delivery_ready(task_ledger) or _ledger_delivery_ready(combined_ledger),
                        visual_turn=bool(turn_observation_digests),
                    ),
                    fast_loop_policy,
                )
                fast_loop_state = decision.state
                if decision.notice:
                    emit({"kind": "notice", "level": "warning", "text": decision.notice})
                if decision.request_replan:
                    messages.append({"role": "user", "content": _fast_loop_prompt("replan", combined_ledger)})
                elif decision.entered_delivery:
                    fast_loop_delivery_entries += 1
                    messages.append({"role": "user", "content": _fast_loop_prompt("delivery", combined_ledger)})
                elif previous_phase != fast_loop_state.phase and fast_loop_state.phase == "conserve":
                    messages.append({
                        "role": "user",
                        "content": (
                            "HOST FAST LOOP — CONSERVE: Opsiyonel keşfi ve tekrar kontrollerini azalt; "
                            "mevcut STATE ile zorunlu işi en kısa yoldan sürdür."
                        ),
                    })
                if decision.stop_reason is not None:
                    reason = decision.stop_reason
                    break

            # Her tur sonunda oturum kontrol noktası atomik olarak saklanır
            try:
                completed_summary = [f"{s['tool']}: {s['detail'][:80]}" for s in steps if s.get("ok")][-5:]
                save_checkpoint(
                    session_id=session_id,
                    goal=goal,
                    facts={key: fact["value"] for key, fact in host_task_ledger["facts"].items()},
                    completed_steps=completed_summary,
                    turn_count=iteration,
                )
            except Exception as cp_err:
                logging.warning("Checkpoint kaydedilemedi: %s", cp_err)

            # Araç/provider hatası model kalitesi sinyali değildir; model fallback yalnız
            # model/API çağrısının kendi hata yolunda (_call_model_with_retries) yapılır.
            no_progress_turns = no_progress_turns + 1 if all_failed else 0
            if no_progress_turns >= NO_PROGRESS_LIMIT:
                stall: str = f"ilerleme yok: {NO_PROGRESS_LIMIT} ardışık tamamen başarısız araç turu"
                emit({"kind": "notice", "level": "warning", "text": stall})
                if not continuous:
                    reason = stall
                    break
                recover_continuous(stall)
        else:
            success = False
            reason = f"maksimum iterasyon sayısına ({max_iterations}) ulaşıldı"
    except Exception as error:
        outcome, reason = f"Kritik hata: {error}", f"kritik hata: {error}"
        emit({"kind": "notice", "level": "error", "text": outcome})
    finally:
        history_answer: str = outcome
        if not success:
            # Yarım kalan görev etiketlenir ve güvenilir çalışma kaydı geçmişe taşınır:
            # kullanıcı "devam et" dediğinde doğrulanmış bilgileri yeniden aramaz.
            # Kanıt kapısına tabi görevde modelin kendi STATE beyanı doğrulanmış değildir;
            # yalnız gerçek araç çıktılarından ayıklanan host gerçeklerini geçmişe taşı.
            history_ledger: str = (
                format_ledger_prompt({**host_task_ledger, "model_state": ""})
                if guarded_final_output else combined_ledger or task_ledger
            )
            history_answer = "\n".join(part for part in (
                f"[Görev tamamlanamadı: {reason}]" if reason else "[Görev tamamlanamadı]",
                history_ledger, outcome,
            ) if part)
        exchange: Exchange = make_exchange(goal, history_answer, steps)
        metrics = {
            "turns": turns, "tool_calls": tool_call_count,
            "elapsed_seconds": round(time.monotonic() - start_time, 2), "backend": current_backend,
            "prompt_tokens": usage["prompt_tokens"], "cached_tokens": usage["cached_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "model_seconds": round(model_seconds, 2), "tool_seconds": round(tool_seconds, 2),
            "fast_loop_transitions": fast_loop_state.transitions,
            "fast_loop_replans": fast_loop_state.replans,
            "fast_loop_delivery_entries": fast_loop_delivery_entries,
            "semantic_progress_events": fast_loop_state.semantic_progress_events,
            "fast_loop_stagnation_events": fast_loop_stagnation_events,
            "observations": observations,
            "observations_reused": observations_reused,
            "duplicate_navigation": duplicate_navigation_count,
            "uncached_prompt_tokens": max(0, usage["prompt_tokens"] - usage["cached_tokens"]),
            "integrations": dict(runtime.metrics),
            "experience_hints": experience_hints,
            "experience_candidates": len(experience_tracker["candidates"]) if success else 0,
        }
        cleanup_errors: List[str] = []
        if success:
            try:
                clear_checkpoint(session_id)
            except Exception as error:
                cleanup_errors.append(f"Checkpoint temizliği: {type(error).__name__}: {error}")
        try:
            sm.save_state(options["state_file"], sm.record_episode(state, goal, steps, outcome, success, metrics))
        except Exception as error:
            cleanup_errors.append(f"Bellek kaydı: {type(error).__name__}: {error}")
        try:
            if experience.tracker_changes_state(experience_tracker, success):
                # Görev sürerken başka süreç (UI/Telegram) ders yazmış olabilir: son hâl üzerine işlenir.
                experience.save_experience(experience_file, experience.finish_task(
                    experience.load_experience(experience_file), experience_tracker, success,
                    datetime.now(timezone.utc).isoformat(),
                ))
        except Exception as error:
            cleanup_errors.append(f"Deneyim belleği: {type(error).__name__}: {error}")
        try:
            await toolbox.close_browser()
        except Exception as error:
            cleanup_errors.append(f"Tarayıcı kapanışı: {type(error).__name__}: {error}")
        try:
            if service.outlook:
                service.outlook.release(runtime)
        except Exception as error:
            cleanup_errors.append(f"Outlook görev temizliği: {type(error).__name__}: {error}")
        try:
            if "integrations" not in options:
                await service.close()
        except Exception as error:
            cleanup_errors.append(f"Entegrasyon kapanışı: {type(error).__name__}: {error}")
        CURRENT_RUNTIME.reset(runtime_token)
        CURRENT_SERVICE.reset(service_token)
        for detail in cleanup_errors:
            logging.warning("Görev temizliği başarısız: %s", detail)
            try:
                emit({"kind": "notice", "level": "warning", "text": detail})
            except Exception:
                logging.exception("Temizlik uyarısı yayınlanamadı")
        # Çıktı kartları görev sonunda (sürekli modda her ilerleme raporunda) bir kez gelir.
        emit_existing_artifacts(artifacts, emit)
        emit({"kind": "run_finished", "success": success, "outcome": outcome, "reason": reason, "metrics": metrics})
    return {"outcome": outcome, "success": success, "reason": reason,
            "metrics": metrics, "exchange": exchange}


def print_event(event: AgentEvent) -> None:
    """Olayları terminale akış olarak basar (CLI): metin harf harf, komut çıktısı satır satır."""
    if event["kind"] == "run_started":
        mode: str = event.get("run_mode", "normal")
        max_turns: int = event.get("max_turns", MAX_ITERATIONS)
        print(f"› {event['goal']}\n  ({event['backend']} · {event['model']} · {mode} · en çok {max_turns} tur)")
    elif event["kind"] == "text_delta":
        sys.stdout.write(event["text"])
        sys.stdout.flush()
    elif event["kind"] == "tool_started":
        print(f"\n⏺ {tool_label(event['name'])}({event['preview'].splitlines()[0] if event['preview'] else ''})")
    elif event["kind"] == "tool_output":
        sys.stdout.write(f"  │ {event['text']}")
        sys.stdout.flush()
    elif event["kind"] == "tool_finished":
        first_line: str = event["text"].strip().splitlines()[0][:160] if event["text"].strip() else ""
        print(f"  ⎿ {'✓' if event['ok'] else '✗'} {first_line} ({event['seconds']:.1f}sn)")
    elif event["kind"] == "model_finished":
        tokens: str = (f"↑{compact_count(event['usage']['prompt_tokens'])} "
                       f"(önbellek {compact_count(event['usage']['cached_tokens'])}) ↓{event['usage']['completion_tokens']}")
        print(f"\n  ⏱ model {event['seconds']:.1f}sn · {tokens}")
    elif event["kind"] == "backend_changed":
        print(f"  ↻ backend: {event['backend']} ({event['reason']})")
    elif event["kind"] == "stream_reset":
        print(f"\n  ↻ akış sıfırlandı: {event['reason']}")
    elif event["kind"] == "notice":
        print(f"  [{event['level']}] {event['text']}")
    elif event["kind"] == "integration_status":
        print(f"  · {event['text']}")
    elif event["kind"] == "run_finished":
        metrics: sm.EpisodeMetrics = event["metrics"]
        status: str = "✓ Tamamlandı" if event["success"] else f"✗ Tamamlanamadı: {event['reason']}"
        print(f"\n{status} · {metrics['turns']} tur · {metrics['tool_calls']} araç · {metrics['elapsed_seconds']:.1f}sn")


async def run_agent(goal: str) -> RunReport:
    """Hedefi terminale akış olarak basarak çalıştırır (CLI)."""
    clients: Dict[str, AsyncOpenAI] = create_model_clients()
    try:
        options: RunOptions = {"requested_backend": None, "should_stop": lambda: False, "state_file": STATE_FILE, "history": []}
        return await run_agent_with_callback(goal, print_event, options, clients)
    finally:
        await close_model_clients(clients)


if __name__ == "__main__":
    cli_goal: str = " ".join(sys.argv[1:]).strip()
    if not cli_goal:
        print("Kullanım: omniagent <hedef metni>")
        raise SystemExit(1)
    # Ayarlar sayfasında kaydedilen anahtarlar yalnız eksikse ortama uygulanır.
    apply_stored_api_keys()
    asyncio.run(run_agent(cli_goal))


# Genel API: modülün dışa açtığı adlar. Testlerin kastî olarak yamaladığı iç seam'ler (ör.
# _trim_old_turns, _call_model_with_retries, _screenshot_observation_with_digest) alt çizgiyle
# başladığı için burada yer almaz; modülde erişilebilir kalır. Star-import yalnız bu adları getirir.
__all__: List[str] = sorted(
    name for name, value in globals().items()
    if not name.startswith("_")
    and (getattr(value, "__module__", None) == __name__
         or isinstance(value, (str, int, float, bool, tuple, frozenset, list)))
)
