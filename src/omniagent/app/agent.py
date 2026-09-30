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
from io import BytesIO
from itertools import count
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, TypedDict, NotRequired
from urllib.request import urlopen

from openai import APIError, AsyncOpenAI
from openai.types import CompletionUsage
from PIL import Image, ImageOps

from omniagent.config import (
    API_KEY_VARIABLES, BACKENDS, CONTINUOUS_GUIDANCE, DEFAULT_BACKEND, ESCALATION_BACKEND,
    FILE_EXCHANGE_GUIDANCE, INTEGRATIONS_GUIDANCE, SCHEDULING_GUIDANCE, SYSTEM_PROMPT, BackendProfile,
    apply_stored_api_keys, redact,
)
from omniagent.core.events import (
    AWAITING_APPROVAL_CODE, AWAITING_DIRECTION_CODE, AgentEvent, ArtifactReady, EventSink, ProviderFallback,
    TokenUsage, argument_point, argument_tag, compact_count, preview_arguments, provider_fallback_text, tool_label,
)
from omniagent.core.fast_loop import (
    FastLoopPolicy, FastLoopState, TurnSignal, advance_fast_loop, classify_semantic_progress,
    normalize_progress_signature,
)
from omniagent.tools import (
    MODEL_SCREEN_SIZE, SCREENSHOT_MAX_EDGE, TOOL_RUNTIME, ToolRuntime, Toolbox, ToolError,
    shell_command_words,
)
from omniagent.tools.ax_snapshot import AX_SUMMARY_MARKER, AX_SUMMARY_TRIMMED
from omniagent.tools.bot_wall import ACCESS_CHALLENGE_CODE
from omniagent.app.continuous import (
    CONTEXT_KEEP_TURNS, CONTEXT_MAX_TURNS, CONTINUE_PROMPT, CONTINUOUS_MODE, CONTINUOUS_STAGNATION_LIMIT,
    GOAL_APPROVAL_TIMEOUT_SECONDS, MAX_GOAL_REPORTS, MAX_CONTINUOUS_REPLANS,
    MAX_IDLE_REPORTS, REPLAN_GUIDANCE, WALL_CONTINUE_PROMPT, WALL_REPLAN_GUIDANCE, continuous_limits_path,
    goal_confirmation_question, goal_report_problem, goal_report_repeat_problem, load_continuous_limits,
    window_messages, text_confirmation_question, gui_progress_problem,
)
from omniagent.approval import approval_granted
from omniagent.app.tool_schema import (
    AUTO_OBSERVATION_PREVIEW,
    ELEMENT_TOOL_NAMES,
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
    _fast_loop_wall_prompt,
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
    screenshot_requested,
    source_change_expected,
    unmet_explicit_deletion,
    explicit_deletion_target,
    unmet_wait_status,
)
from omniagent.app.answer_fidelity import (
    FidelityReport, apply_token_corrections, check_answer_fidelity, code_token_set, correction_notice,
    observed_step_texts, unverified_notice,
)
from omniagent.app.partial_report import (
    access_challenge_notes, format_partial_report, report_facts, stopped_at_access_wall, verified_note_codes,
)
from omniagent.app.model_retry import (
    INTERACTIVE_MODEL_RETRY_SECONDS, STATUS_MIN_WAIT_SECONDS, MODEL_ERROR_KIND_LABELS, UNRETRYABLE_KINDS,
    ModelCallFailed, ModelErrorInfo, RetryDecision, classify_model_error, cooling_backends, decide_retry,
    failure_label, interactive_model_retry_seconds, is_context_overflow, model_call_failure,
    model_retry_deadline, next_different_backend, retry_status_text, unattended_model_retry_seconds,
)
from omniagent.fallback_policy import (
    FallbackAuditFailed, FallbackNotPermitted, FallbackPolicy, count_image_parts, fallback_declined_hint,
    fallback_recipient_problem, load_fallback_policy, permitted_fallbacks, provider_fallback_audit,
    provider_fallback_event, startup_replacement, startup_substitution_problem,
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
from omniagent.app.tool_schema import PERSONAL_MEMORY_SCHEMA
from omniagent.memory.channels import companion_db_beside, load_agent_profile
from omniagent.core.conversation import Exchange, make_exchange, to_messages
from omniagent.core.task_ledger import (
    TaskLedger, empty_task_ledger, facts_changed, record_tool_receipt, record_tool_result,
    format_ledger_prompt, record_model_state, inject_task_ledger_into_messages,
)
from omniagent.core.checkpoint import (
    clear_checkpoint, find_resume_checkpoint,
    format_checkpoint_scratchpad, save_checkpoint, summarize_completed_steps,
)
from omniagent.integrations.capabilities import CapabilityService, ToolEntry, discovery_entry, validate_arguments
from omniagent.paths import migrate_legacy_runtime_data, resolve_output_path, state_file, telegram_settings_file
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
# Ekran gözlem görüntüsü içerik önbelleği: aynı ekrana dönüşte (A→B→A) görüntü yeniden
# kodlanıp gönderilmez (her görüntü ~0,8 sn kodlama + token). İçerik adresli olduğundan bayat
# veri dönemez; sınırlı sayıda giriş tutulur (en eski düşer).
OBSERVATION_IMAGE_CACHE: Dict[Tuple[str, bool], Tuple[str, Optional[str], Tuple[int, int]]] = {}
OBSERVATION_IMAGE_CACHE_LIMIT: int = 8
# Sağlayıcı kesintisinde ilerlemiş görevin aynı turu yeniden denemeden önce beklediği serinleme (sn).
AUTO_RESUME_COOLDOWN_SECONDS: float = 5.0



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
    if entry.get("role") == "user" and isinstance(content, str) and content.startswith(AX_SUMMARY_MARKER):
        # Eski AX özeti (indeksli öğe listesi) bayattır ve her tur ~1 bin token biriktirir: görsel gibi bağlamdan çıkarılır.
        return {**entry, "content": AX_SUMMARY_TRIMMED}
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


class PromptRoute(TypedDict):
    """Görev yönlendirme girdileri: araç şeması (route_tool_schemas) ve sistem istemi blokları aynı bayraklardan türer."""
    chrome_session: bool
    can_send_files: bool
    can_schedule: bool


def build_system_prompt(today: date, goal: Optional[str], memory_block: str) -> str:
    """
    Yalnız hedef metnine bakan sistem istemi (host durumu gerektiren zamanlama ve dosya gönderme kapalı);
    Chrome oturumu açıkça istendiyse o yolun rehberi. Saf.
    """
    route: PromptRoute = {
        "chrome_session": active_chrome_session_goal(goal), "can_send_files": False, "can_schedule": False,
    }
    return route_system_prompt(today, goal, memory_block, route)


# Öğe tabanlı AX araçları modele sunulduğunda sistem istemine eklenen rehber (bkz. route_system_prompt). Hedef
# sırası (öğe listesi > görünür metin > nokta) SYSTEM_PROMPT'un ### GUI ilk maddesindedir: burada yalnız öğe
# araçlarının kullanımı var. STALE_ELEMENT/ACTION_INEFFECTIVE yönlendirmesini araç sonuçları (host hata metinleri)
# taşır.
ELEMENT_GUIDANCE: str = (
    "\n### GUI ELEMENTS\n"
    "- Act on the element list with cua_click_element/cua_set_text_element (cua_snapshot fetches it); every "
    "action turn brings a fresh list (use its id). \"etki doğrulanamadı\" = effect unknown: check the next "
    "observation, never re-click blindly.\n"
)


def route_system_prompt(today: date, goal: Optional[str], memory_block: str, route: PromptRoute) -> str:
    """
    Sabit istemin (SYSTEM_PROMPT) ardına sırayla tarih, kullanıcının kayıtlı hafıza bloğu ve yalnız ilgili
    görevde kısa yöntem blokları ekler: entegrasyon, kamera, Chrome oturumu, zamanlama, dosya alışverişi.
    Bloklar hafızadan SONRA ve sabit sırada gelir; gönderilmeyen blok baytları değiştirmez, böylece aynı
    yoldaki görevlerde önek aynı kalır; ev dizini modele verilmez. route: bkz. PromptRoute, chrome_session_route.
    Öğe rehberi (ELEMENT_GUIDANCE ve Chrome maddesi), entegrasyon (discover_capabilities), zamanlama
    (schedule_task) ve dosya (send_file) blokları yalnız ilgili araç bu yolun şemasında varsa eklenir:
    görünmez ölçüm modu (dev/headless_screen.py) araçları route_tool_schemas'tan çıkarınca rehber de çıkar,
    model olmayan araca yönlendirilmez. Saf (route_tool_schemas'ın kendisi saftır).
    """
    weekday: int = today.weekday()
    tool_names: frozenset[str] = frozenset(
        schema["function"]["name"]
        for schema in route_tool_schemas(
            goal, False, route["chrome_session"], route["can_send_files"], route["can_schedule"],
        )
    )
    element_tools: bool = ELEMENT_TOOL_NAMES <= tool_names
    camera_guidance: str = (
        "\n### CAMERA PHOTO\n- Call capture_photo with {} directly. It chooses the real "
        "~/Desktop path, validates the image and returns its filename. Skip camera/ffmpeg/"
        "Photo Booth probes; use Photo Booth only if the tool fails.\n"
        if goal is not None and camera_photo_goal(goal) else ""
    )
    element_first: str = (
        "- In forms cua_set_text_element replaces a field (no Enter); cua_fill_field only for a field "
        "missing from the list.\n"
        if element_tools else ""
    )
    chrome_guidance: str = (
        "\n### USER'S OPEN CHROME SESSION\n"
        "- Use chrome_active_tab and the visible Chrome GUI. Never use a hidden browser (browse_url), "
        "shell, Node or CDP for this goal.\n"
        "- If the user asks for a new tab, call chrome_active_tab with new_tab=true and the target URL; "
        "otherwise it reuses a matching open tab. Open the exact URL the user gave, or the site's own "
        "page for what they named (for example its home feed), and verify the visible page before "
        "reporting its contents.\n"
        + element_first +
        "- Search box: ONE cua_submit_text call. Multi-field form: fill each field with cua_fill_field "
        "(no Enter; Enter inside a form submits it half-filled), open a dropdown with a click and pick "
        "the option with cua_click_text (if no option list appears, type the option text with "
        "cua_type_text and press enter), and submit with the form's own button only after every "
        "required field is set.\n"
        "- A CAPTCHA/human check here is the site's access control: don't click, type or wait it out; "
        "STOP, tell the user (ask_user kind=confirm: they do it).\n"
        "- A lone cua_scroll 'KAYMADI' is not proof that the list ended. Claim you checked ALL items "
        "only with cua_read_scrollable evidence: 'sona ulaşıldı' without a truncation warning.\n"
        "- In a list/detail layout open each item from the list pane. Once you know their labels, "
        "use run_action_sequence with 2-3 click_text/read_scrollable pairs per call: "
        "{action:click_text,text:<label>,near:[x,y]} then "
        "{action:read_scrollable,point:[detail_x,detail_y],max_pages:15}. "
        "The steps run in order and each detail is read after its click. Keep measured values and "
        "inspected labels in STATE: screenshots and long read results leave the context after "
        f"{FULL_DETAIL_TURNS} turns.\n"
        "- After a turn with actions you automatically receive a screenshot taken once the screen "
        "settles. Do not call take_screenshot after actions and never wait.\n"
        if route["chrome_session"] else ""
    )
    return (
        SYSTEM_PROMPT
        + (ELEMENT_GUIDANCE if element_tools else "")
        + f"\n### TODAY\n- Date: {today.isoformat()} ({TURKISH_WEEKDAYS[weekday]} / {ENGLISH_WEEKDAYS[weekday]}).\n"
        + memory_block
        + (INTEGRATIONS_GUIDANCE if "discover_capabilities" in tool_names else "")
        + camera_guidance + chrome_guidance
        + (SCHEDULING_GUIDANCE if "schedule_task" in tool_names else "")
        + (FILE_EXCHANGE_GUIDANCE if "send_file" in tool_names else "")
    )


def resolve_run_limits(options: RunOptions) -> Tuple[str, int, float]:
    """Görev profilini ve geçersiz override'ları güvenli biçimde çözer. Saf fonksiyon."""
    mode: str = options.get("run_mode", "normal")
    if mode not in RUN_MODE_PROFILES:
        raise ValueError(f"Bilinmeyen görev modu: {mode}")
    if options.get("autonomy") is not None:
        if options.get("unattended"):
            raise ValueError("Otonom işler unattended otomatik onay moduyla çalışamaz")
        if mode != "normal":
            raise ValueError("Otonom işler normal görev bitiş sözleşmesini kullanır")
        return mode, 0, float("inf")
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


def _model_plan(
    clients: Dict[str, AsyncOpenAI], backend: str, runtime: Optional[IntegrationRuntime], now: float,
    allowed: frozenset[str],
) -> Tuple[str, ...]:
    """
    Bu tur için deneme sırası. Başka sağlayıcı yalnız `allowed` (kullanıcının yedek izni) içindeyse plana
    girer. Görev boyu engelli (erişim/bakiye) ve süreli serinlemedeki (hız sınırı) profiller atlanır;
    hiçbiri kalmazsa plan olduğu gibi denenir ki gerçek hata yükselsin.
    """
    blocked: frozenset[str] = frozenset(runtime.blocked_backends) if runtime is not None else frozenset()
    cooling: frozenset[str] = (
        cooling_backends(runtime.backend_cooldowns, now) if runtime is not None else frozenset()
    )
    plan: Tuple[str, ...] = attempt_plan(backend, frozenset(clients) - blocked - cooling, allowed)
    usable: Tuple[str, ...] = tuple(name for name in plan if name not in blocked and name not in cooling)
    return usable or plan


async def _sleep_or_stop(seconds: float, should_stop: Callable[[], bool]) -> bool:
    """
    Bekler; should_stop'u 50 ms'de bir denetler. Kullanıcı durdurduysa True döner (istisna yükseltmez:
    çağıran 'stopped' turu döner). Süre runtime.metrics['wait_seconds']'a eklenir.
    """
    runtime: Optional[IntegrationRuntime] = CURRENT_RUNTIME.get()
    started: float = time.monotonic()
    deadline: float = started + seconds
    try:
        while not should_stop():
            remaining: float = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            await asyncio.sleep(min(0.05, remaining))
        return True
    finally:
        if runtime is not None:
            runtime.metrics["wait_seconds"] += time.monotonic() - started


def _stopped_turn() -> ModelTurn:
    """Kullanıcı bekleme sırasında durdurdu: stream_completion'ın durdurma sonucuyla aynı biçim."""
    return {"content": "", "tool_calls": [], "finish_reason": "stopped", "usage": ZERO_USAGE}


def _switch_reason(last_failure: Optional[ModelErrorInfo]) -> str:
    """
    Yedek geçişin gerekçesi: bu çağrıdaki son hatanın kısa etiketi; henüz hata yoksa seçili profil bu
    çağrıdan önce kullanılamaz durumdadır (görev boyu engelli, hız sınırında ya da görev başında hazır
    değildi) ve plan baştan yedekle başlamıştır. Saf.
    """
    if last_failure is None:
        return "seçili sağlayıcı bu görevde kullanılamıyor (engelli, hız sınırında ya da hazır değil)"
    return failure_label(last_failure)


def _announce_provider_switch(
    emit: EventSink, from_backend: str, to_backend: str, reason: str, image_count: int,
) -> None:
    """
    İstek başka bir sağlayıcıya gitmeden ÖNCE çağrılır: denetim kaydına yazar (audit.jsonl), yapısal
    log bırakır ve arayüz/Telegram olayını yayınlar. Denetim kaydı yazılamazsa FallbackAuditFailed
    yükselir: kayıtsız aktarım yapılmaz.
    """
    event: ProviderFallback = provider_fallback_event(from_backend, to_backend, reason, image_count)
    try:
        approval.append_audit(
            data_root() / "audit.jsonl",
            provider_fallback_audit(event, datetime.now(timezone.utc).isoformat()),
        )
    except OSError as error:
        raise FallbackAuditFailed(
            f"Yedek sağlayıcıya geçiş denetim kaydına yazılamadı, istek gönderilmedi: {type(error).__name__}: {error}"
        ) from error
    logging.warning(
        "Model isteği yedek sağlayıcıya yönlendiriliyor",
        extra={"from_backend": event["from_backend"], "to_backend": event["to_backend"],
               "to_model": event["to_model"], "processor": event["processor"],
               "trigger": event["reason"], "image_count": event["image_count"]},
    )
    emit(event)


# Bağlam taştığında sıkıştırma sonrası tutulan son asistan turu sayısı (araç çıktıları dahil).
COMPACT_KEEP_ASSISTANT_TURNS: int = 2


def _compact_for_overflow(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Bağlam penceresi taştığında mesaj listesini sıkıştırır: sistem istemi ve ilk kullanıcı mesajı
    (görev) korunur; eski turlar tek satırlık bir bildirimle düşer, yalnız son
    COMPACT_KEEP_ASSISTANT_TURNS asistan turu tam metin kalır. Sıkıştırılacak eski tur yoksa girdinin
    kendisi döner (çağıran yeniden denemez). Yeni liste döner; girdi değişmez. Saf.
    """
    system: List[Dict[str, Any]] = (
        [messages[0]] if messages and messages[0].get("role") == "system" else []
    )
    rest: List[Dict[str, Any]] = messages[len(system):]
    assistant_positions: List[int] = [
        index for index, message in enumerate(rest) if message.get("role") == "assistant"
    ]
    if len(assistant_positions) <= COMPACT_KEEP_ASSISTANT_TURNS:
        return messages
    cut: int = assistant_positions[-COMPACT_KEEP_ASSISTANT_TURNS]
    dropped_turns: int = len(assistant_positions[:-COMPACT_KEEP_ASSISTANT_TURNS])
    notice: Dict[str, Any] = {
        "role": "user",
        "content": (
            "HOST — BAĞLAM KOMPAKTLAMA: Bağlam penceresi doldu; önceki "
            f"{dropped_turns} araç turu düşürüldü. STATE'ine işlediğin doğrulanmış değerler dışında "
            "eski araç çıktılarını arama; kaldığın yerden sıradaki somut adımla devam et."
        ),
    }
    return system + rest[:1] + [notice] + rest[cut:]


def _escalation_backend(
    current: str, available: frozenset[str], runtime: Optional[IntegrationRuntime], image_count: int,
) -> Optional[str]:
    """
    Art arda tamamen başarısız araç turundan sonra geçilecek daha güçlü model profili (kalite merdiveni).
    Merdiven, next_quality_backend ile basamak basamak yürünür; her adayda kullanıcının yedek izni,
    karantina ve serinleme denetlenir. Uygun basamak yoksa None döner (KVKK: model isteği başka
    sağlayıcıya yalnız açık izinle gider, bkz. fallback_policy). Saf fonksiyon.
    """
    if runtime is None:
        return None
    allowed: frozenset[str] = permitted_fallbacks(
        runtime.fallback_backends, runtime.fallback_images, image_count,
    )
    candidate: Optional[str] = current
    while True:
        candidate = next_quality_backend(candidate, available)
        if candidate is None:
            return None
        if candidate not in allowed:
            continue
        if candidate in runtime.blocked_backends:
            continue
        if runtime.backend_cooldowns.get(candidate, 0.0) > time.monotonic():
            continue
        return candidate


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
    Model çağrısını yapar ve yanıt veren backend'i de döner. Hata classify_model_error ile sınıflanır
    (akış içi APIError gövdesi dahil): kalıcı (geçersiz istek, bağlam sınırı) hemen ModelCallFailed;
    erişim/bakiye (401/402/403, insufficient_quota) alternatif varsa profil görev boyu engellenip
    beklemeden geçilir, yoksa ModelCallFailed; hız sınırı (429) alternatif varsa profil Retry-After
    (yoksa 30 sn) serinlemeye alınıp beklemeden geçilir, yoksa Retry-After'a uyarak beklenir; zaman
    aşımı alternatif varsa beklemeden geçilir; geçici hata (5xx, bağlantı, TLS, akış içi hata) aynı
    profil bir kez daha ya da sıradaki profille denenir. Plan tükenip süre kalmışsa üstel geri
    çekilmeyle yeni tur başlar; toplam süre runtime.model_retry_until ile (agent dışı çağrıda
    INTERACTIVE_MODEL_RETRY_SECONDS ile) sınırlıdır. Bekleme should_stop'a duyarlıdır: durdurma
    finish_reason='stopped' turu döndürür. Yarıda kesilen akış yeniden denenirse önce stream_reset
    yayınlanır (arayüz o turun akmış içeriğini siler, metin iki kez görünmez). Başka sağlayıcı yalnız
    kullanıcının yedek izniyle (runtime.fallback_backends; ekran görüntülü istek için ayrıca
    fallback_images, bkz. omniagent.fallback_policy) plana girer; seçilen profilden başka bir sağlayıcıya
    gidecek istek (kalıcı yedeğe geçilmiş görev dahil) ÖNCEDEN ProviderFallback olayı, denetim kaydı ve
    log üretir; görev başına her (hedef, görüntülü mü) çifti için bir kez. İzin yoksa hata yükselir ve
    iletisi izin verilmediği için atlanan hazır yedekleri söyler.
    """
    runtime: Optional[IntegrationRuntime] = CURRENT_RUNTIME.get()
    give_up_at: float = (
        runtime.model_retry_until
        if runtime is not None and runtime.model_retry_until is not None
        else time.monotonic() + INTERACTIVE_MODEL_RETRY_SECONDS
    )
    image_count: int = count_image_parts(messages)
    # Kullanıcının seçtiği profil (görev başında sabitlenir; doğrudan çağrıda çağrının backend'i). Kalıcı yedeğe
    # geçilmiş görevde `backend` yedektir, ama veri yine seçilen profilden başka bir işleyiciye gider.
    selected: str = runtime.primary_backend if runtime is not None and runtime.primary_backend is not None else backend
    # Yedek yalnız görev başında çözülen izin listesindeki sağlayıcılardır; runtime yoksa (yalnız doğrudan
    # çağrılar) yedek izni de yoktur.
    policy_backends: frozenset[str] = runtime.fallback_backends if runtime is not None else frozenset()
    policy_images: bool = runtime.fallback_images if runtime is not None else False
    allowed: frozenset[str] = permitted_fallbacks(policy_backends, policy_images, image_count)
    if runtime is not None and runtime.primary_backend is not None:
        # Birincil erişim hatasıyla karantinaya alınıp kalıcı yedeğe geçilmiş görevde sonradan gelen ekran
        # görüntüsü de görüntü iznine tabidir.
        recipient_problem: Optional[str] = fallback_recipient_problem(
            backend, runtime.primary_backend, policy_backends, policy_images, image_count,
        )
        if recipient_problem is not None:
            raise FallbackNotPermitted(recipient_problem)

    def fallback_hint(info: ModelErrorInfo) -> str:
        """Yedeğe geçilebilen hatada, izin verilmediği için atlanan hazır yedekleri anlatan cümle (yoksa boş)."""
        if info.kind in UNRETRYABLE_KINDS:
            return ""
        blocked: frozenset[str] = frozenset(runtime.blocked_backends) if runtime is not None else frozenset()
        return fallback_declined_hint(backend, frozenset(clients) - blocked, policy_backends, policy_images, image_count)

    emitted: List[bool] = [False]

    def tracking_emit(event: AgentEvent) -> None:
        emitted[0] = True
        emit(event)

    plan: Tuple[str, ...] = ()
    plan_index: int = 0   # plan içindeki sıra; plan bitince güncel kullanılabilirlikle yeni plan kurulur
    attempts: int = 0     # bu çağrıdaki toplam deneme sayısı
    waits: int = 0        # yapılan bekleme sayısı: üstel geri çekilme adımı
    waited: float = 0.0   # toplam beklenen süre (sn)
    compacted: bool = False  # bağlam taşmasında tek seferlik kompaktlama yapıldı mı
    last_failure: Optional[ModelErrorInfo] = None  # son başarısız denemenin özeti (yedek geçiş gerekçesi)
    failed_backend: str = selected                 # son başarısız denenen profil (yedek geçişin kaynağı)
    # Görev boyunca bildirilen (hedef, görüntülü mü) çiftleri; runtime yoksa yedek de yoktur.
    announced: set[Tuple[str, bool]] = runtime.announced_fallbacks if runtime is not None else set()
    while True:
        if should_stop():
            return _stopped_turn(), backend
        if plan_index >= len(plan):
            plan, plan_index = _model_plan(clients, backend, runtime, time.monotonic(), allowed), 0
        active: str = plan[plan_index]
        if active != selected and (active, image_count > 0) not in announced:
            # Veri seçili sağlayıcıdan başka bir işleyiciye gidiyor: istekten ÖNCE bildir ve kaydet. Kalıcı
            # yedekte de her yeni görüntü seviyesi (metin, ekran görüntüsü) için yeni kayıt düşer.
            _announce_provider_switch(emit, failed_backend, active, _switch_reason(last_failure), image_count)
            announced.add((active, image_count > 0))
        attempts += 1
        try:
            turn: ModelTurn = await _stream_completion(
                clients[active], BACKENDS[active], messages, tool_schemas, session_id, tracking_emit, should_stop,
            )
        # ssl.SSLError (ör. SSLV3_ALERT_BAD_RECORD_MAC) SDK tarafından sarılmadan akış okumasından
        # yükselebiliyor (anyio yeniden fırlatır, httpcore2 eşlemez); akış içi hata olayları ise düz
        # APIError olarak gelir (openai/_streaming.py). İkisi de sınıflanıp yeniden denenir.
        except (APIError, ssl.SSLError) as error:
            info: ModelErrorInfo = classify_model_error(error, datetime.now(timezone.utc))
            decision: RetryDecision = decide_retry(info, plan[-1] != active, waits, session_id)
            logging.warning(
                "Model çağrısı başarısız",
                extra={"attempt": attempts, "backend": active, "error_type": type(error).__name__,
                       "cause_type": info.cause_type, "kind": info.kind, "action": decision.action,
                       "status_code": info.status_code, "error_code": info.code, "in_stream": info.in_stream,
                       "retry_after": info.retry_after},
            )
            last_failure, failed_backend = info, active
            if decision.action == "raise":
                if not compacted and is_context_overflow(info):
                    # Bağlam taşması kurtarılabilir: eski turlar sıkıştırılıp aynı profille bir kez
                    # daha denenir (başka profile geçilmez; istem aynı profilde de sığmalıdır).
                    squeezed: List[Dict[str, Any]] = _compact_for_overflow(messages)
                    if squeezed is not messages:
                        messages = squeezed
                        compacted = True
                        emit({"kind": "notice", "level": "warning", "text": (
                            "Bağlam penceresi doldu; eski araç turları sıkıştırıldı, aynı profille "
                            "yeniden deneniyor."
                        )})
                        continue
                raise model_call_failure(
                    active, BACKENDS[active]["model"], info, attempts, waited, fallback_hint(info),
                ) from error
            next_index: int = (
                next_different_backend(plan, plan_index) if decision.action == "switch" else plan_index + 1
            )
            # Farklı profile geçiş beklemesiz; aynı profil yeniden ya da plan turu sonu beklemelidir.
            needs_wait: bool = next_index >= len(plan) or plan[next_index] == active
            if needs_wait and time.monotonic() + decision.wait_seconds > give_up_at:
                # Sunucunun istediği bekleme bütçeye sığmadı (ör. aylık kota: 7 gün). Beklemek yerine
                # dürüst bir hatayla bitirilir; kullanıcı bunu geçici bir hız sınırı sanmasın.
                raise model_call_failure(
                    active, BACKENDS[active]["model"], info, attempts, waited, fallback_hint(info),
                    budget_exceeded=True,
                ) from error
            if emitted[0]:
                emit({"kind": "stream_reset", "reason": f"{type(error).__name__} ({active}), yeniden deneniyor"})
                emitted[0] = False
            if runtime is not None:
                if decision.block_backend:
                    runtime.blocked_backends.add(active)
                if decision.cooldown_seconds > 0.0:
                    runtime.backend_cooldowns[active] = time.monotonic() + decision.cooldown_seconds
            plan_index = next_index
            if not needs_wait:
                continue
            if decision.wait_seconds >= STATUS_MIN_WAIT_SECONDS:
                emit({"kind": "integration_status", "stage": "model_retry",
                      "text": retry_status_text(active, info, decision.wait_seconds),
                      "completed": waits + 1, "total": 0})
            waits += 1
            if await _sleep_or_stop(decision.wait_seconds, should_stop):
                return _stopped_turn(), backend
            waited += decision.wait_seconds
        else:
            if runtime is not None:
                runtime.backend_cooldowns.pop(active, None)
            return turn, active


# Kanal katmanları (iMessage sohbet katmanı) ajanın model çağrısı sözleşmesini — yeniden deneme, hata
# sınıflandırması, yedek izni — aynen kullanır; yeni istemci yazılmaz.
call_model_with_retries = _call_model_with_retries


async def _screenshot_observation_with_digest(
    call: ToolCallDraft, allow_source_relative: bool,
) -> Tuple[Dict[str, Any], str]:
    """
    Görsel gözlem mesajını ve tekrar tespiti için ucuz içerik digest'ini döner. Dosya, aracın
    yazdığı yerden okunur: göreli ad workspace'e, kaynak görevinde süreç dizinine çözülür.
    Aynı ekrana dönüşte (A→B→A) görüntü yeniden kodlanmaz: içerik adresli küçük önbellekten döner.
    """
    arguments: Dict[str, Any] = json.loads(call["arguments"] or "{}")
    source_path: str = str(resolve_output_path(arguments["filename"], allow_source_relative=allow_source_relative))
    detail: bool = arguments.get("detail", True)
    # Önbellek anahtarı dosya İÇERİĞİNİN özetidir (bayat veri dönemez); detail ayrı tutulur çünkü
    # referans görüntünün üretilip üretilmediğini belirler.
    cache_key: Tuple[str, bool] = (hashlib.sha256(Path(source_path).read_bytes()).hexdigest(), detail)
    cached: Optional[Tuple[str, Optional[str], Tuple[int, int]]] = OBSERVATION_IMAGE_CACHE.get(cache_key)
    if cached is not None:
        coordinate_image, reference_image, (width, height) = cached
    else:
        coordinate_image, reference_image, (width, height) = await asyncio.to_thread(
            encode_screen_observation, source_path, detail,
        )
        if cache_key not in OBSERVATION_IMAGE_CACHE:
            if len(OBSERVATION_IMAGE_CACHE) >= OBSERVATION_IMAGE_CACHE_LIMIT:
                # En eski giriş düşer (sözlük ekleme sırası korur); görüntüler ~0,5 MB olduğundan
                # sınırsız büyüme kabul edilmez.
                OBSERVATION_IMAGE_CACHE.pop(next(iter(OBSERVATION_IMAGE_CACHE)))
            OBSERVATION_IMAGE_CACHE[cache_key] = (coordinate_image, reference_image, (width, height))
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


async def _requested_screenshot_observation(
    call: ToolCallDraft, allow_source_relative: bool, emit: EventSink,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Modelin açıkça çağırdığı take_screenshot dosyasını gözlem mesajı olarak ekler ve digest'ini
    döner. Dosya okunamazsa arayüze uyarı gider, digest None olur ve dönen user mesajı modele
    görüntüyü görmediğini söyler: araç sonucu "modele iletilir" dediği için bu söylenmezse
    model görmediği ekranı tarif eder. Çağıran mesajı araç mesajlarından sonra ekler.
    """
    try:
        return await _screenshot_observation_with_digest(call, allow_source_relative=allow_source_relative)
    except (OSError, KeyError, ValueError) as error:
        detail: str = f"{type(error).__name__}: {error}"
        logging.warning(
            "Ekran görüntüsü modele eklenemedi",
            extra={"error_type": type(error).__name__, "error_detail": str(error), "tool_call_id": call["id"]},
        )
        emit({"kind": "notice", "level": "warning", "text": f"Ekran görüntüsü modele iliştirilemedi ({detail})."})
        return {
            "role": "user",
            "content": (
                f"Ekran görüntüsü modele eklenemedi: {detail}. "
                "Görüntüyü görmedin; ekran içeriği hakkında tahmin yürütme."
            ),
        }, None


async def _observe_after_actions(
    call_id: str, index: int, preview: str, toolbox: Toolbox, cache: Dict[str, ToolResult], emit: EventSink,
    should_stop: Callable[[], bool],
) -> Tuple[Dict[str, Any], sm.StepRecord, Optional[str]]:
    """
    Eylem turunun sonunda take_screenshot'ı model yerine çalıştırır (ekran durulunca) ve
    görüntüyü gözlem mesajı olarak döner; geçici dosya modele eklendikten sonra silinir.
    Başarısız gözlem modele açık metinle bildirilir. preview arayüzdeki çağrı etiketidir.
    """
    # Sıkıştırmasız ara dosya: PNG kodlama/çözme süresi yok, kayıpsız olduğundan modele giden piksel ve digest aynıdır.
    path: Path = Path(tempfile.gettempdir()) / f"omni-{call_id}.bmp"
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
        # Yol mutlak geçici dosyadır: çözümleme kuralı devreye girmez, bayrak sonucu değiştirmez.
        observation, digest = await _screenshot_observation_with_digest(call, allow_source_relative=False)
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
    call: ToolCallDraft, result: ToolResult, cwd: Path, goal: str, allow_source_relative: bool,
) -> Optional[ArtifactReady]:
    """
    Başarılı çıktı aracını var olan yerel dosyanın sohbet kartına çevirir. Hedef ekran
    görüntüsü istemediyse modelin kendi gözlem görüntüleri kart olmaz. Göreli yol aracın
    yazdığı yerden okunur (bkz. resolve_output_path). Dosya varlığını okur.
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
    # send_file göreli yolu süreç dizininden okur; diğer araçlar workspace kuralını izler.
    reads_process_dir: bool = allow_source_relative or name == "send_file"
    resolved: Path = resolve_output_path(raw_path, allow_source_relative=reads_process_dir)
    path: Path = (resolved if resolved.is_absolute() else cwd / resolved).resolve()
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
    """Sonuç/okuma kanıtı: çıplak tıklama, yazım ve ekran dosyası sonucu hedef kanıtı değildir."""
    return {**evidence, **{
        call["id"]: f"{call['name']}: {result_text(result)[:160]}"
        for call, result in zip(calls, results, strict=True)
        if result.get("ok") and call["name"] not in _ACTION_RECEIPT_TOOLS | {"ask_user", "chrome_active_tab"}
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
    defer_confirmation: bool = False,
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
        if pending_commit:
            # Gönderim koruması aktifken hedef kapandı denemez: canlı kayıtta ajan gönderiyi
            # yayınlamadan "başarıyla paylaşıldı" diye bildiriyordu.
            problem = (
                "Son gönder/paylaş eyleminin tamamlandığı henüz doğrulanmadı; ekrandan gönderim "
                "onayını (kapanan ileti kutusu, 'gönderildi' bildirimi) görmeden hedefi bildirme."
            )
        if problem is None and report_count >= MAX_GOAL_REPORTS:
            problem = (
                f"Bu görevde kullanıcıya {report_count} hedef bildirimi soruldu; daha fazlası "
                "sorulmaz. ask_user ile eksik olanı ve yeni yönü bir kez sor; yanıt gelmeden "
                "aynı hedef bildirimini tekrarlama veya başarıyla kapatma."
            )
        if problem is None:
            problem = goal_report_repeat_problem(evidence_ids, seen_evidence)
    # Yalnız gerçekten var olan kanıt id'leri "değerlendirildi" sayılır: modelin uydurduğu bir
    # id, sonradan gerçek bir çağrıya denk gelirse o çağrıyı haksız yere reddettirmemeli.
    evaluated: frozenset[str] = seen_evidence | (frozenset(evidence_ids) if problem is None else frozenset())
    asked: bool = False
    if problem is not None:
        result: ToolResult = {"tool_call_id": call["id"], "ok": False, "error_type": "GoalNotProven",
                              "error": problem, "code": "GOAL_NOT_PROVEN", "recoverable": True}
    else:
        asked = True
        if defer_confirmation:
            result = {"tool_call_id": call["id"], "ok": True,
                      "code": "GOAL_AWAITING_APPROVAL",
                      "result": "Başarılı sonuç/okuma kayıtları bulundu; hedefin gerçekleşmesini kullanıcı doğrular. "
                                "Kullanıcı /approve yazana kadar oturum açık kalacak; "
                                "/btw yeni yönlendirme ekleyebilir."}
            emit({"kind": "notice", "level": "info", "code": AWAITING_APPROVAL_CODE, "text": (
                f"Hedef kanıtı hazır: {redact(summary[:300])}. Onaylamak için /approve, "
                f"yeni yön vermek için /btw <mesaj> yazın; {GOAL_APPROVAL_TIMEOUT_SECONDS / 60:g} dk "
                "içinde yanıt gelmezse oturum onaylanmamış olarak kapanır."
            )})
        else:
            try:
                # Gerçek boolean onay: masaüstünde izin/ret kartı, uzak arayüzde onay düğmesi.
                answer: Dict[str, Any] = await runtime.ask(
                    redact(goal_confirmation_question(summary, evidence_ids, evidence)),
                    {"onay": {"type": "boolean", "label": "Hedef gerçekleşti", "default": False}},
                    None,
                )
            except IntegrationStopped as error:
                asked = False
                result = {"tool_call_id": call["id"], "ok": False, "error_type": "IntegrationStopped",
                          "error": str(error), "code": "STOPPED", "recoverable": False}
            else:
                reply: str = str(answer.get("yanit", "")).strip()
                if approval_granted(answer.get("onay", reply)):
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
        model_retry_seconds: float = (
            unattended_model_retry_seconds(max_wall_clock)
            if run_mode == CONTINUOUS_MODE or bool(options.get("unattended"))
            else interactive_model_retry_seconds(options.get("model_retry_seconds"))
        )
        # Yedek sağlayıcı izni görev başında bir kez çözülür; geçersiz ortam/kayıt açık hatayla durur.
        fallback_policy: FallbackPolicy = load_fallback_policy()
    except (OSError, ValueError) as error:
        return startup_failure(error, DEFAULT_BACKEND)
    continuous: bool = run_mode == CONTINUOUS_MODE
    max_total_tokens: Optional[int] = None if options.get("autonomy") is not None else options.get("max_total_tokens")
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
    # Seçilen profil: açık seçim (arayüz menüsü, /model, --backend, OMNI_BACKEND) ya da 'Otomatik' = varsayılan
    # profil. Yedek izni ve ekran görüntüsü denetimi bu profile göre yapılır.
    selected_backend: str = backend_override if backend_override else DEFAULT_BACKEND
    current_backend: str = selected_backend
    startup_announced: Optional[Tuple[str, bool]] = None  # başlangıç ikamesinin duyurulan (hedef, görüntülü mü) çifti
    if selected_backend not in available:
        # Seçilen profil hazır değil (anahtar yok, Ollama kapalı): Otomatik dahil, başka sağlayıcıya yalnız
        # yedek izniyle geçilir; görsel ek varsa görüntü izni de gerekir. İzin yoksa sessiz ikame yapılmaz.
        allowed_backends: frozenset[str] = frozenset(fallback_policy["backends"])
        attached_images: int = len(options.get("images", []))
        replacement: Optional[str] = startup_replacement(
            selected_backend, available, allowed_backends, fallback_policy["allow_images"], attached_images,
        )
        if replacement is None:
            return startup_failure(FallbackNotPermitted(startup_substitution_problem(
                selected_backend, available, allowed_backends, fallback_policy["allow_images"], attached_images,
            )), DEFAULT_BACKEND)
        try:
            _announce_provider_switch(emit, selected_backend, replacement, "seçili profil hazır değil", attached_images)
        except FallbackAuditFailed as error:
            return startup_failure(error, DEFAULT_BACKEND)
        startup_announced = (replacement, attached_images > 0)
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
            else:
                # Uygun (tek ve hedefle eşleşen, geçerli) kontrol noktası yok: sessizce sıfırdan başlanmaz
                emit({"kind": "notice", "level": "warning",
                      "text": "Devam edilecek uygun kontrol noktası bulunamadı; yalnız sohbet geçmişiyle sürdürülüyor."})
        state: sm.StateDict = sm.load_state(options["state_file"])
        experience_state: experience.ExperienceState = experience.load_experience(experience_file)
        memory_block: str = user_memory.memory_prompt_block(user_memory.load_memory(memory_file))
        # Kanallar arası kanıtlı profil (companion.db) USER MEMORY'nin ardından gelir: kanıttır, talimat değildir.
        personal_db: Path = companion_db_beside(memory_file)
        memory_block += load_agent_profile(personal_db)
        # Hedefle yüksek örtüşen az sayıda geçmiş görev özeti bağlam olarak eklenir (bkz. core/state.relevant_episodes).
        episodic_hint: str = sm.episodic_hint_text(
            sm.relevant_episodes(state, goal, sm.EPISODIC_HINT_LIMIT, sm.EPISODIC_HINT_MIN_SCORE)
        )
        # Yönlendirme bayrakları mesajlar kurulmadan önce hesaplanır: istem blokları ve araç şeması aynı kaynaktan gelir
        can_send_files: bool = options.get("deliver") is not None
        # Planı Telegram köprüsü çalıştırır; zamanlanmış görevin kendisi yeniden plan kuramaz
        can_schedule: bool = (
            not options.get("scheduled_run", False) and scheduling_goal(goal) and telegram_settings_file().is_file()
        )
        prompt_route: PromptRoute = {
            "chrome_session": chrome_session, "can_send_files": can_send_files, "can_schedule": can_schedule,
        }
        first_message: Dict[str, Any] = await asyncio.to_thread(
            user_message_with_images, user_content, options.get("images", []),
        )
        messages: List[Dict[str, Any]] = (
            [{"role": "system", "content": route_system_prompt(date.today(), goal, memory_block, prompt_route)
              + (CONTINUOUS_GUIDANCE if continuous else "")}]
            + to_messages(options["history"])
            + [first_message]
        )
        if episodic_hint:
            # Kullanıcı mesajı olarak ayrı eklenir: hedef metnine karışmaz (kaynak değişikliği/
            # belirteç doğrulaması kapıları hedefi yalnızca kullanıcının kendi metninden okur).
            messages.append({"role": "user", "content": episodic_hint})
        service = options.get("integrations") or CapabilityService()
        runtime = IntegrationRuntime(emit, options["should_stop"], options.get("answer"), options.get("deliver"))
        runtime.autonomy = options.get("autonomy")
        if continuous:
            runtime.unattended = bool(options.get("unattended"))
            # Etkileşimli eski akış kullanıcı yanıtını süresiz bekleyebilir.
            runtime.user_input_timeout = None
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
        # Beklenmeyen başlangıç istisnası kullanıcıya yalnız 'Kritik hata' olarak gider; kök neden için traceback kalır
        logging.exception(
            "Görev başlangıcı beklenmeyen hatayla başarısız",
            extra={"backend": current_backend, "error_type": type(error).__name__},
        )
        if service is not None and "integrations" not in options:
            try:
                await service.close()
            except Exception:
                logging.exception("Başlangıç hatasından sonra entegrasyon kapanışı başarısız")
        return startup_failure(error, current_backend)
    runtime.primary_backend = selected_backend
    runtime.fallback_backends = frozenset(fallback_policy["backends"])
    runtime.fallback_images = fallback_policy["allow_images"]
    if startup_announced is not None:
        # Başlangıçta duyurulan ve kaydedilen ikame ilk model çağrısında yeniden duyurulmaz.
        runtime.announced_fallbacks.add(startup_announced)
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
    # Sürekli modda son yanıt "bitti" beyanı değil ilerleme raporudur; host önce doğrular.
    guarded_final_output: bool = not continuous and (
        must_change_source or must_execute_action or unmet_wait_status(goal, []) is not None
    )
    buffer_model_output: bool = guarded_final_output or continuous
    last_progress_text: str = ""
    # Nihai yanıttaki kod-benzeri belirteçlerin birebir doğrulanacağı kullanıcı kaynakları: hedef
    # (devam görevinde kontrol noktası dahil) ve önceki konuşma. Araç çıktıları steps'ten okunur.
    user_texts: Tuple[str, ...] = (
        user_content,
        *(part for previous in options["history"] for part in (previous["goal"], previous["answer"])),
    )
    answer_tokens_corrected: int = 0
    answer_tokens_unverified: int = 0
    answer_tokens_unobserved: int = 0
    gui_verified: bool = False
    gui_verification_failures: int = 0
    task_ledger: str = ""
    host_task_ledger: TaskLedger = empty_task_ledger()
    combined_ledger: str = ""
    fast_loop_policy = FastLoopPolicy()
    fast_loop_state = FastLoopState()
    fast_loop_delivery_entries: int = 0
    fast_loop_stagnation_events: int = 0
    continuous_stagnation_streak: int = 0
    continuous_last_signature: Optional[str] = None
    continuous_replans: int = 0
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
    # Görevde bir site doğrulama/erişim engeli (BOT_WALL_DETECTED) görüldü mü: görüldüyse host'un "farklı araç/yöntem
    # dene" yönergeleri yerine engeli aşmama yönergeleri gider (bkz. app/continuous.WALL_* ve progress._fast_loop_wall_prompt).
    access_challenge_seen: bool = False
    # Aynı görsel state'te daha önce no-op olduğu kanıtlanan doğrudan GUI girdileri. Ekran
    # değiştiğinde temizlenir; böylece form düzeltildikten sonra aynı submit yeniden denenebilir.
    no_effect_screen_digest: Optional[str] = None
    no_effect_action_keys: frozenset[str] = frozenset()
    goal_reports_seen: frozenset[str] = frozenset()
    goal_report_count: int = 0
    pending_goal_confirmation: Optional[str] = None
    waiting_for_direction: bool = False
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
        """
        Takılan sürekli görev için yeni yol ister ve sayaçları sıfırlar; görevde erişim engeli görüldüyse yeni yol
        yerine engeli aşmama, kullanıcıya bildirme ve ask_user ile yön isteme yönergesi verir.
        """
        nonlocal no_progress_turns, fast_loop_state, idle_reports, recent_blocked_keys
        nonlocal continuous_stagnation_streak
        nonlocal continuous_replans
        continuous_replans += 1
        guidance: str = WALL_REPLAN_GUIDANCE if access_challenge_seen else REPLAN_GUIDANCE
        messages.append({"role": "user", "content": f"HOST — YENİDEN PLANLA: {stall}. {guidance}"})
        no_progress_turns, fast_loop_state, idle_reports = 0, FastLoopState(), 0
        continuous_stagnation_streak = 0
        # Tekrar engeli kalkar: hata geçici olabilir (ağ, açılmayan uygulama) ve host zaten yeni
        # bir deneme istiyor. Aksi hâlde bir kez düşen çağrı görev boyunca bir daha denenemiyordu.
        recent_blocked_keys = {}

    async def resolve_stalled_run(stall: str) -> bool:
        """Sınırlı kurtarma tükendi: model/araç döngüsünü durdur, kullanıcıya gerçek yön sor."""
        nonlocal waiting_for_direction, outcome, reason, continuous_replans
        if continuous_replans < MAX_CONTINUOUS_REPLANS:
            recover_continuous(stall)
            return True
        if runtime.unattended:
            if options.get("pop_control_messages") is not None:
                waiting_for_direction = True
                emit({"kind": "notice", "level": "warning", "code": AWAITING_DIRECTION_CODE,
                      "text": f"{stall}. Denemeler durdu; /btw ile yeni yön veya durdur komutu bekleniyor."})
                return True
            outcome, reason = f"Görev ilerleyemedi: {stall}", "ilerleme yok: kurtarma sınırı"
            return False
        try:
            answer = await runtime.ask(
                f"Görev ilerleyemiyor: {stall}. Aynı işlemleri tekrar denemeyi durdurdum. "
                "Nasıl devam etmemi istersiniz?",
                {"yanit": {"type": "string", "label": "Yeni yönlendirme", "default": ""}},
                GOAL_APPROVAL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            outcome, reason = f"Görev ilerleyemedi: {stall}", "ilerleme yok: yönlendirme zaman aşımı"
            return False
        direction = str(answer.get("yanit", "")).strip()
        if not direction:
            outcome, reason = f"Görev ilerleyemedi: {stall}", "ilerleme yok: yeni yön verilmedi"
            return False
        messages.append({"role": "user", "content": "Kullanıcının yeni yönlendirmesi: " + direction})
        continuous_replans = 0
        recover_continuous(stall)
        return True

    try:
        deletion_target = explicit_deletion_target(goal) if runtime.autonomy is not None else None
        if deletion_target is not None:
            await require_approval("autonomous_goal", approval.deletion_request("autonomous_goal", {"path": str(deletion_target)}))
        for iteration in (count(1) if runtime.autonomy is not None else range(1, max_iterations + 1)):
            if continuous and options.get("pop_control_messages") is not None:
                controls = options["pop_control_messages"]()
                parked_at: float = time.monotonic()
                while (
                    (pending_goal_confirmation is not None or waiting_for_direction)
                    and not controls and not options["should_stop"]()
                    and not (pending_goal_confirmation is not None
                             and time.monotonic() - parked_at >= GOAL_APPROVAL_TIMEOUT_SECONDS)
                    and time.monotonic() - start_time - runtime.metrics["user_wait_seconds"] <= max_wall_clock
                ):
                    await asyncio.sleep(0.25)
                    controls = options["pop_control_messages"]()
                if time.monotonic() - start_time - runtime.metrics["user_wait_seconds"] > max_wall_clock:
                    controls = []
                if (
                    pending_goal_confirmation is not None and not controls and not options["should_stop"]()
                    and time.monotonic() - parked_at >= GOAL_APPROVAL_TIMEOUT_SECONDS
                ):
                    # Kanıtı host doğruladı: onay gelmedi diye oturum host kilidini tutup diğer görevleri engellemez.
                    outcome = pending_goal_confirmation
                    reason, success = "hedef kullanıcı tarafından onaylanmadı", False
                    emit({"kind": "notice", "level": "info", "text": (
                        f"{GOAL_APPROVAL_TIMEOUT_SECONDS / 60:g} dk içinde /approve gelmedi; "
                        "oturum onaylanmamış sonuçla kapandı; sessizlik onay sayılmadı."
                    )})
                    emit({"kind": "text_delta", "text": outcome})
                    break
                for control in controls:
                    if control == "/approve" and pending_goal_confirmation is not None:
                        outcome, reason, success = pending_goal_confirmation, "", True
                        emit({"kind": "text_delta", "text": outcome})
                        break
                    if control.startswith("/btw "):
                        pending_goal_confirmation = None
                        waiting_for_direction = False
                        runtime.deferred_questions.clear()
                        runtime.denied_confirmation_questions.clear()
                        runtime.denied_approval_requests.clear()
                        idle_reports = 0
                        continuous_replans = 0
                        messages.append({"role": "user", "content": (
                            "KULLANICI /btw — Oturum sürerken gelen yeni yönlendirme: "
                            + control[5:4005]
                        )})
                        emit({"kind": "notice", "level": "info", "text": "/btw yönlendirmesi bu tura eklendi."})
                if success:
                    break
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
            if personal_db.is_file():
                # Kanıtlı kişisel hafıza varsa ana ajan personal_memory ile arar ve (onayla) unutur.
                tool_schemas = tool_schemas + [PERSONAL_MEMORY_SCHEMA]
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
                if buffer_model_output and event["kind"] in ("text_delta", "reasoning_delta"):
                    return
                emit(event)

            # Model çağrısı yeniden denemeyi, görevin kalan duvar saatini (kullanıcı bekleme süresi hariç)
            # aşarak sürdürmez: duvar saati denetimi yalnız iterasyon başında yapılır.
            def set_model_deadline() -> None:
                runtime.model_retry_until = model_retry_deadline(
                    time.monotonic(), model_retry_seconds,
                    start_time + runtime.metrics["user_wait_seconds"] + max_wall_clock,
                )
            set_model_deadline()
            model_started: float = time.monotonic()
            try:
                try:
                    turn, used_backend = await _call_model_with_retries(
                        clients, messages_for_model, tool_schemas, session_id, current_backend,
                        emit_model_event, options["should_stop"],
                    )
                except ModelCallFailed:
                    # Özerklik kaldıracı: ilerlemiş görevde (kontrol noktası var) sağlayıcı kesintisi
                    # görevi düşürmesin; kısa serinlemeden sonra taze bütçeyle TEK kez daha denenir.
                    # İlk turda denemek yok: kullanıcı ekran başında hatayı kendisi görür.
                    if turns == 0 or options["should_stop"]():
                        raise
                    emit({"kind": "notice", "level": "warning", "text": (
                        "Model yanıt vermedi; kısa serinlemenin ardından aynı tur bir kez daha deneniyor."
                    )})
                    if await _sleep_or_stop(AUTO_RESUME_COOLDOWN_SECONDS, options["should_stop"]):
                        turn, used_backend = _stopped_turn(), current_backend
                    else:
                        set_model_deadline()
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
            if not buffer_model_output:
                emit(model_finished_event)

            def finish_guarded_turn(visible_text: Optional[str] = None) -> None:
                """Kabul edilen metni model turunu kapatmadan önce göster; her turu bir kez kapat."""
                if not buffer_model_output:
                    return
                if visible_text:
                    emit({"kind": "text_delta", "text": visible_text})
                emit(model_finished_event)
            if used_backend != current_backend:
                permanent_failure = current_backend in runtime.blocked_backends
                emit({"kind": "backend_changed", "backend": used_backend, "model": BACKENDS[used_backend]["model"],
                      "reason": (
                          f"erişim/bakiye hatası; {current_backend} bu görevde yeniden denenmeyecek"
                          if permanent_failure else
                          f"geçici hata veya hız sınırı; yalnız bu tur için yedek sağlayıcı, "
                          f"{current_backend} sonraki turlarda yeniden denenecek"
                      )})
                if permanent_failure:
                    current_backend = used_backend
            if turn["finish_reason"] == "stopped":
                finish_guarded_turn()
                outcome, reason = "Kullanıcı tarafından durduruldu.", "durduruldu"
                break
            if continuous and not turn["tool_calls"]:
                question = text_confirmation_question(turn["content"])
                if question is not None:
                    turn = {**turn, "tool_calls": [{
                        "id": f"host-confirm-{iteration}", "name": "ask_user",
                        "arguments": json.dumps({"question": question, "kind": "confirm"}, ensure_ascii=False),
                    }], "finish_reason": "tool_calls"}
                    model_finished_event["tool_call_count"] = 1
                    model_finished_event["finish_reason"] = "tool_calls"
            messages.append(_assistant_entry(turn))
            if turn["content"]:
                task_ledger = extract_task_ledger(task_ledger, turn["content"])
                host_task_ledger = record_model_state(host_task_ledger, turn["content"])
            visible_progress: Optional[str] = None
            progress_problem: Optional[str] = None
            if continuous and turn["content"]:
                problem = gui_progress_problem(turn["content"], steps)
                if problem is not None:
                    progress_problem = "HOST — SONUÇ DOĞRULAMA: " + problem
                    emit({"kind": "notice", "level": "warning", "text": problem})
                else:
                    visible_progress = turn["content"].strip()
                    if visible_progress == last_progress_text:
                        visible_progress = None
                    else:
                        last_progress_text = visible_progress

            if not turn["tool_calls"]:
                if progress_problem is not None:
                    messages.append({"role": "user", "content": progress_problem})
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
                    outcome = f"Doğrulanmadı: {reason}" if buffer_model_output else turn["content"]
                    finish_guarded_turn(outcome)
                    break
                if awaiting_real_tool_call:
                    reason = "metinsel araç çağrısından sonra gerçek araç çağrısı yapılmadı"
                    outcome = f"Doğrulanmadı: {reason}" if buffer_model_output else turn["content"]
                    finish_guarded_turn(outcome)
                    break
                if continuous:
                    finish_guarded_turn(visible_progress)
                    # Son yanıt ilerleme raporudur: görev sürer, bu dönemin çıktı kartları şimdi gelir.
                    emit_existing_artifacts(artifacts, emit)
                    artifacts = ()
                    idle_reports += 1
                    if idle_reports < MAX_IDLE_REPORTS:
                        messages.append({
                            "role": "user",
                            "content": WALL_CONTINUE_PROMPT if access_challenge_seen else CONTINUE_PROMPT,
                        })
                        continue
                    if runtime.unattended and runtime.deferred_questions:
                        waiting_for_direction = True
                        emit({"kind": "notice", "level": "info", "code": AWAITING_DIRECTION_CODE, "text": (
                            "Bağımsız adımlar tükendi. Oturum açık; /btw <mesaj> ile eksik bilgiyi "
                            "veya yeni yönü iletin."
                        )})
                        continue
                    if not await resolve_stalled_run(f"{idle_reports} ardışık yanıtta hiçbir araç çalışmadı"):
                        break
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
                # Erişim engelinde dürüstçe durma: kanıt kapıları yeniden deneme turu istemez, yanıtı değiştirmez
                wall_stop: bool = success and stopped_at_access_wall(outcome, steps)
                navigation_gap = requested_chrome_navigation_gap(goal, steps) if success and not wall_stop else None
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
                if success and not wall_stop and status_gap is not None:
                    success, reason = False, status_gap
                    if guarded_final_output:
                        outcome = f"Doğrulanmadı: {reason}"
                if success and not wall_stop and must_change_source and not any(
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
                if success and not wall_stop and must_execute_action:
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
                if success:
                    # Kod/ID karakter hatası kapısı (kabul zincirinin son halkası; ek model turu yok): yanıttaki
                    # kod-benzeri belirteç hedefte veya araç çıktısında birebir yoksa ama gözlenen TEK bir
                    # belirteçle yalnız karışabilir karakterlerle (O-0, I/l-1, S-5, B-8, Z-2, Kiril-Latin)
                    # ayrışıyorsa gözlenen değer yazılır. Hesaplanmış veya görselden okunmuş değerin gözlenen
                    # adayı yoktur; dokunulmadan geçer (yalnız ölçülür). Birden çok aday varsa yalnız uyarılır.
                    fidelity: FidelityReport = check_answer_fidelity(
                        outcome, code_token_set(user_texts) | code_token_set(observed_step_texts(steps)),
                    )
                    answer_tokens_unobserved += len(fidelity.unobserved)
                    if fidelity.corrections:
                        outcome = apply_token_corrections(outcome, fidelity.corrections)
                        answer_tokens_corrected += len(fidelity.corrections)
                        emit({"kind": "notice", "level": "warning", "text": correction_notice(fidelity.corrections)})
                        logging.warning(
                            "Yanıttaki kod-benzeri belirteçler araç çıktısına göre düzeltildi",
                            extra={"corrected_tokens": len(fidelity.corrections)},
                        )
                    if fidelity.ambiguous:
                        answer_tokens_unverified += len(fidelity.ambiguous)
                        emit({"kind": "notice", "level": "warning", "text": unverified_notice(fidelity.ambiguous)})
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

            finish_guarded_turn(visible_progress if continuous else None)
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
                        # Erişim engeli hatası tekrar engeline girmez: aynı çağrı yeniden çalışınca araç aynı engel
                        # iletisini (ağa çıkmadan) verir; "farklı bir adım dene" iletisi engeli aşmaya iterdi.
                        if not blocked and not result.get("ok") and result.get("code") != ACCESS_CHALLENGE_CODE
                    }
                if any(result.get("code") == ACCESS_CHALLENGE_CODE for result in regular_results):
                    access_challenge_seen = True
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
                    elif hosted and pending_goal_confirmation is not None:
                        hosted_results.append({"tool_call_id": call["id"], "ok": False,
                                               "code": "GOAL_ALREADY_PENDING", "recoverable": True,
                                               "error": "Kanıtlı hedef bildirimi zaten kullanıcı onayını bekliyor."})
                    elif hosted:
                        hosted_result, confirmed, goal_reports_seen, asked = await resolve_goal_report(
                            call, index, goal_evidence, runtime, emit,
                            goal_reports_seen, goal_report_count, turn_commit_pending,
                            defer_confirmation=runtime.unattended,
                        )
                        # Tavan yalnız kullanıcıya gerçekten sorulan bildirimleri sayar: geçersiz
                        # kanıtla yapılan denemeler sonraki meşru bildirimi engellememeli.
                        goal_report_count += int(asked)
                        hosted_results.append(hosted_result)
                        confirmed_goal = confirmed
                        if hosted_result.get("code") == "GOAL_AWAITING_APPROVAL":
                            arguments = json.loads(call["arguments"] or "{}")
                            pending_goal_confirmation = str(arguments["summary"]).strip()
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
                artifact: Optional[ArtifactReady] = artifact_event_for_call(
                    call, result, file_cwd, goal, allow_source_relative=must_change_source,
                )
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
                    ACCESS_CHALLENGE_CODE, *approval.APPROVAL_REFUSAL_CODES,
                ):
                    # Host'un yürütmediği çağrı gerçek bir araç hatası değildir; deneyim belleğine
                    # yazılırsa aynı imza için sahte "doğrulanmış çözüm" dersleri birikir. Bot doğrulaması /
                    # erişim engeli de burada: yazılırsa "aynı adreste başka araç dene" dersi ve tekrar
                    # uyarısı, engeli araç değiştirerek aşmaya yönlendirirdi. Onay kapısının retleri
                    # (ret/zaman aşımı/kanal yok/onay sonrası hedef değişimi) de host politikasıdır:
                    # "reddedilince başka yoldan tıkla" dersi asla öğrenilmemeli.
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

            if progress_problem is not None:
                messages.append({"role": "user", "content": progress_problem})
            if confirmed_goal is not None:
                # Sürekli görevin tek başarı yolu: kanıtlı hedefi kullanıcı onayladı.
                outcome, reason, success = confirmed_goal, "", True
                emit({"kind": "text_delta", "text": confirmed_goal})
                break

            # Ekran gözlemleri TÜM araç mesajlarından SONRA eklenir: tool sonuçları assistant
            # tool_calls'ı kesintisiz izlemeli; araya user mesajı 400'e yol açar. Eklenemeyen
            # görüntünün "eklenemedi" mesajı da aynı kurala tabidir.
            turn_acted: bool = any(
                call["name"] in _GUI_VERIFICATION_TOOLS
                and (result.get("ok") or int(result.get("completed_steps") or 0) > 0)
                for call, result in zip(turn["tool_calls"], results, strict=True)
            )
            for shot_call in pending_shots:
                observation, digest = await _requested_screenshot_observation(
                    shot_call, allow_source_relative=must_change_source, emit=emit,
                )
                if digest is None:
                    # Görüntü modele eklenemedi: hata mesajı, görüntüler gibi çağrı sırasıyla gider.
                    # Sayaçlara ve son gözlem digest'ine dokunulmaz; digest'siz tur başka ilerleme
                    # yoksa ilerleme sayılmaz, bu yüzden tekrar eden başarısızlığı hızlı döngü durdurur.
                    messages.append(observation)
                    continue
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
                # Taze AX özeti (indeksli etkileşimli öğeler): yalnız AX yolu etkinse gelir (model bu görevde anlık
                # görüntü aldı ya da görsel kapsam bir uygulama); görünmez modda None. Ayrı mesajdır ki eski özetler
                # _trim_entry ile düşürülebilsin.
                ax_started: float = time.monotonic()
                try:
                    ax_summary: Optional[str] = await asyncio.to_thread(toolbox.observation_ax_summary)
                except Exception as error:  # Gözlem sınırı: isteğe bağlı AX özeti ajan döngüsünü çökertmesin
                    logging.warning("AX özeti alınamadı", extra={"error_type": type(error).__name__})
                    ax_summary = f"AX özeti alınamadı ({type(error).__name__}): {error}"
                finally:
                    tool_seconds += time.monotonic() - ax_started
                if ax_summary is not None:
                    messages.append({"role": "user", "content": ax_summary})

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
            ledger_changed: bool = facts_changed(facts_before_turn, host_task_ledger["facts"])
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
                signature, continuous_last_signature if continuous else fast_loop_state.last_signature,
            ) or shell_output_progress or read_output_progress
            semantic_progress: bool = classify_semantic_progress(
                ledger_changed=ledger_changed,
                deterministic_progress=deterministic_progress,
                has_ledger=bool(task_ledger),
                previous_signature=continuous_last_signature if continuous else fast_loop_state.last_signature,
                signature=signature,
                all_failed=all_failed,
            )
            if not semantic_progress:
                fast_loop_stagnation_events += 1
                continuous_stagnation_streak += 1
            else:
                continuous_stagnation_streak = 0
                continuous_replans = 0
            if continuous:
                continuous_last_signature = signature
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
                    messages.append({"role": "user", "content": (
                        _fast_loop_wall_prompt(combined_ledger) if access_challenge_seen
                        else _fast_loop_prompt("replan", combined_ledger)
                    )})
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
            elif continuous_stagnation_streak >= CONTINUOUS_STAGNATION_LIMIT:
                # Sürekli modda fast-loop faz makinesi kapalı olduğu için "başarılı ama etkisiz"
                # döngüler (aynı ekrana dönüş, imza değişmeyen turlar) burada yakalanır.
                stall: str = (
                    f"{CONTINUOUS_STAGNATION_LIMIT} ardışık ilerlemesiz tur: araçlar çalışıyor ama "
                    "gözlemlenebilir ilerleme üretmiyor"
                )
                emit({"kind": "notice", "level": "warning", "text": stall})
                if not await resolve_stalled_run(stall):
                    break

            # Her tur sonunda oturum kontrol noktası atomik olarak saklanır. fsync'li disk yazımı
            # olay döngüsünü bloke etmesin diye iş parçacığına alınır; kayıt her turda sürer çünkü
            # 'devam et' güvencesi tur başına taze durum ister.
            try:
                await asyncio.to_thread(
                    save_checkpoint,
                    session_id=session_id,
                    goal=goal,
                    facts={key: fact["value"] for key, fact in host_task_ledger["facts"].items()},
                    completed_steps=summarize_completed_steps(steps),
                    turn_count=iteration,
                    model_state=task_ledger,
                )
            except Exception as cp_err:
                # Checkpoint hatası görevi öldürmesin diye bilinçli geniş yakalama; kayıt yapısal alanlarla ve
                # traceback ile loglanır.
                logging.warning(
                    "Checkpoint kaydedilemedi", exc_info=True,
                    extra={"session_id": session_id, "turn": iteration, "error_type": type(cp_err).__name__},
                )

            # Araç/provider hatası tek başına model kalitesi sinyali değildir: tek çağrı hatası
            # modelin suçu olmayabilir. Ancak ART ARDA tamamen başarısız tur seçili modelin görevi
            # çözemediğinin sinyalidir; sürekli modda yedek izni verilmiş daha güçlü bir profile
            # geçilir (bkz. _escalation_backend), izin yoksa mevcut yolla yeniden plan istenir.
            no_progress_turns = no_progress_turns + 1 if all_failed else 0
            if no_progress_turns >= NO_PROGRESS_LIMIT:
                stall: str = f"ilerleme yok: {NO_PROGRESS_LIMIT} ardışık tamamen başarısız araç turu"
                emit({"kind": "notice", "level": "warning", "text": stall})
                if not continuous:
                    reason = stall
                    break
                escalated: Optional[str] = _escalation_backend(
                    current_backend, available, runtime, count_image_parts(messages),
                )
                if escalated is not None:
                    emit({"kind": "notice", "level": "info", "text": (
                        f"{stall}; daha güçlü modele geçiliyor: {escalated} "
                        f"({BACKENDS[escalated]['model']})."
                    )})
                    current_backend = escalated
                    if not await resolve_stalled_run(stall):
                        break
                    continue
                if runtime.unattended and runtime.deferred_questions:
                    waiting_for_direction = True
                    emit({"kind": "notice", "level": "info", "code": AWAITING_DIRECTION_CODE, "text": (
                        "Bağımsız adımlar tükendi. Oturum açık; /btw <mesaj> ile yön verin."
                    )})
                    continue
                if not await resolve_stalled_run(stall):
                    break
        else:
            success = False
            reason = f"maksimum iterasyon sayısına ({max_iterations}) ulaşıldı"
    except (ToolError, IntegrationStopped) as error:
        outcome, reason = str(error), "durduruldu" if isinstance(error, IntegrationStopped) else str(error)
        emit({"kind": "notice", "level": "info", "text": outcome})
    except ModelCallFailed as error:
        # Sağlayıcı kesintisi/kota gibi dış nedenle model yanıt vermedi: gerçek kod hatasından ayrı raporlanır.
        logging.exception(
            "Model çağrısı başarısız",
            extra={"session_id": session_id, "turn": turns + 1, "backend": error.backend, "kind": error.kind,
                   "attempts": error.attempts, "waited_seconds": round(error.waited_seconds, 1)},
        )
        resume_hint: str = (
            " Bağlantı düzelince «devam et» yazarak kaldığınız yerden sürdürmeyi deneyebilirsiniz."
            if turns > 0 else ""
        )
        outcome = f"Model çağrısı başarısız: {error}{resume_hint}"
        reason = f"model çağrısı başarısız ({MODEL_ERROR_KIND_LABELS[error.kind]})"
        emit({"kind": "notice", "level": "error", "text": outcome})
    except FallbackNotPermitted as error:
        # İstek, yedek izni olmayan sağlayıcıya gönderilmedi: beklenen politika reddi, kod hatası değil.
        logging.warning(
            "Model isteği yedek sağlayıcı izni olmadığı için gönderilmedi",
            extra={"session_id": session_id, "turn": turns + 1, "backend": current_backend, "detail": str(error)},
        )
        outcome = f"Model isteği gönderilmedi: {error}"
        reason = "yedek sağlayıcı izni yok"
        emit({"kind": "notice", "level": "error", "text": outcome})
    except Exception as error:
        logging.exception(
            "Görev beklenmeyen hatayla sonlandı",
            extra={"session_id": session_id, "turn": turns + 1, "backend": current_backend,
                   "error_type": type(error).__name__},
        )
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
                if buffer_model_output else combined_ledger or task_ledger
            )
            history_answer = "\n".join(part for part in (
                f"[Görev tamamlanamadı: {reason}]" if reason else "[Görev tamamlanamadı]",
                history_ledger, outcome,
            ) if part)
        # Sohbet geçmişi özeti adımları saklamadaki kırpık biçimiyle görür: tam komut/argüman metni
        # (sır içerebilir) Exchange.tools'a ve oradan geçmiş dosyasına yeni girmez.
        exchange: Exchange = make_exchange(goal, history_answer, [sm.clip_step_for_storage(step) for step in steps])
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
            "answer_tokens_corrected": answer_tokens_corrected,
            "answer_tokens_unverified": answer_tokens_unverified,
            "answer_tokens_unobserved": answer_tokens_unobserved,
        }
        cleanup_errors: List[str] = []
        partial_report: str = ""
        if not success and not outcome.strip():
            # Sınır veya ilerleme yokluğuyla biten görev boş çıktı bırakmaz (kullanıcı hiçbir şey görmezdi):
            # yalnız host defterinden, araç metninde birebir görülen kodlardan ve araç hatasının bildirdiği
            # erişim engelinden derlenen, açıkça "tamamlanamadı" (engelse engel ve adres) etiketli rapor
            # verilir; modelin serbest metni girmez. Sohbet geçmişi yukarıda boş çıktıyla kuruldu ve değişmez.
            # Temizlik (tarayıcı, entegrasyon, bellek kaydı) rapor hatasından etkilenmesin diye hata
            # temizlik uyarısı olarak bildirilir; rapor kurulamazsa çıktı önceki gibi boş kalır.
            try:
                partial_report = format_partial_report(
                    reason,
                    report_facts(host_task_ledger),
                    verified_note_codes(
                        host_task_ledger["model_state"], code_token_set(observed_step_texts(steps)),
                    ),
                    access_challenge_notes(steps),
                )
                outcome = partial_report
            except Exception as error:
                logging.exception("Kısmi rapor derlenemedi", extra={"reason": reason, "steps": len(steps)})
                cleanup_errors.append(f"Kısmi rapor: {type(error).__name__}: {error}")
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
        if partial_report:
            # Masaüstü arayüzü run_finished'te çıktıyı göstermez; rapor ayrıca uyarı olarak yayınlanır.
            emit({"kind": "notice", "level": "warning", "text": partial_report})
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
    elif event["kind"] == "provider_fallback":
        print(f"  ⚠ {provider_fallback_text(event)}")
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
