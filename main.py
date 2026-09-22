import asyncio
import base64
import json
import logging
import os
import sys
import time
import uuid
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, Timeout
from openai.types.chat import ChatCompletion
from PIL import Image

from config import BACKENDS, DEFAULT_BACKEND, ESCALATION_BACKEND, QUALITY_LADDER, SYSTEM_PROMPT, BackendProfile
from tools import MODEL_SCREEN_MAX_EDGE, Toolbox, ToolError
import state_manager as sm

STATE_FILE: str = str(Path(__file__).resolve().parent / "cognitive_memory.json")
MAX_ITERATIONS: int = 25
MAX_WALL_CLOCK_SECONDS: float = 600.0
# Tam ayrıntıyla tutulan son model turu sayısı. Daha eski turların uzun araç çıktıları,
# ekran görüntüleri ve uzun araç argümanları budanır. Yaş TUR ile ölçülür: son turun
# sonuçları (paralel toplu okumalar dahil) model onları görmeden asla kırpılmaz.
FULL_DETAIL_TURNS: int = 2
TRIMMED_CONTENT_LIMIT: int = 400
TRIMMED_ARGS_LIMIT: int = 120
CALL_LABEL_ARGS_LIMIT: int = 100
CONSECUTIVE_FAILURE_ESCALATION_THRESHOLD: int = 2
# SDK varsayılanı 600sn zaman aşımı + 2 gizli yeniden denemeydi: takılan tek bir çağrı tüm
# görev bütçesini yiyebiliyor, yükseltme mantığı da SDK aynı backend'i tekrar denedikten
# sonra devreye giriyordu. Yeniden denemeyi yalnızca bu döngü yönetir.
MODEL_REQUEST_TIMEOUT_SECONDS: float = 60.0
MODEL_CONNECT_TIMEOUT_SECONDS: float = 5.0
TURKISH_WEEKDAYS: Tuple[str, ...] = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
ENGLISH_WEEKDAYS: Tuple[str, ...] = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class ToolResult(TypedDict, total=False):
    tool_call_id: str
    ok: bool
    result: str
    error_type: str
    error: str
    code: str
    recoverable: bool


class RunOptions(TypedDict):
    requested_backend: Optional[str]
    should_stop: Callable[[], bool]
    # Epizot kaydının yazılacağı bellek dosyası (benchmark ayrı dosya kullanır)
    state_file: str


class TokenUsage(TypedDict):
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int


class RunReport(TypedDict):
    """Bir görevin sonucu ve ölçümleri."""
    outcome: str
    success: bool
    metrics: sm.EpisodeMetrics


def encode_image(path: str) -> str:
    """
    Görüntüyü JPEG base64 metnine çevirir. take_screenshot görüntüyü zaten ortak
    koordinat uzayında kaydeder; küçültme yalnızca başka kaynaklı büyük dosyalar için sınırdır.
    """
    with Image.open(path) as source:
        frame: Image.Image = source.convert("RGB")
    frame.thumbnail((MODEL_SCREEN_MAX_EDGE, MODEL_SCREEN_MAX_EDGE))
    buffer: BytesIO = BytesIO()
    frame.save(buffer, format="JPEG", quality=70)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def _function_schema(name: str, description: str, properties: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Tüm parametreleri zorunlu bir function-calling şeması kurar (isteğe bağlılar null alır). Saf."""
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(properties)},
    }}


def build_tool_schemas() -> List[Dict[str, Any]]:
    """
    Modelin gördüğü araçlar. Liste bilerek kısa tutulur: ölçümde 26 araçlı şemada model
    hedefteki tarihi 10 denemenin 5'inde yanlış kopyaladı, tek araçla 10/10 doğruydu.
    Fare/klavye adımları run_action_sequence, şablon tıklama smart_click içindedir.
    """
    return [
        _function_schema("execute_shell", "Sistem kabuğunda (/bin/sh, macOS BSD araçları) komut çalıştırır.", {
            "command": {"type": "string", "description": "Çalıştırılacak kabuk komutu."},
            "use_sudo": {"type": "boolean", "description": "Komut sudo ile mi çalıştırılsın."},
        }),
        _function_schema("process_list", "Süreç sayısını ve CPU'ya göre en ağır 15 süreci döner.", {}),
        _function_schema("read_file", "Bir dosyanın içeriğini okur (uzun dosyalar kısaltılır).", {
            "path": {"type": "string", "description": "Okunacak dosyanın yolu."},
        }),
        _function_schema(
            "write_file",
            "Dosyaya tam içerik yazar: eksik üst dizinleri oluşturur, yazılanı doğrular, eski sürümü "
            "yedekler. Başarı mesajı kanıttır; geri okuma yapma.",
            {
                "path": {"type": "string", "description": "Yazılacak dosyanın yolu."},
                "content": {"type": "string", "description": "Dosyanın tam içeriği."},
            },
        ),
        _function_schema("web_search", "DuckDuckGo üzerinden web araması yapar (ilk 5 sonuç).", {
            "query": {"type": "string", "description": "Arama sorgusu."},
        }),
        _function_schema(
            "fetch_raw",
            "curl ile hızlı HTTP çekimi: JSON olduğu gibi, HTML temiz metin olarak döner. JavaScript "
            "gerektirmeyen sayfalarda browse_url'den hızlıdır.",
            {"url": {"type": "string", "description": "Çekilecek URL."}},
        ),
        _function_schema(
            "browse_url",
            "Kalıcı tarayıcı sekmesi: url verilirse gider (null: mevcut sayfada kalır), actions'ı sırayla "
            "uygular, sonunda URL, başlık, sayfa metni ve seçicileriyle etkileşimli öğeleri döner. "
            "'Alanı doldur → gönder → sonucu oku' akışını TEK çağrıda yap.",
            {
                "url": {"type": ["string", "null"], "description": "Gidilecek URL; mevcut sayfada kalmak için null."},
                "actions": {
                    "type": "array",
                    "description": "Sırayla uygulanacak eylemler; yalnızca okumak için boş liste.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["click", "fill", "press"]},
                            "selector": {"type": "string", "description": "Öğe seçicisi (dönen ÖĞELER listesinden)."},
                            "value": {"type": ["string", "null"], "description": "fill için metin, press için tuş adı (örn. Enter); click için null."},
                        },
                        "required": ["action", "selector", "value"],
                    },
                },
            },
        ),
        _function_schema("execute_js", "Node.js ile JavaScript kodu çalıştırır.", {
            "code": {"type": "string", "description": "Çalıştırılacak JavaScript kodu."},
        }),
        _function_schema(
            "take_screenshot",
            "Ekran görüntüsü alır, kaydeder ve sana görsel olarak gösterir. Görüntü koordinatları "
            "tıklama araçlarıyla aynı uzaydadır. Yavaş ve pahalıdır: önce cua_get_ax_state dene.",
            {"filename": {"type": "string", "description": "Kaydedilecek .png veya .jpg dosya yolu."}},
        ),
        _function_schema("cua_get_app", "Uygulamayı başlatır veya öne getirir.", {
            "app_name": {"type": "string", "description": "Uygulama adı (örn. Safari, Notes)."},
        }),
        _function_schema(
            "cua_get_ax_state",
            "Uygulamanın öndeki penceresindeki etkileşimli öğeleri (buton, alan, bağlantı, satır…) numara, "
            "tür, etiket ve merkez koordinatıyla listeler. Ekran görüntüsünden çok daha hızlıdır.",
            {"app_name": {"type": "string", "description": "Uygulama adı."}},
        ),
        _function_schema(
            "cua_click",
            "cua_get_ax_state listesindeki numaralı öğeye erişilebilirlik ile tıklar; metin alanlarını odaklar.",
            {
                "app_name": {"type": "string", "description": "Uygulama adı."},
                "element_id": {"type": "integer", "description": "Son listedeki öğe numarası."},
            },
        ),
        _function_schema(
            "smart_click",
            "Hibrit tıklama: önce AX öğe numarası, olmazsa görsel şablon (take_screenshot görüntüsünden "
            "kırpılmış PNG) dener.",
            {
                "app_name": {"type": "string", "description": "Uygulama adı."},
                "element_id": {"type": ["integer", "null"], "description": "AX öğe numarası veya null."},
                "template_path": {"type": ["string", "null"], "description": "Şablon görsel yolu veya null."},
                "confidence": {"type": "number", "description": "Şablon eşleşme eşiği (0-1, genelde 0.8)."},
            },
        ),
        _function_schema(
            "run_action_sequence",
            "Fare/klavye eylemlerini TEK çağrıda sırayla çalıştırır. click/move: x,y (ekran görüntüsü/AX "
            "uzayı); type: text (her Unicode metin, Türkçe dahil); press: key ('enter', 'tab', 'escape', "
            "'cmd+c', 'cmd+shift+t'); wait: seconds (en çok 5).",
            {
                "steps": {
                    "type": "array",
                    "description": "Sırayla çalıştırılacak eylemler.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["click", "move", "type", "press", "wait"]},
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "button": {"type": "string", "enum": ["left", "right", "middle"]},
                            "text": {"type": "string"},
                            "key": {"type": "string"},
                            "seconds": {"type": "number"},
                        },
                        "required": ["action"],
                    },
                },
            },
        ),
    ]


# Modelin çağırabileceği adlar: getattr ile Toolbox'ın özel yöntemlerine
# (_read_full, close_browser…) ulaşılmasın.
TOOL_NAMES: frozenset[str] = frozenset(schema["function"]["name"] for schema in build_tool_schemas())

# Salt okunur araçlar aynı (ad + argüman) için önbelleklenebilir. Canlı durum (AX listesi)
# önbelleklenmez; her başarılı yan etkili çağrıdan sonra önbellek tamamen temizlenir.
_CACHEABLE_TOOLS: frozenset[str] = frozenset({"process_list", "read_file", "web_search", "fetch_raw"})

# Yan etkili araçlar model sırasıyla SERİ çalışır (aynı anda iki tıklama/yazma çakışmasın,
# paralel bir okuma yazmadan önce bayat sonuç önbelleğe girmesin, eylem→gözlem sırası
# korunsun); aralarındaki bağımsız salt okunur bloklar gerçek paralellikle çalışır.
_SIDE_EFFECT_TOOLS: frozenset[str] = frozenset({
    "execute_shell", "write_file", "execute_js", "take_screenshot", "browse_url",
    "cua_get_app", "cua_click", "smart_click", "run_action_sequence",
})


def _tool_cache_key(name: str, arguments: Dict[str, Any]) -> str:
    """Önbellek anahtarı: araç adı + kararlı (sıralı) argüman JSON'u."""
    return name + ":" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)


async def execute_tool(call: Any, toolbox: Toolbox, cache: Dict[str, ToolResult]) -> ToolResult:
    """Tek bir araç çağrısını çalıştırır; başarı/hata durumunu yapılandırılmış şekilde döner."""
    name: str = call.function.name
    try:
        arguments: Dict[str, Any] = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError as error:
        return {
            "tool_call_id": call.id, "ok": False,
            "error_type": "JSONDecodeError", "error": f"Araç argümanları çözümlenemedi: {error}",
        }
    if name not in TOOL_NAMES:
        return {
            "tool_call_id": call.id, "ok": False,
            "error_type": "UnknownTool", "error": f"Bilinmeyen araç: {name}. Geçerli araçlar: {', '.join(sorted(TOOL_NAMES))}",
        }

    cache_key: Optional[str] = None
    if name in _CACHEABLE_TOOLS:
        cache_key = _tool_cache_key(name, arguments)
        if cache_key in cache:
            return {**cache[cache_key], "tool_call_id": call.id}

    method: Callable[..., Any] = getattr(toolbox, name)
    try:
        if asyncio.iscoroutinefunction(method):
            result: Any = await method(**arguments)
        else:
            result = await asyncio.to_thread(method, **arguments)
        outcome: ToolResult = {"tool_call_id": call.id, "ok": True, "result": str(result)}
    except ToolError as error:
        outcome = {
            "tool_call_id": call.id, "ok": False,
            "error_type": "ToolError", "error": str(error),
            "code": error.code, "recoverable": error.recoverable,
        }
    except TypeError as error:
        outcome = {
            "tool_call_id": call.id, "ok": False,
            "error_type": "TypeError", "error": f"Geçersiz argümanlar ({name}): {error}",
        }
    except Exception as error:  # Araç sınırı: üçüncü taraf hataları döngüyü çökertmeden modele raporlanır.
        logging.warning("Araç beklenmeyen hata verdi", extra={"tool": name, "error_type": type(error).__name__})
        outcome = {
            "tool_call_id": call.id, "ok": False,
            "error_type": type(error).__name__, "error": str(error),
        }

    if outcome.get("ok") and name in _SIDE_EFFECT_TOOLS:
        cache.clear()
    if cache_key is not None and outcome.get("ok"):
        cache[cache_key] = outcome
    return outcome


async def _execute_tool_calls(calls: List[Any], toolbox: Toolbox, cache: Dict[str, ToolResult]) -> List[ToolResult]:
    """
    Bir turdaki tüm araç çağrılarını MODELİN DÖNDÜRDÜĞÜ SIRAYI koruyarak çalıştırır:
    yan etkili çağrılar seri, aralarındaki bağımsız salt okunur bloklar paralel. Böylece
    'tıkla -> ekran görüntüsü al' gibi eylem-gözlem çiftlerinde gözlem her zaman
    eylemden SONRA gelir (yarış yok).
    """
    results: List[Optional[ToolResult]] = [None] * len(calls)
    index: int = 0
    while index < len(calls):
        if calls[index].function.name in _SIDE_EFFECT_TOOLS:
            results[index] = await execute_tool(calls[index], toolbox, cache)
            index += 1
            continue
        stop: int = index
        while stop < len(calls) and calls[stop].function.name not in _SIDE_EFFECT_TOOLS:
            stop += 1
        group: List[ToolResult] = list(await asyncio.gather(
            *(execute_tool(calls[k], toolbox, cache) for k in range(index, stop))
        ))
        results[index:stop] = group
        index = stop
    return [r for r in results if r is not None]


def _call_label(call: Any) -> str:
    """
    Sonucun hangi çağrıya ait olduğunu gösteren kısa etiket. Paralel toplu sonuçlar yalnızca
    tool_call_id ile eşleşince hızlı model onları karıştırabiliyordu (ölçümde 5 dosyalık
    okumada kodlar yanlış sıralandı/atlandı). Saf fonksiyon.
    """
    arguments: str = " ".join((call.function.arguments or "").split())
    return f"[{call.function.name} {arguments[:CALL_LABEL_ARGS_LIMIT]}]"


def _tool_result_to_message(call: Any, result: ToolResult) -> Dict[str, Any]:
    """Araç sonucunu, başında çağrı etiketiyle modele geri gönderilecek 'tool' mesajına çevirir."""
    if result.get("ok"):
        content: str = result.get("result", "")
    else:
        content = f"HATA [{result.get('error_type')}]: {result.get('error')}"
    return {"role": "tool", "tool_call_id": call.id, "content": f"{_call_label(call)}\n{content}"}


def _assistant_entry(message: Any) -> Dict[str, Any]:
    """
    Assistant yanıtını geçmiş için {role, content, tool_calls} biçiminde normalize eder.
    Sağlayıcıya özel alanlar (örn. qwen'in reasoning_content'i) her turda tekrar
    gönderilmesin ve yükseltmede başka bir API'ye taşınmasın diye atılır. Saf.
    """
    entry: Dict[str, Any] = {"role": "assistant"}
    if message.content:
        entry["content"] = message.content
    if message.tool_calls:
        entry["tool_calls"] = [
            {"id": call.id, "type": "function",
             "function": {"name": call.function.name, "arguments": call.function.arguments or ""}}
            for call in message.tool_calls
        ]
    return entry


def _trim_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Eski bir mesajın büyük parçalarını (araç çıktısı, görsel, uzun argüman) budar. Saf."""
    content: Any = entry.get("content")
    if entry.get("role") == "tool" and isinstance(content, str) and len(content) > TRIMMED_CONTENT_LIMIT:
        return {**entry, "content": content[:TRIMMED_CONTENT_LIMIT] + " …[eski çıktı kısaltıldı]"}
    if isinstance(content, list) and any(isinstance(part, dict) and part.get("type") == "image_url" for part in content):
        return {**entry, "content": "[eski ekran görüntüsü bağlamdan çıkarıldı]"}
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


def build_system_prompt(today: date) -> str:
    """
    Sabit sistem talimatının SONUNA günün tarihini ekler (önek önbelleği korunur). Ev dizini
    bilerek eklenmez: ölçümde modeli istenmeyen ~/output.txt dosyaları yazmaya itti. Saf.
    """
    weekday: int = today.weekday()
    return (
        SYSTEM_PROMPT
        + f"\n### TODAY\n- Date: {today.isoformat()} ({TURKISH_WEEKDAYS[weekday]} / {ENGLISH_WEEKDAYS[weekday]}).\n"
    )


def next_quality_backend(current: str, available: frozenset[str]) -> Optional[str]:
    """
    Art arda başarısız araç turlarında çıkılacak sonraki backend: QUALITY_LADDER'daki bir
    sonraki kullanılabilir basamak (merdiven dışındaki backend'ler doğrudan
    ESCALATION_BACKEND'e çıkar). Çıkılacak basamak yoksa None. Saf fonksiyon.
    """
    if current in QUALITY_LADDER:
        later: Tuple[str, ...] = QUALITY_LADDER[QUALITY_LADDER.index(current) + 1:]
    else:
        later = (ESCALATION_BACKEND,) if current != ESCALATION_BACKEND else ()
    return next((name for name in later if name in available), None)


def attempt_plan(backend: str, available: frozenset[str]) -> Tuple[str, str, str]:
    """
    Model çağrısı deneme planı: ilk iki deneme aynı backend'de (geçici bağlantı/5xx/429),
    son deneme farklı sağlayıcıda (ESCALATION_BACKEND kullanılabiliyorsa). Saf fonksiyon.
    """
    fallback: str = ESCALATION_BACKEND if ESCALATION_BACKEND in available else backend
    return (backend, backend, fallback)


def final_verdict(content: str, finish_reason: Optional[str]) -> Tuple[bool, str]:
    """
    Araç çağrısız son yanıtın gerçekten tamamlanmış bir cevap olup olmadığına karar verir:
    token sınırında kesilen, filtrelenen ya da boş yanıtlar başarı sayılmaz. Saf fonksiyon.
    """
    if finish_reason == "length":
        return False, "yanıt max_tokens sınırında kesildi"
    if finish_reason == "content_filter":
        return False, "yanıt içerik filtresine takıldı"
    if not content.strip():
        return False, "model boş yanıt döndü"
    return True, ""


def token_usage(response: ChatCompletion) -> TokenUsage:
    """Yanıttaki token kullanımını (önbellekten okunan girdi dahil) çıkarır. Saf."""
    usage: Any = response.usage
    if usage is None:
        return {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}
    details: Any = usage.prompt_tokens_details
    cached: int = int(details.cached_tokens or 0) if details is not None else 0
    return {"prompt_tokens": int(usage.prompt_tokens), "cached_tokens": cached, "completion_tokens": int(usage.completion_tokens)}


def add_usage(total: TokenUsage, turn: TokenUsage) -> TokenUsage:
    """İki token kullanımını toplar. Saf."""
    return {
        "prompt_tokens": total["prompt_tokens"] + turn["prompt_tokens"],
        "cached_tokens": total["cached_tokens"] + turn["cached_tokens"],
        "completion_tokens": total["completion_tokens"] + turn["completion_tokens"],
    }


def create_model_clients() -> Dict[str, AsyncOpenAI]:
    """Anahtarı olan her backend için zaman aşımı sınırlı, SDK içi yeniden denemesiz istemci kurar."""
    timeout: Timeout = Timeout(MODEL_REQUEST_TIMEOUT_SECONDS, connect=MODEL_CONNECT_TIMEOUT_SECONDS)
    return {
        name: AsyncOpenAI(api_key=profile["api_key"], base_url=profile["base_url"], timeout=timeout, max_retries=0)
        for name, profile in BACKENDS.items()
        if profile["api_key"]
    }


async def close_model_clients(clients: Dict[str, AsyncOpenAI]) -> None:
    """İstemcilerin bağlantı havuzlarını kapatır."""
    await asyncio.gather(*(client.close() for client in clients.values()))


async def _request_completion(
    client: AsyncOpenAI, profile: BackendProfile, messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]], session_id: str,
) -> ChatCompletion:
    """Profilin modeli, token sınırı, başlıkları ve ek gövdesiyle tek bir tamamlama isteği yapar."""
    headers: Dict[str, str] = dict(profile["extra_headers"])
    if profile["session_header"] is not None:
        headers[profile["session_header"]] = session_id
    return await client.chat.completions.create(
        model=profile["model"],
        messages=messages,
        tools=tool_schemas,
        tool_choice="auto",
        max_tokens=profile["max_tokens"],
        extra_headers=headers,
        extra_body=profile["extra_body"],
    )


async def _call_model_with_retries(
    clients: Dict[str, AsyncOpenAI],
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]],
    session_id: str,
    backend: str,
) -> Tuple[ChatCompletion, str]:
    """
    Model çağrısını attempt_plan'a göre yapar ve yanıt veren backend'i de döner. Kalıcı
    istemci hataları (400/401/402/403…) yeniden denenmez. Zaman aşımında aynı backend'i bir
    kez daha beklemek boşa gider: doğrudan son (farklı sağlayıcı) denemeye atlanır.
    """
    plan: Tuple[str, str, str] = attempt_plan(backend, frozenset(clients))
    last_error: Optional[Exception] = None
    attempt: int = 0
    while attempt < len(plan):
        active: str = plan[attempt]
        try:
            response: ChatCompletion = await _request_completion(
                clients[active], BACKENDS[active], messages, tool_schemas, session_id,
            )
            return response, active
        except (APIStatusError, APIConnectionError) as error:
            status: Optional[int] = error.status_code if isinstance(error, APIStatusError) else None
            if status is not None and status < 500 and status != 429:
                raise
            last_error = error
            logging.warning(
                "Model çağrısı başarısız, yeniden deneniyor",
                extra={"attempt": attempt + 1, "max_attempts": len(plan), "backend": active,
                       "status_code": status, "error_type": type(error).__name__},
            )
            timed_out: bool = isinstance(error, APITimeoutError)
            attempt = len(plan) - 1 if timed_out and attempt < len(plan) - 1 else attempt + 1
            if attempt < len(plan):
                await asyncio.sleep(0.5 * attempt)
    assert last_error is not None
    raise last_error


async def _screenshot_observation(call: Any) -> Dict[str, Any]:
    """Başarılı bir ekran görüntüsünü modele gidecek görsel gözlem mesajına çevirir."""
    arguments: Dict[str, Any] = json.loads(call.function.arguments or "{}")
    image_b64: str = await asyncio.to_thread(encode_image, str(Path(arguments["filename"]).expanduser()))
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "Gözlem: az önce alınan ekran görüntüsü (koordinatlar tıklama araçlarıyla aynı uzayda)."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ],
    }


async def run_agent_with_callback(
    goal: str, callback: Callable[[str], None], options: RunOptions, clients: Dict[str, AsyncOpenAI],
) -> RunReport:
    """
    Hedefi planla-yürüt-gözlemle-onar döngüsüyle çalıştırır; ilerlemeyi ve tur başına
    süre/token ölçümlerini callback'e bildirir. İstemciler çağırana aittir (kapatılmaz),
    böylece arayüz görevler arasında sıcak bağlantıları yeniden kullanır.
    """
    if DEFAULT_BACKEND not in clients:
        raise RuntimeError(
            f"Varsayılan backend '{DEFAULT_BACKEND}' için API anahtarı bulunamadı: "
            "~/.local/share/opencode/auth.json içinde 'opencode-go' girdisi yok."
        )
    callback(f"Kullanılabilir modeller: {', '.join(sorted(clients))}")
    available: frozenset[str] = frozenset(clients)

    backend_override: Optional[str] = options["requested_backend"] or os.environ.get("OMNI_BACKEND")
    current_backend: str = backend_override if backend_override else DEFAULT_BACKEND
    if current_backend not in available:
        callback(f"Uyarı: '{current_backend}' backend'i kullanılamıyor (anahtar yok), '{DEFAULT_BACKEND}' kullanılacak.")
        current_backend = DEFAULT_BACKEND

    toolbox: Toolbox = Toolbox()
    session_id: str = str(uuid.uuid4())
    state: sm.StateDict = sm.load_state(options["state_file"])
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(date.today())},
        {"role": "user", "content": goal},
    ]
    tool_schemas: List[Dict[str, Any]] = build_tool_schemas()
    steps: List[sm.StepRecord] = []
    outcome: str = ""
    success: bool = False
    start_time: float = time.monotonic()
    consecutive_failed_turns: int = 0
    tool_cache: Dict[str, ToolResult] = {}
    turns: int = 0
    tool_call_count: int = 0
    usage: TokenUsage = {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}
    metrics: sm.EpisodeMetrics

    try:
        for iteration in range(1, MAX_ITERATIONS + 1):
            if options["should_stop"]():
                outcome = "Kullanıcı tarafından durduruldu."
                callback(outcome)
                break
            if time.monotonic() - start_time > MAX_WALL_CLOCK_SECONDS:
                outcome = f"Zaman bütçesi ({MAX_WALL_CLOCK_SECONDS:.0f}sn) aşıldı, görev tamamlanamadı."
                callback(outcome)
                break

            messages = _trim_old_turns(messages)
            callback(f"[{iteration}/{MAX_ITERATIONS}] Model düşünüyor... (backend: {current_backend})")
            model_started: float = time.monotonic()
            response, used_backend = await _call_model_with_retries(
                clients, messages, tool_schemas, session_id, current_backend,
            )
            turns += 1
            turn_usage: TokenUsage = token_usage(response)
            usage = add_usage(usage, turn_usage)
            callback(
                f"  ⏱ model {time.monotonic() - model_started:.1f}sn · girdi {turn_usage['prompt_tokens']} "
                f"(önbellek {turn_usage['cached_tokens']}) · çıktı {turn_usage['completion_tokens']} token"
            )
            if used_backend != current_backend:
                callback(f"  -> API hatası nedeniyle '{used_backend}' backend'ine yükseltildi.")
                current_backend = used_backend
            choice: Any = response.choices[0]
            message: Any = choice.message
            messages.append(_assistant_entry(message))

            if not message.tool_calls:
                outcome = message.content or ""
                success, reason = final_verdict(outcome, choice.finish_reason)
                callback(f"Tamamlandı: {outcome}" if success else f"Tamamlanamadı: {reason}. Son yanıt: {outcome[:300]}")
                break

            if choice.finish_reason == "length":
                callback("  -> Uyarı: yanıt max_tokens sınırında kesildi; araç argümanları eksik olabilir.")
            tool_call_count += len(message.tool_calls)
            for call in message.tool_calls:
                callback(f"Araç çağrısı: {call.function.name}({(call.function.arguments or '')[:TRIMMED_ARGS_LIMIT]})")

            tools_started: float = time.monotonic()
            results: List[ToolResult] = await _execute_tool_calls(message.tool_calls, toolbox, tool_cache)
            callback(f"  ⏱ araçlar {time.monotonic() - tools_started:.1f}sn")

            failures_in_turn: int = 0
            pending_shots: List[Any] = []
            for call, result in zip(message.tool_calls, results):
                ok: bool = bool(result.get("ok"))
                detail: str = str(result.get("result", "")) if ok else f"{result.get('error_type')}: {result.get('error')}"
                steps.append(sm.make_step_record(call.function.name, call.function.arguments or "", ok, detail))
                if ok:
                    callback(f"  -> Başarılı ({call.function.name}): {detail[:300]}")
                    if call.function.name == "take_screenshot":
                        pending_shots.append(call)
                else:
                    failures_in_turn += 1
                    callback(f"  -> Hata ({call.function.name}): {detail}")
                messages.append(_tool_result_to_message(call, result))

            # Ekran gözlemleri TÜM araç mesajlarından SONRA eklenir: tool sonuçları assistant
            # tool_calls'ı kesintisiz izlemeli; araya user mesajı 400'e yol açar.
            for shot_call in pending_shots:
                try:
                    messages.append(await _screenshot_observation(shot_call))
                except (OSError, KeyError, ValueError) as error:
                    logging.warning("Ekran görüntüsü modele eklenemedi", extra={"error_type": type(error).__name__})
                    callback(f"  -> Not: ekran görüntüsü modele iliştirilemedi ({type(error).__name__}: {error}).")

            # Yükseltme sayacı TUR bazlıdır: turda herhangi bir başarı varsa sıfırlanır.
            consecutive_failed_turns = consecutive_failed_turns + 1 if failures_in_turn == len(results) else 0
            if consecutive_failed_turns >= CONSECUTIVE_FAILURE_ESCALATION_THRESHOLD:
                upgraded: Optional[str] = next_quality_backend(current_backend, available)
                if upgraded is not None:
                    callback(f"  -> Art arda {consecutive_failed_turns} başarısız tur, '{upgraded}' backend'ine yükseltiliyor.")
                    current_backend = upgraded
                consecutive_failed_turns = 0
        else:
            outcome = f"Maksimum iterasyon sayısına ({MAX_ITERATIONS}) ulaşıldı, görev tamamlanamadı."
            callback(outcome)
    except Exception as error:
        outcome = f"Kritik hata: {error}"
        callback(outcome)
        raise
    finally:
        metrics = {
            "turns": turns, "tool_calls": tool_call_count,
            "elapsed_seconds": round(time.monotonic() - start_time, 2), "backend": current_backend,
            "prompt_tokens": usage["prompt_tokens"], "cached_tokens": usage["cached_tokens"],
            "completion_tokens": usage["completion_tokens"],
        }
        sm.save_state(options["state_file"], sm.record_episode(state, goal, steps, outcome, success, metrics))
        await toolbox.close_browser()

    callback(
        f"Özet: {turns} tur · {tool_call_count} araç · {metrics['elapsed_seconds']:.1f}sn · girdi {usage['prompt_tokens']} "
        f"(önbellek {usage['cached_tokens']}) · çıktı {usage['completion_tokens']} token"
    )
    return {"outcome": outcome, "success": success, "metrics": metrics}


async def run_agent(goal: str) -> RunReport:
    """Hedefi konsola log basarak çalıştırır (CLI)."""
    clients: Dict[str, AsyncOpenAI] = create_model_clients()
    try:
        options: RunOptions = {"requested_backend": None, "should_stop": lambda: False, "state_file": STATE_FILE}
        return await run_agent_with_callback(goal, print, options, clients)
    finally:
        await close_model_clients(clients)


if __name__ == "__main__":
    cli_goal: str = " ".join(sys.argv[1:]).strip()
    if not cli_goal:
        print("Kullanım: python3 main.py <hedef metni>")
        raise SystemExit(1)
    asyncio.run(run_agent(cli_goal))
