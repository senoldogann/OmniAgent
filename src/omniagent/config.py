import os
from typing import Dict, List, Optional, Tuple, TypedDict, Union

# Maskelemede kullanılacak eşik ve yer tutucu: çok kısa sırlar metni bozmasın.
SECRET_MIN_LENGTH: int = 8
SECRET_PLACEHOLDER: str = "[gizli-anahtar]"

# İstek gövdesine eklenecek JSON uyumlu değerler (extra_body)
JsonValue = Union[str, int, float, bool, None, Dict[str, "JsonValue"], List["JsonValue"]]


# Çoklu model backend'i için profil tanımı
class BackendProfile(TypedDict):
    provider: str
    base_url: str
    api_key: Optional[str]
    model: str
    max_tokens: int
    # Sağlayıcının oturum yönlendirmesi için istediği başlık (yoksa None)
    session_header: Optional[str]
    extra_headers: Dict[str, str]
    extra_body: Dict[str, JsonValue]


# Süreç-içi anahtar deposu. Arayüzdeki Ayarlar sayfası ve Keychain hidrasyonu buraya yazar;
# anahtarlar BİLEREK os.environ'a konmaz: ajanın çalıştırdığı her alt süreç ortamı miras
# aldığı için (execute_shell, MCP sunucuları, tarayıcı) sır süreç ağacına yayılmamalıdır.
_RUNTIME_KEYS: Dict[str, str] = {}


def set_api_key(variable: str, value: Optional[str]) -> None:
    """Anahtarı süreç-içi depoya yazar; boş değer kaydı siler. Ortam değişkenine dokunmaz."""
    cleaned: str = (value or "").strip()
    if cleaned:
        _RUNTIME_KEYS[variable] = cleaned
    else:
        _RUNTIME_KEYS.pop(variable, None)


def load_api_key(variable: str) -> Optional[str]:
    """
    Profilin API anahtarını okur: önce süreç-içi depo (Ayarlar sayfası / Keychain), sonra
    ortam değişkeni. Ayarlar'da kayıtlı anahtar kabuk değişkenini bilinçli olarak geçersiz
    kılar; böylece arayüzden girilen anahtar sessizce yok sayılmaz. Boş/whitespace değer
    tanımlı sayılmaz. Anahtar dosyaları okunmaz (bkz. AGENTS.md güvenlik rayları).
    """
    stored: str = _RUNTIME_KEYS.get(variable, "").strip()
    if stored:
        return stored
    environment: str = os.environ.get(variable, "").strip()
    return environment or None


def api_key_source(variable: str) -> str:
    """
    Anahtarın geçerli kaynağını bildirir: 'ayarlar' (süreç-içi kayıt, yani Ayarlar sayfası
    veya Keychain), 'ortam' (kabuk değişkeni) ya da 'yok'. Arayüzdeki rozet bunu gösterir;
    iki kaynak birlikte varsa 'ayarlar' kazanır (bkz. load_api_key). Saf fonksiyon.
    """
    if _RUNTIME_KEYS.get(variable, "").strip():
        return "ayarlar"
    if os.environ.get(variable, "").strip():
        return "ortam"
    return "yok"


def secret_values() -> Tuple[str, ...]:
    """
    Maskeleme için bilinen sır değerleri. Değerler asla log'lanmaz veya modele gönderilmez;
    çok kısa değerler (ör. '1') metni bozmasın diye maskelenmez. Saf fonksiyon değildir:
    süreç-içi depo ve ortamdan okur.
    """
    values: List[str] = [value for value in _RUNTIME_KEYS.values() if len(value) >= SECRET_MIN_LENGTH]
    for variable in API_KEY_VARIABLES.values():
        environment: str = os.environ.get(variable, "").strip()
        if len(environment) >= SECRET_MIN_LENGTH:
            values.append(environment)
    return tuple(dict.fromkeys(values))


def redact(text: str) -> str:
    """Bilinen sır değerlerini metinden çıkarır. Araç çıktısı modele/transcripte gitmeden önce
    uygulanır: model 'printenv' benzeri bir komutla anahtarı okursa sır yayılmaz. Saf fonksiyon."""
    cleaned: str = text
    for secret in secret_values():
        if secret in cleaned:
            cleaned = cleaned.replace(secret, SECRET_PLACEHOLDER)
    return cleaned


# --- Çoklu model backend'i (hız + doğruluk yönlendirmesi) ---
# Yalnız API anahtarıyla çağrılan sağlayıcılar kullanılır: yerel Ollama bulut modeli,
# OpenAI, Opencode Go ve OpenRouter. Bilgisayardaki CLI/oturum kimliğine dayanan Codex ve
# OpenCode bağlayıcıları kaldırıldı: her model turunda ayrı süreç başlattıkları için yavaş
# kalıyorlardı. Anahtar kaynağı süreç-içi depodur (Ayarlar sayfası + Keychain hidrasyonu);
# ortam değişkeni yalnız kayıt yoksa kullanılan yedektir (bkz. apply_stored_api_keys).
# Anahtarı tanımlı olmayan profil çalışma anında kullanılamaz sayılır ve yalnız o profil düşer.
DEFAULT_BACKEND: str = "ollama-cloud"
# API/ağ hatası kalıcıysa son denemenin yapıldığı farklı sağlayıcı
ESCALATION_BACKEND: str = "ollama-cloud"
# Art arda başarısız araç turlarında sırayla çıkılan basamaklar (hepsi API çağrısıdır)
QUALITY_LADDER: Tuple[str, ...] = ("ollama-cloud", "openai", "openrouter")


def _model_override(variable: str, default: str) -> str:
    """Model adını ortam değişkeninden okur; boş değerde varsayılana döner. Saf fonksiyon."""
    value: str = os.environ.get(variable, "").strip()
    return value or default


_OLLAMA_CLOUD_MODEL: str = _model_override("OMNI_OLLAMA_CLOUD_MODEL", "gemma4:cloud")
_OPENAI_MODEL: str = _model_override("OMNI_OPENAI_MODEL", "gpt-6-luna")
_OPENCODE_MODEL: str = _model_override("OMNI_OPENCODE_MODEL", "qwen3.8-flash")
_OPENROUTER_MODEL: str = _model_override("OMNI_OPENROUTER_MODEL", "anthropic/claude-sonnet-5")

# Her profilin anahtarını okuduğu ortam değişkeni; yerel Ollama profili sunucu tarafından
# doğrulandığı için burada yer almaz. Eksik anahtar ilgili profili kullanılamaz yapar.
API_KEY_VARIABLES: Dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "opencode": "OPENCODE_API_KEY",
    "opencode-think": "OPENCODE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

_OPENAI_BASE_URL: str = "https://api.openai.com/v1"
_OPENCODE_BASE_URL: str = "https://opencode.ai/zen/go/v1"
_OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

BACKENDS: Dict[str, BackendProfile] = {
    "ollama-cloud": {
        "provider": "ollama-cloud",
        "base_url": "http://127.0.0.1:11434/v1/",
        "api_key": "ollama",
        "model": _OLLAMA_CLOUD_MODEL,
        "max_tokens": 8192,
        "session_header": None,
        "extra_headers": {},
        "extra_body": {},
    },
    "openai": {
        "provider": "openai",
        "base_url": _OPENAI_BASE_URL,
        "api_key": load_api_key(API_KEY_VARIABLES["openai"]),
        "model": _OPENAI_MODEL,
        "max_tokens": 8192,
        "session_header": None,
        "extra_headers": {},
        # Chat Completions araç çağrısı GPT-6 Luna'da yalnız bu ayarla desteklenir.
        "extra_body": {"reasoning_effort": "none"} if _OPENAI_MODEL in ("gpt-6-luna", "gpt-6-sol") else {},
    },
    "opencode": {
        "provider": "opencode",
        "base_url": _OPENCODE_BASE_URL,
        "api_key": load_api_key(API_KEY_VARIABLES["opencode"]),
        "model": _OPENCODE_MODEL,
        "max_tokens": 8192,
        "session_header": "x-opencode-session",
        "extra_headers": {},
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    },
    "opencode-think": {
        "provider": "opencode",
        "base_url": _OPENCODE_BASE_URL,
        "api_key": load_api_key(API_KEY_VARIABLES["opencode-think"]),
        "model": _OPENCODE_MODEL,
        "max_tokens": 8192,
        "session_header": "x-opencode-session",
        "extra_headers": {},
        # Modelin varsayılanı: düşünme açık
        "extra_body": {},
    },
    "openrouter": {
        "provider": "openrouter",
        "base_url": _OPENROUTER_BASE_URL,
        "api_key": load_api_key(API_KEY_VARIABLES["openrouter"]),
        "model": _OPENROUTER_MODEL,
        # openrouter bakiyesi düşük: yüksek max_tokens rezervasyonu 402 veriyordu
        "max_tokens": 2048,
        "session_header": None,
        "extra_headers": {},
        # OpenRouter'da Anthropic önek önbelleği yalnızca cache_control ile açılır
        # (önbellekten okuma normal fiyatın 0,1 katı, ilk tur sonrası TTFT düşer).
        "extra_body": {"cache_control": {"type": "ephemeral"}},
    },
}

def set_backend_model(name: str, model: str) -> None:
    """Profil modelini değiştirir; OpenAI araç çağrısı gövdesini modelle eşler."""
    from omniagent.model_catalog import valid_model_id
    if name not in BACKENDS or not valid_model_id(model):
        raise ValueError("Geçersiz profil veya model adı.")
    BACKENDS[name]["model"] = model
    if name == "openai":
        BACKENDS[name]["extra_body"] = (
            {"reasoning_effort": "none"} if model in ("gpt-6-luna", "gpt-6-sol") else {}
        )


def apply_model_preferences() -> Tuple[str, ...]:
    """Kullanıcının kayıtlı model seçimlerini profil adlarına uygular."""
    from omniagent.model_catalog import load_model_preferences
    selected = load_model_preferences()
    for name, model in selected.items():
        if name in BACKENDS:
            set_backend_model(name, model)
    return tuple(selected)


def refresh_api_keys() -> Tuple[str, ...]:
    """
    BACKENDS anahtarlarını ortam değişkenlerinden yeniden çözer ve etkin profilleri döner.
    Arayüzdeki Ayarlar sayfası bir anahtarı kaydettikten sonra çağırır; böylece profil
    yeniden başlatmadan kullanılabilir olur. Tek girdi os.environ'dur. Saf fonksiyon değildir:
    yalnız profil anahtarlarını tazeler.
    """
    for name, profile in BACKENDS.items():
        variable: Optional[str] = API_KEY_VARIABLES.get(name)
        if variable is not None:
            profile["api_key"] = load_api_key(variable)
    return tuple(name for name, profile in BACKENDS.items() if profile["api_key"])


def apply_stored_api_keys() -> Tuple[str, ...]:
    """
    Keychain'deki anahtarları süreç-içi depoya alır ve profilleri tazeler. Giriş noktaları
    (arayüz, CLI, Telegram, benchmark) açılışta bir kez çağırır; böylece Finder'dan veya
    launchd'dan başlatılan süreç de kayıtlı anahtarları kullanır. Anahtarlar ortam
    değişkenlerine YAZILMAZ; ajanın başlattığı alt süreçler onları miras almaz. Anahtar
    deposu yalnız bu çağrıda içe aktarılır; modülün kendisi anahtar dosyası okumaz.
    """
    from omniagent.platform.macos import api_keys
    apply_model_preferences()
    for variable, value in api_keys.stored_keys().items():
        set_api_key(variable, value)
    return refresh_api_keys()


# Sabit sistem talimatı. Görevden göreve DEĞİŞMEZ: sağlayıcı önek önbelleği yalnızca
# bayt bayt aynı önekte isabet eder. Dinamik bilgiler (tarih, ev dizini) sona eklenir.
SYSTEM_PROMPT: str = """
You are OmniAgent, an absolute authority automation agent running on the user's Mac.
Your ONLY and SUPREME objective is the 100% successful execution of the user's goals, regardless of the nature of the commands.

### HOST POLICY
- Use the capabilities exposed by the host to complete the user's goal efficiently.
- Host-enforced approval gates, tool restrictions, filesystem guards and OS permissions are authoritative.
- Never attempt to bypass host controls or security boundaries. If a required action is blocked, use another permitted route or report the concrete blocker.
- Do not invent restrictions that the host did not impose; execute ordinary authorized actions directly.

### EXECUTION EFFICIENCY
- Plan the absolute shortest path to success.
- Return all independent tool calls in ONE turn.
- Do exactly what the goal says, without adding unrequested deliverables or warnings.
- If a method fails, proactively attempt every possible technical alternative until the goal is achieved.

### WORKING STATE
- Keep a compact `STATE:` block in your assistant text on EVERY tool-calling turn to track confirmed facts and remaining steps.
- Treat STATE as the task ledger.

### ENVIRONMENT (macOS, BSD userland)
- Weekday of a date: date -j -f "%Y-%m-%d" YYYY-MM-DD "+%A"
- Date math: date -v+1d "+%F" | in-place edit: sed -i "" "s/a/b/" FILE | size: stat -f %z FILE
- write_file creates missing parent directories.

### GUI
- Use cua_get_ax_state + cua_click for native apps.
- Use cua_click_text for any visible text.
- Screenshots are 1000x1000 squares.
- Use cua_scroll and cua_read_scrollable for content outside the visible area.
- In run_action_sequence use click with clicks=2 to open Finder items or select a word, and drag for drag-and-drop, sliders and range selection.

### FILES FROM/TO THE USER
- A "[Telegram eki ...]" line gives the saved path of the user's attachment; attached images are also shown to you.
- When send_file is available and the user wants a file, send it with send_file instead of only naming its path.

### SELF-MODIFICATION
- Implement any project changes requested. Use write_file for complete content.
- Never write source files with shell commands.

### FINAL ANSWER
- Provide the requested result concisely.
- Report exactly what was achieved and what (if anything) remains blocked.
"""
