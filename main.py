import asyncio
import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict

from openai import AsyncOpenAI

from config import BACKENDS, DEFAULT_BACKEND, ESCALATION_BACKEND, BackendProfile, config
from tools import Toolbox, ToolError
import state_manager as sm

STATE_FILE: str = str(Path(__file__).resolve().parent / "cognitive_memory.json")
MAX_ITERATIONS: int = 25
MAX_API_RETRIES: int = 3
MAX_WALL_CLOCK_SECONDS: float = 600.0
MAX_TOOL_MESSAGE_AGE: int = 6
TRIMMED_CONTENT_LIMIT: int = 400
CONSECUTIVE_FAILURE_ESCALATION_THRESHOLD: int = 2
MAX_RESPONSE_TOKENS: int = 2048


class ToolResult(TypedDict, total=False):
    tool_call_id: str
    ok: bool
    result: str
    error_type: str
    error: str
    code: str
    recoverable: bool


def encode_image(path: str) -> str:
    """Görüntü dosyasını base64 metnine çevirir."""
    with open(path, "rb") as source:
        return base64.b64encode(source.read()).decode("utf-8")


def build_tool_schemas() -> List[Dict[str, Any]]:
    """Toolbox araçlarının OpenAI function-calling şemasını döner."""
    return [
        {"type": "function", "function": {
            "name": "execute_shell",
            "description": "Sistem kabuğunda komut çalıştırır.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string", "description": "Çalıştırılacak kabuk komutu."},
                "use_sudo": {"type": "boolean", "description": "Komut sudo ile mi çalıştırılsın."},
            }, "required": ["command", "use_sudo"]},
        }},
        {"type": "function", "function": {
            "name": "process_list",
            "description": "Sistemdeki aktif süreçlerin listesini döner.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }},
        {"type": "function", "function": {
            "name": "get_pointer_position",
            "description": "Farenin mevcut ekran koordinatlarını döner.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }},
        {"type": "function", "function": {
            "name": "session_authority_status",
            "description": "Mevcut oturumun sudo yetki durumunu kontrol eder.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }},
        {"type": "function", "function": {
            "name": "session_authority_end",
            "description": "Yetki döngüsünü sonlandırır.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }},
        {"type": "function", "function": {
            "name": "deep_system_probe",
            "description": "Sistem internals taraması yapar.",
            "parameters": {"type": "object", "properties": {
                "target": {"type": "string", "enum": ["process", "network"], "description": "Taranacak hedef."},
            }, "required": ["target"]},
        }},
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Bir dosyanın içeriğini okur.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Okunacak dosyanın yolu."},
            }, "required": ["path"]},
        }},
        {"type": "function", "function": {
            "name": "write_file",
            "description": "Bir dosyaya tam içerik yazar (atomik, doğrulamalı, yedekli).",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Yazılacak dosyanın yolu."},
                "content": {"type": "string", "description": "Dosyaya yazılacak tam içerik."},
            }, "required": ["path", "content"]},
        }},
        {"type": "function", "function": {
            "name": "web_search",
            "description": "DuckDuckGo üzerinden web araması yapar.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Arama sorgusu."},
            }, "required": ["query"]},
        }},
        {"type": "function", "function": {
            "name": "browse_url",
            "description": "Bir URL'ye gider ve okuma/tıklama/yazma etkileşimi kurar.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "Gidilecek URL."},
                "action": {"type": "string", "enum": ["read", "click", "type"], "description": "Yapılacak işlem."},
                "selector": {"type": "string", "description": "CSS seçici (click/type için gerekli)."},
                "text": {"type": "string", "description": "Yazılacak metin (type için gerekli)."},
            }, "required": ["url", "action"]},
        }},
        {"type": "function", "function": {
            "name": "fetch_raw",
            "description": "curl ile hızlı HTTP çekimi yapar ve metni temizler.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "Çekilecek URL."},
            }, "required": ["url"]},
        }},
        {"type": "function", "function": {
            "name": "find_and_click",
            "description": "Ekranda bir görsel şablonu arar ve bulursa tıklar.",
            "parameters": {"type": "object", "properties": {
                "template_path": {"type": "string", "description": "Aranacak şablon görselinin yolu."},
                "confidence": {"type": "number", "description": "0-1 arası eşleşme güven eşiği."},
                "window_title": {"type": "string", "description": "Aramayı bu pencereyle sınırla (opsiyonel)."},
            }, "required": ["template_path", "confidence"]},
        }},
        {"type": "function", "function": {
            "name": "take_screenshot",
            "description": "Ekran görüntüsü alır ve dosyaya kaydeder.",
            "parameters": {"type": "object", "properties": {
                "filename": {"type": "string", "description": "Kaydedilecek dosya yolu."},
            }, "required": ["filename"]},
        }},
        {"type": "function", "function": {
            "name": "cua_get_app",
            "description": "Uygulamayı aktif hale getirir ve odaklanır.",
            "parameters": {"type": "object", "properties": {
                "app_name": {"type": "string", "description": "Uygulama adı."},
            }, "required": ["app_name"]},
        }},
        {"type": "function", "function": {
            "name": "cua_click",
            "description": "Uygulama içindeki bir erişilebilirlik (AX) elementine tıklar.",
            "parameters": {"type": "object", "properties": {
                "app_name": {"type": "string", "description": "Uygulama adı."},
                "element_id": {"type": "integer", "description": "AX öğe numarası."},
            }, "required": ["app_name", "element_id"]},
        }},
        {"type": "function", "function": {
            "name": "cua_press_key",
            "description": "Uygulama içinde bir tuşa basar.",
            "parameters": {"type": "object", "properties": {
                "app_name": {"type": "string", "description": "Uygulama adı."},
                "key": {"type": "string", "description": "Basılacak tuş."},
            }, "required": ["app_name", "key"]},
        }},
        {"type": "function", "function": {
            "name": "cua_get_ax_state",
            "description": "Uygulama penceresinin erişilebilirlik durumunu getirir.",
            "parameters": {"type": "object", "properties": {
                "app_name": {"type": "string", "description": "Uygulama adı."},
            }, "required": ["app_name"]},
        }},
        {"type": "function", "function": {
            "name": "mouse_click",
            "description": "Belirtilen koordinata fare tıklaması yapar.",
            "parameters": {"type": "object", "properties": {
                "x": {"type": "integer", "description": "X koordinatı."},
                "y": {"type": "integer", "description": "Y koordinatı."},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Tıklama tuşu."},
            }, "required": ["x", "y", "button"]},
        }},
        {"type": "function", "function": {
            "name": "mouse_move",
            "description": "Fareyi belirtilen koordinata taşır.",
            "parameters": {"type": "object", "properties": {
                "x": {"type": "integer", "description": "X koordinatı."},
                "y": {"type": "integer", "description": "Y koordinatı."},
            }, "required": ["x", "y"]},
        }},
        {"type": "function", "function": {
            "name": "keyboard_type",
            "description": "Aktif odakta metin yazar.",
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string", "description": "Yazılacak metin."},
            }, "required": ["text"]},
        }},
        {"type": "function", "function": {
            "name": "keyboard_press",
            "description": "Aktif odakta bir tuşa basar.",
            "parameters": {"type": "object", "properties": {
                "key": {"type": "string", "description": "Basılacak tuş."},
            }, "required": ["key"]},
        }},
        {"type": "function", "function": {
            "name": "execute_js",
            "description": "Node.js ile bir JavaScript dosyasını çalıştırır.",
            "parameters": {"type": "object", "properties": {
                "code": {"type": "string", "description": "Çalıştırılacak JavaScript kodu."},
            }, "required": ["code"]},
        }},
        {"type": "function", "function": {
            "name": "self_modify",
            "description": "Kendi kaynak kodunu okuyup doğrulayarak değiştirir.",
            "parameters": {"type": "object", "properties": {
                "file_path": {"type": "string", "description": "Değiştirilecek dosyanın yolu."},
                "new_content": {"type": "string", "description": "Dosyanın yeni tam içeriği."},
            }, "required": ["file_path", "new_content"]},
        }},
        {"type": "function", "function": {
            "name": "smart_click",
            "description": "Hibrit tıklama: önce erişilebilirlik (AX), sonra görsel şablon dener.",
            "parameters": {"type": "object", "properties": {
                "app_name": {"type": "string", "description": "Uygulama adı."},
                "element_id": {"type": "integer", "description": "AX öğe numarası (opsiyonel)."},
                "template_path": {"type": "string", "description": "Görsel şablon yolu (opsiyonel)."},
                "confidence": {"type": "number", "description": "Görsel eşleşme güven eşiği."},
            }, "required": ["app_name"]},
        }},
        {"type": "function", "function": {
            "name": "get_window_bounds",
            "description": "Belirtilen pencerenin ekran sınırlarını (x, y, w, h) döner.",
            "parameters": {"type": "object", "properties": {
                "window_title": {"type": "string", "description": "Pencere başlığı."},
            }, "required": ["window_title"]},
        }},
        {"type": "function", "function": {
            "name": "run_action_sequence",
            "description": (
                "Bir dizi fare/klavye eylemini (click/move/type/press) TEK çağrıda sırayla "
                "çalıştırır. 'Alana tıkla, metni yaz, enter'a bas' gibi zincirler için her adımı "
                "ayrı bir araç çağrısı yapmak yerine bunu kullan — daha az model turu, daha hızlı."
            ),
            "parameters": {"type": "object", "properties": {
                "steps": {
                    "type": "array",
                    "description": "Sırayla çalıştırılacak eylemler.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["click", "move", "type", "press"]},
                            "x": {"type": "integer", "description": "click/move için X koordinatı."},
                            "y": {"type": "integer", "description": "click/move için Y koordinatı."},
                            "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "click için tuş (belirtilmezse left)."},
                            "text": {"type": "string", "description": "type için yazılacak metin."},
                            "key": {"type": "string", "description": "press için tuş adı."},
                        },
                        "required": ["action"],
                    },
                },
            }, "required": ["steps"]},
        }},
    ]


async def execute_tool(call: Any, toolbox: Toolbox) -> ToolResult:
    """Tek bir araç çağrısını çalıştırır; başarı/hata durumunu yapılandırılmış şekilde döner."""
    name: str = call.function.name
    try:
        arguments: Dict[str, Any] = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError as error:
        return {
            "tool_call_id": call.id, "ok": False,
            "error_type": "JSONDecodeError", "error": f"Araç argümanları çözümlenemedi: {error}",
        }

    method: Optional[Callable[..., Any]] = getattr(toolbox, name, None)
    if method is None or not callable(method):
        return {
            "tool_call_id": call.id, "ok": False,
            "error_type": "UnknownTool", "error": f"Bilinmeyen araç: {name}",
        }

    try:
        if asyncio.iscoroutinefunction(method):
            result: Any = await method(**arguments)
        else:
            result = await asyncio.to_thread(method, **arguments)
        return {"tool_call_id": call.id, "ok": True, "result": str(result)}
    except ToolError as error:
        return {
            "tool_call_id": call.id, "ok": False,
            "error_type": "ToolError", "error": str(error),
            "code": error.code, "recoverable": error.recoverable,
        }
    except TypeError as error:
        return {
            "tool_call_id": call.id, "ok": False,
            "error_type": "TypeError", "error": f"Geçersiz argümanlar ({name}): {error}",
        }
    except Exception as error:  # Araç sınırı: rastgele üçüncü taraf hatalarını döngüyü çökertmeden yakala.
        return {
            "tool_call_id": call.id, "ok": False,
            "error_type": type(error).__name__, "error": str(error),
        }


# Fiziksel fare/klavye eylemleri paralelleştirilemez: aynı anda iki tıklama/yazma
# yarış durumu yaratıp yanlış elemente etki edebilir. Bunlar model bir turda birden
# fazlasını döndürürse orijinal sırayla SERİ çalışır; geri kalan (salt okunur/bağımsız)
# çağrılar gerçek paralellikle çalışır.
_SEQUENTIAL_TOOLS: frozenset[str] = frozenset({
    "mouse_click", "mouse_move", "keyboard_type", "keyboard_press",
    "cua_click", "cua_press_key", "cua_get_app", "find_and_click",
    "smart_click", "run_action_sequence",
})


async def _execute_tool_calls(calls: List[Any], toolbox: Toolbox) -> List[ToolResult]:
    """Bir turdaki tüm araç çağrılarını, fiziksel eylemleri seri tutarak çalıştırır."""
    concurrent_indices: List[int] = [i for i, c in enumerate(calls) if c.function.name not in _SEQUENTIAL_TOOLS]
    sequential_indices: List[int] = [i for i, c in enumerate(calls) if c.function.name in _SEQUENTIAL_TOOLS]

    async def run_sequential() -> List[Tuple[int, ToolResult]]:
        outcomes: List[Tuple[int, ToolResult]] = []
        for i in sequential_indices:
            outcomes.append((i, await execute_tool(calls[i], toolbox)))
        return outcomes

    async def run_concurrent() -> List[Tuple[int, ToolResult]]:
        if not concurrent_indices:
            return []
        outcomes = await asyncio.gather(*(execute_tool(calls[i], toolbox) for i in concurrent_indices))
        return list(zip(concurrent_indices, outcomes))

    sequential_results, concurrent_results = await asyncio.gather(run_sequential(), run_concurrent())
    by_index: Dict[int, ToolResult] = dict(sequential_results + concurrent_results)
    return [by_index[i] for i in range(len(calls))]


def _tool_result_to_message(call: Any, result: ToolResult, hint: Optional[str]) -> Dict[str, Any]:
    """Araç sonucunu modele geri gönderilecek 'tool' mesajına çevirir."""
    if result.get("ok"):
        content: str = result.get("result", "")
    else:
        content = f"HATA [{result.get('error_type')}]: {result.get('error')}"
        if hint:
            content += f"\nBİLİNEN ÇÖZÜM (benzer geçmiş hatadan): {hint}"
    return {"role": "tool", "tool_call_id": call.id, "content": content}


def _trim_old_tool_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Son MAX_TOOL_MESSAGE_AGE mesajın dışında kalan uzun araç sonuçlarını kısaltır.
    Saf fonksiyon: girdiyi değiştirmez, yeni bir liste döner. Bağlamın turlar
    ilerledikçe sınırsız büyümesini (ve her turu giderek yavaşlatmasını) önler.
    """
    cutoff: int = len(messages) - MAX_TOOL_MESSAGE_AGE
    trimmed: List[Dict[str, Any]] = []
    for index, entry in enumerate(messages):
        content: Any = entry.get("content")
        if (
            index < cutoff
            and entry.get("role") == "tool"
            and isinstance(content, str)
            and len(content) > TRIMMED_CONTENT_LIMIT
        ):
            trimmed.append({**entry, "content": content[:TRIMMED_CONTENT_LIMIT] + " …[eski çıktı kısaltıldı]"})
        else:
            trimmed.append(entry)
    return trimmed


async def _call_model_with_retries(
    clients: Dict[str, AsyncOpenAI],
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]],
    session_id: str,
    backend: str,
) -> Tuple[Any, str]:
    """
    Model çağrısını yeniden deneme politikasıyla yapar. İlk deneme verilen backend'de,
    kalan denemeler (ESCALATION_BACKEND kullanılabilirse ve zaten o değilse) daha güçlü
    ESCALATION_BACKEND'de yapılır — hız için ucuz/varsayılan model önce denenir, ısrarlı
    hata/aksama durumunda otomatik olarak daha güçlü modele geçilir. Hangi backend'in
    gerçekten yanıt verdiğini de döner ki çağıran taraf mevcut backend'i güncelleyebilsin.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_API_RETRIES + 1):
        escalate: bool = attempt > 1 and backend != ESCALATION_BACKEND and ESCALATION_BACKEND in clients
        active_backend: str = ESCALATION_BACKEND if escalate else backend
        client: AsyncOpenAI = clients[active_backend]
        profile: BackendProfile = BACKENDS[active_backend]
        headers: Dict[str, str] = dict(profile["extra_headers"])
        if active_backend == "opencode":
            headers["x-opencode-session"] = session_id
        try:
            response: Any = await client.chat.completions.create(
                model=profile["model"],
                messages=messages,
                tools=tool_schemas,
                tool_choice="auto",
                max_tokens=MAX_RESPONSE_TOKENS,
                extra_headers=headers,
            )
            return response, active_backend
        except Exception as error:
            last_error = error
            logging.warning(
                "Model çağrısı başarısız, yeniden deneniyor",
                extra={
                    "attempt": attempt, "max_attempts": MAX_API_RETRIES,
                    "backend": active_backend, "error_type": type(error).__name__,
                },
            )
            if attempt < MAX_API_RETRIES:
                await asyncio.sleep(2 ** (attempt - 1))
    assert last_error is not None
    raise last_error


async def run_agent_with_callback(goal: str, callback: Callable[[str], None]) -> str:
    """Hedefi planla-yürüt-gözlemle-onar döngüsüyle çalıştırır; ilerlemeyi callback'e bildirir."""
    clients: Dict[str, AsyncOpenAI] = {
        name: AsyncOpenAI(api_key=profile["api_key"], base_url=profile["base_url"])
        for name, profile in BACKENDS.items()
        if profile["api_key"]
    }
    if DEFAULT_BACKEND not in clients:
        raise RuntimeError(
            f"Varsayılan backend '{DEFAULT_BACKEND}' için API anahtarı bulunamadı: "
            "~/.local/share/opencode/auth.json içinde 'opencode-go' girdisi yok."
        )
    callback(f"Kullanılabilir modeller: {', '.join(sorted(clients))}")

    requested_backend: str = os.environ.get("OMNI_BACKEND", DEFAULT_BACKEND)
    if requested_backend not in clients:
        callback(f"Uyarı: '{requested_backend}' backend'i kullanılamıyor (anahtar yok), '{DEFAULT_BACKEND}' kullanılacak.")
        requested_backend = DEFAULT_BACKEND

    toolbox: Toolbox = Toolbox()
    session_id: str = str(uuid.uuid4())
    state: sm.StateDict = sm.load_state(STATE_FILE)

    known_fix: Optional[str] = sm.check_for_lessons(state, goal)
    system_content: str = config["SYSTEM_PROMPT"]
    if known_fix:
        system_content += f"\n\n### BİLİNEN DERS\nBenzer bir görevde şu çözüm işe yaramıştı: {known_fix}"

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": goal},
    ]
    tool_schemas: List[Dict[str, Any]] = build_tool_schemas()
    steps: List[Dict[str, Any]] = []
    outcome: str = ""
    success: bool = False
    start_time: float = time.monotonic()
    current_backend: str = requested_backend
    consecutive_tool_failures: int = 0

    try:
        for iteration in range(1, MAX_ITERATIONS + 1):
            elapsed: float = time.monotonic() - start_time
            if elapsed > MAX_WALL_CLOCK_SECONDS:
                outcome = f"Zaman bütçesi ({MAX_WALL_CLOCK_SECONDS:.0f}sn) aşıldı, görev tamamlanamadı."
                callback(outcome)
                break

            messages = _trim_old_tool_messages(messages)
            callback(f"[{iteration}/{MAX_ITERATIONS}] Model düşünüyor... (backend: {current_backend})")
            response: Any
            used_backend: str
            response, used_backend = await _call_model_with_retries(
                clients, messages, tool_schemas, session_id, current_backend
            )
            if used_backend != current_backend:
                callback(f"  -> API hatası nedeniyle '{used_backend}' backend'ine yükseltildi.")
                current_backend = used_backend
            message: Any = response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))

            if not message.tool_calls:
                outcome = message.content or ""
                success = True
                callback(f"Tamamlandı: {outcome}")
                break

            for call in message.tool_calls:
                callback(f"Araç çağrısı: {call.function.name}({call.function.arguments})")

            # Bağımsız araç çağrıları paralel, fiziksel GUI eylemleri sıralı çalışır.
            results: List[ToolResult] = await _execute_tool_calls(message.tool_calls, toolbox)

            for call, result in zip(message.tool_calls, results):
                steps.append({"tool": call.function.name, "result": result})
                hint: Optional[str] = None
                if result.get("ok"):
                    consecutive_tool_failures = 0
                    callback(f"  -> Başarılı ({call.function.name}): {str(result.get('result', ''))[:300]}")
                else:
                    consecutive_tool_failures += 1
                    error_text: str = f"{result.get('error_type')}: {result.get('error')}"
                    callback(f"  -> Hata ({call.function.name}): {error_text}")
                    hint = sm.check_for_lessons(state, error_text)
                    if hint:
                        callback(f"  -> Bilinen ders uygulanıyor: {hint}")
                messages.append(_tool_result_to_message(call, result, hint))

                if result.get("ok") and call.function.name == "take_screenshot":
                    _attach_screenshot_observation(messages, call, callback)

            if (
                consecutive_tool_failures >= CONSECUTIVE_FAILURE_ESCALATION_THRESHOLD
                and current_backend != ESCALATION_BACKEND
                and ESCALATION_BACKEND in clients
            ):
                callback(f"  -> Art arda {consecutive_tool_failures} araç hatası, '{ESCALATION_BACKEND}' backend'ine yükseltiliyor.")
                current_backend = ESCALATION_BACKEND
                consecutive_tool_failures = 0
        else:
            outcome = f"Maksimum iterasyon sayısına ({MAX_ITERATIONS}) ulaşıldı, görev tamamlanamadı."
            callback(outcome)
    except Exception as error:
        outcome = f"Kritik hata: {error}"
        callback(outcome)
        raise
    finally:
        state = sm.record_episode(state, goal, steps, outcome, success)
        sm.save_state(STATE_FILE, state)
        await toolbox.close_browser()
        await asyncio.gather(*(c.close() for c in clients.values()))

    return outcome


def _attach_screenshot_observation(messages: List[Dict[str, Any]], call: Any, callback: Callable[[str], None]) -> None:
    """Başarılı bir ekran görüntüsünü modele görsel gözlem olarak ekler (en iyi çaba)."""
    try:
        arguments: Dict[str, Any] = json.loads(call.function.arguments or "{}")
        image_b64: str = encode_image(arguments["filename"])
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": "Gözlem: az önce alınan ekran görüntüsü."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ],
        })
    except Exception as error:
        logging.warning("Ekran görüntüsü modele eklenemedi", extra={"error_type": type(error).__name__})
        callback(f"  -> Not: ekran görüntüsü modele iliştirilemedi ({type(error).__name__}).")


async def run_agent(goal: str) -> str:
    """Hedefi konsola log basarak çalıştırır."""
    return await run_agent_with_callback(goal, callback=print)


if __name__ == "__main__":
    import sys
    cli_goal: str = " ".join(sys.argv[1:]).strip()
    if not cli_goal:
        print("Kullanım: python3 main.py <hedef metni>")
        raise SystemExit(1)
    asyncio.run(run_agent(cli_goal))
