"""
Host-Managed Task Ledger: Modelin metin disiplinine bağımlı kalmadan, araç çıktılarından
yapılandırılmış anahtar-değer çiftlerini, sistem durumlarını ve doğrulanmış gerçekleri
alan-bağımsız (domain-agnostic) olarak ayıklar, sınırlar ve tur boyunca saklar.
Saf fonksiyonlar ve katı tipleme kullanır.
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple, TypedDict

LEDGER_MAX_FACTS: int = 40
LEDGER_MAX_VALUE_LEN: int = 120
LEDGER_PROMPT_MAX_BYTES: int = 1500

# Evrensel Key-Value ve Markdown etiket kalıbı (Alan bağımsız: İngilizce, Türkçe vb. tüm diller)
# Örnekler: "Status: Running", "IP: 10.0.0.1", "Aylık maaş: 6300 €", "Total Stars: 42", "**CPU:** 15%"
_GENERIC_KV_PATTERN: re.Pattern[str] = re.compile(
    r"(?m)^[ \t]*(?:[\*\-_•][ \t]*)?(?:\*\*|__)?([A-Za-z0-9_.\- ÇĞİÖŞÜçğıöşü]{2,35}?)(?:\*\*|__)?\s*[:=][ \t]*([^\r\n]+)$"
)

# Hariç tutulacak sözde anahtarlar (URL şemaları, meta komutlar, vb.)
_EXCLUDED_KEYS: frozenset[str] = frozenset({
    "http", "https", "ftp", "file", "state", "remaining", "facts", "kalan",
    "note", "warning", "info", "error", "dikkat", "uyari",
})

_HTML_TAG_PATTERN: re.Pattern[str] = re.compile(r"<[^>]+>")
_MARKDOWN_CLEANUP_PATTERN: re.Pattern[str] = re.compile(r"^\*\*|^\b__|\*\*|\b__")

_TR_MAP: Dict[int, int] = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


class TaskFact(TypedDict):
    key: str
    value: str
    source: str
    turn: int


class TaskLedger(TypedDict):
    facts: Dict[str, TaskFact]
    model_state: str
    turn: int


def empty_task_ledger() -> TaskLedger:
    """Boş görev hafızası defteri oluşturur. Saf fonksiyon."""
    return {"facts": {}, "model_state": "", "turn": 0}


def normalize_key(raw_key: str) -> str:
    """
    Anahtar adını alan bağımsız, temiz ve standart snake_case formatına çevirir.
    Ör: 'Aylık maaş' -> 'aylik_maas', 'TOTAL_STARS' -> 'total_stars', 'CPU Usage' -> 'cpu_usage'.
    Saf fonksiyon.
    """
    ascii_key: str = raw_key.translate(_TR_MAP)
    cleaned: str = re.sub(r"[^a-zA-Z0-9_.]", "_", ascii_key).lower()
    return re.sub(r"_+", "_", cleaned).strip("_")


def clean_fact_value(value: str) -> str:
    """Değerden HTML/Markdown etiketlerini temizler, boşlukları normalize eder ve sınırlar. Saf."""
    without_html: str = _HTML_TAG_PATTERN.sub(" ", value)
    without_bold: str = _MARKDOWN_CLEANUP_PATTERN.sub("", without_html)
    normalized: str = " ".join(without_bold.split()).strip()
    return normalized[:LEDGER_MAX_VALUE_LEN]


def _flatten_json_dict(data: Dict[str, Any], prefix: str = "") -> List[Tuple[str, str]]:
    """JSON sözlüğünü tek düzeyli anahtar-değer çiftlerine indirger. Saf."""
    results: List[Tuple[str, str]] = []
    for key, value in data.items():
        full_key: str = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            results.extend(_flatten_json_dict(value, full_key))
        elif isinstance(value, (str, int, float, bool)):
            results.append((full_key, str(value)))
    return results


def extract_facts_from_text(text: str, source: str, turn: int) -> List[TaskFact]:
    """
    Araç çıktısından doğrulanmış anahtar-değer gerçeklerini alan-bağımsız olarak ayıklar.
    Saf fonksiyon.
    """
    if not text or not text.strip():
        return []
    facts: List[TaskFact] = []
    seen_keys: set[str] = set()

    # 1. Evrensel Yapılandırılmış Key-Value Ayrıştırma
    for match in _GENERIC_KV_PATTERN.finditer(text):
        raw_key: str = match.group(1).strip()
        raw_value: str = match.group(2).strip()
        norm_key: str = normalize_key(raw_key)

        # Geçersiz / gürültülü anahtarları filtrele
        if not norm_key or norm_key in _EXCLUDED_KEYS or norm_key.isdigit():
            continue
        # Çok uzun anlatı paragraflarını atla (gerçekler kısa ve özdür)
        if len(raw_value) > LEDGER_MAX_VALUE_LEN * 2:
            continue

        val: str = clean_fact_value(raw_value)
        if val and norm_key not in seen_keys:
            facts.append({"key": norm_key, "value": val, "source": source, "turn": turn})
            seen_keys.add(norm_key)

    # 2. JSON çıktıları (fetch_raw, web_search, cdp veya shell json yanıtları)
    trimmed: str = text.strip()
    if (trimmed.startswith("{") and trimmed.endswith("}")) or (trimmed.startswith("[") and trimmed.endswith("]")):
        try:
            parsed: object = json.loads(trimmed)
            if isinstance(parsed, dict):
                for k, v in _flatten_json_dict(parsed):
                    norm_k: str = normalize_key(k)
                    if norm_k and norm_k not in seen_keys and norm_k not in _EXCLUDED_KEYS:
                        facts.append({"key": norm_k, "value": clean_fact_value(v), "source": source, "turn": turn})
                        seen_keys.add(norm_k)
        except (ValueError, TypeError):
            pass

    return facts


def record_tool_result(ledger: TaskLedger, tool_name: str, result_text: str, turn: int) -> TaskLedger:
    """
    Araç sonucunu inceler, bulunan yeni gerçekleri mevcut hafızaya deterministik olarak ekler.
    Saf fonksiyon: yeni bir TaskLedger nesnesi döner.
    """
    new_facts: List[TaskFact] = extract_facts_from_text(result_text, tool_name, turn)
    if not new_facts:
        return {**ledger, "turn": max(ledger["turn"], turn)}

    updated_facts: Dict[str, TaskFact] = dict(ledger["facts"])
    for fact in new_facts:
        updated_facts[fact["key"]] = fact

    # Sınırı koru: en son güncellenen ilk LEDGER_MAX_FACTS gerçeği tut
    if len(updated_facts) > LEDGER_MAX_FACTS:
        sorted_keys = sorted(updated_facts.keys(), key=lambda k: updated_facts[k]["turn"], reverse=True)
        updated_facts = {k: updated_facts[k] for k in sorted_keys[:LEDGER_MAX_FACTS]}

    return {
        "facts": updated_facts,
        "model_state": ledger["model_state"],
        "turn": max(ledger["turn"], turn),
    }


def record_model_state(ledger: TaskLedger, content: str) -> TaskLedger:
    """
    Modelin assistant metnindeki STATE: bloğunu yakalar ve hafızaya işler.
    Saf fonksiyon.
    """
    match = re.search(r"(?im)^\s*STATE\s*:", content)
    if match is None:
        return ledger
    extracted: str = content[match.start():].strip()[:1000]
    return {
        "facts": ledger["facts"],
        "model_state": extracted,
        "turn": ledger["turn"],
    }


def format_ledger_prompt(ledger: TaskLedger) -> str:
    """
    Model bağlamına enjekte edilecek kompakt, dayanıklı çalışma defterini metne döker.
    Eski görsel ve okuma çıktıları kırpılsa bile model bu gerçekleri her tur görür.
    Saf fonksiyon.
    """
    parts: List[str] = []
    if ledger["facts"]:
        parts.append("### TASK SCRATCHPAD (Host-Verified Facts)")
        # Anahtarları alfabetik ve deterministik sırada listele
        keys = sorted(ledger["facts"].keys())
        for key in keys:
            fact = ledger["facts"][key]
            parts.append(f"- {fact['key']}: {fact['value']} (via {fact['source']})")

    if ledger["model_state"]:
        parts.append(ledger["model_state"])

    rendered: str = "\n".join(parts)
    return rendered[:LEDGER_PROMPT_MAX_BYTES]


def inject_task_ledger_into_messages(
    messages: List[Dict[str, Any]], ledger: TaskLedger
) -> List[Dict[str, Any]]:
    """
    Doğrulanmış gerçekleri (scratchpad) mesaj geçmişine enjekte eder.
    Statik system prompt önekini bozmaz (prefix cache dostudur).
    Saf fonksiyon: yeni mesaj listesi döner.
    """
    rendered: str = format_ledger_prompt(ledger)
    if not rendered:
        return messages
    if not messages:
        return [{"role": "user", "content": rendered}]

    last_msg: Dict[str, Any] = messages[-1]
    if last_msg.get("role") == "user":
        content: Any = last_msg.get("content")
        if isinstance(content, str):
            if "### TASK SCRATCHPAD" in content:
                base: str = content.split("### TASK SCRATCHPAD")[0].rstrip()
                updated_str: str = f"{base}\n\n{rendered}" if base else rendered
            else:
                updated_str = f"{content}\n\n{rendered}"
            return messages[:-1] + [{**last_msg, "content": updated_str}]
        elif isinstance(content, list):
            new_parts: List[Dict[str, Any]] = []
            replaced: bool = False
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    part_text: str = str(part.get("text", ""))
                    if "### TASK SCRATCHPAD" in part_text:
                        base = part_text.split("### TASK SCRATCHPAD")[0].rstrip()
                        new_text: str = f"{base}\n\n{rendered}" if base else rendered
                        new_parts.append({"type": "text", "text": new_text})
                        replaced = True
                    else:
                        new_parts.append(part)
                else:
                    new_parts.append(part)
            if not replaced:
                new_parts.append({"type": "text", "text": f"\n{rendered}"})
            return messages[:-1] + [{**last_msg, "content": new_parts}]
    return messages + [{"role": "user", "content": rendered}]
