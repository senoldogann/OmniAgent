import asyncio
import base64
import json
import logging
import os
import re
import shutil
import ssl
import sys
import tempfile
import time
import uuid
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict, NotRequired

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, Timeout
from openai.types import CompletionUsage
from PIL import Image

from config import BACKENDS, DEFAULT_BACKEND, ESCALATION_BACKEND, QUALITY_LADDER, SYSTEM_PROMPT, BackendProfile
from cli_backends import CliModelError, run_cli_model
from events import AgentEvent, EventSink, TokenUsage, compact_count, preview_arguments, tool_label
from fast_loop import (
    FastLoopPolicy, FastLoopState, TurnSignal, advance_fast_loop, normalize_progress_signature,
)
from tools import MODEL_SCREEN_SIZE, TOOL_RUNTIME, Toolbox, ToolError
import state_manager as sm
from conversation import Exchange, make_exchange, to_messages
from capabilities import CapabilityService, DISCOVERY_SCHEMA, discovery_entry, validate_arguments
from integration_runtime import (
    AnswerSink, CURRENT_RUNTIME, CURRENT_SERVICE, IntegrationRuntime,
    IntegrationStopped, InteractionRequired,
)

STATE_FILE: str = str(Path(__file__).resolve().parent / "cognitive_memory.json")
MAX_ITERATIONS: int = 25
MAX_WALL_CLOCK_SECONDS: float = 600.0
# Uzun görevler sabit 25 tur sınırına takılmasın; her profil hâlâ açık ve sınırlı bir bütçedir.
# Otonom profil güvenlik rayları kapatmaz; yalnızca tur/zaman bütçesini genişletir.
class RunModeProfile(TypedDict):
    label: str
    max_iterations: int
    max_wall_clock_seconds: float


RUN_MODE_PROFILES: Dict[str, RunModeProfile] = {
    "normal": {"label": "Normal", "max_iterations": 25, "max_wall_clock_seconds": 600.0},
    "extended": {"label": "Uzun", "max_iterations": 50, "max_wall_clock_seconds": 1200.0},
    "autonomous": {"label": "Otonom", "max_iterations": 100, "max_wall_clock_seconds": 2700.0},
}
NO_PROGRESS_LIMIT: int = 4
# Tam ayrıntıyla tutulan son model turu sayısı. Daha eski turların uzun araç çıktıları,
# ekran görüntüleri ve uzun araç argümanları budanır. Yaş TUR ile ölçülür: son turun
# sonuçları (paralel toplu okumalar dahil) model onları görmeden asla kırpılmaz.
FULL_DETAIL_TURNS: int = 2
TRIMMED_CONTENT_LIMIT: int = 400
TRIMMED_ARGS_LIMIT: int = 120
CALL_LABEL_ARGS_LIMIT: int = 100
# Arayüze giden araç sonucu metninin üst sınırı (özet gösterimi için yeterli)
EVENT_RESULT_LIMIT: int = 4000
CONSECUTIVE_FAILURE_ESCALATION_THRESHOLD: int = 2
# Bunlar sert kesme değil, modeli zorunlu kalan işe yönelten yumuşak bütçe eşikleridir.
# Toplam prompt token yerine önbelleksiz giriş kullanılır; önek önbelleği isabetleri gereksiz
# alarm üretmesin. Uzun GUI araştırmasında 24 araç / 60k yeni girişten sonra yeni keşif yerine
# kayıtlı STATE kullanılarak teslim adımlarına öncelik verilir.
SOFT_UNCACHED_PROMPT_TOKEN_BUDGET: int = 60_000
SOFT_TOOL_CALL_BUDGET: int = 24
MAX_FINAL_LENGTH_RECOVERIES: int = 1
TASK_LEDGER_LIMIT: int = 4000

# SDK varsayılanı 600sn zaman aşımı + 2 gizli yeniden denemeydi: takılan tek bir çağrı tüm
# görev bütçesini yiyebiliyor, yükseltme mantığı da SDK aynı backend'i tekrar denedikten
# sonra devreye giriyordu. Yeniden denemeyi yalnızca bu döngü yönetir. Akışta zaman aşımı
# iki parça arasındaki beklemeye uygulanır.
MODEL_REQUEST_TIMEOUT_SECONDS: float = 60.0
MODEL_CONNECT_TIMEOUT_SECONDS: float = 5.0
TURKISH_WEEKDAYS: Tuple[str, ...] = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
ENGLISH_WEEKDAYS: Tuple[str, ...] = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ZERO_USAGE: TokenUsage = {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}


class ToolResult(TypedDict, total=False):
    tool_call_id: str
    ok: bool
    result: str
    error_type: str
    error: str
    code: str
    recoverable: bool


class ToolCallDraft(TypedDict):
    """Modelin istediği araç çağrısı (akış parçalarından birleştirilmiş)."""
    id: str
    name: str
    arguments: str


class ModelTurn(TypedDict):
    """Bir model turunun akıştan birleştirilmiş sonucu."""
    content: str
    tool_calls: List[ToolCallDraft]
    finish_reason: Optional[str]
    usage: TokenUsage


class RunOptions(TypedDict):
    requested_backend: Optional[str]
    should_stop: Callable[[], bool]
    # Epizot kaydının yazılacağı bellek dosyası (benchmark ayrı dosya kullanır)
    state_file: str
    # Kullanıcı tercihleri için ayrı, atomik JSON deposu; verilmezse state_file ile aynı klasörde olur.
    memory_file: NotRequired[str]
    history: List[Exchange]
    integrations: NotRequired[CapabilityService]
    answer: NotRequired[AnswerSink]
    run_mode: NotRequired[str]
    max_iterations: NotRequired[int]
    max_wall_clock_seconds: NotRequired[float]


class RunReport(TypedDict):
    """Bir görevin sonucu ve ölçümleri."""
    outcome: str
    success: bool
    metrics: sm.EpisodeMetrics
    exchange: Exchange


def encode_image(path: str) -> str:
    """
    Görüntüyü JPEG base64 metnine çevirir. take_screenshot görüntüyü zaten ortak
    koordinat uzayında kaydeder; küçültme yalnızca başka kaynaklı büyük dosyalar için sınırdır.
    """
    with Image.open(path) as source:
        frame: Image.Image = source.convert("RGB")
    frame.thumbnail((MODEL_SCREEN_SIZE, MODEL_SCREEN_SIZE))
    buffer: BytesIO = BytesIO()
    frame.save(buffer, format="JPEG", quality=70)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


# Ekran noktası tek [x, y] alanıdır: ayrı x/y tamsayı alanlarında qwen, yerel biçimi olan
# [x, y]'yi x alanına yazıyordu (ölçümde 10 çağrının 7'si bozuk; point ile 0/10).
POINT_SCHEMA: Dict[str, Any] = {
    "type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
    "description": "[x, y]: 1000×1000 ekran görüntüsündeki nokta.",
}


def _function_schema(name: str, description: str, properties: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Tüm parametreleri zorunlu bir function-calling şeması kurar (isteğe bağlılar null alır). Saf."""
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(properties)},
    }}


def camera_photo_goal(goal: str) -> bool:
    """Tek kare kamera çekimi hedeflerinde özel aracı açar; diğer görevleri sade tutar."""
    lowered: str = goal.casefold()
    return (
        any(term in lowered for term in ("fotoğraf", "fotograf", "photo", "selfie", "picture"))
        and any(term in lowered for term in ("çek", "take", "capture", "shoot"))
        and any(term in lowered for term in ("desktop", "masaüst", "masaust"))
        and not any(extension in lowered for extension in (".jpg", ".jpeg", ".png"))
        and not any(app in lowered for app in ("photo booth", "photobooth"))
    )


def active_chrome_session_goal(goal: Optional[str]) -> bool:
    """Hedefte kullanıcının mevcut Chrome oturumu açıkça istendi mi? Saf."""
    if not goal:
        return False
    lowered = goal.casefold()
    return "chrome" in lowered and any(
        term in lowered for term in (
            "açık", "acik", "oturum", "session", "existing", "already open",
            "sekme", "tab", "kullan", "use", "chrome'da", "chrome’da",
        )
    ) and not any(term in lowered for term in ("chrome kullanma", "do not use chrome"))


_MEMORY_MUTATION_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bhatırla\b",
    r"\bunut\b",
    r"\bremember\b",
    r"\bforget\b",
    r"\b(?:hafızaya|hafizaya|belleğe|bellege)\s+(?:kaydet|ekle|yaz)\b",
    r"\bkalıcı\s+(?:hafızaya|hafizaya|belleğe|bellege)\s+(?:kaydet|ekle|yaz)\b",
))


def memory_mutation_requested(goal: str) -> bool:
    """Kalıcı kullanıcı hafızasını değiştirmek için açık kullanıcı niyeti var mı? Saf."""
    return any(pattern.search(goal) is not None for pattern in _MEMORY_MUTATION_PATTERNS)


def build_tool_schemas(goal: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Modelin gördüğü araçlar. Liste bilerek kısa tutulur: ölçümde 26 araçlı şemada model
    hedefteki tarihi 10 denemenin 5'inde yanlış kopyaladı, tek araçla 10/10 doğruydu.
    Fare/klavye adımları run_action_sequence, şablon tıklama smart_click içindedir.
    """
    schemas: List[Dict[str, Any]] = [
        _function_schema("execute_shell", "Sistem kabuğunda (/bin/sh, macOS BSD araçları) komut çalıştırır.", {
            "command": {"type": "string", "description": "Çalıştırılacak kabuk komutu."},
            "use_sudo": {"type": "boolean", "description": "Komut sudo ile mi çalıştırılsın."},
        }),
        _function_schema("process_list", "Süreç sayısını ve CPU'ya göre en ağır 15 süreci döner.", {}),
        _function_schema(
            "user_memory",
            "Kullanıcının açıkça belirttiği kalıcı tercih, sık kullanılan yol veya kararı saklar; "
            "gerektiğinde arar veya siler. Parola, token ve API anahtarı saklamaz.",
            {
                "action": {"type": "string", "enum": ["remember", "recall", "forget"]},
                "key": {"type": ["string", "null"], "description": "Tercih/yol/karar anahtarı."},
                "value": {"type": ["string", "null"], "description": "remember işleminde saklanacak kısa değer."},
                "query": {"type": ["string", "null"], "description": "recall işleminde arama metni; boşsa tüm kayıtlar."},
                "category": {"type": ["string", "null"], "enum": ["preference", "path", "decision", None]},
            },
        ),
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
                            "action": {"type": "string", "enum": ["click", "fill", "press", "wait_for"]},
                            "selector": {"type": "string", "description": "Öğe seçicisi (dönen ÖĞELER listesinden)."},
                            "value": {"type": ["string", "null"], "description": "fill: metin; press: tuş; wait_for: visible/hidden/attached/detached; click: null."},
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
            "Ekranın 1000×1000 görüntüsünü alır, kaydeder ve sana görsel olarak gösterir; görüntüdeki "
            "noktalar tıklama araçlarına aynen verilir. Son fare/klavye eyleminden sonra ekran durulana kadar "
            "kendisi bekler: öncesine bekleme ekleme. Pahalıdır: önce cua_get_ax_state dene.",
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
            "Fare/klavye eylemlerini TEK çağrıda sırayla çalıştırır. click/move: point [x, y] (ekran "
            "görüntüsü/AX uzayı); type: text (her Unicode metin, Türkçe dahil); press: key ('enter', 'tab', "
            "'escape', 'cmd+c', 'cmd+shift+t'); wait: seconds (en çok 5).",
            {
                "steps": {
                    "type": "array",
                    "description": "Sırayla çalıştırılacak eylemler.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["click", "move", "type", "press", "wait"]},
                            "point": POINT_SCHEMA,
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
    ] + [DISCOVERY_SCHEMA]
    if active_chrome_session_goal(goal):
        # Kullanıcının açık oturumu istendiğinde gizli Playwright/API yolu ve CDP
        # araştırmasına yol açan kabuk/Node araçları bu görevden çıkarılır.
        # Chrome AX ağacı sayfa içeriğini değil yalnız tarayıcı çubuğunu gösterdiği için AX
        # araçları canlı ölçümde yalnız boşa tur harcattı; öne getirme chrome_active_tab'dadır.
        excluded = {
            "browse_url", "discover_capabilities", "fetch_raw", "web_search",
            "execute_shell", "execute_js", "process_list",
            "run_action_sequence", "smart_click", "cua_get_ax_state", "cua_click", "cua_get_app",
        }
        schemas = [entry for entry in schemas if entry["function"]["name"] not in excluded]
        schemas.append(_function_schema(
            "chrome_active_tab",
            "Kullanıcının açık Google Chrome penceresini kullanır. URL verilirse aynı sitedeki "
            "mevcut sekmeyi bulur ve görünür kılar; yoksa etkin sekmeye gider; sayfanın yüklenmesini bekler. "
            "Null ise etkin sekmeyi okur. Giriş yapılmış Chrome profilini korur, ayrı tarayıcı açmaz.",
            {"url": {"type": ["string", "null"], "description": "Gidilecek http(s) adresi; mevcut sekmeyi okumak için null."}},
        ))
        schemas.extend([
            _function_schema("cua_click_point", "Son ekran görüntüsündeki noktaya sol tıklar.", {
                "point": POINT_SCHEMA,
            }),
            _function_schema("cua_type_text", "Odaklı alana Unicode metin yazar.", {
                "text": {"type": "string"},
            }),
            _function_schema("cua_press_key", "Tuşa/kısayola basar: enter, tab, escape, cmd+a gibi.", {
                "key": {"type": "string"},
            }),
            _function_schema(
                "cua_submit_text",
                "point'teki alana tıklar, içeriğini text ile değiştirir ve Enter'a basar. Arama kutusu "
                "veya tek alanlı gönderim için tıkla/yaz/Enter yerine bunu kullan.",
                {"point": POINT_SCHEMA, "text": {"type": "string"}},
            ),
        ])
    if goal is not None and camera_photo_goal(goal):
        schemas.append(_function_schema(
            "capture_photo",
            "Varsayılan Mac kamerasından TEK fotoğrafı otomatik benzersiz adla ~/Desktop'a kaydeder; "
            "görüntüyü doğrular, mevcut dosyayı ezmez ve tam yolu sonuçta verir. "
            "Ayrı kamera/ffmpeg/Photo Booth yoklaması yapmadan doğrudan kullan. Başarısız olursa Photo Booth'a geç.",
            {},
        ))
    return schemas


# Modelin çağırabileceği adlar: getattr ile Toolbox'ın özel yöntemlerine
# (_read_full, close_browser…) ulaşılmasın.
TOOL_NAMES: frozenset[str] = frozenset(
    schema["function"]["name"]
    for sample in ("fotoğraf çek masaüstüne", "açık Chrome oturumunu kullan")
    for schema in build_tool_schemas(sample)
)

# Salt okunur araçlar aynı (ad + argüman) için önbelleklenebilir. Canlı durum (AX listesi)
# önbelleklenmez; her başarılı yan etkili çağrıdan sonra önbellek tamamen temizlenir.
_CACHEABLE_TOOLS: frozenset[str] = frozenset({"process_list", "read_file", "web_search", "fetch_raw"})

# Yan etkili araçlar model sırasıyla SERİ çalışır (aynı anda iki tıklama/yazma çakışmasın,
# paralel bir okuma yazmadan önce bayat sonuç önbelleğe girmesin, eylem→gözlem sırası
# korunsun); aralarındaki bağımsız salt okunur bloklar gerçek paralellikle çalışır.
_SIDE_EFFECT_TOOLS: frozenset[str] = frozenset({
    "execute_shell", "write_file", "execute_js", "take_screenshot", "browse_url",
    "cua_get_app", "cua_click", "smart_click", "run_action_sequence", "capture_photo",
    "chrome_active_tab", "cua_click_point", "cua_type_text", "cua_press_key", "cua_submit_text",
    "user_memory",
})

# Açık Chrome yolunda ekranı değiştiren araçlar. Bunlardan sonra görüntü alınmadıysa tur
# sonunda ekran kendiliğinden gözlenir: canlı kayıtta her tıklama ayrı bir "ekran görüntüsü
# al" turu ve 2 sn sabit bekleme gerektiriyordu (22 tur, 100 sn).
_SCREEN_ACTION_TOOLS: frozenset[str] = frozenset({
    "chrome_active_tab", "cua_click_point", "cua_type_text", "cua_press_key", "cua_submit_text",
})
AUTO_OBSERVATION_PREVIEW: str = "otomatik gözlem"


def _tool_cache_key(name: str, arguments: Dict[str, Any]) -> str:
    """Önbellek anahtarı: araç adı + kararlı (sıralı) argüman JSON'u."""
    return name + ":" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)


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
    token = TOOL_RUNTIME.set({
        "emit_output": lambda text: emit({"kind": "tool_output", "call_id": call["id"], "text": text}),
        "should_stop": should_stop,
    })
    try:
        if runtime is not None:
            runtime.check()
        if dynamic:
            validate_arguments(dynamic, arguments)
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
    """Araç sonucunun gösterim/kayıt metni (başarıda çıktı, hatada 'tip: mesaj'). Saf."""
    if result.get("ok"):
        return str(result.get("result", ""))
    return f"{result.get('error_type')}: {result.get('error')}"


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
        content: str = result.get("result", "")
    else:
        content = f"HATA [{result.get('error_type')}]: {result.get('error')}"
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


def build_system_prompt(today: date, goal: Optional[str] = None) -> str:
    """
    Sabit istemin sonuna tarih ve yalnız ilgili görevde kısa yöntem bilgisi ekler.
    Genel görevlerde önek aynı kalır; ev dizini modele verilmez. Saf.
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
        "- chrome_active_tab opens the URL in the matching open tab and waits for it to load.\n"
        "- Search or submit a single field with ONE cua_submit_text call (click + replace text + "
        "enter). Otherwise use cua_click_point (coordinates from the latest screenshot), "
        "cua_type_text and cua_press_key; put every step you can already locate in ONE turn.\n"
        "- After a turn with actions you automatically receive a screenshot taken once the screen "
        "settles. Do not call take_screenshot after actions and never wait.\n"
        "- To inspect several items already visible, return cua_click_point + take_screenshot "
        "pairs for all of them in ONE turn; each screenshot waits for its page to settle.\n"
        f"- Screenshots leave the context after {FULL_DETAIL_TURNS} turns: in the turn you read a needed "
        "value (code, name, number), also write it in your reply text.\n"
        "- A click alone is not proof: finish only when a screenshot shows the result.\n"
        if active_chrome_session_goal(goal) else ""
    )
    return (
        SYSTEM_PROMPT
        + f"\n### TODAY\n- Date: {today.isoformat()} ({TURKISH_WEEKDAYS[weekday]} / {ENGLISH_WEEKDAYS[weekday]}).\n"
        + camera_guidance + chrome_guidance
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
    if backend == "openai" and "zen-free" in available:
        fallback: str = "zen-free"
    elif backend == "zen-free" and "openai" in available:
        fallback = "openai"
    else:
        fallback = ESCALATION_BACKEND if ESCALATION_BACKEND in available else backend
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


def token_usage(usage: Optional[CompletionUsage]) -> TokenUsage:
    """Akışın son parçasındaki token kullanımını (önbellekten okunan girdi dahil) çıkarır. Saf."""
    if usage is None:
        return ZERO_USAGE
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


def _fast_loop_prompt(kind: str, ledger: str) -> str:
    ledger_text: str = ledger or "STATE: henüz kalıcı görev kaydı yok"
    if kind == "replan":
        instruction = (
            "HOST FAST LOOP — YENİDEN PLAN: Anlamlı ilerleme durdu. Yeni keşif açma; aşağıdaki "
            "STATE'i kullanarak eksik zorunlu işi en az model turu ve araç çağrısıyla tamamlayacak "
            "tek kısa yol seç."
        )
    else:
        instruction = (
            "HOST FAST LOOP — TESLİM MODU: Opsiyonel keşfi bırak. Yalnız kalan zorunlu hesaplama, "
            "dosya/rapor yazma, açıkça istenen final doğrulama ve cleanup adımlarını tamamla."
        )
    return f"{instruction}\n\n{ledger_text}"


def merge_tool_call_delta(
    drafts: List[ToolCallDraft], index: int, call_id: Optional[str], name: Optional[str], arguments: Optional[str],
) -> List[ToolCallDraft]:
    """
    Akıştaki bir araç çağrısı parçasını taslak listesine işler: kimlik ve ad ilk parçada
    gelir (sonrakilerde boş/None), argümanlar parça parça eklenir. Saf fonksiyon.
    """
    padded: List[ToolCallDraft] = drafts + [
        {"id": "", "name": "", "arguments": ""} for _ in range(index + 1 - len(drafts))
    ]
    current: ToolCallDraft = padded[index]
    updated: ToolCallDraft = {
        "id": call_id or current["id"],
        "name": current["name"] + (name or ""),
        "arguments": current["arguments"] + (arguments or ""),
    }
    return padded[:index] + [updated] + padded[index + 1:]


def create_model_clients() -> Dict[str, Optional[AsyncOpenAI]]:
    """API istemcilerini ve oturumlu CLI modellerinin kullanılabilirliğini kurar."""
    timeout: Timeout = Timeout(MODEL_REQUEST_TIMEOUT_SECONDS, connect=MODEL_CONNECT_TIMEOUT_SECONDS)
    clients: Dict[str, Optional[AsyncOpenAI]] = {
        name: AsyncOpenAI(api_key=profile["api_key"], base_url=profile["base_url"], timeout=timeout, max_retries=0)
        for name, profile in BACKENDS.items()
        if profile["api_key"]
    }
    if shutil.which("opencode"):
        clients["zen-free"] = None
    if shutil.which("codex"):
        clients["openai"] = None
    return clients


async def close_model_clients(clients: Dict[str, Optional[AsyncOpenAI]]) -> None:
    """API bağlantı havuzlarını kapatır; oturumlu CLI modellerinde havuz yoktur."""
    await asyncio.gather(*(client.close() for client in clients.values() if client is not None))


async def _stream_completion(
    client: Optional[AsyncOpenAI], profile: BackendProfile, messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]], session_id: str, emit: EventSink, should_stop: Callable[[], bool],
) -> ModelTurn:
    """
    Tamamlamayı AKIŞ olarak ister: metin, düşünme metni ve araç çağrısı önizlemeleri geldikçe
    yayınlanır (arayüzde komut harf harf belirir). Durdurma istenirse akış hemen kapatılır ve
    tur finish_reason='stopped' ile döner.
    """
    if profile["provider"] in ("codex-cli", "opencode-cli"):
        provider: str = "codex" if profile["provider"] == "codex-cli" else "opencode"
        return await run_cli_model(provider, profile["model"], messages, tool_schemas, emit, should_stop)
    if client is None:
        raise CliModelError("API istemcisi bulunamadı.")
    headers: Dict[str, str] = dict(profile["extra_headers"])
    if profile["session_header"] is not None:
        headers[profile["session_header"]] = session_id
    stream: Any = await client.chat.completions.create(
        model=profile["model"],
        messages=messages,
        tools=tool_schemas,
        tool_choice="auto",
        max_tokens=profile["max_tokens"],
        extra_headers=headers,
        extra_body=profile["extra_body"],
        stream=True,
        stream_options={"include_usage": True},
    )
    content_parts: List[str] = []
    drafts: List[ToolCallDraft] = []
    previews: Dict[int, str] = {}
    finish_reason: Optional[str] = None
    usage: TokenUsage = ZERO_USAGE
    try:
        async for chunk in stream:
            if should_stop():
                finish_reason = "stopped"
                break
            if chunk.usage is not None:
                usage = token_usage(chunk.usage)
            if not chunk.choices:
                continue
            choice: Any = chunk.choices[0]
            delta: Any = choice.delta
            if delta.content:
                content_parts.append(delta.content)
                emit({"kind": "text_delta", "text": delta.content})
            reasoning: object = (delta.model_extra or {}).get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                emit({"kind": "reasoning_delta", "text": reasoning})
            for call_delta in delta.tool_calls or []:
                function: Any = call_delta.function
                drafts = merge_tool_call_delta(
                    drafts, call_delta.index, call_delta.id,
                    function.name if function is not None else None,
                    function.arguments if function is not None else None,
                )
                draft: ToolCallDraft = drafts[call_delta.index]
                preview: str = preview_arguments(draft["name"], draft["arguments"])
                # Yalnızca önizleme değiştiğinde yayınla (büyük write_file içeriği arayüzü boğmasın)
                if previews.get(call_delta.index) != preview:
                    previews[call_delta.index] = preview
                    emit({"kind": "tool_call_preview", "index": call_delta.index, "name": draft["name"], "preview": preview})
            if choice.finish_reason:
                finish_reason = choice.finish_reason
    finally:
        await stream.close()
    return {"content": "".join(content_parts), "tool_calls": drafts, "finish_reason": finish_reason, "usage": usage}


async def _call_model_with_retries(
    clients: Dict[str, Optional[AsyncOpenAI]],
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]],
    session_id: str,
    backend: str,
    emit: EventSink,
    should_stop: Callable[[], bool],
) -> Tuple[ModelTurn, str]:
    """
    Model çağrısını attempt_plan'a göre yapar ve yanıt veren backend'i de döner. Kalıcı
    istemci hataları (400/401/402/403…) yeniden denenmez. Zaman aşımında aynı backend'i bir
    kez daha beklemek boşa gider: doğrudan son (farklı sağlayıcı) denemeye atlanır. Yarıda
    kesilen bir akış yeniden denenirse önce stream_reset yayınlanır (arayüz o turun akmış
    içeriğini siler, metin iki kez görünmez).
    """
    plan: Tuple[str, str, str] = attempt_plan(backend, frozenset(clients))
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
        except (APIStatusError, APIConnectionError, ssl.SSLError, CliModelError) as error:
            status: Optional[int] = error.status_code if isinstance(error, APIStatusError) else None
            if status is not None and status < 500 and status != 429:
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
            cli_failed: bool = isinstance(error, CliModelError)
            attempt = len(plan) - 1 if (timed_out or cli_failed) and attempt < len(plan) - 1 else attempt + 1
            if attempt < len(plan):
                runtime = CURRENT_RUNTIME.get()
                if runtime is not None:
                    await runtime.delay(0.5 * attempt)
                else:
                    await asyncio.sleep(0.5 * attempt)
    assert last_error is not None
    raise last_error


async def _screenshot_observation(call: ToolCallDraft) -> Dict[str, Any]:
    """Başarılı bir ekran görüntüsünü modele gidecek görsel gözlem mesajına çevirir."""
    arguments: Dict[str, Any] = json.loads(call["arguments"] or "{}")
    image_b64: str = await asyncio.to_thread(encode_image, str(Path(arguments["filename"]).expanduser()))
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "Gözlem: az önce alınan ekran görüntüsü (koordinatlar tıklama araçlarıyla aynı uzayda). "
                                     f"Görüntü {FULL_DETAIL_TURNS} tur sonra bağlamdan silinir: gereken değerleri "
                                     "(kod, ad, sayı) bu turdaki yanıt metnine yaz."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ],
    }


def needs_action_observation(calls: List[ToolCallDraft], results: List[ToolResult]) -> bool:
    """
    Turdaki son başarılı ekran eyleminden sonra başarılı bir take_screenshot yoksa True:
    model eylemin sonucunu görmek için ayrı bir tur harcamasın. Saf fonksiyon.
    """
    pairs: List[Tuple[ToolCallDraft, ToolResult]] = list(zip(calls, results, strict=True))
    acted: List[int] = [
        index for index, (call, result) in enumerate(pairs)
        if call["name"] in _SCREEN_ACTION_TOOLS and result.get("ok")
    ]
    if not acted:
        return False
    return not any(call["name"] == "take_screenshot" and result.get("ok") for call, result in pairs[acted[-1] + 1:])


async def _observe_after_actions(
    call_id: str, index: int, toolbox: Toolbox, cache: Dict[str, ToolResult], emit: EventSink,
    should_stop: Callable[[], bool],
) -> Tuple[Dict[str, Any], sm.StepRecord]:
    """
    Eylem turunun sonunda take_screenshot'ı model yerine çalıştırır (ekran durulunca) ve
    görüntüyü gözlem mesajı olarak döner; geçici dosya modele eklendikten sonra silinir.
    Başarısız gözlem modele açık metinle bildirilir.
    """
    path: Path = Path(tempfile.gettempdir()) / f"omni-{call_id}.png"
    call: ToolCallDraft = {"id": call_id, "name": "take_screenshot", "arguments": json.dumps({"filename": str(path)})}
    result: ToolResult = await _run_tool_with_events(
        index, call, AUTO_OBSERVATION_PREVIEW, toolbox, cache, emit, should_stop)
    step: sm.StepRecord = sm.make_step_record(call["name"], call["arguments"], bool(result.get("ok")), result_text(result))
    if not result.get("ok"):
        return {"role": "user", "content": f"Otomatik gözlem alınamadı: {result_text(result)}"}, step
    try:
        return await _screenshot_observation(call), step
    except (OSError, ValueError) as error:
        logging.warning("Otomatik gözlem modele eklenemedi", extra={"error_type": type(error).__name__})
        return {"role": "user", "content": f"Otomatik gözlem modele eklenemedi: {type(error).__name__}: {error}"}, step
    finally:
        path.unlink(missing_ok=True)


async def run_agent_with_callback(
    goal: str, emit: EventSink, options: RunOptions, clients: Dict[str, Optional[AsyncOpenAI]],
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
        return {"outcome": failure, "success": False, "metrics": initial_metrics,
                "exchange": make_exchange(goal, failure, [])}

    try:
        run_mode, max_iterations, max_wall_clock = resolve_run_limits(options)
    except ValueError as error:
        return startup_failure(error, DEFAULT_BACKEND)
    if not clients:
        return startup_failure(RuntimeError(
            "Kullanılabilir model yok: OpenCode veya Codex CLI kurulumu ve oturumu gerekli."
        ), DEFAULT_BACKEND)
    available: frozenset[str] = frozenset(clients)
    backend_override: Optional[str] = options["requested_backend"] or os.environ.get("OMNI_BACKEND")
    current_backend: str = backend_override if backend_override else (
        DEFAULT_BACKEND if DEFAULT_BACKEND in available else next(iter(clients))
    )
    if current_backend not in available:
        emit({"kind": "notice", "level": "warning",
              "text": f"'{current_backend}' backend'i kullanılamıyor (anahtar yok), '{DEFAULT_BACKEND}' kullanılacak."})
        current_backend = DEFAULT_BACKEND
    emit({"kind": "run_started", "goal": goal, "backend": current_backend, "model": BACKENDS[current_backend]["model"],
          "run_mode": run_mode, "max_turns": max_iterations, "max_wall_clock_seconds": max_wall_clock})

    service: Optional[CapabilityService] = None
    try:
        memory_file: str = options.get("memory_file") or str(
            Path(options["state_file"]).with_name("user_memory.json")
        )
        toolbox: Toolbox = Toolbox(
            memory_file=memory_file,
            allow_memory_mutation=memory_mutation_requested(goal),
        )
        session_id: str = str(uuid.uuid4())
        state: sm.StateDict = sm.load_state(options["state_file"])
        messages: List[Dict[str, Any]] = (
            [{"role": "system", "content": build_system_prompt(date.today(), goal)}]
            + to_messages(options["history"])
            + [{"role": "user", "content": goal}]
        )
        service = options.get("integrations") or CapabilityService()
        runtime = IntegrationRuntime(emit, options["should_stop"], options.get("answer"))
        if not active_chrome_session_goal(goal):
            runtime.selected["discover_capabilities"] = discovery_entry(service, runtime)
        tool_schemas: List[Dict[str, Any]] = build_tool_schemas(goal)
    except Exception as error:
        if service is not None and "integrations" not in options:
            try:
                await service.close()
            except Exception:
                logging.exception("Başlangıç hatasından sonra entegrasyon kapanışı başarısız")
        return startup_failure(error, current_backend)
    runtime_token = CURRENT_RUNTIME.set(runtime)
    service_token = CURRENT_SERVICE.set(service)
    chrome_session: bool = active_chrome_session_goal(goal)
    steps: List[sm.StepRecord] = []
    outcome: str = ""
    reason: str = ""
    success: bool = False
    start_time: float = time.monotonic()
    consecutive_failed_turns: int = 0
    no_progress_turns: int = 0
    tool_cache: Dict[str, ToolResult] = {}
    chrome_visits: Dict[str, int] = {}
    final_length_recoveries: int = 0
    task_ledger: str = ""
    fast_loop_policy = FastLoopPolicy(
        soft_uncached_prompt_tokens=SOFT_UNCACHED_PROMPT_TOKEN_BUDGET,
        soft_tool_calls=SOFT_TOOL_CALL_BUDGET,
    )
    fast_loop_state = FastLoopState()
    fast_loop_delivery_entries: int = 0
    fast_loop_stagnation_events: int = 0
    turns: int = 0
    tool_call_count: int = 0
    usage: TokenUsage = ZERO_USAGE
    model_seconds: float = 0.0
    tool_seconds: float = 0.0
    metrics: sm.EpisodeMetrics

    try:
        for iteration in range(1, max_iterations + 1):
            if options["should_stop"]():
                outcome, reason = "Kullanıcı tarafından durduruldu.", "durduruldu"
                break
            if time.monotonic() - start_time - runtime.metrics["user_wait_seconds"] > max_wall_clock:
                outcome, reason = "", f"zaman bütçesi ({max_wall_clock:.0f}sn) aşıldı"
                break

            runtime.published = dict(runtime.selected)
            tool_schemas = build_tool_schemas(goal) + [
                entry["schema"] for name, entry in runtime.published.items() if name != "discover_capabilities"]
            runtime.allowed_tools = frozenset(entry["function"]["name"] for entry in tool_schemas)
            messages = _trim_old_turns(messages)
            emit({"kind": "turn_started", "turn": iteration, "max_turns": max_iterations,
                  "backend": current_backend, "model": BACKENDS[current_backend]["model"]})
            model_started: float = time.monotonic()
            try:
                turn, used_backend = await _call_model_with_retries(
                    clients, messages, tool_schemas, session_id, current_backend, emit, options["should_stop"],
                )
            finally:
                model_elapsed: float = time.monotonic() - model_started
                model_seconds += model_elapsed
            turns += 1
            usage = add_usage(usage, turn["usage"])
            emit({"kind": "model_finished", "turn": iteration,
                  "seconds": round(model_elapsed, 2), "usage": turn["usage"]})
            if used_backend != current_backend:
                emit({"kind": "backend_changed", "backend": used_backend, "model": BACKENDS[used_backend]["model"],
                      "reason": f"API hatası; yalnız bu tur için geçici fallback, sonraki tur {current_backend} yeniden denenecek"})
            if turn["finish_reason"] == "stopped":
                outcome, reason = "Kullanıcı tarafından durduruldu.", "durduruldu"
                break
            messages.append(_assistant_entry(turn))

            if not turn["tool_calls"]:
                outcome = turn["content"]
                success, reason = final_verdict(outcome, turn["finish_reason"])
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
                    continue
                break

            if turn["finish_reason"] == "length":
                emit({"kind": "notice", "level": "warning",
                      "text": "Yanıt max_tokens sınırında kesildi; araç argümanları eksik olabilir."})
            tool_call_count += len(turn["tool_calls"])
            tools_started: float = time.monotonic()
            try:
                results: List[ToolResult] = await _execute_tool_calls(
                    turn["tool_calls"], toolbox, tool_cache, emit, options["should_stop"],
                )
            finally:
                tool_seconds += time.monotonic() - tools_started

            failures_in_turn: int = 0
            pending_shots: List[ToolCallDraft] = []
            duplicate_navigation_notes: List[str] = []
            for call, result in zip(turn["tool_calls"], results, strict=True):
                ok: bool = bool(result.get("ok"))
                steps.append(sm.make_step_record(call["name"], call["arguments"], ok, result_text(result)))
                if ok and call["name"] == "take_screenshot":
                    pending_shots.append(call)
                if not ok:
                    failures_in_turn += 1
                messages.append(_tool_result_to_message(call, result))
                chrome_visits, duplicate_note = update_chrome_visits(chrome_visits, call, result)
                if duplicate_note is not None:
                    duplicate_navigation_notes.append(duplicate_note)

            # Ekran gözlemleri TÜM araç mesajlarından SONRA eklenir: tool sonuçları assistant
            # tool_calls'ı kesintisiz izlemeli; araya user mesajı 400'e yol açar.
            for shot_call in pending_shots:
                try:
                    messages.append(await _screenshot_observation(shot_call))
                except (OSError, KeyError, ValueError) as error:
                    logging.warning("Ekran görüntüsü modele eklenemedi", extra={"error_type": type(error).__name__})
                    emit({"kind": "notice", "level": "warning",
                          "text": f"Ekran görüntüsü modele iliştirilemedi ({type(error).__name__}: {error})."})

            # Açık Chrome yolunda eylem turu kendi gözlemiyle biter: model sonucu görmek için
            # ayrı bir "ekran görüntüsü al" turu harcamaz.
            if chrome_session and needs_action_observation(turn["tool_calls"], results):
                observation_started: float = time.monotonic()
                try:
                    observation, observation_step = await _observe_after_actions(
                        f"otomatik-gozlem-{session_id[:8]}-{iteration}", len(turn["tool_calls"]),
                        toolbox, tool_cache, emit, options["should_stop"],
                    )
                finally:
                    tool_seconds += time.monotonic() - observation_started
                steps.append(observation_step)
                messages.append(observation)

            if duplicate_navigation_notes:
                messages.append({"role": "user", "content": "\n".join(duplicate_navigation_notes)})
            if turn["finish_reason"] == "length":
                messages.append({
                    "role": "user",
                    "content": (
                        "Bu araç çağrısı turu max_tokens sınırında kesildi. Başarılı çağrıları STATE'e "
                        "işlenmiş kabul et; eksik/başarısız çağrıları yalnız zorunluysa yeniden oluştur. "
                        "Aynı hedefleri baştan dolaşma ve kalan teslim adımlarına öncelik ver."
                    ),
                })

            all_failed: bool = bool(results) and failures_in_turn == len(results)
            previous_ledger: str = task_ledger
            task_ledger = extract_task_ledger(task_ledger, turn["content"])
            ledger_changed: bool = task_ledger != previous_ledger
            signature: str = normalize_progress_signature(
                tool_facts=[
                    (call["name"], _call_signature_arguments(call))
                    for call in turn["tool_calls"]
                ],
                result_facts=[result_text(result)[:TRIMMED_CONTENT_LIMIT] for result in results],
                observation_digest=None,
                ledger_digest=task_ledger,
                unresolved_deliverables=0 if _ledger_delivery_ready(task_ledger) else 1,
            )
            semantic_progress: bool = ledger_changed or (
                not task_ledger and signature != fast_loop_state.last_signature
            )
            if all_failed and not ledger_changed:
                semantic_progress = False
            if not semantic_progress:
                fast_loop_stagnation_events += 1
            previous_phase = fast_loop_state.phase
            decision = advance_fast_loop(
                fast_loop_state,
                TurnSignal(
                    signature=signature,
                    semantic_progress=semantic_progress,
                    unresolved_deliverables=0 if _ledger_delivery_ready(task_ledger) else 1,
                    uncached_prompt_tokens=max(0, usage["prompt_tokens"] - usage["cached_tokens"]),
                    tool_calls=tool_call_count,
                    delivery_ready=_ledger_delivery_ready(task_ledger),
                ),
                fast_loop_policy,
            )
            fast_loop_state = decision.state
            if decision.notice:
                emit({"kind": "notice", "level": "warning", "text": decision.notice})
            if decision.request_replan:
                messages.append({"role": "user", "content": _fast_loop_prompt("replan", task_ledger)})
            elif decision.entered_delivery:
                fast_loop_delivery_entries += 1
                messages.append({"role": "user", "content": _fast_loop_prompt("delivery", task_ledger)})
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

            # Yükseltme sayacı TUR bazlıdır: turda herhangi bir başarı varsa sıfırlanır.
            consecutive_failed_turns = consecutive_failed_turns + 1 if all_failed else 0
            no_progress_turns = no_progress_turns + 1 if all_failed else 0
            if consecutive_failed_turns >= CONSECUTIVE_FAILURE_ESCALATION_THRESHOLD:
                upgraded: Optional[str] = next_quality_backend(current_backend, available)
                if upgraded is not None:
                    emit({"kind": "backend_changed", "backend": upgraded, "model": BACKENDS[upgraded]["model"],
                          "reason": f"art arda {consecutive_failed_turns} başarısız tur"})
                    current_backend = upgraded
                consecutive_failed_turns = 0
            if no_progress_turns >= NO_PROGRESS_LIMIT:
                reason = f"ilerleme yok: {NO_PROGRESS_LIMIT} ardışık tamamen başarısız araç turu"
                emit({"kind": "notice", "level": "warning", "text": reason})
                break
        else:
            reason = f"maksimum iterasyon sayısına ({max_iterations}) ulaşıldı"
    except Exception as error:
        outcome, reason = f"Kritik hata: {error}", f"kritik hata: {error}"
        emit({"kind": "notice", "level": "error", "text": outcome})
    finally:
        history_answer: str = outcome or reason
        if not success and reason and reason not in history_answer:
            history_answer = f"[Görev tamamlanamadı: {reason}]\n{history_answer}"
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
            "integrations": dict(runtime.metrics),
        }
        cleanup_errors: List[str] = []
        try:
            sm.save_state(options["state_file"], sm.record_episode(state, goal, steps, outcome, success, metrics))
        except Exception as error:
            cleanup_errors.append(f"Bellek kaydı: {type(error).__name__}: {error}")
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
        emit({"kind": "run_finished", "success": success, "outcome": outcome, "reason": reason, "metrics": metrics})
    return {"outcome": outcome, "success": success, "metrics": metrics, "exchange": exchange}


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
    clients: Dict[str, Optional[AsyncOpenAI]] = create_model_clients()
    try:
        options: RunOptions = {"requested_backend": None, "should_stop": lambda: False, "state_file": STATE_FILE, "history": []}
        return await run_agent_with_callback(goal, print_event, options, clients)
    finally:
        await close_model_clients(clients)


if __name__ == "__main__":
    cli_goal: str = " ".join(sys.argv[1:]).strip()
    if not cli_goal:
        print("Kullanım: python3 main.py <hedef metni>")
        raise SystemExit(1)
    asyncio.run(run_agent(cli_goal))
